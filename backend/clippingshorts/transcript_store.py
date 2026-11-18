"""The live transcript of each stream, kept only for the last few minutes (a sorted set in Redis per stream).

Nothing accumulates in RAM: the transcript of an eight-hour stream is never held anywhere, only the window the
judges and the extractor can still use.
"""
from __future__ import annotations

import json
import time

from . import redis_bus as rb
from .config import settings


def key(stream_id: str) -> str:
    return rb.k("tr", stream_id)


def add_words(stream_id: str, words: list, keep_seconds: float | None = None) -> None:
    if not words:
        return
    keep = keep_seconds or (settings.rolling_buffer_seconds + settings.source_post_seconds + 300)
    newest = max(w["s"] for w in words)
    pipe = rb.r().pipeline()
    pipe.zadd(key(stream_id), {json.dumps({"w": w["w"], "s": w["s"], "e": w["e"]}): w["s"] for w in words})
    pipe.zremrangebyscore(key(stream_id), "-inf", newest - keep)
    pipe.expire(key(stream_id), 3 * 3600)
    pipe.hset(rb.k("trmeta", stream_id), mapping={"until": max(w["e"] for w in words), "updated": time.time()})
    pipe.expire(rb.k("trmeta", stream_id), 3 * 3600)
    pipe.execute()


def mark_done(stream_id: str, until: float, latency: float, provider: str, ok: bool = True, error: str = "") -> None:
    rb.r().hset(rb.k("trmeta", stream_id), mapping={"done_until": until, "latency": round(latency, 2),
                                                    "provider": provider, "ok": int(ok), "error": error[:200],
                                                    "updated": time.time()})
    rb.r().expire(rb.k("trmeta", stream_id), 3 * 3600)


def meta(stream_id: str) -> dict:
    return rb.r().hgetall(rb.k("trmeta", stream_id)) or {}


def words_between(stream_id: str, start: float, end: float) -> list:
    rows = rb.r().zrangebyscore(key(stream_id), start, end)
    out = []
    for row in rows:
        try:
            out.append(json.loads(row))
        except ValueError:
            pass
    out.sort(key=lambda w: w["s"])
    return out


def clear(stream_id: str) -> None:
    rb.r().delete(key(stream_id), rb.k("trmeta", stream_id))
