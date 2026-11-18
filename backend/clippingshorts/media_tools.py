"""Small FFmpeg / ffprobe helpers. Every FFmpeg job takes a slot from the global FFmpeg semaphore."""
from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

from . import redis_bus as rb


class MediaError(RuntimeError):
    pass


def run(cmd: list, timeout: float = 900, cwd=None) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           cwd=cwd, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise MediaError(f"{Path(cmd[0]).name} timed out after {timeout:.0f}s") from e
    if p.returncode != 0:
        raise MediaError(f"{Path(cmd[0]).name} failed: {p.stderr.strip()[-600:]}")
    return p.stdout


def probe(path: Path) -> dict:
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], 60)
    d = json.loads(out)
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    return {"duration": float(d.get("format", {}).get("duration") or v.get("duration") or 0),
            "width": int(v.get("width") or 0), "height": int(v.get("height") or 0), "has_audio": a is not None,
            "size": int(d.get("format", {}).get("size") or 0)}


@contextmanager
def ffmpeg_slot(wait: float = 600):
    """Hold one of MAX_FFMPEG_PROCESSES slots for the duration of a short FFmpeg job."""
    sem = rb.ffmpeg_semaphore()
    sem.lease = max(sem.lease, wait + 600)
    token = sem.acquire(wait=wait)
    if not token:
        raise MediaError("no free FFmpeg slot (MAX_FFMPEG_PROCESSES reached)")
    try:
        yield token
    finally:
        sem.release(token)
