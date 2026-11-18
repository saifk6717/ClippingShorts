"""Everything ClippingShorts keeps in Redis: queues, events, locks, live state.

Only small JSON lives here: never video, never raw audio. Every key starts with REDIS_PREFIX ("cs:") and live
keys carry a TTL, so a Redis shared with other applications is not filled up or collided with.

Queues are Redis Streams with one consumer group each:
  * bounded: a producer is refused (QueueFull) once the queue holds `cap` messages, which is the backpressure;
  * at-least-once: a message stays pending until acked; one held longer than `visibility` seconds by a dead
    consumer is claimed again;
  * retries: a failed message is re-queued until `max_attempts`, then moved to the dead-letter stream.
"""
from __future__ import annotations

import json
import os
import socket
import time
import uuid
from dataclasses import dataclass

import redis

from .config import settings

_client: redis.Redis | None = None


def r() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=15,
                                       socket_connect_timeout=5, health_check_interval=30)
    return _client


def k(*parts) -> str:
    return settings.redis_prefix + ":".join(str(p) for p in parts)


def consumer_name() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def dumps(v) -> str:
    return json.dumps(v, separators=(",", ":"), default=str)


# ---------------------------------------------------------------- queues
class QueueFull(Exception):
    pass


QUEUES = {
    "stream_control": 200,
    "transcription": settings.queue_cap_transcription,
    "highlight_eval": settings.queue_cap_highlight,
    "clip_extraction": settings.queue_cap_extraction,
    "rendering": settings.queue_cap_render,
    "publishing": settings.queue_cap_publish,
}


@dataclass
class Message:
    queue: str
    id: str
    data: dict
    attempts: int


class Queue:
    def __init__(self, name: str, cap: int | None = None, visibility: int = 900, max_attempts: int | None = None):
        self.name = name
        self.cap = cap or QUEUES.get(name, 100)
        self.visibility = visibility
        self.max_attempts = max_attempts or settings.queue_max_attempts
        self.key = k("q", name)
        self.dlq = k("dlq", name)
        self.group = "workers"
        self._ensured = False

    def _ensure(self):
        if self._ensured:
            return
        try:
            r().xgroup_create(self.key, self.group, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
        self._ensured = True

    def length(self) -> int:
        try:
            return int(r().xlen(self.key))
        except redis.RedisError:
            return 0

    def put(self, data: dict, force: bool = False, attempts: int = 0) -> str:
        """Add a message. Raises QueueFull when the queue is at capacity (unless force, used for retries)."""
        self._ensure()
        if not force and self.length() >= self.cap:
            r().hincrby(k("stats", "queue_rejected"), self.name, 1)
            raise QueueFull(f"queue {self.name} is full ({self.cap})")
        return r().xadd(self.key, {"d": dumps(data), "a": str(attempts), "t": str(time.time())})

    def get(self, block_ms: int = 5000, consumer: str | None = None) -> Message | None:
        """Next message for this consumer: first any stale message a crashed consumer left, then a new one."""
        self._ensure()
        consumer = consumer or consumer_name()
        try:
            claimed = r().xautoclaim(self.key, self.group, consumer, min_idle_time=self.visibility * 1000,
                                     start_id="0-0", count=1)
            items = claimed[1] if claimed and len(claimed) > 1 else []
            items = [i for i in items if i and i[1]]
            if not items:
                got = r().xreadgroup(self.group, consumer, {self.key: ">"}, count=1, block=block_ms)
                items = got[0][1] if got else []
        except redis.ResponseError as e:
            if "NOGROUP" in str(e):
                self._ensured = False
                return None
            raise
        if not items:
            return None
        mid, fields = items[0]
        try:
            data = json.loads(fields.get("d", "{}"))
        except ValueError:
            data = {}
        return Message(self.name, mid, data, int(fields.get("a", "0")))

    def ack(self, msg: Message) -> None:
        pipe = r().pipeline()
        pipe.xack(self.key, self.group, msg.id)
        pipe.xdel(self.key, msg.id)
        pipe.execute()

    def touch(self, msg: Message, consumer: str | None = None) -> None:
        """Tell Redis this consumer is still working on a long job, so it is not reclaimed."""
        try:
            r().xclaim(self.key, self.group, consumer or consumer_name(), 0, [msg.id], justid=True)
        except redis.RedisError:
            pass

    def fail(self, msg: Message, error: str) -> bool:
        """Retry the message later, or dead-letter it. Returns True if it was dead-lettered."""
        attempts = msg.attempts + 1
        self.ack(msg)
        if attempts >= self.max_attempts:
            r().xadd(self.dlq, {"d": dumps(msg.data), "a": str(attempts), "error": error[:500],
                                "t": str(time.time())}, maxlen=1000, approximate=True)
            r().hincrby(k("stats", "dead_lettered"), self.name, 1)
            return True
        self.put(msg.data, force=True, attempts=attempts)
        return False

    def requeue(self, msg: Message) -> None:
        """Give a message back without counting an attempt (e.g. deferred for resource pressure)."""
        self.ack(msg)
        self.put(msg.data, force=True, attempts=msg.attempts)

    def dead_letters(self, count: int = 50) -> list:
        out = []
        for mid, f in r().xrevrange(self.dlq, count=count):
            try:
                d = json.loads(f.get("d", "{}"))
            except ValueError:
                d = {}
            out.append({"id": mid, "data": d, "attempts": int(f.get("a", 0)), "error": f.get("error", ""),
                        "t": float(f.get("t", 0))})
        return out

    def retry_dead(self, mid: str) -> bool:
        got = r().xrange(self.dlq, min=mid, max=mid)
        if not got:
            return False
        data = json.loads(got[0][1].get("d", "{}"))
        self.put(data, force=True, attempts=0)
        r().xdel(self.dlq, mid)
        return True


_queues: dict[str, Queue] = {}


def queue(name: str) -> Queue:
    if name not in _queues:
        _queues[name] = Queue(name, visibility=1800 if name in ("rendering", "publishing") else 600)
    return _queues[name]


# ---------------------------------------------------------------- per-stream command lists
def stream_cmd_key(stream_id: str) -> str:
    return k("stream", stream_id, "cmd")


def send_stream_command(stream_id: str, cmd: dict) -> None:
    pipe = r().pipeline()
    pipe.rpush(stream_cmd_key(stream_id), dumps(cmd))
    pipe.ltrim(stream_cmd_key(stream_id), -100, -1)
    pipe.expire(stream_cmd_key(stream_id), 3600)
    pipe.execute()


# ---------------------------------------------------------------- events (for the dashboard's live updates)
EVENTS = k("events")


def publish(kind: str, **data) -> None:
    try:
        r().publish(EVENTS, dumps({"type": kind, "t": time.time(), **data}))
    except redis.RedisError:
        pass


def log_activity(stream_id: str, text: str, level: str = "info") -> None:
    """A short, bounded activity log shown on the dashboard."""
    entry = {"t": time.time(), "stream_id": stream_id, "text": text[:300], "level": level}
    try:
        pipe = r().pipeline()
        pipe.lpush(k("activity"), dumps(entry))
        pipe.ltrim(k("activity"), 0, 199)
        pipe.execute()
    except redis.RedisError:
        pass
    publish("activity", **entry)


# ---------------------------------------------------------------- live state
def set_json(key: str, value, ttl: int = 60) -> None:
    r().set(key, dumps(value), ex=ttl)


def get_json(key: str, default=None):
    try:
        v = r().get(key)
    except redis.RedisError:
        return default
    if v is None:
        return default
    try:
        return json.loads(v)
    except ValueError:
        return default


# ---------------------------------------------------------------- locks
class Lock:
    """A Redis lock with an owner token and expiry, so a crashed holder cannot block others for ever."""

    _RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
    _EXTEND = ("if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('pexpire', KEYS[1], ARGV[2]) "
               "else return 0 end")

    def __init__(self, name: str, ttl: float = 30):
        self.key = k("lock", name)
        self.ttl_ms = int(ttl * 1000)
        self.token = uuid.uuid4().hex

    def acquire(self, wait: float = 0) -> bool:
        deadline = time.time() + wait
        while True:
            if r().set(self.key, self.token, nx=True, px=self.ttl_ms):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.1)

    def extend(self) -> bool:
        return bool(r().eval(self._EXTEND, 1, self.key, self.token, self.ttl_ms))

    def release(self) -> None:
        try:
            r().eval(self._RELEASE, 1, self.key, self.token)
        except redis.RedisError:
            pass

    def __enter__(self):
        if not self.acquire(wait=self.ttl_ms / 1000):
            raise TimeoutError(f"could not lock {self.key}")
        return self

    def __exit__(self, *exc):
        self.release()


