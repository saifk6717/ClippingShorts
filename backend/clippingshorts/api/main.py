"""ClippingShorts REST API (FastAPI). Run: uvicorn clippingshorts.api.main:app

Every route is fast: long work (capture, transcription, AI, extraction, rendering, uploads) happens in workers;
the API only reads and writes metadata and queues jobs.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import case, func, or_, select

from .. import clip_storage, db, google_oauth, monitoring, stream_manager
from .. import redis_bus as rb
from ..clip_renderer import ASPECTS
from ..config import settings
from ..jobs import JobError, create_edit, create_upload, default_params, enqueue_render
from ..resource_manager import current as resources_now
from . import security
from .ranges import serve

_stop = threading.Event()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not settings.secret_key or len(settings.secret_key) < 32:
        raise RuntimeError("Set CLIPPINGSHORTS_SECRET_KEY to a random value of at least 32 characters.")
    if not settings.admin_password or len(settings.admin_password) < 10:
        raise RuntimeError("Set CLIPPINGSHORTS_ADMIN_PASSWORD (at least 10 characters).")
    settings.ensure_dirs()
    db.init_db()
    threading.Thread(target=monitoring.sampler, args=(_stop,), daemon=True, name="sampler").start()
    yield
    _stop.set()


app = FastAPI(title="ClippingShorts API", version="1.0.0", lifespan=lifespan, docs_url="/api/docs",
              openapi_url="/api/openapi.json")


@app.middleware("http")
async def headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


# ---------------------------------------------------------------- health & auth
@app.get("/healthz")
def healthz():
    return {"ok": True}


class LoginIn(BaseModel):
    password: str = Field(min_length=1, max_length=200)


@app.post("/api/auth/login")
def auth_login(body: LoginIn, request: Request, response: Response):
    security.check_login_rate(request)
    return {"role": security.login(body.password, response)}


@app.post("/api/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie(security.COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(request: Request):
    role = security.role_of(request)
    if role is None:
        raise HTTPException(401, "Not signed in.")
    return {"role": role}


# ---------------------------------------------------------------- streams
class StreamIn(BaseModel):
    url: str = Field(min_length=3, max_length=500)
    platform: Literal["twitch", "youtube", "synthetic"]
    creator_name: str = Field(default="", max_length=120)
    enabled: bool = True
    settings: dict = Field(default_factory=dict)


class StreamsIn(BaseModel):
    streams: list[StreamIn] = Field(min_length=1, max_length=10)
    start: bool = False


class StreamPatch(BaseModel):
    creator_name: str | None = Field(default=None, max_length=120)
    enabled: bool | None = None
    settings: dict | None = None


def _stream_or_404(sid: str) -> db.Stream:
    st = db.get(db.Stream, sid)
    if st is None:
        raise HTTPException(404, "Stream not found.")
    return st


@app.post("/api/streams", status_code=201)
def create_streams(body: StreamsIn, _r=Depends(security.admin)):
    try:
        created, errors = stream_manager.add_streams([s.model_dump() for s in body.streams])
    except stream_manager.StreamError as e:
        raise HTTPException(422, str(e)) from e
    if not created and errors:
        raise HTTPException(422, {"message": "No stream was added.", "errors": errors})
    if body.start:
        for c in created:
            if c["enabled"]:
                stream_manager.request(c["id"], "start")
    return {"created": created, "errors": errors}


@app.get("/api/streams")
def list_streams(_r=Depends(security.viewer)):
    return {"streams": [stream_manager.view(s) for s in db.all_of(db.Stream, order=db.Stream.created_at)]}


@app.get("/api/streams/{sid}")
def get_stream(sid: str, _r=Depends(security.viewer)):
    st = _stream_or_404(sid)
    d = stream_manager.view(st)
    with db.session() as s:
        d["highlight_count"] = s.scalar(select(func.count()).select_from(db.Highlight).where(
            db.Highlight.stream_id == sid, db.Highlight.status.in_(("accepted", "extracted")))) or 0
        d["recent_highlights"] = [db.to_dict(h) for h in s.scalars(
            select(db.Highlight).where(db.Highlight.stream_id == sid).order_by(db.Highlight.detected_at.desc())
            .limit(15))]
    return d


@app.patch("/api/streams/{sid}")
def patch_stream(sid: str, body: StreamPatch, _r=Depends(security.admin)):
    _stream_or_404(sid)
    fields = {}
    if body.creator_name is not None:
        fields["creator_name"] = body.creator_name.strip()
    if body.enabled is not None:
        fields["enabled"] = body.enabled
        if not body.enabled:
            stream_manager.request(sid, "stop")
    if body.settings is not None:
        try:
            fields["settings"] = stream_manager.clean_settings(body.settings)
        except stream_manager.StreamError as e:
            raise HTTPException(422, str(e)) from e
    db.update(db.Stream, sid, **fields)
    rb.send_stream_command(sid, {"type": "reload"})
    return stream_manager.view(db.get(db.Stream, sid))


@app.delete("/api/streams/{sid}")
def delete_stream(sid: str, _r=Depends(security.admin)):
    st = _stream_or_404(sid)
    if st.desired_state == "running":
        stream_manager.request(sid, "stop")
    with db.session() as s:
        obj = s.get(db.Stream, sid)
        s.query(db.Highlight).filter(db.Highlight.stream_id == sid).delete()
        for c in s.scalars(select(db.Clip).where(db.Clip.stream_id == sid)):
            c.stream_id = None                       # saved clips stay in the library
        s.delete(obj)
    return {"ok": True}


@app.post("/api/streams/start-all")
def start_all(_r=Depends(security.admin)):
    return {"streams": stream_manager.request_all("start")}


@app.post("/api/streams/stop-all")
def stop_all(_r=Depends(security.admin)):
    return {"streams": stream_manager.request_all("stop")}


@app.post("/api/streams/{sid}/start")
def start_stream(sid: str, _r=Depends(security.admin)):
    _stream_or_404(sid)
    try:
        return stream_manager.request(sid, "start")
    except stream_manager.StreamError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/streams/{sid}/stop")
def stop_stream(sid: str, _r=Depends(security.admin)):
    _stream_or_404(sid)
    return stream_manager.request(sid, "stop")


@app.post("/api/streams/{sid}/restart")
def restart_stream(sid: str, _r=Depends(security.admin)):
    st = _stream_or_404(sid)
    db.update(db.Stream, sid, desired_state="running", status="starting", last_error="")
    rb.queue("stream_control").put({"action": "restart", "stream_id": sid}, force=True)
    return stream_manager.view(st)


@app.post("/api/streams/{sid}/clip")
def manual_clip(sid: str, seconds: int = Query(60, ge=15, le=240), _r=Depends(security.admin)):
    _stream_or_404(sid)
    if not stream_manager.live_state(sid):
        raise HTTPException(409, "This stream is not being monitored right now.")
    rb.send_stream_command(sid, {"type": "manual_clip", "seconds": seconds})
    return {"ok": True}


@app.get("/api/streams/{sid}/health")
def stream_health(sid: str, _r=Depends(security.viewer)):
    st = _stream_or_404(sid)
    live = stream_manager.live_state(sid)
    fresh = bool(live) and time.time() - live.get("updated", 0) < 15
    return {"stream_id": sid, "desired_state": st.desired_state, "status": live.get("status", st.status),
            "worker_alive": fresh, "error": live.get("error") or st.last_error, "live": live}


# ---------------------------------------------------------------- clips
def clip_view(c: db.Clip, detail: bool = False) -> dict:
    d = db.to_dict(c)
    d.pop("rel_dir", None)
    d["thumbnail_url"] = f"/api/clips/{c.id}/thumbnail" if c.status == "ready" else None
    d["preview_url"] = f"/api/clips/{c.id}/preview" if c.status == "ready" else None
    with db.session() as s:
        edits = list(s.scalars(select(db.Edit).where(db.Edit.clip_id == c.id).order_by(db.Edit.version.desc())))
        ups = list(s.scalars(select(db.Upload).where(db.Upload.clip_id == c.id).order_by(db.Upload.created_at.desc())))
    d["render_status"] = edits[0].status if edits else "none"
    d["upload_status"] = next((u.status for u in ups if u.status == "done"), ups[0].status if ups else "none")
    d["edits_count"] = len(edits)
    if detail:
        d["edits"] = [edit_view(e) for e in edits]
        d["uploads"] = [db.to_dict(u) for u in ups]
        d["default_params"] = default_params(c)
        sprite = clip_storage.source_dir(c) / "sprite.json"
        d["sprite"] = json.loads(sprite.read_text()) if sprite.is_file() else None
        d["sprite_url"] = f"/api/clips/{c.id}/sprite" if d["sprite"] else None
        d["highlight"] = db.to_dict(h) if (h := db.get(db.Highlight, c.highlight_id)) else None
    return d


def edit_view(e: db.Edit) -> dict:
    d = db.to_dict(e)
    d["file_url"] = f"/api/edits/{e.id}/file" if e.status == "done" else None
    d["thumbnail_url"] = f"/api/edits/{e.id}/thumbnail" if e.status == "done" else None
    return d


def _clip_or_404(cid: str) -> db.Clip:
    c = db.get(db.Clip, cid)
    if c is None or c.status == "expired":
        raise HTTPException(404, "Clip not found.")
    return c


@app.get("/api/clips")
def list_clips(page: int = Query(1, ge=1), page_size: int = Query(24, ge=1, le=100),
               platform: str | None = None, stream_id: str | None = None, creator: str | None = None,
               category: str | None = None, review: str | None = None,
               state: Literal["pending", "rendered", "uploaded", "all"] = "all",
               date_from: float | None = None, date_to: float | None = None, q: str | None = None,
               _r=Depends(security.viewer)):
    with db.session() as s:
        query = select(db.Clip).where(db.Clip.status.in_(("ready", "extracting", "failed")))
        if platform:
            query = query.where(db.Clip.platform == platform)
        if stream_id:
            query = query.where(db.Clip.stream_id == stream_id)
        if creator:
            query = query.where(db.Clip.creator_name.ilike(f"%{creator}%"))
        if category:
            query = query.where(db.Clip.category == category)
        if review:
            query = query.where(db.Clip.review_status == review)
        if q:
            query = query.where(or_(db.Clip.title.ilike(f"%{q}%"), db.Clip.creator_name.ilike(f"%{q}%")))
        if date_from:
            query = query.where(db.Clip.created_at >= date_from)
        if date_to:
            query = query.where(db.Clip.created_at <= date_to)
        if state == "pending":
            query = query.where(db.Clip.review_status == "pending", db.Clip.status == "ready")
        elif state == "rendered":
            query = query.where(db.Clip.id.in_(select(db.Edit.clip_id).where(db.Edit.status == "done")))
        elif state == "uploaded":
            query = query.where(db.Clip.id.in_(select(db.Upload.clip_id).where(db.Upload.status == "done")))
        total = s.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = list(s.scalars(query.order_by(db.Clip.created_at.desc()).offset((page - 1) * page_size)
                              .limit(page_size)))
    return {"items": [clip_view(c) for c in rows], "page": page, "page_size": page_size, "total": total,
            "pages": (total + page_size - 1) // page_size}


@app.get("/api/clips/{cid}")
def get_clip(cid: str, _r=Depends(security.viewer)):
    return clip_view(_clip_or_404(cid), detail=True)


@app.get("/api/clips/{cid}/preview")
def clip_preview(cid: str, request: Request, _r=Depends(security.viewer)):
    c = _clip_or_404(cid)
    return serve(clip_storage.source_file(c), request)


@app.get("/api/clips/{cid}/thumbnail")
def clip_thumb(cid: str, _r=Depends(security.viewer)):
    c = _clip_or_404(cid)
    f = clip_storage.source_dir(c) / "thumb.jpg"
    if not f.is_file():
        raise HTTPException(404, "No thumbnail.")
    return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/clips/{cid}/sprite")
def clip_sprite(cid: str, _r=Depends(security.viewer)):
    c = _clip_or_404(cid)
    f = clip_storage.source_dir(c) / "sprite.jpg"
    if not f.is_file():
        raise HTTPException(404, "No timeline images.")
    return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/clips/{cid}/waveform")
def clip_waveform(cid: str, _r=Depends(security.viewer)):
    c = _clip_or_404(cid)
    f = clip_storage.source_dir(c) / "waveform.json"
    return json.loads(f.read_text()) if f.is_file() else {"per_second": 10, "peaks": []}


@app.get("/api/clips/{cid}/words")
def clip_words(cid: str, _r=Depends(security.viewer)):
    c = _clip_or_404(cid)
    return {"words": clip_storage.read_words(c), "status": c.words_status}


class ClipPatch(BaseModel):
    review_status: Literal["pending", "approved", "rejected"] | None = None
    title: str | None = Field(default=None, max_length=200)
    hook: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    hashtags: list[str] | None = None
    keep: bool | None = None


@app.patch("/api/clips/{cid}")
def patch_clip(cid: str, body: ClipPatch, _r=Depends(security.admin)):
    _clip_or_404(cid)
    db.update(db.Clip, cid, **{k: v for k, v in body.model_dump().items() if v is not None})
    return clip_view(db.get(db.Clip, cid), detail=True)


def _delete_clip(cid: str) -> None:
    c = _clip_or_404(cid)
    with db.session() as s:
        if s.scalar(select(func.count()).select_from(db.Upload).where(db.Upload.clip_id == cid,
                                                                      db.Upload.status == "uploading")):
            raise HTTPException(409, "This clip is uploading right now.")
    clip_storage.delete_clip_files(c)
    with db.session() as s:
        obj = s.get(db.Clip, cid)
        s.query(db.Edit).filter(db.Edit.clip_id == cid).delete()
        s.delete(obj)


@app.delete("/api/clips/{cid}")
def delete_clip(cid: str, _r=Depends(security.admin)):
    _delete_clip(cid)
    return {"ok": True}


class EditParams(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    aspect: Literal["9:16", "16:9", "1:1"] = "9:16"
    fit: Literal["crop", "blur"] = "crop"
    crop_x: float | None = Field(default=None, ge=0, le=1)
    crop_y: float | None = Field(default=0.5, ge=0, le=1)
    zoom: float = Field(default=1.0, ge=1, le=3)
    smart_crop: bool = True
    quality: Literal["high", "fast"] = "high"
    captions: dict = Field(default_factory=dict)
    hook: dict = Field(default_factory=dict)
    audio: dict = Field(default_factory=dict)
    title: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=4900)
    hashtags: list[str] = Field(default_factory=list, max_length=15)

    @field_validator("end")
    @classmethod
    def _range(cls, v, info):
        start = info.data.get("start", 0)
        if v - start < 1:
            raise ValueError("end must be at least 1 s after start")
        if v - start > 300:
            raise ValueError("an edit can be at most 5 minutes long")
        return v

    @field_validator("captions")
    @classmethod
    def _caps(cls, v):
        words = v.get("words")
        if words is not None:
            if not isinstance(words, list) or len(words) > 5000:
                raise ValueError("captions.words must be a list of at most 5000 words")
            for w in words:
                if not isinstance(w, dict) or not {"w", "s", "e"} <= set(w):
                    raise ValueError("each caption word needs w, s and e")
        if "style" in v and v["style"] not in ("bounce", "hype", "clean"):
            raise ValueError("unknown caption style")
        return v


@app.post("/api/clips/{cid}/edit", status_code=201)
def save_edit(cid: str, body: EditParams, _r=Depends(security.admin)):
    c = _clip_or_404(cid)
    if body.end > c.duration + 0.5:
        raise HTTPException(422, f"end is past the clip's length ({c.duration:.1f}s)")
    if body.aspect not in ASPECTS:
        raise HTTPException(422, "unknown aspect")
    e = create_edit(c, body.model_dump())
    db.update(db.Clip, cid, title=body.title or c.title)
    return edit_view(e)


class RenderIn(BaseModel):
    edit_id: str | None = None
    params: EditParams | None = None


@app.post("/api/clips/{cid}/render", status_code=202)
def render_clip(cid: str, body: RenderIn, _r=Depends(security.admin)):
    c = _clip_or_404(cid)
    if body.edit_id:
        e = db.get(db.Edit, body.edit_id)
        if e is None or e.clip_id != cid:
            raise HTTPException(404, "Edit not found.")
        if e.status in ("queued", "rendering"):
            raise HTTPException(409, "This version is already rendering.")
    else:
        e = create_edit(c, body.params.model_dump() if body.params else {})
    try:
        enqueue_render(e.id)
    except JobError as err:
        raise HTTPException(503, str(err)) from err
    return edit_view(db.get(db.Edit, e.id))


class BatchIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)
    action: Literal["approve", "reject", "delete", "keep", "render"]


@app.post("/api/clips/batch")
def batch(body: BatchIn, _r=Depends(security.admin)):
    done, errors = [], []
    for cid in body.ids:
        try:
            if body.action == "delete":
                _delete_clip(cid)
            elif body.action in ("approve", "reject"):
                _clip_or_404(cid)
                db.update(db.Clip, cid, review_status="approved" if body.action == "approve" else "rejected")
            elif body.action == "keep":
                _clip_or_404(cid)
                db.update(db.Clip, cid, keep=True)
            elif body.action == "render":
                c = _clip_or_404(cid)
                e = create_edit(c, {})
                enqueue_render(e.id)
            done.append(cid)
        except (HTTPException, JobError) as e:
            errors.append({"id": cid, "error": getattr(e, "detail", str(e))})
    return {"done": done, "errors": errors}


@app.get("/api/edits/{eid}")
def get_edit(eid: str, _r=Depends(security.viewer)):
    e = db.get(db.Edit, eid)
    if e is None:
        raise HTTPException(404, "Edit not found.")
    return edit_view(e)


@app.get("/api/edits/{eid}/file")
def edit_file(eid: str, request: Request, download: bool = False, _r=Depends(security.viewer)):
    e = db.get(db.Edit, eid)
    if e is None or e.status != "done":
        raise HTTPException(404, "Not rendered.")
    resp = serve(clip_storage.edit_file(e), request)
    if download:
        resp.headers["Content-Disposition"] = f'attachment; filename="clippingshorts-{e.clip_id}-v{e.version}.mp4"'
    return resp


@app.get("/api/edits/{eid}/thumbnail")
def edit_thumb(eid: str, _r=Depends(security.viewer)):
    e = db.get(db.Edit, eid)
    if e is None or e.status != "done":
        raise HTTPException(404, "Not rendered.")
    f = clip_storage.edit_file(e).with_suffix(".jpg")
    if not f.is_file():
        raise HTTPException(404, "No thumbnail.")
    return FileResponse(f, media_type="image/jpeg")


@app.delete("/api/edits/{eid}")
def delete_edit(eid: str, _r=Depends(security.admin)):
    e = db.get(db.Edit, eid)
    if e is None:
        raise HTTPException(404, "Edit not found.")
    if e.status in ("rendering",):
        raise HTTPException(409, "Rendering right now.")
    if e.output_rel:
        clip_storage.edit_file(e).unlink(missing_ok=True)
        clip_storage.edit_file(e).with_suffix(".jpg").unlink(missing_ok=True)
    with db.session() as s:
        s.delete(s.get(db.Edit, eid))
    return {"ok": True}


# ---------------------------------------------------------------- YouTube
class UploadIn(BaseModel):
    edit_id: str
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=4900)
    hashtags: list[str] = Field(default_factory=list, max_length=15)
    privacy: Literal["private", "unlisted", "public"] = "private"
    publish_at: str | float | None = None


_channel_cache = {"t": 0.0, "v": None}


@app.get("/api/youtube/channel")
def yt_channel(_r=Depends(security.viewer)):
    if not google_oauth.configured():
        return {"configured": False}
    if _channel_cache["v"] is None or time.time() - _channel_cache["t"] > 600:
        from ..youtube_uploader import channel
        try:
            _channel_cache.update(v=channel(), t=time.time())
        except Exception as e:  # noqa: BLE001
            return {"configured": True, "error": str(e)[:200]}
    return {"configured": True, **_channel_cache["v"]}


@app.post("/api/youtube/upload/{cid}", status_code=202)
def yt_upload(cid: str, body: UploadIn, _r=Depends(security.admin)):
    _clip_or_404(cid)
    if not google_oauth.configured():
        raise HTTPException(503, "YouTube publishing is not configured on this server.")
    e = db.get(db.Edit, body.edit_id)
    if e is None or e.clip_id != cid:
        raise HTTPException(404, "Edit not found.")
    if e.status != "done":
        raise HTTPException(409, "Render this version first.")
    publish_at = 0.0
    if body.publish_at:
        try:
            publish_at = float(body.publish_at) if isinstance(body.publish_at, (int, float)) else \
                datetime.fromisoformat(str(body.publish_at).replace("Z", "+00:00")).timestamp()
        except ValueError as err:
            raise HTTPException(422, "publish_at must be an ISO date-time") from err
        if publish_at < time.time() + 120:
            raise HTTPException(422, "A scheduled time must be at least a few minutes in the future.")
    tags = [t.strip().lstrip("#")[:30] for t in body.hashtags if t.strip()]
    try:
        up = create_upload(e, body.title, body.description, tags, body.privacy, publish_at,
                           settings.youtube_channel_id or None)
    except JobError as err:
        raise HTTPException(409, str(err)) from err
    return db.to_dict(up)


@app.get("/api/uploads")
def list_uploads(_r=Depends(security.viewer)):
    rows = db.all_of(db.Upload, order=db.Upload.created_at.desc(), limit=100)
    return {"items": [db.to_dict(u) for u in rows]}


@app.post("/api/uploads/{uid}/retry", status_code=202)
def retry_upload(uid: str, _r=Depends(security.admin)):
    up = db.get(db.Upload, uid)
    if up is None:
        raise HTTPException(404, "Upload not found.")
    if up.status != "failed":
        raise HTTPException(409, f"Only failed uploads can be retried (this one is {up.status}).")
    db.update(db.Upload, uid, status="queued", error="", progress=0)
    rb.queue("publishing").put({"upload_id": uid}, force=True)
    return db.to_dict(db.get(db.Upload, uid))


# ---------------------------------------------------------------- jobs & system
@app.get("/api/jobs")
def jobs(_r=Depends(security.viewer)):
    with db.session() as s:
        edits = list(s.scalars(select(db.Edit).order_by(db.Edit.updated_at.desc()).limit(50)))
        ups = list(s.scalars(select(db.Upload).order_by(db.Upload.created_at.desc()).limit(50)))
        hl = list(s.scalars(select(db.Highlight).order_by(db.Highlight.detected_at.desc()).limit(50)))
    dead = {n: rb.queue(n).dead_letters(20) for n in monitoring.QUEUE_NAMES}
    return {"renders": [edit_view(e) for e in edits], "uploads": [db.to_dict(u) for u in ups],
            "highlights": [db.to_dict(h) for h in hl], "queues": monitoring.queues(), "dead_letters": dead}


@app.post("/api/jobs/dead/{queue}/{mid}/retry")
def retry_dead(queue: str, mid: str, _r=Depends(security.admin)):
    if queue not in monitoring.QUEUE_NAMES:
        raise HTTPException(404, "Unknown queue.")
    if not rb.queue(queue).retry_dead(mid):
        raise HTTPException(404, "Message not found.")
    return {"ok": True}


@app.get("/api/system/health")
def system_health(_r=Depends(security.viewer)):
    return monitoring.health()


@app.get("/api/system/resources")
def system_resources(_r=Depends(security.viewer)):
    out = dict(resources_now())
    out["workers"] = monitoring.workers()
    out["streams"] = {s.id: rb.get_json(rb.k("stream", s.id, "usage")) for s in db.all_of(db.Stream)}
    out["limits"] = monitoring.dashboard()["limits"]
    return out


@app.get("/api/system/storage")
def system_storage(refresh: bool = False, _r=Depends(security.viewer)):
    rep = rb.get_json(rb.k("storage"))
    if rep is None or refresh:
        rep = clip_storage.usage_report()
        rb.set_json(rb.k("storage"), rep, ttl=1200)
    return rep


@app.get("/api/system/usage")
def system_usage(days: int = Query(1, ge=1, le=90), _r=Depends(security.viewer)):
    from ..transcription import speech_seconds_today
    from ..youtube_connector import quota_used_today
    since = time.time() - days * 86400
    with db.session() as s:
        rows = s.execute(select(db.AIUsage.service, db.AIUsage.model, db.AIUsage.purpose, func.count(),
                                func.sum(db.AIUsage.units_in), func.sum(db.AIUsage.units_out),
                                func.avg(db.AIUsage.latency_ms), func.sum(db.AIUsage.est_cost_usd),
                                func.sum(case((db.AIUsage.ok.is_(False), 1), else_=0)))
                         .where(db.AIUsage.ts > since)
                         .group_by(db.AIUsage.service, db.AIUsage.model, db.AIUsage.purpose)).all()
    return {"days": days, "rows": [{"service": r[0], "model": r[1], "purpose": r[2], "calls": r[3],
                                    "units_in": round(r[4] or 0, 1), "units_out": round(r[5] or 0, 1),
                                    "avg_latency_ms": round(r[6] or 0), "est_cost_usd": round(r[7] or 0, 4),
                                    "failures": int(r[8] or 0)} for r in rows],
            "today": {"speech_minutes": round(speech_seconds_today() / 60, 1),
                      "speech_budget_minutes": settings.speech_daily_minutes_budget,
                      "youtube_chat_units": quota_used_today(),
                      "youtube_chat_budget": settings.youtube_chat_daily_quota_budget,
                      "gemini_request_budget": settings.gemini_daily_request_budget},
            "pricing_note": "Costs are estimates from the prices set in GEMINI_PRICE_* and SPEECH_PRICE_PER_MINUTE."}


@app.get("/api/system/config")
def system_config(_r=Depends(security.viewer)):
    return {"gemini_model": settings.gemini_model, "gemini_location": settings.gemini_location,
            "ai_mode": settings.ai_mode, "transcription": settings.transcription_provider,
            "speech_location": settings.speech_location, "synthetic": settings.enable_synthetic,
            "rolling_buffer_seconds": settings.rolling_buffer_seconds,
            "segment_seconds": settings.segment_seconds, "source_pre_seconds": settings.source_pre_seconds,
            "source_post_seconds": settings.source_post_seconds,
            "highlight_pre_seconds": settings.highlight_pre_seconds,
            "highlight_post_seconds": settings.highlight_post_seconds,
            "retention_days": settings.source_clip_retention_days, "auto_upload": settings.auto_upload,
            "youtube_configured": google_oauth.configured()}


@app.get("/api/dashboard")
def dashboard(_r=Depends(security.viewer)):
    d = monitoring.dashboard()
    d["activity"] = [json.loads(x) for x in rb.r().lrange(rb.k("activity"), 0, 39)]
    return d


# ---------------------------------------------------------------- live updates (Server-Sent Events)
@app.get("/api/events")
async def events(request: Request, _r=Depends(security.viewer)):
    """Pushes worker events as they happen, plus a compact snapshot of every stream every 3 seconds."""
    client = aioredis.from_url(settings.redis_url, decode_responses=True)
    pubsub = client.pubsub()
    await pubsub.subscribe(rb.EVENTS)

    async def gen():
        last_snap = 0.0
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg.get("type") == "message":
                    data = json.loads(msg["data"])
                    if data.get("type") != "stream_state":           # those arrive in the snapshot
                        yield f"event: {data.get('type', 'message')}\ndata: {msg['data']}\n\n"
                if time.time() - last_snap >= 3:
                    last_snap = time.time()
                    snap = await asyncio.to_thread(_snapshot)
                    yield f"event: snapshot\ndata: {json.dumps(snap, default=str)}\n\n"
        finally:
            await pubsub.unsubscribe(rb.EVENTS)
            await pubsub.aclose()
            await client.aclose()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


def _snapshot() -> dict:
    streams = {}
    for st in db.all_of(db.Stream):
        live = stream_manager.live_state(st.id)
        streams[st.id] = {"status": live.get("status") or st.status, "desired": st.desired_state,
                          "chat": {k: (live.get("chat") or {}).get(k) for k in ("msgs_per_sec", "unique_60s")},
                          "audio": {k: (live.get("audio") or {}).get(k) for k in ("db", "excitement")},
                          "t": live.get("t"), "worker": live.get("worker")}
    res = rb.get_json(rb.k("resources")) or {}
    return {"streams": streams, "resources": {k: res.get(k) for k in ("cpu_pct", "mem_pct", "disk", "level",
                                                                      "reasons")}}
