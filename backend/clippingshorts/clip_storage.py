"""Where files live, how they are written, and when they are removed.

  /data/clippingshorts/
    temp/rolling_buffers/<stream>/   live segments (deleted continuously)
    temp/audio/<stream>/             audio chunks waiting for transcription (deleted after use)
    temp/work/                       scratch space for FFmpeg jobs
    source_clips/<stream>/<clip>/    source.mp4 + thumb.jpg + sprite.jpg + waveform.json + words.json  (immutable)
    edited_clips/<clip>/<edit>.mp4   every rendered version, never overwriting the source
    thumbnails/  subtitles/  exports/  logs/

Files are written to a temporary name in the same folder and renamed into place, so a reader never sees half a
file and a crash leaves no corrupt clip. Large binaries never go to Redis or the database.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from . import db
from . import redis_bus as rb
from .config import settings
from .resource_manager import dir_size


def source_dir(clip: db.Clip) -> Path:
    return settings.source_dir / clip.rel_dir


def source_file(clip: db.Clip) -> Path:
    return source_dir(clip) / "source.mp4"


def edit_file(edit: db.Edit) -> Path:
    return settings.edited_dir / edit.output_rel


def safe_join(base: Path, rel: str) -> Path:
    """A path under base, refusing anything that would escape it."""
    p = (base / rel).resolve()
    if not str(p).startswith(str(base.resolve()) + os.sep) and p != base.resolve():
        raise ValueError("path outside storage")
    return p


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, value) -> None:
    atomic_write_text(path, json.dumps(value, separators=(",", ":")))


def tmp_name(final: Path) -> Path:
    """A temporary file next to `final` (same filesystem, so os.replace is atomic). Keeps the extension so FFmpeg
    knows the format."""
    return final.with_name(f".tmp-{os.getpid()}-{int(time.time() * 1000)}{final.suffix}")


def read_words(clip: db.Clip) -> list:
    for name in ("words.precise.json", "words.json"):
        f = source_dir(clip) / name
        if f.is_file():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
    return []


def delete_clip_files(clip: db.Clip) -> None:
    if clip.rel_dir:
        shutil.rmtree(source_dir(clip), ignore_errors=True)
    shutil.rmtree(settings.edited_dir / clip.id, ignore_errors=True)


def clean_temp() -> dict:
    """Remove stale scratch files and orphaned audio chunks/buffers of streams that are no longer running."""
    removed = 0
    cutoff = time.time() - settings.temp_file_max_age_seconds
    for base in (settings.audio_dir, settings.work_dir):
        for root, _dirs, files in os.walk(base):
            for f in files:
                p = os.path.join(root, f)
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.unlink(p)
                        removed += 1
                except OSError:
                    pass
    # rolling-buffer sessions that no live worker owns and no pin protects
    from .rolling_buffer import has_pins, pins_key, seg_key
    for sdir in settings.buffers_dir.glob("*"):
        if not sdir.is_dir():
            continue
        live = (rb.get_json(rb.k("stream", sdir.name, "hb")) or {}).get("session")
        for sess in sdir.iterdir():
            bid = f"{sdir.name}.{sess.name}"
            if not sess.is_dir() or sess.name == live or has_pins(bid):
                continue
            newest = max((f.stat().st_mtime for f in sess.iterdir() if f.is_file()), default=0)
            if newest < cutoff:
                shutil.rmtree(sess, ignore_errors=True)
                rb.r().delete(seg_key(bid), pins_key(bid))
                removed += 1
    return {"removed": removed}


def apply_retention() -> dict:
    """Expire source clips past their retention date (unless kept), with every edited version of them."""
    now = time.time()
    expired = 0
    for clip in db.all_of(db.Clip, db.Clip.expires_at > 0, db.Clip.expires_at < now, db.Clip.keep.is_(False),
                          db.Clip.status != "expired", limit=200):
        delete_clip_files(clip)
        db.update(db.Clip, clip.id, status="expired")
        expired += 1
    return {"expired": expired}


def usage_report() -> dict:
    parts = {
        "rolling_buffers": settings.buffers_dir, "audio_chunks": settings.audio_dir, "work": settings.work_dir,
        "source_clips": settings.source_dir, "edited_clips": settings.edited_dir,
        "thumbnails": settings.thumbs_dir, "subtitles": settings.subtitles_dir, "exports": settings.exports_dir,
        "logs": settings.logs_dir,
    }
    sizes = {name: dir_size(p) for name, p in parts.items()}
    total = sum(sizes.values())
    du = shutil.disk_usage(settings.data_dir)
    return {"t": time.time(), "dirs": {k: round(v / 2**20, 1) for k, v in sizes.items()},
            "total_mb": round(total / 2**20, 1), "limit_gb": settings.storage_limit_gb,
            "disk_free_gb": round(du.free / 2**30, 1), "disk_total_gb": round(du.total / 2**30, 1),
            "retention_days": settings.source_clip_retention_days}
