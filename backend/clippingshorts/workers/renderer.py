"""Renderer container: renders edits with FFmpeg, MAX_CONCURRENT_RENDER_JOBS at a time.

Rendering is the most expensive and the least urgent work, so it is the first to wait under pressure: when the
admission controller says no, the job is marked "deferred" and tried again shortly, while stream ingestion and
highlight extraction carry on.
"""
from __future__ import annotations

import os
import signal
import threading
import time
import traceback

from .. import db
from .. import redis_bus as rb
from ..clip_renderer import render
from ..config import settings
from ..jobs import JobError, create_upload
from ..media_tools import MediaError, ffmpeg_slot
from ..resource_manager import admit, cgroup_memory

STOP = threading.Event()
ACTIVE: dict[str, float] = {}


def work(n: int):
    q = rb.queue("rendering")
    consumer = f"{rb.consumer_name()}-r{n}"
    while not STOP.is_set():
        try:
            msg = q.get(block_ms=3000, consumer=consumer)
        except Exception:  # noqa: BLE001
            time.sleep(3)
            continue
        if not msg:
            continue
        eid = msg.data.get("edit_id")
        edit = db.get(db.Edit, eid)
        if edit is None:
            q.ack(msg)
            continue
        ok, why = admit("render")
        if not ok:
            db.update(db.Edit, eid, status="deferred", error=f"waiting for resources: {why}")
            rb.publish("edit", edit_id=eid, status="deferred", reason=why)
            STOP.wait(10)
            q.requeue(msg)
            continue
        db.update(db.Edit, eid, status="rendering", progress=0, error="")
        rb.publish("edit", edit_id=eid, status="rendering", progress=0)
        ACTIVE[eid] = time.time()
        last_touch = [time.time()]

        def progress(p, eid=eid):
            db.update(db.Edit, eid, progress=round(p, 1))
            rb.publish("edit", edit_id=eid, status="rendering", progress=round(p, 1))
            if time.time() - last_touch[0] > 60:
                q.touch(msg, consumer)
                last_touch[0] = time.time()
        t0 = time.time()
        try:
            with ffmpeg_slot(wait=600):
                out = render(eid, progress)
            params = dict(edit.params or {})
            params["resolved_crop_x"] = out.pop("crop_x", None)
            db.update(db.Edit, eid, status="done", progress=100, params=params, **out)
            rb.r().lpush(rb.k("metrics", "render_latency"), round(time.time() - t0, 2))
            rb.r().ltrim(rb.k("metrics", "render_latency"), 0, 199)
            rb.publish("edit", edit_id=eid, status="done", progress=100)
            q.ack(msg)
            if msg.data.get("auto_upload"):
                e = db.get(db.Edit, eid)
                p = e.params or {}
                try:
                    create_upload(e, p.get("title") or "Highlight", p.get("description") or "",
                                  p.get("hashtags") or [], msg.data["auto_upload"])
                except JobError as je:
                    rb.log_activity("", f"auto upload skipped: {je}", "warn")
        except (MediaError, Exception) as e:  # noqa: BLE001
            traceback.print_exc()
            err = str(e)[:600]
            dead = q.fail(msg, err)
            db.update(db.Edit, eid, status="failed" if dead else "queued", error=err)
            rb.publish("edit", edit_id=eid, status="failed" if dead else "queued", error=err[:200])
        finally:
            ACTIVE.pop(eid, None)


def heartbeat():
    while not STOP.wait(5):
        rb.set_json(rb.k("worker", "renderer", "hb"), {"t": time.time(), "active": list(ACTIVE), "pid": os.getpid(),
                                                       "slots": settings.max_concurrent_render_jobs,
                                                       "container": cgroup_memory()}, ttl=20)


def feeder():
    """Queues Shorts that are waiting (render queue was full) as room frees up, oldest first."""
    from sqlalchemy import select
    from ..jobs import enqueue_render
    while not STOP.wait(20):
        try:
            room = min(10, settings.queue_cap_render) - rb.queue("rendering").length()
            if room <= 0:
                continue
            with db.session() as s:
                waiting = list(s.scalars(select(db.Edit).where(db.Edit.status == "waiting")
                                         .order_by(db.Edit.created_at).limit(room)))
            for e in waiting:
                enqueue_render(e.id, auto_upload=(e.params or {}).get("auto_upload") or None)
        except Exception:  # noqa: BLE001 - try again on the next round
            traceback.print_exc()


def main():
    settings.ensure_dirs()
    db.init_db()
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(target=feeder, daemon=True).start()
    threads = [threading.Thread(target=work, args=(i,), name=f"render-{i}")
               for i in range(settings.max_concurrent_render_jobs)]
    for t in threads:
        t.start()
    print(f"renderer up: {settings.max_concurrent_render_jobs} slot(s)", flush=True)
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
