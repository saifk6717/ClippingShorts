"""YouTube Live chat through the official YouTube Data API v3.

1. Broadcast discovery: the video id comes from the link, or from yt-dlp's answer for channel /live links.
2. liveChatId: videos.list(part=liveStreamingDetails) -> activeLiveChatId (1 quota unit).
3. Messages: liveChatMessages.streamList, a server-streaming HTTP call (GET .../liveChat/messages/stream, 5 units
   per connection) whose body is a JSON array of liveChatMessageListResponse objects that keeps growing. When the
   connection drops it is resumed with the last nextPageToken. If streaming is refused, liveChatMessages.list is
   polled instead, never faster than pollingIntervalMillis or YOUTUBE_CHAT_MIN_POLL_SECONDS.
4. Quota: every call is counted per day in Redis against YOUTUBE_CHAT_DAILY_QUOTA_BUDGET (shared by all streams);
   when the budget is spent chat is paused until the next day and detection continues on audio alone.
5. End: `offlineAt` in a response, or a 403 liveChatEnded / 404, ends the chat connection for this broadcast.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import parse_qs, urlparse

import httpx

from . import google_oauth
from . import redis_bus as rb
from .chat_analyzer import ChatEvent
from .config import settings

API = "https://www.googleapis.com/youtube/v3"
STREAM_API = "https://youtube.googleapis.com/youtube/v3/liveChat/messages/stream"
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def video_id_from_url(url: str) -> str:
    """The 11-character video id in a watch, youtu.be, /live/<id> or /shorts/<id> link; "" for channel links."""
    u = urlparse(url.strip())
    host = (u.hostname or "").lower()
    if host.endswith("youtu.be"):
        vid = u.path.strip("/").split("/")[0]
        return vid if _ID.match(vid) else ""
    if "youtube.com" in host:
        q = parse_qs(u.query).get("v", [""])[0]
        if _ID.match(q):
            return q
        m = re.match(r"^/(?:live|shorts|embed)/([A-Za-z0-9_-]{11})", u.path)
        if m:
            return m.group(1)
    return ""


def is_channel_url(url: str) -> bool:
    return bool(re.match(r"^https?://(?:www\.|m\.)?youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+)(?:/live)?/?$",
                         url.strip()))


def spend_quota(units: int) -> bool:
    """Count quota units for today; False (and nothing counted) when today's chat budget is used up."""
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    key = rb.k("quota", "youtube_chat", day)
    used = rb.r().incrby(key, units)
    rb.r().expire(key, 2 * 86400)
    if used > settings.youtube_chat_daily_quota_budget:
        rb.r().decrby(key, units)
        return False
    return True


def quota_used_today() -> int:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return int(rb.r().get(rb.k("quota", "youtube_chat", day)) or 0)


def live_chat_id(video_id: str) -> tuple[str, dict]:
    if not spend_quota(1):
        raise RuntimeError("YouTube chat quota budget for today is used up")
    r = httpx.get(f"{API}/videos", params={"part": "liveStreamingDetails,snippet", "id": video_id},
                  headers=google_oauth.headers(), timeout=20)
    r.raise_for_status()
    items = r.json().get("items", [])
    if not items:
        raise RuntimeError("YouTube does not know this video")
    det = items[0].get("liveStreamingDetails", {})
    return det.get("activeLiveChatId", ""), {"title": items[0]["snippet"].get("title", ""),
                                             "channel": items[0]["snippet"].get("channelTitle", ""),
                                             "live": items[0]["snippet"].get("liveBroadcastContent") == "live"}


def to_event(item: dict, stream_id: str) -> ChatEvent | None:
    sn, au = item.get("snippet", {}), item.get("authorDetails", {})
    kind = sn.get("type", "")
    text = sn.get("displayMessage") or sn.get("textMessageDetails", {}).get("messageText", "")
    if kind not in ("textMessageEvent", "superChatEvent", "superStickerEvent", "newSponsorEvent",
                    "memberMilestoneChatEvent"):
        return None
    emotes = len(re.findall(r":[a-z0-9_-]+:", text, re.I))     # YouTube writes custom emoji as :name:
    # YouTube delivers chat in batches every few seconds; the message's own publishedAt time places it where it
    # was actually written, so a batch doesn't look like a sudden spike
    ts = time.time()
    try:
        pub = datetime.fromisoformat(str(sn.get("publishedAt", "")).replace("Z", "+00:00")).timestamp()
        if 0 <= ts - pub < 120:
            ts = pub
    except ValueError:
        pass
    return ChatEvent(platform="youtube", stream_id=stream_id, ts=ts,
                     user_id=au.get("channelId") or sn.get("authorChannelId", ""), user=au.get("displayName", ""),
                     text=text[:500], emotes=emotes, is_mod=bool(au.get("isChatModerator") or au.get("isChatOwner")),
                     is_member=bool(au.get("isChatSponsor")),
                     kind="superchat" if kind.startswith("super") else ("sub" if "Sponsor" in kind or "Milestone" in kind
                                                                         else "text"))


