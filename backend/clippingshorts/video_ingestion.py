"""Turning a live stream link into a running FFmpeg capture.

yt-dlp finds the stream's playable address (both Twitch and YouTube Live). FFmpeg then copies the stream, without
re-encoding, into short MPEG-TS segments for the rolling buffer, and at the same time decodes only the audio to
16 kHz mono PCM on stdout for analysis. Copying keeps CPU low; nothing is held in RAM except one second of audio.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import settings

SR = 16000


class NotLive(Exception):
    """The channel is offline, the stream ended, or the site refused to hand over the stream."""


@dataclass
class Resolved:
    urls: list
    title: str
    uploader: str
    video_id: str
    is_live: bool
    height: int


class _Quiet:
    def debug(self, m): pass
    def info(self, m): pass
    def warning(self, m): pass
    def error(self, m): pass


def _private_cookies(shared: str) -> str:
    """A private copy of the cookies file for this process. yt-dlp writes refreshed cookies back to the file it was
    given; with one shared file, several stream workers writing at once could corrupt it."""
    import os
    import shutil
    import tempfile
    own = Path(tempfile.gettempdir()) / f"cs-cookies-{os.getpid()}.txt"
    if not own.exists() or own.stat().st_mtime < Path(shared).stat().st_mtime:
        shutil.copyfile(shared, own)
        os.chmod(own, 0o600)
    return str(own)


def resolve(url: str, platform: str) -> Resolved:
    """Ask yt-dlp for the live stream's address. Raises NotLive when it is not live or cannot be fetched."""
    if platform == "synthetic":
        from . import synthetic
        return synthetic.resolve(url)
    import yt_dlp
    h = settings.max_ingest_height
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "logger": _Quiet(),
            "format": f"b[height<={h}]/bv*[height<={h}]+ba/b", "socket_timeout": 20}
    if settings.ytdlp_cookies_file and Path(settings.ytdlp_cookies_file).is_file():
        opts["cookiefile"] = _private_cookies(settings.ytdlp_cookies_file)
    if settings.ytdlp_proxy:
        opts["proxy"] = settings.ytdlp_proxy
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001 - yt-dlp raises many kinds
        msg = str(e)
        if "confirm you" in msg and "bot" in msg:
            raise NotLive("YouTube refused this server (bot check). Set YTDLP_COOKIES_FILE or YTDLP_PROXY.") from e
        raise NotLive(msg[-240:]) from e
    if not info or not info.get("is_live"):
        raise NotLive("This channel is not live right now.")
    fmts = info.get("requested_formats") or [info]
    title = info.get("title") or ""
    if platform == "twitch" and info.get("description"):
        title = str(info["description"]).splitlines()[0][:150]       # Twitch keeps the stream title here
    return Resolved(urls=[f["url"] for f in fmts], title=title,
                    uploader=info.get("uploader") or info.get("channel") or "",
                    video_id=str(info.get("id") or ""), is_live=True, height=int(info.get("height") or 0))


def capture_command(res: Resolved, seg_dir: Path, part: str, segment_seconds: int, realtime: bool = False) -> list:
    """FFmpeg: copy video+audio into segments, and decode the audio to PCM on stdout.

    aresample=async=1 fills gaps in the audio with silence, so the count of audio samples keeps matching the
    stream's own clock: second N of audio is second N of the segments, which is what clip extraction relies on.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    for u in res.urls:
        if u.startswith(("http://", "https://")):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
                    "-rw_timeout", "20000000"]
        if realtime:
            cmd += ["-re", "-stream_loop", "-1"]
        cmd += ["-i", u]
    a_in = "1:a:0" if len(res.urls) > 1 else "0:a:0"
    cmd += ["-map", "0:v:0", "-map", a_in, "-c", "copy", "-f", "segment",
            "-segment_time", str(segment_seconds), "-reset_timestamps", "1", "-segment_format", "mpegts",
            "-segment_list", str(seg_dir / f"{part}.csv"), "-segment_list_type", "csv",
            "-segment_list_flags", "live", str(seg_dir / f"{part}_%06d.ts"),
            "-map", a_in, "-vn", "-af", "aresample=async=1:first_pts=0", "-ac", "1", "-ar", str(SR),
            "-f", "s16le", "pipe:1"]
    return cmd


def start_capture(cmd: list, log_path: Path) -> subprocess.Popen:
    log = open(log_path, "ab", buffering=0)  # noqa: SIM115 - handed to the child, closed by the caller
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, stdin=subprocess.DEVNULL, bufsize=0)