# ---------------------------------------------------------------- global counting semaphore (e.g. FFmpeg processes)
_SEM_ACQUIRE = """
redis.call('zremrangebyscore', KEYS[1], '-inf', ARGV[1])
if redis.call('zcard', KEYS[1]) < tonumber(ARGV[2]) then
  redis.call('zadd', KEYS[1], ARGV[3], ARGV[4])
  redis.call('expire', KEYS[1], 86400)
  return 1
end
return 0
"""


class Semaphore:
    """At most `limit` holders across every container. Each hold is a lease that expires if not renewed."""

    def __init__(self, name: str, limit: int, lease: float = 120):
        self.key = k("sem", name)
        self.limit = limit
        self.lease = lease

    def acquire(self, wait: float = 0, token: str | None = None) -> str | None:
        token = token or uuid.uuid4().hex
        deadline = time.time() + wait
        while True:
            t = time.time()
            if r().eval(_SEM_ACQUIRE, 1, self.key, t, self.limit, t + self.lease, token):
                return token
            if t >= deadline:
                return None
            time.sleep(0.5)

    def renew(self, token: str) -> None:
        r().zadd(self.key, {token: time.time() + self.lease}, xx=True)

    def release(self, token: str | None) -> None:
        if token:
            try:
                r().zrem(self.key, token)
            except redis.RedisError:
                pass

    def in_use(self) -> int:
        try:
            r().zremrangebyscore(self.key, "-inf", time.time())
            return int(r().zcard(self.key))
        except redis.RedisError:
            return 0


def ffmpeg_semaphore() -> Semaphore:
    return Semaphore("ffmpeg", settings.max_ffmpeg_processes, lease=60)


def ping() -> bool:
    try:
        return bool(r().ping())
    except redis.RedisError:
        return False
