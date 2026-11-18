"""Access tokens for the YouTube Data API from the configured OAuth client and refresh token.

The refresh token never leaves the server. Access tokens are cached in memory until shortly before they expire.
"""
from __future__ import annotations

import threading
import time

import httpx

from .config import settings

_lock = threading.Lock()
_cache = {"token": "", "exp": 0.0, "scopes": ""}


class NotConfigured(Exception):
    pass


def configured() -> bool:
    return bool(settings.youtube_client_id and settings.youtube_client_secret and settings.youtube_refresh_token)


def access_token(force: bool = False) -> str:
    if not configured():
        raise NotConfigured("Set YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET and YOUTUBE_REFRESH_TOKEN.")
    with _lock:
        if not force and _cache["token"] and _cache["exp"] - time.time() > 120:
            return _cache["token"]
        r = httpx.post("https://oauth2.googleapis.com/token", timeout=20, data={
            "client_id": settings.youtube_client_id, "client_secret": settings.youtube_client_secret,
            "refresh_token": settings.youtube_refresh_token, "grant_type": "refresh_token"})
        if r.status_code != 200:
            raise RuntimeError(f"YouTube token refresh failed ({r.status_code}): {r.text[:200]}")
        j = r.json()
        _cache.update(token=j["access_token"], exp=time.time() + int(j.get("expires_in", 3600)),
                      scopes=j.get("scope", ""))
        return _cache["token"]


def scopes() -> str:
    access_token()
    return _cache["scopes"]


def headers() -> dict:
    return {"Authorization": f"Bearer {access_token()}"}
