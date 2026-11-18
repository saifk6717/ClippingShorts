"""Turns an accepted highlight into a standalone, immutable source clip.

1. The highlight's time range was pinned when the candidate was raised (rolling_buffer.add_pin).
2. Wait until the buffer holds the post-event footage (or the stream stopped).
3. Join the pinned segments without re-encoding into source.mp4 (written to a temp name, then renamed).
4. Make the editor's helpers: cover thumbnail, timeline sprite sheet, waveform peaks, the live transcript words.
5. Release the pin, so the rolling buffer may delete those segments again.
"""
from __future__ import annotations

import json
import math
import subprocess
import time

import numpy as np

from . import db, rolling_buffer, transcript_store
from . import redis_bus as rb
from .clip_storage import atomic_write_json, tmp_name
from .config import settings
from .media_tools import MediaError, ffmpeg_slot, probe, run

SPRITE_EVERY = 2.0
SPRITE_W = 160
SPRITE_COLS = 10


class ExtractionError(Exception):
    pass


def _session_alive(stream_id: str, session: str) -> bool:
    hb = rb.get_json(rb.k("stream", stream_id, "hb")) or {}
    return hb.get("session") == session


def wait_for_footage(stream_id: str, session: str, end: float, timeout: float) -> float:
    """Wait until the buffer holds footage up to `end`, unless that worker session has ended (then use what exists)."""
    bid = f"{stream_id}.{session}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        newest = rolling_buffer.newest_end(bid)
        if newest >= end + 1 or not _session_alive(stream_id, session):
            return newest
        time.sleep(1.0)
    return rolling_buffer.newest_end(bid)


def extract(highlight_id: str) -> str:
    """Returns the new clip id. Raises ExtractionError with a readable reason."""
    t0 = time.time()
    h = db.get(db.Highlight, highlight_id)
    if h is None:
        raise ExtractionError("highlight no longer exists")
    stream = db.get(db.Stream, h.stream_id)
    bid = f"{h.stream_id}.{h.session_id}"
    pin = rolling_buffer.get_pin(bid, h.pin_id) if h.pin_id else None
    start = pin["start"] if pin else h.start_t
    end = pin["end"] if pin else h.end_t
    if end - start > settings.source_max_seconds:
        start = max(start, end - settings.source_max_seconds)
    wait_for_footage(h.stream_id, h.session_id, end, timeout=settings.source_post_seconds + 120)
    segs = [s for s in rolling_buffer.segments_between(bid, start, end) if s.path.exists()]
    if not segs:
        raise ExtractionError("that part of the stream is no longer in the buffer")
    if segs[0].start > start + 15:
        start = segs[0].start          # buffer did not reach back that far (e.g. stream just started): keep what exists
    clip = db.Clip(stream_id=h.stream_id, highlight_id=h.id, platform=stream.platform if stream else "",
                   creator_name=(stream.creator_name or stream.channel) if stream else "",
                   source_url=stream.url if stream else "", detected_at=h.detected_at, category=h.category,
                   confidence=h.confidence, title=h.title, hook=h.hook, description=h.description,
                   hashtags=h.hashtags or [], reason=h.reason, status="extracting",
                   expires_at=(time.time() + settings.source_clip_retention_days * 86400)
                   if settings.source_clip_retention_days > 0 else 0)
    with db.session() as s:
        s.add(clip)
    clip.rel_dir = f"{h.stream_id}/{clip.id}"
    out_dir = settings.source_dir / clip.rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / "source.mp4"
    lst = out_dir / ".concat.txt"
    lst.write_text("".join(f"file '{s.path.as_posix()}'\n" for s in segs), encoding="utf-8")
    tmp = tmp_name(final)
    try:
        with ffmpeg_slot(wait=300):
            run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                 "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-bsf:a", "aac_adtstoasc",
                 "-movflags", "+faststart", str(tmp)], timeout=300)
        tmp.replace(final)
    except MediaError as e:
        tmp.unlink(missing_ok=True)
        db.update(db.Clip, clip.id, status="failed", error=str(e)[:500], rel_dir=clip.rel_dir)
        raise ExtractionError(str(e)) from e
    finally:
        lst.unlink(missing_ok=True)
    # the pinned segments are copied: the buffer may now forget them
    rolling_buffer.release_pin(bid, h.pin_id)
    info = probe(final)
    base = segs[0].start
    words = [{"w": w["w"], "s": round(w["s"] - base, 2), "e": round(w["e"] - base, 2)}
             for w in transcript_store.words_between(h.stream_id, base - 1, base + info["duration"] + 1)]
    words = [w for w in words if 0 <= w["s"] <= info["duration"]]
    atomic_write_json(out_dir / "words.json", words)
    sug_s = max(0.0, h.start_t - base)
    sug_e = min(info["duration"], max(sug_s + 5, h.end_t - base))
    try:
        make_helpers(final, out_dir, info["duration"], sug_s)
    except MediaError as e:  # helpers are nice to have; the clip itself is fine
        rb.log_activity(h.stream_id, f"preview helpers failed: {str(e)[:120]}", "warn")
    db.update(db.Clip, clip.id, status="ready", rel_dir=clip.rel_dir, duration=info["duration"],
              width=info["width"], height=info["height"], suggested_start=round(sug_s, 2),
              suggested_end=round(sug_e, 2), words_status="live" if words else "none")
    db.update(db.Highlight, h.id, status="extracted")
    latency = time.time() - t0
    rb.r().lpush(rb.k("metrics", "extract_latency"), round(latency, 2))
    rb.r().ltrim(rb.k("metrics", "extract_latency"), 0, 199)
    rb.publish("clip", clip_id=clip.id, stream_id=h.stream_id, status="ready")
    rb.log_activity(h.stream_id, f"Saved {info['duration']:.0f}s source clip: {h.title or h.trigger}")
    return clip.id


