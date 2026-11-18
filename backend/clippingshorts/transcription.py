"""Speech to text behind one interface, so the provider can change without touching the pipeline.

Providers
  chirp3    Google Cloud Speech-to-Text v2, model "chirp_3" (default). Verified against the API: available in the
            `us` and `eu` multi-regions; word-level timestamps are returned by Recognize/BatchRecognize but NOT by
            StreamingRecognize. Word timing is what clipping needs, so live audio is sent as short chunks (about
            10 s, cut at a quiet moment) through sync Recognize: near-live (a few seconds behind) with exact word
            times. Every chunk result is final; there are no interim (partial) results in this mode.
  parakeet  Local NVIDIA Parakeet TDT 0.6B through sherpa-onnx, the offline fallback. CPU heavy (one chunk at a
            time on a small VM) and ~500 MB of RAM: use it only when the cloud is unavailable.
  mock      Placeholder words, for load tests that must not spend money.
  none      No transcription (detection uses audio and chat only).

Words are dicts {"w": text, "s": start, "e": end} in seconds relative to the audio given.
"""
from __future__ import annotations

import base64
import io
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import redis_bus as rb
from .config import settings

SR = 16000


class TranscriptionError(Exception):
    pass


class BudgetExceeded(TranscriptionError):
    pass


def wav_bytes(pcm: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.astype(np.int16).tobytes())
    return buf.getvalue()


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def quiet_cut(audio: np.ndarray, lo: float, hi: float) -> int:
    """Sample index of the quietest 50 ms between lo and hi seconds, so no word is cut in half."""
    hop = 800
    a, b = int(lo * SR) // hop, min(len(audio), int(hi * SR)) // hop
    if b <= a:
        return len(audio)
    frames = audio[: b * hop].reshape(-1, hop).astype(np.float32)
    rms = np.sqrt((frames[a:b] ** 2).mean(axis=1))
    return (a + int(np.argmin(rms))) * hop + hop // 2


def _spend_minutes(seconds: float) -> None:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    key = rb.k("quota", "speech_seconds", day)
    used = rb.r().incrbyfloat(key, seconds)
    rb.r().expire(key, 2 * 86400)
    if float(used) > settings.speech_daily_minutes_budget * 60:
        rb.r().incrbyfloat(key, -seconds)
        raise BudgetExceeded("Speech-to-Text daily minutes budget used up")


def speech_seconds_today() -> float:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return float(rb.r().get(rb.k("quota", "speech_seconds", day)) or 0)


# ---------------------------------------------------------------- Google credentials (service account)
_gc = {"creds": None, "lock": threading.Lock()}


def google_token() -> tuple[str, str]:
    """(access token, project id) from GOOGLE_APPLICATION_CREDENTIALS. Shared with the Gemini judge."""
    import json

    import google.auth.transport.requests
    from google.oauth2 import service_account
    with _gc["lock"]:
        if _gc["creds"] is None:
            path = settings.google_credentials
            if not path or not Path(path).is_file():
                raise TranscriptionError("GOOGLE_APPLICATION_CREDENTIALS does not point to a key file")
            _gc["creds"] = service_account.Credentials.from_service_account_file(
                path, scopes=["https://www.googleapis.com/auth/cloud-platform"])
            _gc["project"] = settings.google_project or json.loads(Path(path).read_text())["project_id"]
        c = _gc["creds"]
        if not c.valid or (c.expiry and (c.expiry.timestamp() - time.time()) < 120):
            c.refresh(google.auth.transport.requests.Request())
        return c.token, _gc["project"]


# ---------------------------------------------------------------- providers
class Provider:
    name = "base"
    model = ""

    def transcribe(self, pcm: np.ndarray) -> list:
        raise NotImplementedError


class Chirp3(Provider):
    name = "chirp3"
    model = "chirp_3"
    MAX_SYNC = 55.0          # sync Recognize is meant for audio under a minute

    def __init__(self):
        import httpx
        self.http = httpx.Client(timeout=60)

    def transcribe(self, pcm: np.ndarray) -> list:
        words, pos = [], 0
        while pos < len(pcm):
            end = len(pcm) if len(pcm) - pos <= self.MAX_SYNC * SR else pos + quiet_cut(pcm[pos:], 40, self.MAX_SYNC)
            for w in self._one(pcm[pos:end]):
                words.append({"w": w["w"], "s": round(w["s"] + pos / SR, 2), "e": round(w["e"] + pos / SR, 2)})
            pos = end
        return words

    def _one(self, pcm: np.ndarray) -> list:
        secs = len(pcm) / SR
        if secs < 0.3:
            return []
        _spend_minutes(secs)
        token, project = google_token()
        loc = settings.speech_location
        host = "speech.googleapis.com" if loc == "global" else f"{loc}-speech.googleapis.com"
        body = {"config": {"model": "chirp_3", "languageCodes": [settings.speech_language],
                           "features": {"enableWordTimeOffsets": True, "enableAutomaticPunctuation": True},
                           "autoDecodingConfig": {}},
                "content": base64.b64encode(wav_bytes(pcm)).decode()}
        r = self.http.post(f"https://{host}/v2/projects/{project}/locations/{loc}/recognizers/_:recognize",
                           headers={"Authorization": f"Bearer {token}"}, json=body)
        if r.status_code != 200:
            raise TranscriptionError(f"Chirp 3 HTTP {r.status_code}: {r.text[:200]}")
        out = []
        for res in r.json().get("results", []):
            alts = res.get("alternatives") or []
            if not alts:
                continue
            for w in alts[0].get("words", []):
                text = (w.get("word") or "").strip()
                if not text or text == "[BACKGROUND]":
                    continue
                out.append({"w": text, "s": _secs(w.get("startOffset")), "e": _secs(w.get("endOffset"))})
        return out