class _ArrayStream:
    """Reads objects out of an ever-growing JSON array: '[{...},\\r\\n{...},...'."""

    def __init__(self):
        self.buf = ""
        self.dec = json.JSONDecoder()

    def feed(self, text: str) -> list:
        self.buf += text
        out = []
        while True:
            s = self.buf.lstrip(" \r\n\t,[")
            if not s or s.startswith("]"):
                self.buf = ""
                return out
            try:
                obj, end = self.dec.raw_decode(s)
            except ValueError:
                self.buf = s
                if len(self.buf) > 4 * 2**20:        # never hold more than a few MB of half-read JSON
                    self.buf = ""
                return out
            out.append(obj)
            self.buf = s[end:]


class YouTubeChat:
    def __init__(self, url: str, stream_id: str, on_event: Callable[[ChatEvent], None],
                 on_state: Callable[[bool, str], None] | None = None, video_id: str = ""):
        self.url = url
        self.stream_id = stream_id
        self.video_id = video_id or video_id_from_url(url)
        self.on_event = on_event
        self.on_state = on_state or (lambda ok, why: None)
        self.stop_flag = threading.Event()
        self.ended = False
        self.page_token = ""
        self.seen = set()                     # message ids, so a resumed stream doesn't count history twice
        self.thread = threading.Thread(target=self._run, name=f"yt-chat-{stream_id}", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_flag.set()

    def set_video(self, video_id: str):
        if video_id and video_id != self.video_id:
            self.video_id, self.page_token, self.ended = video_id, "", False

    def _handle(self, resp: dict) -> float:
        for item in resp.get("items", []):
            mid = item.get("id")
            if mid in self.seen:
                continue
            self.seen.add(mid)
            ev = to_event(item, self.stream_id)
            if ev:
                self.on_event(ev)
        if len(self.seen) > 5000:
            self.seen = set(list(self.seen)[-2000:])
        if resp.get("nextPageToken"):
            self.page_token = resp["nextPageToken"]
        if resp.get("offlineAt"):
            self.ended = True
        return float(resp.get("pollingIntervalMillis") or 5000) / 1000.0

    def _stream(self, chat_id: str):
        if not spend_quota(5):
            raise _Budget()
        params = {"liveChatId": chat_id, "part": "snippet,authorDetails", "maxResults": 500}
        if self.page_token:
            params["pageToken"] = self.page_token
        with httpx.stream("GET", STREAM_API, params=params, headers=google_oauth.headers(),
                          timeout=httpx.Timeout(20, read=120)) as r:
            if r.status_code in (403, 404):
                body = r.read().decode("utf-8", "replace")
                if "liveChatEnded" in body or r.status_code == 404:
                    self.ended = True
                    return
                raise _Refused(body[:200])
            if r.status_code != 200:
                raise RuntimeError(f"streamList HTTP {r.status_code}")
            self.on_state(True, "")
            parser = _ArrayStream()
            for chunk in r.iter_text():
                if self.stop_flag.is_set():
                    return
                for obj in parser.feed(chunk):
                    self._handle(obj)
                if self.ended:
                    return

    def _poll(self, chat_id: str):
        while not self.stop_flag.is_set() and not self.ended:
            if not spend_quota(5):
                raise _Budget()
            params = {"liveChatId": chat_id, "part": "snippet,authorDetails", "maxResults": 2000}
            if self.page_token:
                params["pageToken"] = self.page_token
            r = httpx.get(f"{API}/liveChat/messages", params=params, headers=google_oauth.headers(), timeout=20)
            if r.status_code in (403, 404) and ("liveChatEnded" in r.text or r.status_code == 404):
                self.ended = True
                return
            r.raise_for_status()
            self.on_state(True, "")
            wait = self._handle(r.json())
            self.stop_flag.wait(max(wait, settings.youtube_chat_min_poll_seconds))

    def _run(self):
        backoff, use_stream = 3.0, True
        while not self.stop_flag.is_set():
            if not google_oauth.configured():
                self.on_state(False, "YouTube API credentials not configured: chat disabled")
                return
            if not self.video_id:
                self.on_state(False, "waiting for the broadcast's video id")
                self.stop_flag.wait(10)
                continue
            try:
                chat_id, _meta = live_chat_id(self.video_id)
                if not chat_id:
                    self.ended = True
                if self.ended:
                    self.on_state(False, "chat ended or not available for this broadcast")
                    # a new broadcast id may arrive through set_video; until then look rarely (1 unit per look)
                    ended_for = self.video_id
                    for _ in range(60):
                        if self.stop_flag.wait(5) or self.video_id != ended_for:
                            break
                    continue
                if use_stream:
                    try:
                        self._stream(chat_id)
                    except _Refused:
                        use_stream = False
                else:
                    self._poll(chat_id)
                backoff = 3.0
            except _Budget:
                self.on_state(False, "YouTube chat quota budget for today used up: audio-only detection")
                self.stop_flag.wait(900)
            except Exception as e:  # noqa: BLE001
                self.on_state(False, str(e)[-160:])
                self.stop_flag.wait(backoff)
                backoff = min(backoff * 2, 120)


class _Budget(Exception):
    pass


class _Refused(Exception):
    pass
