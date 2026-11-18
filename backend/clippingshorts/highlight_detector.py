"""Local (free, instant) highlight candidates, decided inside each stream's worker.

Gemini is never called per chat message or per audio frame. Instead this detector watches cheap signals every
second and raises a *candidate* only when something stands out: a loudness jump against the streamer's own
baseline, many different people reacting at once, or someone saying "clip it". Candidates pass a cooldown and a
per-hour cap before the AI judges ever see them. A periodic check (AI_EVAL_INTERVAL_SECONDS) also lets the
transcript judge look at calm stretches where a story might be worth clipping.
"""
from __future__ import annotations

import collections
import re
import time
from dataclasses import dataclass, field

from .config import settings

CLIP_WORDS = re.compile(r"\bclip\s*(it|that|this)\b|\bthat'?s a clip\b", re.I)


@dataclass
class Candidate:
    t: float                  # stream-clock second of the moment
    local_score: float
    trigger: str
    signals: dict = field(default_factory=dict)
    periodic: bool = False


class HighlightDetector:
    def __init__(self, overrides: dict | None = None):
        o = overrides or {}
        self.trigger_score = float(o.get("local_trigger_score", settings.local_trigger_score))
        self.cooldown = float(o.get("candidate_cooldown_seconds", settings.candidate_cooldown_seconds))
        self.per_hour = int(o.get("max_highlights_per_hour", settings.max_highlights_per_hour))
        self.interval = float(o.get("ai_eval_interval_seconds", settings.ai_eval_interval_seconds))
        self.keywords = [w.strip().lower() for w in str(o.get("trigger_words", "")).split(",") if w.strip()]
        self.last_candidate = -1e9
        self.last_periodic = 0.0
        self.recent = collections.deque(maxlen=64)           # wall times of candidates (per-hour cap)
        self.scores = collections.deque(maxlen=600)          # local score per second (dashboard sparkline)
        self.pending_keyword: list[tuple[float, str]] = []
        self.last_spike = None

    def score(self, audio_exc: float, chat_exc: float) -> float:
        """Both signals together count for more than either alone."""
        hi, lo = max(audio_exc, chat_exc), min(audio_exc, chat_exc)
        return min(100.0, hi + 0.45 * lo)

    def _cap_ok(self) -> bool:
        now = time.time()
        return sum(1 for t in self.recent if now - t < 3600) < self.per_hour

    def on_words(self, words: list) -> None:
        """Called with new transcript words: "clip it" or a creator trigger phrase raises a candidate."""
        text = " ".join(w["w"].lower() for w in words)
        for m in CLIP_WORDS.finditer(text):
            idx = len(text[: m.start()].split())
            t = words[min(idx, len(words) - 1)]["s"]
            self.pending_keyword.append((t, 'someone said "clip it"'))
        for kw in self.keywords:
            if kw in text:
                idx = len(text[: text.index(kw)].split())
                self.pending_keyword.append((words[min(idx, len(words) - 1)]["s"], f'trigger phrase "{kw}"'))
        del self.pending_keyword[:-5]

    def step(self, t: float, audio_exc: float, chat_exc: float, signals: dict, has_speech: bool) -> Candidate | None:
        s = self.score(audio_exc, chat_exc)
        self.scores.append(round(s, 1))
        if s >= self.trigger_score:
            self.last_spike = {"t": t, "score": round(s, 1), "at": time.time()}
        cool = t - self.last_candidate >= self.cooldown
        if self.pending_keyword and cool and self._cap_ok():
            kt, why = self.pending_keyword.pop(0)
            return self._fire(Candidate(kt, max(s, self.trigger_score), why, signals))
        if s >= self.trigger_score and cool and self._cap_ok():
            parts = []
            if audio_exc >= self.trigger_score * 0.6:
                parts.append(f"audio jump {signals.get('jump_pct', 0):+.0f}%")
            if chat_exc >= self.trigger_score * 0.6:
                parts.append(f"chat {signals.get('unique_per_sec', 0):.1f} people/s")
            return self._fire(Candidate(t, s, ", ".join(parts) or "combined signals", signals))
        if (self.interval > 0 and has_speech and t - self.last_periodic >= self.interval and cool
                and t - self.last_candidate >= self.interval / 2 and self._cap_ok()):
            self.last_periodic = t
            return Candidate(max(0.0, t - 30), s, "periodic check", signals, periodic=True)
        return None

    def _fire(self, c: Candidate) -> Candidate:
        self.last_candidate = c.t
        self.recent.append(time.time())
        return c
