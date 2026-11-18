"""Synthetic streams for testing without real streamers (enable with CLIPPINGSHORTS_ENABLE_SYNTHETIC=1).

A synthetic stream URL looks like  synthetic://name?bursts=60&chat=3&spam=1&explode=1&fail=0
  bursts   seconds between loud audio bursts (shouting stand-in)
  chat     baseline chat messages per second
  explode  1 = a chat explosion from many different people right after each burst
  spam     1 = one user floods chat every so often (must NOT count as a highlight)
  drop     seconds after which the capture dies once (tests reconnection); 0 = never

The video is a test pattern encoded ONCE to a file, then each synthetic stream plays it in real time with
`-c copy`, exactly like a real stream ingest (no per-stream encoding), so CPU and memory measurements are
representative of real ingestion.
"""
from __future__ import annotations

import random
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .chat_analyzer import ChatEvent
from .config import settings
from .video_ingestion import NotLive, Resolved

LOOP_SECONDS = 300
BURST_EVERY = 60


def source_file() -> Path:
    return settings.data_dir / "synthetic" / f"source_{LOOP_SECONDS}s.ts"


def ensure_source() -> Path:
    """Encode the 5-minute synthetic source once (720p30 test pattern, tone + noise with a loud burst each minute)."""
    import fcntl
    out = source_file()
    if out.exists() and out.stat().st_size > 1_000_000:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out.with_suffix(".lock"), "w") as lk:       # one encoder, however many synthetic streams start at once
        fcntl.flock(lk, fcntl.LOCK_EX)
        if out.exists() and out.stat().st_size > 1_000_000:
            return out
        return _encode(out)


def _encode(out: Path) -> Path:
    tmp = out.with_suffix(".tmp.ts")
    # quiet pink-ish noise + a soft tone, and every BURST_EVERY s a 4 s burst ~18 dB louder
    expr = (f"0.03*sin(2*PI*220*t)+0.02*(random(0)-0.5)"
            f"+if(lt(mod(t\\,{BURST_EVERY})\\,4)*gt(t\\,20)\\,0.45*sin(2*PI*660*t)*(0.6+0.4*sin(2*PI*3*t))\\,0)")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={LOOP_SECONDS}",
           "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000:d={LOOP_SECONDS}",
           "-vf", "drawtext=text='ClippingShorts synthetic %{pts\\:hms}':x=40:y=40:fontsize=42:fontcolor=white:"
                  "box=1:boxcolor=black@0.5",
           "-c:v", "libx264", "-preset", "veryfast", "-g", "60", "-b:v", "2500k", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-f", "mpegts", str(tmp)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if p.returncode != 0:
        # drawtext needs a font; fall back to the plain pattern
        cmd = [c for c in cmd]
        i = cmd.index("-vf")
        del cmd[i:i + 2]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if p.returncode != 0:
            raise RuntimeError("could not build the synthetic source: " + p.stderr[-300:])
    tmp.replace(out)
    return out


def options(url: str) -> dict:
    u = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    return {"name": u.netloc or "synthetic", "bursts": float(q.get("bursts", BURST_EVERY)),
            "chat": float(q.get("chat", 3)), "explode": q.get("explode", "1") == "1",
            "spam": q.get("spam", "1") == "1", "drop": float(q.get("drop", 0)),
            "offline": q.get("offline", "0") == "1"}


def resolve(url: str) -> Resolved:
    if not settings.enable_synthetic:
        raise NotLive("Synthetic streams are disabled (CLIPPINGSHORTS_ENABLE_SYNTHETIC=0).")
    o = options(url)
    if o["offline"]:
        raise NotLive("This channel is not live right now.")
    return Resolved(urls=[str(ensure_source())], title=f"Synthetic stream {o['name']}", uploader=o["name"],
                    video_id="", is_live=True, height=720)


WORDS = ["nice", "lets go", "W", "gg", "lol", "OMEGALUL", "no way", "what", "insane", "PogChamp", "clip it",
         "hello chat", "first time here", "how long is stream", "LUL", "KEKW", "holy", "bruh"]


class SyntheticChat:
    """Chat that follows the synthetic audio: baseline chatter, an explosion of many users after each burst,
    and an occasional single-user spam flood (which the detector must ignore)."""

    def __init__(self, url: str, stream_id: str, on_event: Callable[[ChatEvent], None], clock: Callable[[], float],
                 on_state: Callable[[bool, str], None] | None = None):
        self.o = options(url)
        self.stream_id = stream_id
        self.on_event = on_event
        self.clock = clock
        self.on_state = on_state or (lambda ok, why: None)
        self.stop_flag = threading.Event()
        self.rng = random.Random(hash(stream_id) & 0xFFFF)
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"synthetic-chat-{stream_id}")

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_flag.set()

    def _send(self, user: str, text: str, emotes: int = 0):
        self.on_event(ChatEvent(platform="synthetic", stream_id=self.stream_id, ts=time.time(), user_id=user,
                                user=user, text=text, emotes=emotes))

    def _run(self):
        self.on_state(True, "")
        every = self.o["bursts"]
        while not self.stop_flag.wait(0.25):
            t = self.clock() % LOOP_SECONDS
            phase = t % every
            rate = self.o["chat"]
            if self.o["explode"] and t > 20 and 1.0 <= phase < 9.0:
                rate *= 8
                for _ in range(self._n(rate * 0.25)):
                    u = f"viewer{self.rng.randint(1, 4000)}"
                    self._send(u, self.rng.choice(["OMEGALUL", "LMAOOO", "no way", "W", "clip it", "KEKW KEKW"]), 1)
                continue
            if self.o["spam"] and 30 <= phase < 36:
                for _ in range(self._n(6)):
                    self._send("spammer_bot", "BUY FOLLOWERS cheap", 0)
            for _ in range(self._n(rate * 0.25)):
                self._send(f"viewer{self.rng.randint(1, 400)}", self.rng.choice(WORDS))

    def _n(self, mean: float) -> int:
        whole = int(mean)
        return whole + (1 if self.rng.random() < mean - whole else 0)
