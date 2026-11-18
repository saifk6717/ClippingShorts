"""Publishing rendered clips to YouTube with the Data API v3 resumable upload protocol (OAuth 2.0).

* The upload session is created once; the file is sent in 8 MB chunks; a network error or 5xx resumes from the
  byte the server confirms instead of starting again.
* privacy: private | unlisted | public. Scheduling sets status.publishAt, which YouTube only accepts on private
  videos, so a scheduled upload is sent as private and goes public at that time.
* Duplicate protection: one uploads row per (edit, channel), enforced by a unique constraint, and a row that is
  already uploading or done is never started again. The YouTube video id is stored as soon as it is known.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

from . import db, google_oauth
from .config import settings

UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CHUNK = 8 * 2**20
UPLOAD_BASE = None          # tests point this at a fake server


class UploadError(Exception):
    def __init__(self, msg: str, retryable: bool = True):
        super().__init__(msg)
        self.retryable = retryable


def channel() -> dict:
    """The channel the configured refresh token belongs to."""
    r = httpx.get("https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
                  headers=google_oauth.headers(), timeout=20)
    r.raise_for_status()
    items = r.json().get("items", [])
    if not items:
        raise UploadError("the YouTube token has no channel", retryable=False)
    return {"id": items[0]["id"], "title": items[0]["snippet"]["title"],
            "thumbnail": items[0]["snippet"].get("thumbnails", {}).get("default", {}).get("url", "")}


def build_body(up: db.Upload) -> dict:
    tags = [t.lstrip("#") for t in (up.tags or []) if t][:15]
    desc = up.description or ""
    hashtags = " ".join(f"#{t}" for t in tags[:5])
    if hashtags and not re.search(r"(^|\s)#\w", desc):
        desc = (desc + "\n\n" + hashtags).strip()
    status = {"privacyStatus": up.privacy, "selfDeclaredMadeForKids": False}
    if up.publish_at and up.publish_at > time.time() + 60:
        status["privacyStatus"] = "private"
        status["publishAt"] = datetime.fromtimestamp(up.publish_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"snippet": {"title": up.title[:100], "description": desc[:4900], "tags": tags,
                        "categoryId": settings.youtube_category_id},
            "status": status}


def upload(upload_id: str, path: Path, on_progress: Callable[[float], None] | None = None) -> str:
    up = db.get(db.Upload, upload_id)
    if up is None:
        raise UploadError("upload row missing", retryable=False)
    size = path.stat().st_size
    base = UPLOAD_BASE or UPLOAD_URL
    with httpx.Client(timeout=httpx.Timeout(60, write=300)) as http:
        r = http.post(base, params={"uploadType": "resumable", "part": "snippet,status"},
                      headers={**google_oauth.headers(), "X-Upload-Content-Type": "video/mp4",
                               "X-Upload-Content-Length": str(size)}, json=build_body(up))
        if r.status_code in (401, 403):
            raise UploadError(f"YouTube refused the upload ({r.status_code}): {r.text[:300]}",
                              retryable=r.status_code == 401)
        if r.status_code != 200 or "location" not in r.headers:
            raise UploadError(f"could not start upload ({r.status_code}): {r.text[:300]}")
        session = r.headers["location"]
        sent, errors = 0, 0
        with open(path, "rb") as f:
            while sent < size:
                f.seek(sent)
                chunk = f.read(CHUNK)
                end = sent + len(chunk) - 1
                try:
                    r = http.put(session, content=chunk, headers={
                        **google_oauth.headers(), "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {sent}-{end}/{size}"})
                except httpx.HTTPError as e:
                    errors += 1
                    if errors > 6:
                        raise UploadError(f"network kept failing: {e}") from e
                    time.sleep(min(60, 2 ** errors))
                    sent, vid = _confirmed(http, session, size)
                    if vid:
                        return vid
                    continue
                if r.status_code in (200, 201):
                    vid = r.json().get("id", "")
                    if on_progress:
                        on_progress(100.0)
                    return vid
                if r.status_code == 308:
                    rng = r.headers.get("range")
                    sent = int(rng.split("-")[1]) + 1 if rng else 0
                    errors = 0
                    if on_progress:
                        on_progress(sent * 100.0 / size)
                    continue
                if r.status_code >= 500:
                    errors += 1
                    if errors > 6:
                        raise UploadError(f"YouTube kept failing ({r.status_code})")
                    time.sleep(min(60, 2 ** errors))
                    sent, vid = _confirmed(http, session, size)
                    if vid:
                        return vid
                    continue
                raise UploadError(f"upload rejected ({r.status_code}): {r.text[:300]}",
                                  retryable=r.status_code in (401, 429))
    raise UploadError("upload ended without a video id")


def _confirmed(http: httpx.Client, session: str, size: int) -> tuple[int, str]:
    """How many bytes YouTube has, and the video id if the upload is in fact already complete."""
    r = http.put(session, headers={**google_oauth.headers(), "Content-Length": "0",
                                   "Content-Range": f"bytes */{size}"})
    if r.status_code == 308:
        rng = r.headers.get("range")
        return (int(rng.split("-")[1]) + 1 if rng else 0), ""
    if r.status_code in (200, 201):
        return size, r.json().get("id", "")
    raise UploadError(f"could not resume upload ({r.status_code})")
