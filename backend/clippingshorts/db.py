"""Persistent metadata: streams, highlights, clips, edits, uploads and AI usage.

Only small rows live here. Video files stay on disk (clip_storage); live state stays in Redis.
SQLite (WAL mode) is the default because it needs no extra service; set DATABASE_URL for PostgreSQL.
"""
from __future__ import annotations

import time
import uuid
from contextlib import contextmanager

from sqlalchemy import (JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine,
                        event, select)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def now() -> float:
    return time.time()


class Base(DeclarativeBase):
    pass


class Stream(Base):
    __tablename__ = "streams"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    url: Mapped[str] = mapped_column(String(500), unique=True)
    platform: Mapped[str] = mapped_column(String(16))           # twitch | youtube | synthetic
    channel: Mapped[str] = mapped_column(String(200), default="")
    creator_name: Mapped[str] = mapped_column(String(120), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    desired_state: Mapped[str] = mapped_column(String(16), default="stopped")   # running | stopped
    status: Mapped[str] = mapped_column(String(24), default="stopped")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=now)
    updated_at: Mapped[float] = mapped_column(Float, default=now, onupdate=now)


class Highlight(Base):
    __tablename__ = "highlights"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    stream_id: Mapped[str] = mapped_column(String(32), ForeignKey("streams.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str] = mapped_column(String(32), default="")
    detected_at: Mapped[float] = mapped_column(Float, default=now, index=True)
    event_t: Mapped[float] = mapped_column(Float)              # seconds on the stream session clock
    start_t: Mapped[float] = mapped_column(Float)
    end_t: Mapped[float] = mapped_column(Float)
    trigger: Mapped[str] = mapped_column(String(200), default="")
    local_score: Mapped[float] = mapped_column(Float, default=0)
    transcript_score: Mapped[int] = mapped_column(Integer, default=0)
    reaction_score: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[str] = mapped_column(String(24), default="other")
    title: Mapped[str] = mapped_column(String(200), default="")
    hook: Mapped[str] = mapped_column(String(120), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    hashtags: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(Text, default="")
    signals: Mapped[dict] = mapped_column(JSON, default=dict)
    pin_id: Mapped[str] = mapped_column(String(32), default="")
    # candidate -> evaluating -> accepted | rejected | merged | duplicate -> extracted | failed
    status: Mapped[str] = mapped_column(String(16), default="candidate", index=True)


class Clip(Base):
    __tablename__ = "clips"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    stream_id: Mapped[str] = mapped_column(String(32), ForeignKey("streams.id", ondelete="SET NULL"), nullable=True,
                                           index=True)
    highlight_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    platform: Mapped[str] = mapped_column(String(16), default="")
    creator_name: Mapped[str] = mapped_column(String(120), default="")
    source_url: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[float] = mapped_column(Float, default=now, index=True)
    detected_at: Mapped[float] = mapped_column(Float, default=now)
    duration: Mapped[float] = mapped_column(Float, default=0)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    # where the highlight sits inside the source clip (seconds from the source file's start)
    suggested_start: Mapped[float] = mapped_column(Float, default=0)
    suggested_end: Mapped[float] = mapped_column(Float, default=0)
    category: Mapped[str] = mapped_column(String(24), default="other", index=True)
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(200), default="")
    hook: Mapped[str] = mapped_column(String(120), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    hashtags: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(Text, default="")
    rel_dir: Mapped[str] = mapped_column(String(300), default="")      # relative to settings.source_dir
    status: Mapped[str] = mapped_column(String(16), default="extracting", index=True)  # extracting|ready|failed|expired
    review_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|approved|rejected
    words_status: Mapped[str] = mapped_column(String(16), default="live")    # live | precise | none
    keep: Mapped[bool] = mapped_column(Boolean, default=False)              # exempt from retention
    expires_at: Mapped[float] = mapped_column(Float, default=0)
    error: Mapped[str] = mapped_column(Text, default="")


class Edit(Base):
    __tablename__ = "edits"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    clip_id: Mapped[str] = mapped_column(String(32), ForeignKey("clips.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[float] = mapped_column(Float, default=now)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)  # draft|queued|deferred|rendering|done|failed
    progress: Mapped[float] = mapped_column(Float, default=0)
    output_rel: Mapped[str] = mapped_column(String(300), default="")   # relative to settings.edited_dir
    duration: Mapped[float] = mapped_column(Float, default=0)
    render_seconds: Mapped[float] = mapped_column(Float, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[float] = mapped_column(Float, default=now, onupdate=now)


class Upload(Base):
    __tablename__ = "uploads"
    __table_args__ = (UniqueConstraint("edit_id", "channel_id", name="uq_upload_edit_channel"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    edit_id: Mapped[str] = mapped_column(String(32), ForeignKey("edits.id", ondelete="CASCADE"), index=True)
    clip_id: Mapped[str] = mapped_column(String(32), index=True)
    channel_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    privacy: Mapped[str] = mapped_column(String(16), default="private")
    publish_at: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)   # queued|uploading|done|failed
    progress: Mapped[float] = mapped_column(Float, default=0)
    youtube_video_id: Mapped[str] = mapped_column(String(32), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=now)
    finished_at: Mapped[float] = mapped_column(Float, default=0)


class AIUsage(Base):
    __tablename__ = "ai_usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float, default=now, index=True)
    stream_id: Mapped[str] = mapped_column(String(32), default="")
    service: Mapped[str] = mapped_column(String(16))        # gemini | speech
    model: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(64))
    units_in: Mapped[float] = mapped_column(Float, default=0)    # tokens, or audio seconds for speech
    units_out: Mapped[float] = mapped_column(Float, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    est_cost_usd: Mapped[float] = mapped_column(Float, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")


_engine = None
_Session = None


def engine():
    global _engine, _Session
    if _engine is None:
        url = settings.db_url
        if url.startswith("sqlite"):
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            _engine = create_engine(url, connect_args={"timeout": 30, "check_same_thread": False},
                                    pool_pre_ping=True)

            @event.listens_for(_engine, "connect")
            def _pragmas(dbapi_conn, _rec):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        else:
            _engine = create_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True)
        _Session = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def init_db() -> None:
    Base.metadata.create_all(engine())


@contextmanager
def session():
    engine()
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get(model, obj_id):
    with session() as s:
        return s.get(model, obj_id)


def update(model, obj_id, **fields) -> None:
    with session() as s:
        obj = s.get(model, obj_id)
        if obj is not None:
            for k, v in fields.items():
                setattr(obj, k, v)


def to_dict(obj) -> dict:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def all_of(model, *where, order=None, limit=None):
    with session() as s:
        q = select(model)
        for w in where:
            q = q.where(w)
        if order is not None:
            q = q.order_by(order)
        if limit:
            q = q.limit(limit)
        return list(s.scalars(q))
