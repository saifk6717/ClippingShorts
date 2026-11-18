"""AI worker container: transcription, highlight judging and YouTube publishing.

Each queue has a fixed number of consumers (MAX_CONCURRENT_TRANSCRIPTIONS, MAX_CONCURRENT_AI_JOBS,
MAX_CONCURRENT_UPLOADS), so there is never more work in flight than configured, however many streams run. Blocking
calls run in threads; Gemini calls are async. Every job has a timeout; failures are retried, then dead-lettered.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
import traceback
from pathlib import Path

import httpx
from sqlalchemy import select, update

from .. import db, gemini_judge, rolling_buffer, transcript_store
from .. import redis_bus as rb
from ..clip_extractor import precise_words
from ..clip_storage import edit_file
from ..config import settings
from ..resource_manager import admit, cgroup_memory
from ..transcription import read_wav, transcribe
from ..youtube_uploader import UploadError, upload

STOP = asyncio.Event()
INFLIGHT = {"transcription": 0, "highlight_eval": 0, "publishing": 0}


def _speech_usage(stream_id, seconds, ms, ok, prov, err=""):
    try:
        with db.session() as s:
            s.add(db.AIUsage(stream_id=stream_id, service="speech", model=prov, purpose="transcription",
                             units_in=seconds, units_out=0, latency_ms=ms,
                             est_cost_usd=seconds / 60 * settings.speech_price_per_minute if prov == "chirp3" else 0,
                             ok=ok, error=err[:300]))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- transcription
def do_transcription(msg: rb.Message) -> None:
    d = msg.data
    if d.get("kind") == "clip":
        precise_words(d["clip_id"], only_short=True)      # a failure is retried; the final one is handled in on_dead
        if d.get("auto_upload"):
            queue_metadata(d["clip_id"], d["auto_upload"])
        return
    path = Path(d["path"])
    if not path.exists():
        return                       # dropped by cleanup: nothing to do
    sid = d["stream_id"]
    # a chunk older than two minutes is no use for live detection any more
    if time.time() - d.get("created", time.time()) > 120:
        path.unlink(missing_ok=True)
        return
    ok, why = admit("transcribe")
    if not ok:
        path.unlink(missing_ok=True)
        transcript_store.mark_done(sid, d["t0"] + d["dur"], 0, settings.transcription_provider, False,
                                   f"skipped: {why}")
        return
    pcm = read_wav(path)
    t0 = time.time()
    try:
        words, prov = transcribe(pcm)
    except Exception as e:
        _speech_usage(sid, d["dur"], int((time.time() - t0) * 1000), False, settings.transcription_provider, str(e))
        transcript_store.mark_done(sid, d["t0"], time.time() - t0, settings.transcription_provider, False, str(e))
        raise
    latency = time.time() - t0
    shifted = [{"w": w["w"], "s": round(w["s"] + d["t0"], 2), "e": round(w["e"] + d["t0"], 2)} for w in words]
    transcript_store.add_words(sid, shifted)
    transcript_store.mark_done(sid, d["t0"] + d["dur"], latency, prov)
    _speech_usage(sid, d["dur"], int(latency * 1000), True, prov)
    rb.r().lpush(rb.k("metrics", "transcription_latency"), round(latency, 3))
    rb.r().ltrim(rb.k("metrics", "transcription_latency"), 0, 199)
    if shifted:
        rb.send_stream_command(sid, {"type": "words", "session": d.get("session"), "words": shifted})
    path.unlink(missing_ok=True)


def queue_metadata(clip_id: str, auto_upload: str) -> None:
    """Next step of the automatic Short: Gemini writes the post (title, description, hashtags, hook)."""
    rb.queue("highlight_eval").put({"kind": "metadata", "clip_id": clip_id, "auto_upload": auto_upload}, force=True)


async def make_post(d: dict, client) -> None:
    """Write the title/description with Gemini, then render the default 40 s Short and (via the renderer) upload it."""
    from ..clip_storage import read_words
    from ..jobs import create_edit, enqueue_render
    clip = db.get(db.Clip, d["clip_id"])
    if clip is None or clip.status != "ready":
        return
    st = db.get(db.Stream, clip.stream_id) if clip.stream_id else None
    words = [w for w in read_words(clip) if clip.suggested_start - 1 <= w["s"] <= clip.suggested_end + 1]
    try:
        live_title = (rb.get_json(rb.k("stream", clip.stream_id, "hb")) or {}).get("title", "") if clip.stream_id else ""
        meta = await gemini_judge.write_metadata(words, live_title or (st.url if st else ""),
                                                 clip.creator_name, clip.platform, clip.reason, clip.stream_id or "",
                                                 client)
    except Exception as e:  # noqa: BLE001 - a missing title must not lose the Short
        rb.log_activity(clip.stream_id or "", f"title writer failed, using a plain title: {str(e)[:120]}", "warn")
        meta = {"title": "", "hook": "", "description": "", "hashtags": []}
    title = meta["title"] or clip.title or f"{clip.creator_name} live moment"
    credit = f"Clip from {clip.creator_name or 'the streamer'}'s live stream" + \
        (f": {clip.source_url}" if clip.source_url else ".")
    description = "\n\n".join(x for x in (meta["description"] or clip.description, credit) if x)
    tags = list(dict.fromkeys((meta["hashtags"] or clip.hashtags or []) + ["shorts"]))
    db.update(db.Clip, clip.id, title=title[:200], hook=meta["hook"] or clip.hook, description=description,
              hashtags=tags)
    clip = db.get(db.Clip, clip.id)
    e = create_edit(clip, {"start": clip.suggested_start, "end": clip.suggested_end, "title": title[:100],
                           "description": description, "hashtags": tags,
                           "hook": {"enabled": True, "text": meta["hook"] or clip.hook, "position": 0.14}}, auto=True)
    enqueue_render(e.id, auto_upload=d["auto_upload"])
    rb.log_activity(clip.stream_id or "", f"Short written: \"{title[:80]}\" – rendering, then uploading ({d['auto_upload']})")


# ---------------------------------------------------------------- highlight evaluation
async def evaluate(d: dict, client: httpx.AsyncClient) -> str:
    if d.get("kind") == "metadata":
        await make_post(d, client)
        return "post"
    hid = d["highlight_id"]
    h = db.get(db.Highlight, hid)
    if h is None or h.status not in ("candidate", "evaluating"):
        return "gone"
    bid = f"{h.stream_id}.{h.session_id}"
    if d.get("periodic") and not admit("periodic_eval")[0]:
        rolling_buffer.release_pin(bid, h.pin_id)
        db.update(db.Highlight, hid, status="skipped", reason="periodic check deferred: resource pressure")
        return "deferred"
    t0 = time.time()
    event, due = float(d["event_t"]), float(d["due"])
    w_start = max(0.0, event - 75)
    words = transcript_store.words_between(h.stream_id, w_start, due + 5)
    local = float(d.get("local_score", 0))
    if settings.ai_mode == "local":
        accept = local >= 75 and not d.get("periodic")
        t = {"found": accept, "score": int(local), "start": h.start_t, "end": h.end_t, "category": "hype",
             "title": f"{d.get('creator', '')} – {', '.join(d.get('triggers') or [])}"[:90], "hook": "",
             "description": "", "hashtags": [], "reason": "local signals only (AI_MODE=local)"}
        r = {"found": accept, "score": int(local), "start": h.start_t, "end": h.end_t, "reason": "local"}
    else:
        trig = ", ".join(d.get("triggers") or []) or h.trigger
        tj = gemini_judge.transcript_judge(words, w_start, due + 5, event, trig, d.get("title", ""),
                                           d.get("creator", ""), h.stream_id, client=client)
        z = d.get("z") or []
        rj = gemini_judge.reaction_judge(z, d.get("people"), float(d.get("window_start", w_start)), event,
                                         {"triggers": d.get("triggers"), "chat": {k: (d.get("chat") or {}).get(k)
                                          for k in ("unique_users", "baseline_unique_per_sec", "reaction_ratio",
                                                    "top_user_share", "repeat_ratio")}}, h.stream_id, client=client)
        results = await asyncio.gather(asyncio.wait_for(tj, settings.gemini_timeout_seconds + 10),
                                       asyncio.wait_for(rj, settings.gemini_timeout_seconds + 10),
                                       return_exceptions=True)
        errs = [x for x in results if isinstance(x, BaseException)]
        no_words = isinstance(results[0], dict) and results[0].get("reason") == "not enough speech"
        if len(errs) == 2 or (no_words and isinstance(results[1], BaseException)):
            # nothing was actually judged (a Gemini timeout/error): retry the job instead of rejecting the moment
            raise RuntimeError(f"no judge answered: {errs[0]!r}")
        t = results[0] if not isinstance(results[0], BaseException) else {"found": False, "score": 0,
                                                                            "reason": f"error: {results[0]!r}"[:200]}
        r = results[1] if not isinstance(results[1], BaseException) else {"found": False, "score": 0,
                                                                            "reason": f"error: {results[1]!r}"[:200]}
    accept, conf, why = gemini_judge.combine(t, r, local)
    if d.get("triggers") == ["manual clip"]:
        accept, why = True, "requested manually"
    latency = time.time() - t0
    rb.r().lpush(rb.k("metrics", "ai_latency"), round(latency, 2))
    rb.r().ltrim(rb.k("metrics", "ai_latency"), 0, 199)
    fields = dict(transcript_score=int(t.get("score", 0)), reaction_score=int(r.get("score", 0)), confidence=conf,
                  reason=f"{why}. words: {t.get('reason', '')} | reaction: {r.get('reason', '')}"[:900])
    if not accept:
        rolling_buffer.release_pin(bid, h.pin_id)
        db.update(db.Highlight, hid, status="rejected", **fields)
        return "rejected"
    if settings.fixed_short_range or not t.get("found"):
        start, end = max(0.0, event - settings.highlight_pre_seconds), event + settings.highlight_post_seconds
    else:
        start, end = float(t["start"]), float(t["end"])
    # duplicates: an accepted highlight of this stream that overlaps or is within the merge gap
    with db.session() as s:
        recent = list(s.scalars(select(db.Highlight).where(
            db.Highlight.stream_id == h.stream_id, db.Highlight.id != hid,
            db.Highlight.status.in_(("accepted", "extracted")),
            db.Highlight.detected_at > time.time() - 3 * 3600)))
        per_hour = sum(1 for x in recent if x.detected_at > time.time() - 3600)
    for x in recent:
        if x.session_id == h.session_id and start < x.end_t + settings.merge_gap_seconds and \
                end > x.start_t - settings.merge_gap_seconds:
            rolling_buffer.release_pin(bid, h.pin_id)
            db.update(db.Highlight, hid, status="duplicate", **fields)
            return "duplicate"
    if per_hour >= settings.max_highlights_per_hour and why != "requested manually":
        rolling_buffer.release_pin(bid, h.pin_id)
        db.update(db.Highlight, hid, status="skipped", **dict(fields, reason="hourly highlight cap reached"))
        return "capped"
    db.update(db.Highlight, hid, status="accepted", start_t=round(start, 2), end_t=round(end, 2),
              category=t.get("category", "other"), title=t.get("title") or h.trigger, hook=t.get("hook", ""),
              description=t.get("description", ""), hashtags=t.get("hashtags", []), **fields)
    try:
        rb.queue("clip_extraction").put({"highlight_id": hid})
    except rb.QueueFull:
        rolling_buffer.release_pin(bid, h.pin_id)
        db.update(db.Highlight, hid, status="failed", reason="extraction queue full (backpressure)")
        return "failed"
    rb.publish("highlight", stream_id=h.stream_id, highlight_id=hid, confidence=conf, title=t.get("title", ""))
    rb.log_activity(h.stream_id, f"Highlight accepted ({conf}%): {t.get('title') or h.trigger}")
    return "accepted"


# ---------------------------------------------------------------- publishing
def do_publish(msg: rb.Message) -> None:
    uid = msg.data["upload_id"]
    with db.session() as s:
        res = s.execute(update(db.Upload).where(db.Upload.id == uid, db.Upload.status.in_(("queued", "failed")))
                        .values(status="uploading", attempts=db.Upload.attempts + 1, error=""))
        claimed = res.rowcount == 1
    if not claimed:
        return                      # already uploading or done somewhere: never twice
    up = db.get(db.Upload, uid)
    edit = db.get(db.Edit, up.edit_id)
    if edit is None or edit.status != "done":
        db.update(db.Upload, uid, status="failed", error="the rendered file is not ready")
        return
    path = edit_file(edit)
    if not path.is_file():
        db.update(db.Upload, uid, status="failed", error="rendered file missing")
        return
    t0 = time.time()
    last = {"p": 0.0}

    def prog(p):
        if p - last["p"] >= 5 or p >= 100:
            last["p"] = p
            db.update(db.Upload, uid, progress=round(p, 1))
            rb.publish("upload", upload_id=uid, status="uploading", progress=round(p, 1))
    try:
        vid = upload(uid, path, prog)
    except UploadError as e:
        db.update(db.Upload, uid, status="failed", error=str(e)[:500])
        rb.publish("upload", upload_id=uid, status="failed", error=str(e)[:200])
        if not e.retryable:
            return
        raise
    db.update(db.Upload, uid, status="done", progress=100, youtube_video_id=vid, finished_at=time.time())
    rb.r().lpush(rb.k("metrics", "upload_latency"), round(time.time() - t0, 2))
    rb.r().ltrim(rb.k("metrics", "upload_latency"), 0, 199)
    rb.publish("upload", upload_id=uid, status="done", video_id=vid)
    rb.log_activity("", f"Uploaded to YouTube: {up.title} ({vid})")


# ---------------------------------------------------------------- consumers
async def consume(name: str, n: int, handler, is_async: bool = False, client=None):
    q = rb.queue(name)
    consumer = f"{rb.consumer_name()}-{name}-{n}"
    while not STOP.is_set():
        try:
            msg = await asyncio.to_thread(q.get, 3000, consumer)
        except Exception:  # noqa: BLE001
            await asyncio.sleep(3)
            continue
        if not msg:
            continue
        INFLIGHT[name] += 1
        try:
            if is_async:
                await handler(msg.data, client)
            else:
                await asyncio.wait_for(asyncio.to_thread(handler, msg), timeout=3600)
            q.ack(msg)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            await asyncio.sleep(min(30, 3 * (msg.attempts + 1)))      # a short pause before the retry
            dead = q.fail(msg, f"{type(e).__name__}: {e}")
            if dead:
                on_dead(name, msg, str(e))
        finally:
            INFLIGHT[name] -= 1


def on_dead(name: str, msg: rb.Message, err: str):
    d = msg.data
    if name == "transcription" and d.get("path"):
        Path(d["path"]).unlink(missing_ok=True)
    if name == "transcription" and d.get("kind") == "clip" and d.get("auto_upload"):
        queue_metadata(d["clip_id"], d["auto_upload"])       # captions failed: still publish, without them
    if name == "highlight_eval":
        h = db.get(db.Highlight, d.get("highlight_id"))
        if h:
            rolling_buffer.release_pin(f"{h.stream_id}.{h.session_id}", h.pin_id)
            db.update(db.Highlight, h.id, status="failed", reason=f"AI evaluation failed: {err[:300]}")


async def heartbeat():
    while not STOP.is_set():
        rb.set_json(rb.k("worker", "ai", "hb"), {"t": time.time(), "inflight": INFLIGHT, "pid": os.getpid(),
                                                 "container": cgroup_memory(), "mode": settings.ai_mode,
                                                 "model": settings.gemini_model,
                                                 "transcription": settings.transcription_provider}, ttl=20)
        await asyncio.sleep(5)


async def main_async():
    settings.ensure_dirs()
    db.init_db()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, STOP.set)
    limits = httpx.Limits(max_connections=settings.max_concurrent_ai_jobs * 2 + 2)
    async with httpx.AsyncClient(limits=limits) as client:
        tasks = [asyncio.create_task(heartbeat())]
        for i in range(settings.max_concurrent_transcriptions):
            tasks.append(asyncio.create_task(consume("transcription", i, do_transcription)))
        for i in range(settings.max_concurrent_ai_jobs):
            tasks.append(asyncio.create_task(consume("highlight_eval", i, evaluate, True, client)))
        for i in range(settings.max_concurrent_uploads):
            tasks.append(asyncio.create_task(consume("publishing", i, do_publish)))
        print(f"ai worker up: model={settings.gemini_model}@{settings.gemini_location} mode={settings.ai_mode} "
              f"speech={settings.transcription_provider}", flush=True)
        await STOP.wait()
        await asyncio.sleep(1)
        for t in tasks:
            t.cancel()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
