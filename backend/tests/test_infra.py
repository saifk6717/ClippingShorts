import time

import pytest

from clippingshorts import redis_bus as rb
from clippingshorts import rolling_buffer
from clippingshorts.config import settings
from clippingshorts.stream_manager import StreamError, add_streams, normalize


def test_queue_backpressure_retry_and_dead_letter():
    q = rb.Queue("t_q", cap=3, visibility=1, max_attempts=2)
    for i in range(3):
        q.put({"i": i})
    with pytest.raises(rb.QueueFull):
        q.put({"i": 99})
    m = q.get(block_ms=100)
    assert m.data == {"i": 0}
    q.ack(m)
    m = q.get(block_ms=100)
    assert not q.fail(m, "boom")          # first failure: retried
    for _ in range(3):
        m = q.get(block_ms=100)
        if m.data["i"] == 1:
            break
        q.ack(m)
    assert m.attempts == 1
    assert q.fail(m, "boom again")        # second failure: dead-lettered
    assert q.dead_letters()[0]["data"] == {"i": 1}


def test_queue_reclaims_message_of_dead_consumer():
    q = rb.Queue("t_q2", cap=10, visibility=1)
    q.put({"job": 1})
    m = q.get(block_ms=100, consumer="crashed")
    assert m is not None
    time.sleep(1.2)
    m2 = q.get(block_ms=100, consumer="healthy")
    assert m2 is not None and m2.data == {"job": 1}
    q.ack(m2)


def test_semaphore_limit():
    s = rb.Semaphore("t_sem", 2, lease=5)
    a, b = s.acquire(), s.acquire()
    assert a and b and s.acquire() is None
    s.release(a)
    assert s.acquire() is not None


def test_lock_is_exclusive():
    l1, l2 = rb.Lock("t_lock", ttl=5), rb.Lock("t_lock", ttl=5)
    assert l1.acquire() and not l2.acquire()
    l1.release()
    assert l2.acquire()
    l2.release()


def test_rolling_buffer_expires_but_keeps_pinned(tmp_path):
    bid = "streamA.sess1"
    buf = rolling_buffer.RollingBuffer(bid, window_seconds=20)
    buf.reset()
    buf.new_part("p001", 0)
    rows = []
    for i in range(20):                         # 20 segments x 4 s = 80 s
        name = f"p001_{i:06d}.ts"
        (buf.dir / name).write_bytes(b"x" * 1000)
        rows.append(f"{name},{i * 4}.0,{(i + 1) * 4}.0")
    (buf.dir / "p001.csv").write_text("\n".join(rows) + "\n")
    assert buf.scan() == 20
    pin = rolling_buffer.add_pin(bid, 10, 18, "test")
    buf.expire()
    names = {s.path.name for s in buf.segments}
    assert "p001_000000.ts" not in names                     # old, not pinned: deleted
    assert {"p001_000002.ts", "p001_000003.ts", "p001_000004.ts"} <= names   # pinned: kept
    assert (buf.dir / "p001_000003.ts").exists()
    assert len(rolling_buffer.segments_between(bid, 10, 18)) == 3
    rolling_buffer.release_pin(bid, pin)
    buf.expire()
    assert not (buf.dir / "p001_000003.ts").exists()
    assert all(s.end >= buf.clock - 20 for s in buf.segments)


def test_pins_are_bounded():
    bid = "streamB.s"
    ids = [rolling_buffer.add_pin(bid, i, i + 5) for i in range(settings.max_pins_per_stream)]
    assert all(ids)
    assert rolling_buffer.add_pin(bid, 100, 110) is None
    assert rolling_buffer.extend_pin(bid, ids[0], 0, 50)
    assert rolling_buffer.get_pin(bid, ids[0])["end"] == 50


def test_url_validation():
    assert normalize("twitch.tv/SomeOne", "twitch") == ("https://www.twitch.tv/someone", "someone")
    assert normalize("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3", "youtube")[1] == "dQw4w9WgXcQ"
    assert normalize("https://youtu.be/dQw4w9WgXcQ", "youtube")[1] == "dQw4w9WgXcQ"
    assert normalize("https://www.youtube.com/@Creator/live", "youtube")[0] == "https://www.youtube.com/@Creator/live"
    for url, plat in [("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "twitch"), ("https://twitch.tv/abc", "youtube"),
                      ("https://evil.com/twitch.tv/abc", "twitch"), ("https://www.twitch.tv/directory", "twitch"),
                      ("https://user:pw@twitch.tv/abc", "twitch"), ("https://www.youtube.com/feed", "youtube")]:
        with pytest.raises(StreamError):
            normalize(url, plat)


def test_add_streams_rejects_duplicates():
    created, errors = add_streams([
        {"url": "https://www.twitch.tv/dupe_test", "platform": "twitch"},
        {"url": "twitch.tv/DUPE_TEST", "platform": "twitch"},
        {"url": "https://youtu.be/aaaaaaaaaaa", "platform": "youtube"},
        {"url": "https://youtu.be/aaaaaaaaaaa", "platform": "twitch"},
    ])
    assert len(created) == 2
    assert {e["index"] for e in errors} == {1, 3}
    _, errors2 = add_streams([{"url": "https://www.twitch.tv/dupe_test", "platform": "twitch"}])
    assert "already" in errors2[0]["error"]