def make_helpers(src, out_dir, duration: float, cover_t: float) -> None:
    with ffmpeg_slot(wait=300):
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{min(duration - 0.1, cover_t + 1):.2f}",
             "-i", str(src), "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(out_dir / "thumb.jpg")], 60)
        n = max(1, math.ceil(duration / SPRITE_EVERY))
        rows = math.ceil(n / SPRITE_COLS)
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vf",
             f"fps=1/{SPRITE_EVERY},scale={SPRITE_W}:-2,tile={SPRITE_COLS}x{rows}", "-frames:v", "1", "-q:v", "6",
             str(out_dir / "sprite.jpg")], 180)
        # waveform: 10 peaks per second from 8 kHz mono, decoded straight from the pipe (bounded: 5 min = 4.8 MB)
        p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1",
                            "-ar", "8000", "-f", "s16le", "pipe:1"], capture_output=True, timeout=120)
    sprite_h = 0
    try:
        sprite_h = probe(out_dir / "sprite.jpg")["height"]
    except MediaError:
        pass
    peaks = []
    if p.returncode == 0 and p.stdout:
        x = np.abs(np.frombuffer(p.stdout[: len(p.stdout) // 2 * 2], dtype=np.int16).astype(np.float32)) / 32768
        hop = 800
        n = len(x) // hop
        if n:
            peaks = [round(float(v), 3) for v in x[: n * hop].reshape(n, hop).max(axis=1)]
    atomic_write_json(out_dir / "waveform.json", {"per_second": 10, "peaks": peaks})
    atomic_write_json(out_dir / "sprite.json", {"every": SPRITE_EVERY, "cols": SPRITE_COLS, "tile_w": SPRITE_W,
                                                "rows": math.ceil(max(1, math.ceil(duration / SPRITE_EVERY))
                                                                  / SPRITE_COLS), "sheet_h": sprite_h})


def precise_words(clip_id: str) -> int:
    """Re-transcribe the saved clip's own audio, so captions line up exactly with this file."""
    from .transcription import SR, transcribe
    clip = db.get(db.Clip, clip_id)
    if clip is None or clip.status != "ready":
        return 0
    src = settings.source_dir / clip.rel_dir / "source.mp4"
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1",
                        "-ar", str(SR), "-f", "s16le", "pipe:1"], capture_output=True, timeout=180)
    if p.returncode != 0:
        raise ExtractionError("could not read the clip audio")
    pcm = np.frombuffer(p.stdout[: len(p.stdout) // 2 * 2], dtype=np.int16)
    words, _prov = transcribe(pcm)
    atomic_write_json(settings.source_dir / clip.rel_dir / "words.precise.json", words)
    db.update(db.Clip, clip_id, words_status="precise")
    rb.publish("clip", clip_id=clip_id, status="words")
    return len(words)


def describe_signals(sig: dict) -> str:
    return json.dumps({k: sig[k] for k in list(sig)[:12]})
