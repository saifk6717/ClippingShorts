"""Stream configuration and control, used by the API.

The database holds what *should* happen (desired_state running/stopped); the stream worker supervisor makes it so,
one isolated OS process per running stream. Start/stop requests only change the desired state and nudge the
supervisor through the stream_control queue, so an API request never waits on FFmpeg or the network.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlparse

from sqlalchemy import func, select

from . import db
from . import redis_bus as rb
from .config import settings
from .youtube_connector import is_channel_url, video_id_from_url

PLATFORMS = ("twitch", "youtube")


class StreamError(ValueError):
    pass


def normalize(url: str, platform: str) -> tuple[str, str]:
    """(normalized url, channel/video key). Raises StreamError when the link does not match the platform."""
    url = (url or "").strip()
    if platform == "synthetic":
        if not settings.enable_synthetic:
            raise StreamError("Synthetic streams are disabled on this server.")
        if not url.startswith("synthetic://"):
            raise StreamError("A synthetic link looks like synthetic://name?bursts=60")
        return url, urlparse(url).netloc
    if platform not in PLATFORMS:
        raise StreamError("Platform must be Twitch or YouTube Live.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    try:
        u = urlparse(url)
    except ValueError as e:
        raise StreamError("That is not a valid link.") from e
    host = (u.hostname or "").lower()
    if u.username or u.password or u.port not in (None, 443, 80):
        raise StreamError("That is not a valid link.")
    if platform == "twitch":
        if host not in ("twitch.tv", "www.twitch.tv", "m.twitch.tv"):
            raise StreamError("This is not a Twitch link (expected twitch.tv/<channel>).")
        m = re.match(r"^/([A-Za-z0-9_]{2,25})/?$", u.path)
        if not m or m.group(1).lower() in ("directory", "videos", "settings", "p", "search", "downloads"):
            raise StreamError("A Twitch link must point at a channel: https://www.twitch.tv/<channel>")
        ch = m.group(1).lower()
        return f"https://www.twitch.tv/{ch}", ch
    # youtube
    if not (host in ("youtube.com", "youtu.be") or host.endswith(".youtube.com")):
        raise StreamError("This is not a YouTube link.")
    vid = video_id_from_url(url)
    if vid:
        return f"https://www.youtube.com/watch?v={vid}", vid
    if is_channel_url(url):
        path = re.sub(r"/live/?$", "", u.path.rstrip("/"))
        return f"https://www.youtube.com{path}/live", path.strip("/").lower()
    raise StreamError("A YouTube Live link is a watch?v=… link, a /live/<id> link, or a channel (@name) link.")


def add_streams(items: list[dict]) -> tuple[list, list]:
    """Add several streams at once. Returns (created, errors); duplicates and bad links are reported per row."""
    created, errors, seen = [], [], set()
    with db.session() as s:
        total = s.scalar(select(func.count()).select_from(db.Stream)) or 0
        existing = {u for (u,) in s.execute(select(db.Stream.url))}
        for i, it in enumerate(items):
            try:
                url, ch = normalize(it.get("url", ""), (it.get("platform") or "").lower())
            except StreamError as e:
                errors.append({"index": i, "url": it.get("url"), "error": str(e)})
                continue
            if url in existing or url in seen:
                errors.append({"index": i, "url": it.get("url"), "error": "This stream is already added."})
                continue
            if total + len(created) >= settings.max_streams:
                errors.append({"index": i, "url": it.get("url"),
                               "error": f"At most {settings.max_streams} streams can be configured (MAX_STREAMS)."})
                continue
            seen.add(url)
            st = db.Stream(url=url, platform=(it.get("platform") or "").lower(), channel=ch,
                           creator_name=(it.get("creator_name") or "").strip()[:120],
                           enabled=bool(it.get("enabled", True)), settings=clean_settings(it.get("settings") or {}))
            s.add(st)
            created.append(st)
    return [db.to_dict(c) for c in created], errors


SETTING_LIMITS = {
    "local_trigger_score": (10, 100), "candidate_cooldown_seconds": (10, 3600), "max_highlights_per_hour": (0, 60),
    "ai_eval_interval_seconds": (0, 7200), "source_pre_seconds": (10, 270), "source_post_seconds": (5, 120),
}


def clean_settings(raw: dict) -> dict:
    out = {}
    for k_, (lo, hi) in SETTING_LIMITS.items():
        if k_ in raw and raw[k_] not in (None, ""):
            try:
                out[k_] = max(lo, min(hi, float(raw[k_])))
            except (TypeError, ValueError):
                raise StreamError(f"{k_} must be a number") from None
    if "trigger_words" in raw:
        out["trigger_words"] = str(raw["trigger_words"])[:300]
    if "auto_upload" in raw:
        out["auto_upload"] = raw["auto_upload"] if raw["auto_upload"] in ("off", "private", "unlisted",
                                                                           "public") else "off"
    pre = out.get("source_pre_seconds", settings.source_pre_seconds)
    if pre + 10 > settings.rolling_buffer_seconds + 60:
        raise StreamError("source_pre_seconds must fit inside the rolling buffer")
    return out


def request(stream_id: str, action: str) -> dict:
    with db.session() as s:
        st = s.get(db.Stream, stream_id)
        if st is None:
            raise KeyError(stream_id)
        if action == "start":
            if not st.enabled:
                raise StreamError("This stream is disabled. Enable it first.")
            st.desired_state = "running"
            if st.status in ("stopped", "failed", "stopping"):
                st.status = "starting"
            st.last_error = ""
        elif action == "stop":
            st.desired_state = "stopped"
            if st.status not in ("stopped",):
                st.status = "stopping"
        out = db.to_dict(st)
    rb.queue("stream_control").put({"action": action, "stream_id": stream_id, "t": time.time()}, force=True)
    rb.publish("stream", stream_id=stream_id, status=out["status"])
    return out


def request_all(action: str) -> list:
    ids = [st.id for st in db.all_of(db.Stream, db.Stream.enabled.is_(True))]
    out = []
    for sid in ids:
        try:
            out.append(request(sid, action))
        except StreamError:
            continue
    return out


def live_state(stream_id: str) -> dict:
    hb = rb.get_json(rb.k("stream", stream_id, "hb")) or {}
    usage = rb.get_json(rb.k("stream", stream_id, "usage")) or {}
    if hb:
        hb["worker"] = usage
    return hb


def view(st: db.Stream) -> dict:
    d = db.to_dict(st)
    live = live_state(st.id)
    d["live"] = live
    if st.desired_state == "running" and not live and st.status not in ("failed", "deferred"):
        d["status"] = "starting" if time.time() - st.updated_at < 60 else "worker_down"
    elif live:
        d["status"] = live.get("status", d["status"])
    return d
