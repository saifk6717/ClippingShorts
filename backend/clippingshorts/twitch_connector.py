"""Twitch chat through the read-only anonymous IRC gateway (no Twitch account or app needed).

EventSub's chat events need a Twitch app plus a user token with chat scopes for every channel watched; anonymous
IRC reads any public chat, so it is used here. IRCv3 tags give a stable user id, emote count and mod status.
One connection per stream; it reconnects with backoff and never takes the stream worker down with it.
"""
from __future__ import annotations

import random
import re
import socket
import ssl
import threading
import time
from typing import Callable

from .chat_analyzer import ChatEvent

HOST, PORT = "irc.chat.twitch.tv", 6697
_PRIV = re.compile(r"^(?:@(?P<tags>\S+) )?:(?P<nick>[^!\s]+)!\S+ PRIVMSG #\S+ :(?P<text>.*)$")


def channel_from_url(url: str) -> str:
    m = re.match(r"^https?://(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]{2,25})/?$", url.strip())
    if not m:
        raise ValueError("A Twitch link looks like https://www.twitch.tv/<channel>")
    return m.group(1).lower()


def _tags(raw: str | None) -> dict:
    out = {}
    for part in (raw or "").split(";"):
        if "=" in part:
            a, b = part.split("=", 1)
            out[a] = b
    return out


def parse_line(line: str, stream_id: str) -> ChatEvent | None:
    m = _PRIV.match(line)
    if not m:
        return None
    tags = _tags(m.group("tags"))
    emotes = 0
    if tags.get("emotes"):
        emotes = sum(len(e.split(":", 1)[1].split(",")) for e in tags["emotes"].split("/") if ":" in e)
    badges = tags.get("badges", "")
    return ChatEvent(platform="twitch", stream_id=stream_id, ts=time.time(),
                     user_id=tags.get("user-id") or m.group("nick").lower(),
                     user=tags.get("display-name") or m.group("nick"), text=m.group("text")[:500], emotes=emotes,
                     is_mod=tags.get("mod") == "1" or "broadcaster" in badges,
                     is_member=tags.get("subscriber") == "1",
                     kind="sub" if tags.get("msg-id") in ("sub", "resub") else "text")


class TwitchChat:
    def __init__(self, url: str, stream_id: str, on_event: Callable[[ChatEvent], None],
                 on_state: Callable[[bool, str], None] | None = None):
        self.channel = channel_from_url(url)
        self.stream_id = stream_id
        self.on_event = on_event
        self.on_state = on_state or (lambda ok, why: None)
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"twitch-chat-{self.channel}", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_flag.set()

    def _run(self):
        backoff = 2.0
        while not self.stop_flag.is_set():
            sock = None
            try:
                raw = socket.create_connection((HOST, PORT), timeout=20)
                sock = ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
                nick = f"justinfan{random.randint(10000, 99999)}"
                sock.sendall(f"CAP REQ :twitch.tv/tags\r\nNICK {nick}\r\nJOIN #{self.channel}\r\n".encode())
                sock.settimeout(60)
                self.on_state(True, "")
                backoff = 2.0
                rest = b""
                last_rx = time.time()
                while not self.stop_flag.is_set():
                    try:
                        data = sock.recv(16384)
                    except socket.timeout:
                        if time.time() - last_rx > 300:
                            raise ConnectionError("no data from Twitch chat for 5 minutes")
                        sock.sendall(b"PING :tmi.twitch.tv\r\n")
                        continue
                    if not data:
                        raise ConnectionError("Twitch chat closed the connection")
                    last_rx = time.time()
                    *lines, rest = (rest + data).split(b"\r\n")
                    if len(rest) > 65536:             # a broken line must not grow without end
                        rest = b""
                    for bline in lines:
                        line = bline.decode("utf-8", "replace")
                        if line.startswith("PING"):
                            sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                            continue
                        ev = parse_line(line, self.stream_id)
                        if ev:
                            self.on_event(ev)
            except Exception as e:  # noqa: BLE001 - any failure: report, wait, reconnect
                self.on_state(False, str(e)[-160:])
            finally:
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
            self.stop_flag.wait(backoff)
            backoff = min(backoff * 2, 60)