def _secs(v) -> float:
    if not v:
        return 0.0
    return float(str(v).rstrip("s"))


class Parakeet(Provider):
    """Local fallback. Loads lazily, once, and decodes one piece at a time."""
    name = "parakeet"
    model = "parakeet-tdt-0.6b-v2-int8"
    URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
           "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2")
    _lock = threading.Lock()
    _rec = None

    def _dir(self) -> Path:
        return Path(settings.parakeet_model_dir or settings.data_dir / "models" /
                    "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8")

    def _ensure(self):
        if Parakeet._rec is not None:
            return Parakeet._rec
        d = self._dir()
        if not (d / "tokens.txt").is_file():
            import shutil
            import tarfile
            import urllib.request
            d.parent.mkdir(parents=True, exist_ok=True)
            tmp = d.parent / "parakeet.tar.bz2"
            urllib.request.urlretrieve(self.URL, tmp)
            with tarfile.open(tmp, "r:bz2") as t:
                for m in t.getmembers():
                    if (m.isfile() or m.isdir()) and ".." not in m.name.split("/") and not m.name.startswith("/"):
                        t.extract(m, d.parent)
            tmp.unlink(missing_ok=True)
            if not (d / "tokens.txt").is_file():
                shutil.rmtree(d, ignore_errors=True)
                raise TranscriptionError("Parakeet model download incomplete")
        import sherpa_onnx
        files = {p: str(next(d.glob(f"{p}.int8.onnx"), None) or next(d.glob(f"{p}*.onnx"))) for p in
                 ("encoder", "decoder", "joiner")}
        Parakeet._rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=files["encoder"], decoder=files["decoder"], joiner=files["joiner"], tokens=str(d / "tokens.txt"),
            num_threads=2, model_type="nemo_transducer", decoding_method="greedy_search")
        return Parakeet._rec

    def transcribe(self, pcm: np.ndarray) -> list:
        with Parakeet._lock:
            rec = self._ensure()
            st = rec.create_stream()
            st.accept_waveform(SR, pcm.astype(np.float32) / 32768.0)
            rec.decode_stream(st)
            tokens, stamps = list(st.result.tokens), [float(t) for t in st.result.timestamps]
        words, cur = [], None
        for tok, t in zip(tokens, stamps):
            piece = tok.replace("▁", " ")
            if piece.startswith(" ") or cur is None:
                if cur:
                    words.append(cur)
                cur = {"w": piece.strip(), "s": round(t, 2), "e": round(t + 0.12, 2)}
            else:
                cur["w"] += piece
                cur["e"] = round(t + 0.12, 2)
        if cur and cur["w"]:
            words.append(cur)
        return [w for w in words if w["w"]]


class Mock(Provider):
    name = "mock"
    model = "mock"

    def transcribe(self, pcm: np.ndarray) -> list:
        secs = len(pcm) / SR
        time.sleep(min(2.0, 0.1 + secs * 0.05))
        return [{"w": f"word{i}", "s": round(i * 0.5, 2), "e": round(i * 0.5 + 0.4, 2)} for i in range(int(secs * 2))]


class NoTranscription(Provider):
    name = "none"

    def transcribe(self, pcm: np.ndarray) -> list:
        return []


_providers: dict[str, Provider] = {}


def provider(name: str | None = None) -> Provider:
    name = (name or settings.transcription_provider).lower()
    if name not in _providers:
        _providers[name] = {"chirp3": Chirp3, "parakeet": Parakeet, "mock": Mock}.get(name, NoTranscription)()
    return _providers[name]


def transcribe(pcm: np.ndarray) -> tuple[list, str]:
    """Transcribe with the main provider, falling back to TRANSCRIPTION_FALLBACK if it fails."""
    main = provider()
    try:
        return main.transcribe(pcm), main.name
    except Exception as first:  # noqa: BLE001
        fb = settings.transcription_fallback.lower()
        if fb in ("", "none") or fb == main.name:
            raise
        try:
            return provider(fb).transcribe(pcm), fb
        except Exception as second:  # noqa: BLE001
            raise TranscriptionError(f"{main.name}: {first}; {fb}: {second}") from second
