"""Creating edits, render jobs and uploads: shared by the API and the workers."""
from __future__ import annotations

import time

from sqlalchemy import func, select

from . import db
from . import redis_bus as rb
from .clip_renderer import DEFAULT_PARAMS, merge_params
from .config import settings


class JobError(ValueError):
    pass


def default_params(clip: db.Clip) -> dict:
    p = merge_params(DEFAULT_PARAMS, {"start": clip.suggested_start, "end": clip.suggested_end or clip.duration,
                                      "title": clip.title, "description": clip.description,
                                      "hashtags": clip.hashtags or []})
    p["hook"] = {**p["hook"], "text": clip.hook}
    return p


def create_edit(clip: db.Clip, params: dict, auto: bool = False) -> db.Edit:
    with db.session() as s:
        n = s.scalar(select(func.max(db.Edit.version)).where(db.Edit.clip_id == clip.id)) or 0
        e = db.Edit(clip_id=clip.id, version=n + 1, params=merge_params(default_params(clip), params),
                    status="draft")
        if auto:
            e.params["auto"] = True
        s.add(e)
    return e


def enqueue_render(edit_id: str, auto_upload: str | None = None) -> None:
    try:
        rb.queue("rendering").put({"edit_id": edit_id, "auto_upload": auto_upload or "", "t": time.time()})
    except rb.QueueFull as e:
        raise JobError("The render queue is full right now; try again shortly.") from e
    db.update(db.Edit, edit_id, status="queued", progress=0, error="")
    rb.publish("edit", edit_id=edit_id, status="queued")


def create_upload(edit: db.Edit, title: str, description: str, tags: list, privacy: str,
                  publish_at: float = 0, channel_id: str | None = None) -> db.Upload:
    """One upload per (edit, channel). A failed one may be retried; a done or running one is never repeated."""
    channel_id = channel_id or settings.youtube_channel_id or "default"
    if privacy not in ("private", "unlisted", "public"):
        raise JobError("privacy must be private, unlisted or public")
    with db.session() as s:
        existing = s.scalar(select(db.Upload).where(db.Upload.edit_id == edit.id, db.Upload.channel_id == channel_id))
        if existing is not None:
            if existing.status in ("done", "uploading", "queued"):
                raise JobError(f"This version is already {existing.status} on this channel"
                               + (f" (video {existing.youtube_video_id})" if existing.youtube_video_id else "") + ".")
            existing.title, existing.description, existing.tags = title, description, tags
            existing.privacy, existing.publish_at = privacy, publish_at
            existing.status, existing.error, existing.progress = "queued", "", 0
            up = existing
        else:
            # the same clip published from a different version is allowed, but the same file twice is not
            done_same_clip = s.scalar(select(func.count()).select_from(db.Upload).where(
                db.Upload.clip_id == edit.clip_id, db.Upload.channel_id == channel_id, db.Upload.status == "done"))
            up = db.Upload(edit_id=edit.id, clip_id=edit.clip_id, channel_id=channel_id, title=title[:100],
                           description=description, tags=tags, privacy=privacy, publish_at=publish_at)
            s.add(up)
            if done_same_clip:
                up.error = "note: another version of this clip is already on this channel"
    try:
        rb.queue("publishing").put({"upload_id": up.id})
    except rb.QueueFull as e:
        db.update(db.Upload, up.id, status="failed", error="publishing queue full; retry later")
        raise JobError("The publishing queue is full right now.") from e
    rb.publish("upload", upload_id=up.id, status="queued")
    return up
