"""Stream worker container: supervises one process per running stream, extracts clips, keeps storage tidy.

Reconciliation loop (every 2 s): streams whose desired state is "running" get exactly one child process; streams
set to "stopped" are stopped gracefully (SIGTERM, then SIGKILL after 15 s). A crashed child is restarted with
exponential backoff; one that crashes 6 times within 30 minutes is marked "failed" and left alone until someone
presses Restart, so a broken stream can never turn into a restart storm. A child above
STREAM_WORKER_MEM_LIMIT_MB (FFmpeg included) is stopped and restarted before the kernel's OOM killer gets near it.
"""
from __future__ import annotations

import collections
import os
import signal
import subprocess
import sys
import threading
import time
import traceback

from .. import clip_storage, db, rolling_buffer
from .. import redis_bus as rb
from ..clip_extractor import ExtractionError, extract
from ..config import settings
from ..resource_manager import admit, cgroup_memory, process_tree_usage

CRASH_WINDOW, CRASH_LIMIT = 1800, 6


class Child:
    def __init__(self, sid: str, proc: subprocess.Popen):
        self.sid, self.proc = sid, proc
        self.started = time.time()
        self.stopping_since = 0.0


class Supervisor:
    def __init__(self):
        self.children: dict[str, Child] = {}
        self.crashes: dict[str, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=20))
        self.not_before: dict[str, float] = {}
        self.stop_flag = threading.Event()
        self.manual_reset: set[str] = set()

    # ---------------------------------------------------------------- children
    def spawn(self, sid: str):
        log = open(settings.logs_dir / f"stream-{sid}.log", "ab", buffering=0)  # noqa: SIM115
        proc = subprocess.Popen([sys.executable, "-m", "clippingshorts.workers.stream_proc", sid], stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
        self.children[sid] = Child(sid, proc)
        rb.log_activity(sid, f"stream worker started (pid {proc.pid})")

    def signal_group(self, child: Child, sig):
        try:
            os.killpg(child.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def stop_child(self, child: Child, hard_after: float = 15):
        if not child.stopping_since:
            child.stopping_since = time.time()
            rb.send_stream_command(child.sid, {"type": "stop"})
            self.signal_group(child, signal.SIGTERM)
        elif time.time() - child.stopping_since > hard_after:
            self.signal_group(child, signal.SIGKILL)

    def reap(self):
        for sid, child in list(self.children.items()):
            code = child.proc.poll()
            if code is None:
                continue
            del self.children[sid]
            st = db.get(db.Stream, sid)
            if child.stopping_since or (st and st.desired_state != "running"):
                continue
            self.crashes[sid].append(time.time())
            recent = [t for t in self.crashes[sid] if time.time() - t < CRASH_WINDOW]
            delay = min(300, 5 * 2 ** (len(recent) - 1))
            self.not_before[sid] = time.time() + delay
            rb.log_activity(sid, f"stream worker exited with code {code}; restart in {delay}s", "warn")
            if len(recent) >= CRASH_LIMIT:
                db.update(db.Stream, sid, status="failed",
                          last_error=f"crashed {len(recent)} times in 30 min; press Restart after fixing the cause")
                rb.publish("stream", stream_id=sid, status="failed")

    def reconcile(self):
        self.reap()
        streams = {s.id: s for s in db.all_of(db.Stream)}
        for sid, child in list(self.children.items()):
            st = streams.get(sid)
            if st is None or st.desired_state != "running" or not st.enabled:
                self.stop_child(child)
        for sid, st in streams.items():
            if st.desired_state != "running" or not st.enabled or sid in self.children:
                continue
            if st.status == "failed" and sid not in self.manual_reset:
                continue
            if time.time() < self.not_before.get(sid, 0):
                continue
            running = sum(1 for c in self.children.values() if not c.stopping_since)
            if running >= settings.max_streams:
                db.update(db.Stream, sid, status="deferred", last_error=f"MAX_STREAMS={settings.max_streams} reached")
                continue
            ok, why = admit("new_stream")
            if not ok:
                if st.status != "deferred":
                    db.update(db.Stream, sid, status="deferred", last_error=f"waiting for resources: {why}")
                    rb.log_activity(sid, f"start deferred: {why}", "warn")
                continue
            self.manual_reset.discard(sid)
            self.spawn(sid)

    def control(self):
        """Messages from the API: start/stop/restart take effect immediately instead of on the next round."""
        q = rb.queue("stream_control")
        while not self.stop_flag.is_set():
            try:
                msg = q.get(block_ms=2000)
            except Exception:  # noqa: BLE001
                time.sleep(2)
                continue
            if not msg:
                continue
            sid, action = msg.data.get("stream_id"), msg.data.get("action")
            if action in ("restart", "start"):
                self.not_before.pop(sid, None)
                self.crashes.pop(sid, None)
                self.manual_reset.add(sid)
                if action == "restart" and sid in self.children:
                    self.stop_child(self.children[sid])
            q.ack(msg)

    def watch_memory(self):
        total_rss = 0.0
        for sid, child in list(self.children.items()):
            u = process_tree_usage(child.proc.pid)
            total_rss += u["rss_mb"]
            rb.set_json(rb.k("stream", sid, "usage"), dict(u, pid=child.proc.pid, uptime=round(time.time() -
                                                                                            child.started)), ttl=15)
            if u["rss_mb"] > settings.stream_worker_mem_limit_mb and not child.stopping_since:
                rb.log_activity(sid, f"stream worker used {u['rss_mb']:.0f} MB (> {settings.stream_worker_mem_limit_mb}"
                                     " MB limit): restarting it", "warn")
                self.stop_child(child, hard_after=5)
        rb.set_json(rb.k("worker", "stream", "hb"), {
            "t": time.time(), "children": len(self.children), "rss_mb": round(total_rss, 1),
            "container": cgroup_memory(), "pid": os.getpid()}, ttl=20)

    # ---------------------------------------------------------------- extraction
    def extraction_loop(self, n: int):
        q = rb.queue("clip_extraction")
        while not self.stop_flag.is_set():
            try:
                msg = q.get(block_ms=3000, consumer=f"{rb.consumer_name()}-x{n}")
            except Exception:  # noqa: BLE001
                time.sleep(3)
                continue
            if not msg:
                continue
            hid = msg.data.get("highlight_id")
            ok, why = admit("extract")
            if not ok:
                time.sleep(5)
                q.requeue(msg)
                continue
            try:
                clip_id = extract(hid)
                q.ack(msg)
                self.after_extract(clip_id, msg.data)
            except ExtractionError as e:
                self.extraction_failed(msg, q, hid, str(e))
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self.extraction_failed(msg, q, hid, f"{type(e).__name__}: {e}")

    def extraction_failed(self, msg, q, hid, err):
        dead = q.fail(msg, err)
        if dead or "no longer in the buffer" in err:
            if not dead:
                q.ack(msg)
            h = db.get(db.Highlight, hid)
            if h:
                rolling_buffer.release_pin(f"{h.stream_id}.{h.session_id}", h.pin_id)
                db.update(db.Highlight, hid, status="failed", reason=f"extraction failed: {err[:300]}")
                rb.log_activity(h.stream_id, f"clip extraction failed: {err[:150]}", "error")

    def after_extract(self, clip_id: str, data: dict):
        """Precise captions from the clip's own audio, and the optional automatic Short + upload."""
        if settings.transcription_provider in ("chirp3", "parakeet"):
            try:
                rb.queue("transcription").put({"kind": "clip", "clip_id": clip_id})
            except rb.QueueFull:
                pass
        clip = db.get(db.Clip, clip_id)
        st = db.get(db.Stream, clip.stream_id) if clip and clip.stream_id else None
        mode = ((st.settings or {}).get("auto_upload") if st else None) or settings.auto_upload
        if clip and mode in ("private", "unlisted", "public") and clip.confidence >= settings.auto_upload_min_confidence:
            from ..jobs import create_edit, enqueue_render
            e = create_edit(clip, {"start": clip.suggested_start, "end": clip.suggested_end}, auto=True)
            enqueue_render(e.id, auto_upload=mode)

    # ---------------------------------------------------------------- housekeeping
    def janitor(self):
        while not self.stop_flag.wait(1 if not hasattr(self, "_jan") else 300):
            self._jan = True
            try:
                clip_storage.clean_temp()
                clip_storage.apply_retention()
                rep = clip_storage.usage_report()
                rb.set_json(rb.k("storage"), rep, ttl=1200)
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    # ---------------------------------------------------------------- main
    def run(self):
        settings.ensure_dirs()
        db.init_db()
        signal.signal(signal.SIGTERM, lambda *_: self.stop_flag.set())
        signal.signal(signal.SIGINT, lambda *_: self.stop_flag.set())
        threading.Thread(target=self.control, daemon=True, name="control").start()
        threading.Thread(target=self.janitor, daemon=True, name="janitor").start()
        for i in range(settings.max_concurrent_extractions):
            threading.Thread(target=self.extraction_loop, args=(i,), daemon=True, name=f"extract-{i}").start()
        print(f"stream supervisor up: MAX_STREAMS={settings.max_streams}", flush=True)
        while not self.stop_flag.is_set():
            try:
                self.reconcile()
                self.watch_memory()
            except Exception:  # noqa: BLE001 - the supervisor itself must never die on one bad round
                traceback.print_exc()
            self.stop_flag.wait(2)
        # graceful shutdown: stop every child (desired states stay, so they resume when the container returns)
        for child in self.children.values():
            child.stopping_since = 0
            self.stop_child(child)
        deadline = time.time() + 20
        while self.children and time.time() < deadline:
            self.reap_quiet()
            time.sleep(0.5)
        for child in self.children.values():
            self.signal_group(child, signal.SIGKILL)

    def reap_quiet(self):
        for sid, child in list(self.children.items()):
            if child.proc.poll() is not None:
                del self.children[sid]


def main():
    Supervisor().run()


if __name__ == "__main__":
    main()
