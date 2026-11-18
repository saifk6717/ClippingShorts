"""The rolling buffer: the last few minutes of a stream as short segments on disk, never the whole stream.

FFmpeg writes segments and appends a line per finished segment to a CSV list. This module reads those lines,
places every segment on the stream's session clock, publishes the index to Redis (so the extractor in another
process can find them) and deletes segments older than the buffer window.

A highlight "pins" a time range first. Pinned segments are never deleted until the pin is released or expires,
so a slow AI decision or a slow extraction cannot lose the footage. Pins are bounded in number and lifetime,
which bounds the disk a stream can hold. All changes to one stream's index happen under one lock.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import redis_bus as rb
from .config import settings


@dataclass
class Segment:
    path: Path
    start: float
    end: float

    def as_dict(self) -> dict:
        return {"p": self.path.name, "s": round(self.start, 3), "e": round(self.end, 3)}


# A buffer belongs to one worker *session* of a stream: "<stream_id>.<session_id>". A restarted worker starts a new
# session with its own folder and index, so footage still pinned by the previous session survives the restart.
def seg_key(buffer_id: str) -> str:
    return rb.k("seg", buffer_id)


def pins_key(buffer_id: str) -> str:
    return rb.k("pins", buffer_id)


def buffer_dir(buffer_id: str) -> Path:
    sid, _, session = buffer_id.partition(".")
    return settings.buffers_dir / sid / (session or "default")


def has_pins(buffer_id: str) -> bool:
    now = time.time()
    for v in rb.r().hvals(pins_key(buffer_id)):
        try:
            if json.loads(v).get("expires", 0) >= now:
                return True
        except ValueError:
            continue
    return False


class RollingBuffer:
    def __init__(self, stream_id: str, window_seconds: int | None = None):
        self.stream_id = stream_id      # the buffer id: "<stream>.<session>"
        self.dir = buffer_dir(stream_id)
        self.window = window_seconds or settings.rolling_buffer_seconds
        self.lock = threading.RLock()
        self.segments: list[Segment] = []
        self.clock = 0.0                   # end of the newest segment on the session clock
        self._csv_rows: dict[str, int] = {}
        self._parts: list[str] = []
        self.bytes = 0
        self.deleted = 0

    # ---------------------------------------------------------------- setup
    def reset(self) -> None:
        """A fresh session. Older sessions of this stream are removed, unless a pin still protects their footage."""
        import shutil
        with self.lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            sid = self.stream_id.partition(".")[0]
            for old in self.dir.parent.iterdir():
                if old.is_dir() and old != self.dir and not has_pins(f"{sid}.{old.name}"):
                    shutil.rmtree(old, ignore_errors=True)
                    rb.r().delete(seg_key(f"{sid}.{old.name}"), pins_key(f"{sid}.{old.name}"))
            self.segments.clear()
            self._csv_rows.clear()
            self._parts.clear()
            self.clock = 0.0
            pipe = rb.r().pipeline()
            pipe.delete(seg_key(self.stream_id))
            pipe.delete(pins_key(self.stream_id))
            pipe.execute()

    def new_part(self, part: str, at_clock: float) -> None:
        """FFmpeg (re)started: its segments continue from `at_clock` (the audio clock), so both stay aligned."""
        with self.lock:
            self._parts.append(part)
            self.clock = max(self.clock, at_clock)

    # ---------------------------------------------------------------- scanning
    def scan(self) -> int:
        """Pick up segments FFmpeg has finished. Returns how many were added."""
        added = []
        with self.lock:
            for part in self._parts:
                f = self.dir / f"{part}.csv"
                if not f.exists():
                    continue
                try:
                    lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
                except OSError:
                    continue
                seen = self._csv_rows.get(part, 0)
                for line in lines[seen:]:
                    cells = line.split(",")
                    if len(cells) < 3:
                        continue
                    try:
                        dur = max(0.0, float(cells[2]) - float(cells[1]))
                    except ValueError:
                        continue
                    seg = Segment(self.dir / cells[0], self.clock, self.clock + dur)
                    self.clock += dur
                    self.segments.append(seg)
                    added.append(seg)
                    try:
                        self.bytes += seg.path.stat().st_size
                    except OSError:
                        pass
                self._csv_rows[part] = len(lines)
            if added:
                pipe = rb.r().pipeline()
                for seg in added:
                    pipe.zadd(seg_key(self.stream_id), {json.dumps(seg.as_dict()): seg.start})
                pipe.expire(seg_key(self.stream_id), 3600)
                pipe.execute()
        return len(added)

    # ---------------------------------------------------------------- pins
    def pins(self) -> list[dict]:
        out, now = [], time.time()
        raw = rb.r().hgetall(pins_key(self.stream_id))
        expired = []
        for pid, v in raw.items():
            try:
                p = json.loads(v)
            except ValueError:
                expired.append(pid)
                continue
            if p.get("expires", 0) < now:
                expired.append(pid)
            else:
                out.append(dict(p, id=pid))
        if expired:
            rb.r().hdel(pins_key(self.stream_id), *expired)
        return out

    # ---------------------------------------------------------------- expiry
    def expire(self) -> int:
        """Delete segments older than the window, except those any live pin still covers."""
        removed = 0
        with self.lock:
            keep_from = self.clock - self.window
            pins = self.pins()
            kept = []
            stale = []
            for seg in self.segments:
                old = seg.end < keep_from
                pinned = any(seg.end > p["start"] - 1 and seg.start < p["end"] + 1 for p in pins)
                if old and not pinned:
                    stale.append(seg)
                else:
                    kept.append(seg)
            if stale:
                pipe = rb.r().pipeline()
                for seg in stale:
                    try:
                        self.bytes -= seg.path.stat().st_size
                    except OSError:
                        pass
                    seg.path.unlink(missing_ok=True)
                    pipe.zrem(seg_key(self.stream_id), json.dumps(seg.as_dict()))
                    removed += 1
                pipe.execute()
                self.segments = kept
                self.deleted += removed
        return removed

    def stats(self) -> dict:
        with self.lock:
            span = (self.segments[-1].end - self.segments[0].start) if self.segments else 0
            return {"segments": len(self.segments), "seconds": round(span, 1), "bytes": max(0, self.bytes),
                    "clock": round(self.clock, 1), "pins": len(self.pins())}


# ---------------------------------------------------------------- used from any process
def add_pin(stream_id: str, start: float, end: float, reason: str = "", pin_id: str | None = None) -> str | None:
    """Protect [start, end] of this stream's buffer. Returns the pin id, or None when the stream already holds
    the maximum number of pins (the caller then merges into an existing one or gives up)."""
    import uuid
    with rb.Lock(f"pins:{stream_id}", ttl=10):
        existing = rb.r().hgetall(pins_key(stream_id))
        now = time.time()
        live = {}
        for k_, v in existing.items():
            try:
                p = json.loads(v)
            except ValueError:
                continue
            if p.get("expires", 0) >= now:
                live[k_] = p
        if pin_id is None and len(live) >= settings.max_pins_per_stream:
            return None
        pin_id = pin_id or uuid.uuid4().hex[:12]
        rb.r().hset(pins_key(stream_id), pin_id, json.dumps(
            {"start": start, "end": end, "reason": reason[:100], "expires": now + settings.pin_ttl_seconds}))
        rb.r().expire(pins_key(stream_id), settings.pin_ttl_seconds * 2)
        return pin_id


def get_pin(stream_id: str, pin_id: str) -> dict | None:
    v = rb.r().hget(pins_key(stream_id), pin_id)
    return json.loads(v) if v else None


def extend_pin(stream_id: str, pin_id: str, start: float, end: float) -> bool:
    with rb.Lock(f"pins:{stream_id}", ttl=10):
        v = rb.r().hget(pins_key(stream_id), pin_id)
        if not v:
            return False
        p = json.loads(v)
        p["start"], p["end"] = min(p["start"], start), max(p["end"], end)
        p["expires"] = time.time() + settings.pin_ttl_seconds
        rb.r().hset(pins_key(stream_id), pin_id, json.dumps(p))
        return True


def release_pin(stream_id: str, pin_id: str) -> None:
    if pin_id:
        rb.r().hdel(pins_key(stream_id), pin_id)


def segments_between(stream_id: str, start: float, end: float) -> list[Segment]:
    """Segments covering [start, end] from the published index, oldest first."""
    rows = rb.r().zrangebyscore(seg_key(stream_id), start - 30, end + 1)
    out = []
    d = buffer_dir(stream_id)
    for row in rows:
        try:
            v = json.loads(row)
        except ValueError:
            continue
        if v["e"] > start and v["s"] < end:
            out.append(Segment(d / v["p"], v["s"], v["e"]))
    out.sort(key=lambda s: s.start)
    return out


def newest_end(stream_id: str) -> float:
    got = rb.r().zrevrange(seg_key(stream_id), 0, 0)
    if not got:
        return 0.0
    try:
        return float(json.loads(got[0])["e"])
    except (ValueError, KeyError):
        return 0.0
