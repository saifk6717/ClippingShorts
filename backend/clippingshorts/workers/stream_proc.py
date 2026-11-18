"""One live stream, in its own OS process: `python -m clippingshorts.workers.stream_proc <stream_id>`.

Owns exactly one FFmpeg capture, one chat connection, the audio/chat analysers, the rolling buffer and the local
highlight detector for this stream. Everything it keeps is bounded. If it crashes, only this stream is affected;
the supervisor restarts it with backoff. A Redis lock guarantees at most one process per stream, even across
supervisor restarts.

Session clock: seconds of audio received since this process started (the stream's "t"). Segments, transcript
words, signals and highlights all use it.
"""
from __future__ import annotations

import collections
import json
import os
import signal
import sys
import threading
import time
import traceback

import numpy as np

from .. import db, rolling_buffer, transcript_store
from .. import redis_bus as rb
from ..audio_analyzer import AudioAnalyzer
from ..chat_analyzer import ChatAnalyzer, ChatEvent
from ..config import settings
from ..highlight_detector import Candidate, HighlightDetector
from ..resource_manager import admit
from ..transcription import quiet_cut, wav_bytes
from ..video_ingestion import SR, NotLive, capture_command, resolve, start_capture


class StreamWorker:
    def __init__(self, stream_id: str):
        self.sid = stream_id
        st = db.get(db.Stream, stream_id)
        if st is None:
            raise SystemExit(f"stream {stream_id} not found")
        self.st = st
        self.cfg = dict(st.settings or {})
        self.session = db.new_id()
        self.stop_flag = threading.Event()
        self.lock = threading.RLock()
        self.bid = f"{stream_id}.{self.session}"
        self.buffer = rolling_buffer.RollingBuffer(self.bid)
        self.audio = AudioAnalyzer()
        self.chat = ChatAnalyzer()
        self.detector = HighlightDetector(self.cfg)
        self.samples = 0
        self.pending = bytearray()                 # audio not yet sent for transcription (bounded)
        self.pending_t0 = 0.0
        self.proc = None
        self.status = "starting"
        self.error = ""
        self.title = ""
        self.video_id = ""
        self.started_at = time.time()
        self.live_since = 0.0
        self.reconnects = 0
        self.highlights = 0
        self.candidates = 0
        self.chunks_sent = 0
        self.chunks_dropped = 0
        self.open_candidates: list[dict] = []      # pinned, waiting for post-event audio + transcript
        self.chat_state = {"connected": False, "error": ""}
        self.levels = collections.deque(maxlen=120)
        self.last_status_saved = ""
        self.sem_token = None
        self.lock_obj = rb.Lock(f"streamproc:{stream_id}", ttl=30)
        self.chat_conn = None
        self.synthetic_dropped = False

    # ---------------------------------------------------------------- helpers
    @property
    def t(self) -> float:
        return self.samples / SR

    def set_status(self, status: str, error: str = ""):
        self.status, self.error = status, error
        if (status, error) != self.last_status_saved:
            self.last_status_saved = (status, error)
            try:
                db.update(db.Stream, self.sid, status=status, last_error=error[:500])
            except Exception:  # noqa: BLE001
                pass
            rb.publish("stream", stream_id=self.sid, status=status, error=error)
            if error:
                rb.log_activity(self.sid, f"{self.label()}: {status} – {error[:150]}", "warn")
            else:
                rb.log_activity(self.sid, f"{self.label()}: {status}")

    def label(self) -> str:
        return self.st.creator_name or self.st.channel or self.st.url

    # ---------------------------------------------------------------- chat
    def on_chat(self, ev: ChatEvent):
        if self.samples == 0:
            return            # not capturing yet: there is no stream clock to place this message on
        late = min(60.0, max(0.0, time.time() - ev.ts))    # batched delivery: count it when it was written
        self.chat.add(ev, int(max(0.0, self.t - late)))

    def on_chat_state(self, ok: bool, why: str):
        self.chat.connected = ok
        self.chat_state = {"connected": ok, "error": why}

    def start_chat(self):
        p = self.st.platform
        try:
            if p == "twitch":
                from ..twitch_connector import TwitchChat
                self.chat_conn = TwitchChat(self.st.url, self.sid, self.on_chat, self.on_chat_state)
            elif p == "youtube":
                from ..youtube_connector import YouTubeChat
                self.chat_conn = YouTubeChat(self.st.url, self.sid, self.on_chat, self.on_chat_state)
            elif p == "synthetic":
                from ..synthetic import SyntheticChat
                self.chat_conn = SyntheticChat(self.st.url, self.sid, self.on_chat, lambda: self.t, self.on_chat_state)
            if self.chat_conn:
                self.chat_conn.start()
        except Exception as e:  # noqa: BLE001 - no chat is degraded, not fatal
            self.on_chat_state(False, str(e)[:160])

    # ---------------------------------------------------------------- ingestion
    def ingest(self):
        backoff = 5.0
        part_n = 0
        while not self.stop_flag.is_set():
            try:
                res = resolve(self.st.url, self.st.platform)
            except NotLive as e:
                self.set_status("offline" if "not live" in str(e) else "disconnected", str(e))
                self.stop_flag.wait(backoff)
                backoff = min(backoff * 1.6, 180)
                continue
            except Exception as e:  # noqa: BLE001
                self.set_status("disconnected", f"resolve failed: {str(e)[:200]}")
                self.stop_flag.wait(backoff)
                backoff = min(backoff * 1.6, 180)
                continue
            self.title = res.title
            if not self.st.creator_name and res.uploader:
                # remember the real channel name (a YouTube watch link only gives us the video id)
                self.st.creator_name = res.uploader[:120]
                db.update(db.Stream, self.sid, creator_name=self.st.creator_name)
            if res.video_id and self.st.platform == "youtube":
                self.video_id = res.video_id
                if self.chat_conn and hasattr(self.chat_conn, "set_video"):
                    self.chat_conn.set_video(res.video_id)
            sem = rb.ffmpeg_semaphore()
            self.sem_token = sem.acquire(wait=20)
            if not self.sem_token:
                self.set_status("waiting", "no free FFmpeg slot (MAX_FFMPEG_PROCESSES)")
                self.stop_flag.wait(15)
                continue
            part_n += 1
            part = f"p{part_n:03d}"
            self.buffer.new_part(part, self.t)
            cmd = capture_command(res, self.buffer.dir, part, settings.segment_seconds,
                                  realtime=self.st.platform == "synthetic")
            got = 0
            try:
                self.proc = start_capture(cmd, settings.logs_dir / f"ffmpeg-{self.sid}.log")
                self.set_status("live")
                self.live_since = time.time()
                last_renew = time.time()
                drop_after = 0.0
                if self.st.platform == "synthetic":
                    from ..synthetic import options
                    drop_after = options(self.st.url)["drop"]
                while not self.stop_flag.is_set():
                    data = self.proc.stdout.read(SR * 2)
                    if not data:
                        break
                    got += len(data)
                    self.on_audio(data)
                    if time.time() - last_renew > 20:
                        sem.renew(self.sem_token)
                        last_renew = time.time()
                    if drop_after and not self.synthetic_dropped and self.t > drop_after:
                        self.synthetic_dropped = True
                        self.proc.kill()          # test hook: simulate the capture dying once
            finally:
                if self.proc and self.proc.poll() is None:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=10)
                    except Exception:  # noqa: BLE001
                        self.proc.kill()
                sem.release(self.sem_token)
                self.sem_token = None
            if self.stop_flag.is_set():
                break
            self.reconnects += 1
            if got > SR * 2 * 30:
                backoff = 5.0
                self.set_status("reconnecting", "capture ended, reconnecting")
                self.stop_flag.wait(3)
            else:
                self.set_status("disconnected", "capture failed quickly; retrying")
                self.stop_flag.wait(backoff)
                backoff = min(backoff * 1.6, 180)

    def on_audio(self, data: bytes):
        data = data[: len(data) // 2 * 2]
        feat = self.audio.feed(data)
        with self.lock:
            if not self.pending:
                self.pending_t0 = self.t
            self.pending += data
            self.samples += len(data) // 2
            # never hold more than 60 s of audio: if transcription can't keep up, the oldest is dropped
            limit = SR * 2 * 60
            if len(self.pending) > limit:
                cut = len(self.pending) - limit
                del self.pending[:cut]
                self.pending_t0 += cut / 2 / SR
                self.chunks_dropped += 1
        self.levels.append(feat["db"])
        self.maybe_send_chunk()
        self.on_second(feat)

    def maybe_send_chunk(self):
        chunk = settings.audio_chunk_seconds
        with self.lock:
            if len(self.pending) < chunk * SR * 2:
                return
            audio = np.frombuffer(bytes(self.pending), dtype=np.int16)
            cut = quiet_cut(audio, chunk * 0.6, chunk)
            piece, t0 = audio[:cut], self.pending_t0
            del self.pending[: cut * 2]
            self.pending_t0 += cut / SR
        if settings.transcription_provider == "none" or not settings.live_transcription:
            return                     # only saved Shorts are transcribed (LIVE_TRANSCRIPTION=0)
        ok, why = admit("transcribe")
        if not ok:
            self.chunks_dropped += 1
            return
        d = settings.audio_dir / self.sid
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{self.session}-{int(t0 * 1000):010d}.wav"
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(wav_bytes(piece))
        os.replace(tmp, path)
        try:
            rb.queue("transcription").put({"kind": "live", "stream_id": self.sid, "session": self.session,
                                           "path": str(path), "t0": round(t0, 3), "dur": round(len(piece) / SR, 3),
                                           "created": time.time()})
            self.chunks_sent += 1
        except rb.QueueFull:
            path.unlink(missing_ok=True)
            self.chunks_dropped += 1

    # ---------------------------------------------------------------- detection
    def on_second(self, feat: dict):
        t = self.t
        if t < 20:
            return
        a_exc = self.audio.excitement()
        c_exc, cstats = self.chat.excitement(int(t))
        voiced = list(self.audio.voiced)[-30:]
        has_speech = bool(voiced) and float(np.mean(voiced)) > 0.15
        sig = {"jump_pct": feat.get("jump_pct", 0), "z": feat.get("z", 0), "db": feat.get("db"),
               "baseline_db": feat.get("baseline_db"), "audio_exc": round(a_exc, 1), "chat_exc": round(c_exc, 1),
               "unique_per_sec": cstats["unique_per_sec"], "msgs_per_sec": cstats["msgs_per_sec"],
               "top_user_share": cstats["top_user_share"], "reaction_ratio": cstats["reaction_ratio"]}
        cand = self.detector.step(t, a_exc, c_exc, sig, has_speech)
        if cand:
            if cand.periodic and not admit("periodic_eval")[0]:
                return
            try:
                self.raise_candidate(cand)
            except Exception as e:  # noqa: BLE001
                rb.log_activity(self.sid, f"candidate failed: {str(e)[:150]}", "warn")

    def raise_candidate(self, c: Candidate):
        pre = float(self.cfg.get("source_pre_seconds", settings.source_pre_seconds))
        post = float(self.cfg.get("source_post_seconds", settings.source_post_seconds))
        start, end = max(0.0, c.t - pre), c.t + post
        # overlapping events: an open candidate whose window overlaps grows instead of making a second clip
        for oc in self.open_candidates:
            if start < oc["end"] and end > oc["start"] and not c.periodic:
                new_end = min(oc["start"] + settings.source_max_seconds, max(oc["end"], end))
                rolling_buffer.extend_pin(self.bid, oc["pin"], oc["start"], new_end)
                oc["end"] = new_end
                oc["due"] = max(oc["due"], min(new_end, c.t + settings.highlight_post_seconds))
                oc["triggers"].append(c.trigger)
                oc["score"] = max(oc["score"], c.local_score)
                return
        pin = rolling_buffer.add_pin(self.bid, start, end, c.trigger)
        if pin is None:
            rb.log_activity(self.sid, "candidate skipped: too many clips waiting for this stream", "warn")
            return
        h = db.Highlight(stream_id=self.sid, session_id=self.session, event_t=round(c.t, 2),
                         start_t=round(max(0.0, c.t - settings.highlight_pre_seconds), 2),
                         end_t=round(c.t + settings.highlight_post_seconds, 2), trigger=c.trigger[:200],
                         local_score=round(c.local_score, 1), signals=c.signals, pin_id=pin, status="candidate")
        with db.session() as s:
            s.add(h)
        self.candidates += 1
        self.open_candidates.append({"hid": h.id, "pin": pin, "t": c.t, "start": start, "end": end,
                                     "due": c.t + settings.highlight_post_seconds, "created": time.time(),
                                     "triggers": [c.trigger], "score": c.local_score, "periodic": c.periodic})
        if not c.periodic:
            rb.publish("spike", stream_id=self.sid, t=c.t, trigger=c.trigger, score=round(c.local_score, 1))

    def dispatch_candidates(self):
        """Send candidates to the judges once the post-event audio is in and its words are (mostly) transcribed."""
        if not self.open_candidates:
            return
        tr_until = float(transcript_store.meta(self.sid).get("done_until") or 0)
        for oc in list(self.open_candidates):
            if self.t < oc["due"]:
                continue
            waited = time.time() - oc["created"]
            if tr_until < oc["due"] and waited < 45 and settings.transcription_provider != "none" \
                    and settings.live_transcription:
                continue
            self.open_candidates.remove(oc)
            a, b = int(max(0, oc["t"] - 90)), int(oc["due"])
            z = self.audio.window(int(self.t) - a + 1)[: b - a + 1]
            people = self.chat.per_second_people(a, b)
            payload = {"highlight_id": oc["hid"], "stream_id": self.sid, "event_t": oc["t"], "due": oc["due"],
                       "window_start": a, "z": z, "people": people, "triggers": oc["triggers"][:5],
                       "local_score": oc["score"], "periodic": oc["periodic"], "title": self.title,
                       "creator": self.label(), "chat": self.chat.stats(int(oc["t"]) + 8)}
            if oc["triggers"] and len(oc["triggers"]) > 1:
                db.update(db.Highlight, oc["hid"], trigger=" + ".join(oc["triggers"][:4])[:200],
                          local_score=round(oc["score"], 1))
            try:
                rb.queue("highlight_eval").put(payload)
                db.update(db.Highlight, oc["hid"], status="evaluating")
            except rb.QueueFull:
                rolling_buffer.release_pin(self.bid, oc["pin"])
                db.update(db.Highlight, oc["hid"], status="skipped", reason="AI queue full (backpressure)")

    # ---------------------------------------------------------------- commands from API / AI worker
    def handle_commands(self):
        for _ in range(20):
            raw = rb.r().lpop(rb.stream_cmd_key(self.sid))
            if not raw:
                return
            try:
                cmd = json.loads(raw)
            except ValueError:
                continue
            kind = cmd.get("type")
            if kind == "stop":
                self.stop_flag.set()
            elif kind == "words" and cmd.get("session") == self.session:
                self.detector.on_words(cmd.get("words") or [])
            elif kind == "manual_clip":
                secs = float(cmd.get("seconds", 60))
                self.raise_candidate(Candidate(max(0.0, self.t - secs / 2), 100, "manual clip", {}))
            elif kind == "reload":
                st = db.get(db.Stream, self.sid)
                if st:
                    self.st, self.cfg = st, dict(st.settings or {})
                    self.detector = HighlightDetector(self.cfg)

    # ---------------------------------------------------------------- heartbeat
    def heartbeat(self):
        cstats = self.chat.stats(int(self.t))
        trm = transcript_store.meta(self.sid)
        state = {
            "stream_id": self.sid, "session": self.session, "status": self.status, "error": self.error,
            "pid": os.getpid(), "title": self.title, "platform": self.st.platform, "t": round(self.t, 1),
            "started_at": self.started_at, "live_since": self.live_since, "reconnects": self.reconnects,
            "audio": {**{k: self.audio.last.get(k) for k in ("db", "peak_db", "z", "jump_pct", "baseline_db",
                                                              "voiced")},
                      "excitement": round(self.audio.excitement(), 1), "levels": list(self.levels)[-60:]},
            "chat": {**cstats, "error": self.chat_state.get("error", "")},
            "transcription": {"provider": trm.get("provider", settings.transcription_provider),
                              "done_until": float(trm.get("done_until") or 0),
                              "latency": float(trm.get("latency") or 0), "ok": trm.get("ok", "1") == "1",
                              "error": trm.get("error", ""), "sent": self.chunks_sent,
                              "dropped": self.chunks_dropped,
                              "lag": round(self.t - float(trm.get("done_until") or 0), 1)},
            "detector": {"last_spike": self.detector.last_spike, "scores": list(self.detector.scores)[-60:],
                         "candidates": self.candidates, "open": len(self.open_candidates)},
            "buffer": self.buffer.stats(),
            "updated": time.time(),
        }
        rb.set_json(rb.k("stream", self.sid, "hb"), state, ttl=15)
        rb.publish("stream_state", stream_id=self.sid, status=self.status)

    # ---------------------------------------------------------------- main
    def run(self):
        if not self.lock_obj.acquire(wait=20):
            print(f"stream {self.sid}: another worker holds it, exiting", flush=True)
            sys.exit(3)
        signal.signal(signal.SIGTERM, lambda *_: self.stop_flag.set())
        signal.signal(signal.SIGINT, lambda *_: self.stop_flag.set())
        self.buffer.reset()
        transcript_store.clear(self.sid)
        self.set_status("connecting")
        self.start_chat()
        ing = threading.Thread(target=self._guard(self.ingest), name="ingest", daemon=True)
        ing.start()
        last_hb = 0.0
        try:
            while not self.stop_flag.is_set():
                self.lock_obj.extend()
                self.buffer.scan()
                self.buffer.expire()
                self.handle_commands()
                self.dispatch_candidates()
                if time.time() - last_hb >= 2:
                    self.heartbeat()
                    last_hb = time.time()
                if not ing.is_alive() and not self.stop_flag.is_set():
                    raise RuntimeError("ingestion thread died")
                self.stop_flag.wait(1.0)
        finally:
            self.shutdown()

    def _guard(self, fn):
        def run():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}"
                traceback.print_exc()
        return run

    def shutdown(self):
        self.stop_flag.set()
        if self.chat_conn:
            self.chat_conn.stop()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                self.proc.kill()
        if self.sem_token:
            rb.ffmpeg_semaphore().release(self.sem_token)
        # candidates still open are sent with what exists, so a stop doesn't lose a highlight
        for oc in self.open_candidates:
            oc["due"] = min(oc["due"], self.t)
        try:
            self.buffer.scan()
            self.dispatch_candidates_final()
        except Exception:  # noqa: BLE001
            pass
        st = db.get(db.Stream, self.sid)
        final = "stopped" if (st is None or st.desired_state == "stopped") else "restarting"
        self.set_status(final)
        rb.r().delete(rb.k("stream", self.sid, "hb"))
        self.lock_obj.release()

    def dispatch_candidates_final(self):
        for oc in self.open_candidates:
            oc["created"] = 0
        self.dispatch_candidates()


def main():
    sid = sys.argv[1]
    settings.ensure_dirs()
    StreamWorker(sid).run()


if __name__ == "__main__":
    main()
