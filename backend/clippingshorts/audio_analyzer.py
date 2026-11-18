"""Per-stream audio analysis with NumPy: loudness, a rolling baseline, and sudden relative jumps.

Fed one second of 16 kHz mono PCM at a time. Keeps only fixed-size history (deques), so memory does not grow with
stream length. Excitement (shouting, screaming, laughing, hype) is detected *relative* to this streamer's own
recent baseline, not with a fixed volume threshold: a quiet streamer who suddenly shouts counts, a loud one who is
always loud does not.
"""
from __future__ import annotations

import collections
import math

import numpy as np

SR = 16000
FRAME = 400          # 25 ms frames inside each second


class AudioAnalyzer:
    def __init__(self, baseline_seconds: int = 120, history_seconds: int = 600):
        self.db = collections.deque(maxlen=history_seconds)        # loudness per second (dBFS)
        self.peak_db = collections.deque(maxlen=history_seconds)   # loudest 25 ms frame per second
        self.voiced = collections.deque(maxlen=history_seconds)    # share of frames that look like speech
        self.baseline_seconds = baseline_seconds
        self.seconds = 0
        self.last = {"db": -100.0, "peak_db": -100.0, "z": 0.0, "jump_pct": 0.0, "voiced": 0.0,
                     "baseline_db": None}

    def feed(self, pcm: bytes) -> dict:
        """One second (or less) of s16le mono audio. Returns this second's features."""
        x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
        if len(x) == 0:
            return self.last
        rms = float(np.sqrt(np.mean(x * x) + 1e-12))
        db = 20 * math.log10(rms + 1e-10)
        n = len(x) // FRAME
        if n:
            frames = x[: n * FRAME].reshape(n, FRAME)
            f_rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
            peak = float(20 * np.log10(f_rms.max() + 1e-10))
            # a cheap voice-activity estimate: frames clearly above this second's noise floor with a moderate
            # zero-crossing rate (voice) rather than very high (hiss) or near zero (hum)
            floor = np.percentile(f_rms, 20)
            zc = np.mean(np.abs(np.diff(np.sign(frames), axis=1)) > 0, axis=1)
            voiced = float(np.mean((f_rms > floor * 2.5) & (f_rms > 0.005) & (zc > 0.02) & (zc < 0.35)))
        else:
            peak, voiced = db, 0.0
        self.db.append(db)
        self.peak_db.append(peak)
        self.voiced.append(voiced)
        self.seconds += 1
        self.last = self._features(db, peak, voiced)
        return self.last

    def _features(self, db: float, peak: float, voiced: float) -> dict:
        hist = np.array(self.db, dtype=np.float32)
        out = {"db": round(db, 1), "peak_db": round(peak, 1), "voiced": round(voiced, 2), "z": 0.0,
               "jump_pct": 0.0, "baseline_db": None, "warming": len(hist) < 30}
        if len(hist) < 30:
            return out
        # baseline: the last `baseline_seconds`, leaving out the newest 10 s so a long shout doesn't hide itself
        base = hist[-(self.baseline_seconds + 10):-10] if len(hist) > 40 else hist[:-5]
        med = float(np.median(base))
        mad = float(np.median(np.abs(base - med))) * 1.4826 + 0.75   # robust spread, floored at 0.75 dB
        recent = float(np.mean(hist[-3:]))
        out["baseline_db"] = round(med, 1)
        out["z"] = round((recent - med) / mad, 2)
        # in linear amplitude, so "+100%" means twice as loud
        out["jump_pct"] = round((10 ** ((recent - med) / 20) - 1) * 100, 1)
        return out

    def excitement(self) -> float:
        """0..100: how far above this stream's own baseline the last few seconds are, peaks weighted in."""
        f = self.last
        if f.get("warming"):
            return 0.0
        z = max(0.0, f["z"])
        peaks = np.array(self.peak_db, dtype=np.float32)
        peak_bonus = 0.0
        if len(peaks) > 40:
            pm = float(np.median(peaks[-130:-10]))
            peak_bonus = max(0.0, (float(np.max(peaks[-3:])) - pm) / 3.0)
        return float(min(100.0, z * 18 + peak_bonus * 8))

    def window(self, seconds: int) -> list:
        """The last `seconds` of loudness z-scores (for the reaction judge)."""
        hist = np.array(self.db, dtype=np.float32)
        if len(hist) < 30:
            return []
        med = float(np.median(hist))
        mad = float(np.median(np.abs(hist - med))) * 1.4826 + 0.75
        return [round(float((v - med) / mad), 2) for v in hist[-seconds:]]
