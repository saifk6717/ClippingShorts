"""ClippingShorts settings.

Every setting comes from an environment variable so the same image runs on a laptop, a small VM or a larger server.
The resource limits are starting points, not guarantees: measure your own machine (scripts/loadtest.py) and tune.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    # "KEY=   # comment" in an env file reaches us as the comment itself: treat it as empty
    if v is None or v.strip() == "" or v.strip().startswith("#"):
        return default
    return v.strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


def _secret(name: str) -> str:
    """A secret given directly (NAME) or through a file (NAME_FILE, e.g. a Docker secret)."""
    direct = _env(name)
    if direct:
        return direct
    path = _env(name + "_FILE")
    if path and Path(path).is_file():
        return Path(path).read_text(encoding="utf-8").strip()
    return ""


@dataclass(frozen=True)
class Settings:
    # ---------------------------------------------------------------- identity / security
    app_name: str = "ClippingShorts"
    secret_key: str = field(default_factory=lambda: _secret("CLIPPINGSHORTS_SECRET_KEY"))
    admin_password: str = field(default_factory=lambda: _secret("CLIPPINGSHORTS_ADMIN_PASSWORD"))
    viewer_password: str = field(default_factory=lambda: _secret("CLIPPINGSHORTS_VIEWER_PASSWORD"))
    api_token: str = field(default_factory=lambda: _secret("CLIPPINGSHORTS_API_TOKEN"))
    session_hours: int = field(default_factory=lambda: _int("CLIPPINGSHORTS_SESSION_HOURS", 72))
    cookie_secure: bool = field(default_factory=lambda: _bool("CLIPPINGSHORTS_COOKIE_SECURE", False))
    enable_synthetic: bool = field(default_factory=lambda: _bool("CLIPPINGSHORTS_ENABLE_SYNTHETIC", False))

    # ---------------------------------------------------------------- infrastructure
    data_dir: Path = field(default_factory=lambda: Path(_env("CLIPPINGSHORTS_DATA_DIR", "/data/clippingshorts")))
    database_url: str = field(default_factory=lambda: _secret("DATABASE_URL"))
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL", "redis://redis:6379/4"))
    redis_prefix: str = field(default_factory=lambda: _env("REDIS_PREFIX", "cs:"))

    # ---------------------------------------------------------------- resource limits (tune after measuring)
    max_streams: int = field(default_factory=lambda: _int("MAX_STREAMS", 10))
    max_concurrent_transcriptions: int = field(default_factory=lambda: _int("MAX_CONCURRENT_TRANSCRIPTIONS", 3))
    max_concurrent_ai_jobs: int = field(default_factory=lambda: _int("MAX_CONCURRENT_AI_JOBS", 3))
    max_concurrent_render_jobs: int = field(default_factory=lambda: _int("MAX_CONCURRENT_RENDER_JOBS", 1))
    max_concurrent_extractions: int = field(default_factory=lambda: _int("MAX_CONCURRENT_EXTRACTIONS", 2))
    max_ffmpeg_processes: int = field(default_factory=lambda: _int("MAX_FFMPEG_PROCESSES", 14))
    max_concurrent_uploads: int = field(default_factory=lambda: _int("MAX_CONCURRENT_UPLOADS", 1))
    stream_worker_mem_limit_mb: int = field(default_factory=lambda: _int("STREAM_WORKER_MEM_LIMIT_MB", 400))
    cpu_high_pct: float = field(default_factory=lambda: _float("RESOURCE_CPU_HIGH_PCT", 85))
    cpu_critical_pct: float = field(default_factory=lambda: _float("RESOURCE_CPU_CRITICAL_PCT", 95))
    mem_high_pct: float = field(default_factory=lambda: _float("RESOURCE_MEM_HIGH_PCT", 80))
    mem_critical_pct: float = field(default_factory=lambda: _float("RESOURCE_MEM_CRITICAL_PCT", 90))
    disk_high_pct: float = field(default_factory=lambda: _float("RESOURCE_DISK_HIGH_PCT", 85))
    disk_critical_pct: float = field(default_factory=lambda: _float("RESOURCE_DISK_CRITICAL_PCT", 93))
    storage_limit_gb: float = field(default_factory=lambda: _float("STORAGE_LIMIT_GB", 50))

    # queue capacities (backpressure: producers are refused once a queue is this long)
    queue_cap_transcription: int = field(default_factory=lambda: _int("QUEUE_CAP_TRANSCRIPTION", 40))
    queue_cap_highlight: int = field(default_factory=lambda: _int("QUEUE_CAP_HIGHLIGHT", 30))
    queue_cap_extraction: int = field(default_factory=lambda: _int("QUEUE_CAP_EXTRACTION", 20))
    queue_cap_render: int = field(default_factory=lambda: _int("QUEUE_CAP_RENDER", 50))
    queue_cap_publish: int = field(default_factory=lambda: _int("QUEUE_CAP_PUBLISH", 50))
    queue_max_attempts: int = field(default_factory=lambda: _int("QUEUE_MAX_ATTEMPTS", 3))

    # ---------------------------------------------------------------- rolling buffer and source clips
    segment_seconds: int = field(default_factory=lambda: _int("SEGMENT_SECONDS", 4))
    rolling_buffer_seconds: int = field(default_factory=lambda: _int("ROLLING_BUFFER_SECONDS", 180))
    highlight_pre_seconds: int = field(default_factory=lambda: _int("HIGHLIGHT_PRE_SECONDS", 20))
    highlight_post_seconds: int = field(default_factory=lambda: _int("HIGHLIGHT_POST_SECONDS", 20))
    # the saved source recording around a highlight (2-5 minutes); must fit inside the rolling buffer
    source_pre_seconds: int = field(default_factory=lambda: _int("SOURCE_CLIP_PRE_SECONDS", 120))
    source_post_seconds: int = field(default_factory=lambda: _int("SOURCE_CLIP_POST_SECONDS", 40))
    source_max_seconds: int = field(default_factory=lambda: _int("SOURCE_CLIP_MAX_SECONDS", 300))
    max_pins_per_stream: int = field(default_factory=lambda: _int("MAX_PINS_PER_STREAM", 4))
    pin_ttl_seconds: int = field(default_factory=lambda: _int("PIN_TTL_SECONDS", 600))
    source_clip_retention_days: float = field(default_factory=lambda: _float("SOURCE_CLIP_RETENTION_DAYS", 14))
    temp_file_max_age_seconds: int = field(default_factory=lambda: _int("TEMP_FILE_MAX_AGE_SECONDS", 900))
    max_ingest_height: int = field(default_factory=lambda: _int("MAX_INGEST_HEIGHT", 720))

    # ---------------------------------------------------------------- detection
    audio_chunk_seconds: float = field(default_factory=lambda: _float("AUDIO_CHUNK_SECONDS", 10))
    local_trigger_score: float = field(default_factory=lambda: _float("LOCAL_TRIGGER_SCORE", 55))
    candidate_cooldown_seconds: int = field(default_factory=lambda: _int("CANDIDATE_COOLDOWN_SECONDS", 45))
    ai_eval_interval_seconds: int = field(default_factory=lambda: _int("AI_EVAL_INTERVAL_SECONDS", 300))
    merge_gap_seconds: int = field(default_factory=lambda: _int("HIGHLIGHT_MERGE_GAP_SECONDS", 45))
    # how many candidates a stream may send to the AI per hour (bounds Gemini cost); separate from the accepted cap
    max_candidates_per_hour: int = field(default_factory=lambda: _int("MAX_CANDIDATES_PER_HOUR", 40))
    max_highlights_per_hour: int = field(default_factory=lambda: _int("MAX_HIGHLIGHTS_PER_HOUR", 8))
    accept_transcript_min: int = field(default_factory=lambda: _int("ACCEPT_TRANSCRIPT_MIN", 60))
    accept_reaction_min: int = field(default_factory=lambda: _int("ACCEPT_REACTION_MIN", 50))
    # with live transcription off there are no words to judge, so the reaction judge alone decides
    accept_reaction_only_min: int = field(default_factory=lambda: _int("ACCEPT_REACTION_ONLY_MIN", 70))
    # transcribe whole streams live (costly) or only each saved Short (default)
    live_transcription: bool = field(default_factory=lambda: _bool("LIVE_TRANSCRIPTION", False))
    # Shorts are exactly HIGHLIGHT_PRE_SECONDS before and HIGHLIGHT_POST_SECONDS after the detected moment
    fixed_short_range: bool = field(default_factory=lambda: _bool("FIXED_SHORT_RANGE", True))
    accept_single_judge_min: int = field(default_factory=lambda: _int("ACCEPT_SINGLE_JUDGE_MIN", 88))

    # ---------------------------------------------------------------- Google (Gemini + Speech-to-Text)
    google_credentials: str = field(default_factory=lambda: _env("GOOGLE_APPLICATION_CREDENTIALS"))
    google_project: str = field(default_factory=lambda: _env("GOOGLE_CLOUD_PROJECT"))
    ai_mode: str = field(default_factory=lambda: _env("AI_MODE", "gemini"))  # gemini | local | mock
    gemini_model: str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-3.8-flash"))
    gemini_location: str = field(default_factory=lambda: _env("GEMINI_LOCATION", "global"))
    gemini_thinking_level: str = field(default_factory=lambda: _env("GEMINI_THINKING_LEVEL", ""))
    gemini_timeout_seconds: float = field(default_factory=lambda: _float("GEMINI_TIMEOUT_SECONDS", 90))
    gemini_price_in_per_m: float = field(default_factory=lambda: _float("GEMINI_PRICE_INPUT_PER_M_TOKENS", 0.0))
    gemini_price_out_per_m: float = field(default_factory=lambda: _float("GEMINI_PRICE_OUTPUT_PER_M_TOKENS", 0.0))
    gemini_daily_request_budget: int = field(default_factory=lambda: _int("GEMINI_DAILY_REQUEST_BUDGET", 3000))
    transcription_provider: str = field(default_factory=lambda: _env("TRANSCRIPTION_PROVIDER", "chirp3"))  # chirp3|parakeet|mock|none
    transcription_fallback: str = field(default_factory=lambda: _env("TRANSCRIPTION_FALLBACK", "none"))
    speech_location: str = field(default_factory=lambda: _env("SPEECH_LOCATION", "us"))
    speech_language: str = field(default_factory=lambda: _env("SPEECH_LANGUAGE", "en-US"))
    speech_daily_minutes_budget: float = field(default_factory=lambda: _float("SPEECH_DAILY_MINUTES_BUDGET", 1500))
    speech_price_per_minute: float = field(default_factory=lambda: _float("SPEECH_PRICE_PER_MINUTE", 0.0))
    parakeet_model_dir: str = field(default_factory=lambda: _env("PARAKEET_MODEL_DIR", ""))

    # ---------------------------------------------------------------- YouTube (chat + publishing)
    youtube_client_id: str = field(default_factory=lambda: _secret("YOUTUBE_CLIENT_ID"))
    youtube_client_secret: str = field(default_factory=lambda: _secret("YOUTUBE_CLIENT_SECRET"))
    youtube_refresh_token: str = field(default_factory=lambda: _secret("YOUTUBE_REFRESH_TOKEN"))
    youtube_channel_id: str = field(default_factory=lambda: _env("YOUTUBE_CHANNEL_ID"))
    youtube_category_id: str = field(default_factory=lambda: _env("YOUTUBE_CATEGORY_ID", "20"))
    youtube_chat_daily_quota_budget: int = field(default_factory=lambda: _int("YOUTUBE_CHAT_DAILY_QUOTA_BUDGET", 4000))
    youtube_chat_min_poll_seconds: float = field(default_factory=lambda: _float("YOUTUBE_CHAT_MIN_POLL_SECONDS", 8))
    auto_upload: str = field(default_factory=lambda: _env("AUTO_UPLOAD", "off"))  # off | private | unlisted | public
    auto_upload_min_confidence: int = field(default_factory=lambda: _int("AUTO_UPLOAD_MIN_CONFIDENCE", 85))
    ytdlp_cookies_file: str = field(default_factory=lambda: _env("YTDLP_COOKIES_FILE"))
    ytdlp_proxy: str = field(default_factory=lambda: _env("YTDLP_PROXY"))

    # ---------------------------------------------------------------- paths
    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "temp"

    @property
    def buffers_dir(self) -> Path:
        return self.temp_dir / "rolling_buffers"

    @property
    def audio_dir(self) -> Path:
        return self.temp_dir / "audio"

    @property
    def work_dir(self) -> Path:
        return self.temp_dir / "work"

    @property
    def source_dir(self) -> Path:
        return self.data_dir / "source_clips"

    @property
    def edited_dir(self) -> Path:
        return self.data_dir / "edited_clips"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbnails"

    @property
    def subtitles_dir(self) -> Path:
        return self.data_dir / "subtitles"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.data_dir / 'clippingshorts.db').as_posix()}"

    def ensure_dirs(self) -> None:
        for d in (self.buffers_dir, self.audio_dir, self.work_dir, self.source_dir, self.edited_dir,
                  self.thumbs_dir, self.subtitles_dir, self.exports_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
