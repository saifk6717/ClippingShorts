"""Test setup: an empty temporary data folder, SQLite, and a private key prefix on Redis (deleted afterwards)."""
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="cs-test-")
os.environ.update({
    "CLIPPINGSHORTS_DATA_DIR": _TMP,
    "DATABASE_URL": "",
    "REDIS_URL": os.environ.get("TEST_REDIS_URL", "redis://redis:6379/15"),
    "REDIS_PREFIX": f"cstest:{uuid.uuid4().hex[:8]}:",
    "CLIPPINGSHORTS_SECRET_KEY": "test-secret-key-" + "x" * 32,
    "CLIPPINGSHORTS_ADMIN_PASSWORD": "admin-password-123",
    "CLIPPINGSHORTS_VIEWER_PASSWORD": "viewer-password-123",
    "CLIPPINGSHORTS_ENABLE_SYNTHETIC": "1",
    "AI_MODE": "mock",
    "TRANSCRIPTION_PROVIDER": "mock",
    "MAX_STREAMS": "10",
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clippingshorts import db  # noqa: E402
from clippingshorts import redis_bus as rb  # noqa: E402
from clippingshorts.config import settings  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _env():
    settings.ensure_dirs()
    db.init_db()
    yield
    keys = list(rb.r().scan_iter(settings.redis_prefix + "*", count=500))
    if keys:
        rb.r().delete(*keys)


def make_video(path: Path, seconds: float = 12, size: str = "1280x720", loud_at: float | None = None) -> Path:
    """A small test video (pattern + tone), optionally with a loud burst."""
    path.parent.mkdir(parents=True, exist_ok=True)
    expr = "0.05*sin(2*PI*440*t)"
    if loud_at is not None:
        expr += f"+if(between(t\\,{loud_at}\\,{loud_at + 2})\\,0.6*sin(2*PI*880*t)\\,0)"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size={size}:rate=30:duration={seconds}", "-f", "lavfi", "-i",
                    f"aevalsrc='{expr}':s=48000:d={seconds}", "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path
