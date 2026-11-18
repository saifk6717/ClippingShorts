"""Health and metrics for the dashboard: resources, worker heartbeats, queues and measured latencies."""
from __future__ import annotations

import statistics
import threading
import time

from sqlalchemy import func, select

from . import db
from . import redis_bus as rb
from .config import settings
from .resource_manager import publish_sample

WORKERS = ("stream", "ai", "renderer")
QUEUE_NAMES = ("stream_control", "transcription", "highlight_eval", "clip_extraction", "rendering", "publishing")


def sampler(stop: threading.Event):
    """Runs in the API process: measures the machine every 2 s for the admission controller."""
    while not stop.wait(2):
        try:
            storage = rb.get_json(rb.k("storage")) or {}
            extra = {"storage_used_gb": round(storage.get("total_mb", 0) / 1024, 2)} if storage else None
            publish_sample(extra)
        except Exception:  # noqa: BLE001
            pass


def workers() -> dict:
    out = {}
    for w in WORKERS:
        hb = rb.get_json(rb.k("worker", w, "hb"))
        out[w] = {"ok": bool(hb) and time.time() - hb.get("t", 0) < 20, **(hb or {})}
    return out


def queues() -> dict:
    out = {}
    for name in QUEUE_NAMES:
        q = rb.queue(name)
        try:
            dlq = int(rb.r().xlen(q.dlq))
        except Exception:  # noqa: BLE001
            dlq = 0
        out[name] = {"length": q.length(), "cap": q.cap, "dead_letters": dlq}
    return out


def latency(name: str) -> dict:
    vals = [float(v) for v in rb.r().lrange(rb.k("metrics", name), 0, 199)]
    if not vals:
        return {"n": 0}
    vals.sort()
    return {"n": len(vals), "median": round(statistics.median(vals), 2),
            "p90": round(vals[min(len(vals) - 1, int(len(vals) * 0.9))], 2), "max": round(vals[-1], 2)}


def health() -> dict:
    redis_ok = rb.ping()
    try:
        with db.session() as s:
            s.execute(select(1))
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    w = workers() if redis_ok else {}
    res = rb.get_json(rb.k("resources")) if redis_ok else None
    return {"ok": redis_ok and db_ok and all(x["ok"] for x in w.values()), "redis": redis_ok, "database": db_ok,
            "workers": w, "resources_level": (res or {}).get("level", "unknown"), "t": time.time()}


def dashboard() -> dict:
    with db.session() as s:
        def count(model, *where):
            q = select(func.count()).select_from(model)
            for x in where:
                q = q.where(x)
            return s.scalar(q) or 0
        streams = list(s.scalars(select(db.Stream)))
        out = {
            "streams_total": len(streams),
            "highlights": count(db.Highlight, db.Highlight.status.in_(("accepted", "extracted"))),
            "candidates": count(db.Highlight),
            "clips_pending_review": count(db.Clip, db.Clip.status == "ready", db.Clip.review_status == "pending"),
            "clips_ready": count(db.Clip, db.Clip.status == "ready"),
            "clips_rendered": count(db.Edit, db.Edit.status == "done"),
            "clips_uploaded": count(db.Upload, db.Upload.status == "done"),
            "renders_active": count(db.Edit, db.Edit.status.in_(("queued", "rendering", "deferred"))),
            "ai_cost_today_usd": round(s.scalar(select(func.coalesce(func.sum(db.AIUsage.est_cost_usd), 0))
                                                .where(db.AIUsage.ts > time.time() - 86400)) or 0, 4),
            "ai_calls_today": count(db.AIUsage, db.AIUsage.ts > time.time() - 86400, db.AIUsage.service == "gemini"),
        }
    active = disconnected = 0
    for st in streams:
        hb = rb.get_json(rb.k("stream", st.id, "hb")) or {}
        if hb.get("status") == "live":
            active += 1
        elif st.desired_state == "running":
            disconnected += 1
    out.update(streams_active=active, streams_disconnected=disconnected)
    out["resources"] = rb.get_json(rb.k("resources")) or {}
    out["storage"] = rb.get_json(rb.k("storage")) or {}
    out["health"] = health()
    out["latency"] = {n: latency(n) for n in ("transcription_latency", "ai_latency", "extract_latency",
                                              "render_latency", "upload_latency")}
    out["limits"] = {"max_streams": settings.max_streams, "transcriptions": settings.max_concurrent_transcriptions,
                     "ai_jobs": settings.max_concurrent_ai_jobs, "renders": settings.max_concurrent_render_jobs,
                     "ffmpeg": settings.max_ffmpeg_processes, "ffmpeg_in_use": rb.ffmpeg_semaphore().in_use()}
    return out
