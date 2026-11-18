"""Chat analysis shared by every platform.

Connectors turn platform messages into ChatEvent (one schema for Twitch, YouTube and the synthetic generator).
The analyzer keeps per-second counters for a fixed window only, so memory stays flat however long a stream runs.

What it measures per second: messages, unique people, emote-only/reaction messages, repeated (copy-pasted)
messages, reaction keywords, and how much of the chat a single person makes up (spam). A real reaction is many
different people reacting at once; one person spamming is discounted.
"""
from __future__ import annotations

import collections
import re
import threading
import time
from dataclasses import dataclass, field

REACTION_WORDS = re.compile(
    r"\b(clip(\s*(it|that|this))?|omg|wtf|lmao+|lol+|no+ way|holy|insane|crazy|wow+|gg|ez|pog+|poggers|w+|"
    r"l+|rip|bruh|ohh+|yoo+|let'?s go+|sheesh|kek|kekw|lul|omegalul|monka\w*|pepe\w*|hype|clutch)\b", re.I)
LAUGH = re.compile(r"(ha){2,}|(he){3,}|(xd)+|😂|🤣|💀", re.I)


@dataclass
class ChatEvent:
    platform: str
    stream_id: str
    ts: float                 # wall-clock seconds when received
    user_id: str
    user: str
    text: str
    emotes: int = 0           # number of emotes in the message
    is_mod: bool = False
    is_member: bool = False
    kind: str = "text"        # text | superchat | sub | other
    extra: dict = field(default_factory=dict)


class ChatAnalyzer:
    def __init__(self, window_seconds: int = 600):
        self.lock = threading.Lock()
        self.window = window_seconds
        self.sec: collections.OrderedDict[int, dict] = collections.OrderedDict()
        self.total = 0
        self.connected = False
        self.last_message_at = 0.0
        self.recent_text = collections.deque(maxlen=300)      # for repeated-message detection

    def add(self, ev: ChatEvent, second: int) -> None:
        norm = re.sub(r"\s+", " ", ev.text.lower()).strip()[:80]
        with self.lock:
            b = self.sec.get(second)
            if b is None:
                b = self.sec[second] = {"m": 0, "users": collections.Counter(), "emote": 0, "react": 0,
                                        "repeat": 0, "laugh": 0}
                if len(self.sec) > self.window + 5:           # messages can arrive out of order: prune by age
                    newest = max(self.sec)
                    for old in [t for t in self.sec if t < newest - self.window]:
                        del self.sec[old]
            b["m"] += 1
            b["users"][ev.user_id or ev.user] += 1
            words = len(norm.split()) or 1
            if ev.emotes and ev.emotes >= words * 0.5:
                b["emote"] += 1
            if REACTION_WORDS.search(norm):
                b["react"] += 1
            if LAUGH.search(ev.text):
                b["laugh"] += 1
            if norm and norm in self.recent_text:
                b["repeat"] += 1
            self.recent_text.append(norm)
            self.total += 1
            self.last_message_at = ev.ts

    def _range(self, a: int, b: int) -> list:
        return [self.sec.get(t) for t in range(a, b + 1)]

    def stats(self, now_second: int, span: int = 10) -> dict:
        """Activity over the last `span` seconds, against the baseline of the minutes before."""
        with self.lock:
            cur = [x for x in self._range(now_second - span + 1, now_second) if x]
            base = [x for x in self._range(now_second - 180, now_second - span) if x]
            users = collections.Counter()
            for x in cur:
                users.update(x["users"])
            m = sum(x["m"] for x in cur)
            base_m = sum(x["m"] for x in base) / max(1, 180 - span)
            base_u = sum(len(x["users"]) for x in base) / max(1, 180 - span)
            uniq_60 = set()
            for x in self._range(now_second - 59, now_second):
                if x:
                    uniq_60.update(x["users"])
            top_share = (users.most_common(1)[0][1] / m) if m else 0.0
            return {
                "msgs_per_sec": round(m / span, 2),
                "unique_users": len(users),
                "unique_per_sec": round(len(users) / span, 2),
                "unique_60s": len(uniq_60),
                "baseline_msgs_per_sec": round(base_m, 2),
                "baseline_unique_per_sec": round(base_u, 2),
                "emote_ratio": round(sum(x["emote"] for x in cur) / m, 2) if m else 0.0,
                "reaction_ratio": round(sum(x["react"] + x["laugh"] for x in cur) / m, 2) if m else 0.0,
                "repeat_ratio": round(sum(x["repeat"] for x in cur) / m, 2) if m else 0.0,
                "top_user_share": round(top_share, 2),
                "total": self.total,
                "connected": self.connected,
            }

    def excitement(self, now_second: int) -> tuple[float, dict]:
        """0..100 from chat, counted in people rather than messages, with single-user spam discounted."""
        s = self.stats(now_second)
        if s["baseline_unique_per_sec"] == 0 and s["unique_users"] < 3:
            return 0.0, s
        base_u = max(s["baseline_unique_per_sec"], 0.2)
        ratio = s["unique_per_sec"] / base_u
        score = max(0.0, (ratio - 1.0)) * 22
        score += s["reaction_ratio"] * 25 + min(s["repeat_ratio"], 0.6) * 15 + s["emote_ratio"] * 10
        if s["top_user_share"] > 0.4 and s["unique_users"] <= 3:
            score *= 0.15                       # one person spamming is not a crowd reacting
        if s["unique_users"] < 3:
            score *= 0.5
        return float(min(100.0, score)), s

    def per_second_people(self, a: int, b: int) -> list:
        with self.lock:
            return [len(x["users"]) if x else 0 for x in self._range(a, b)]
