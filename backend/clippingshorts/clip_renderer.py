"""Renders an edit of a source clip with FFmpeg: trim, aspect (9:16 / 16:9 / 1:1), crop or blurred fit, smart crop,
captions and hook, volume and loudness normalisation, quality preset.

The source clip is only ever read. Each render writes a new file under edited_clips/<clip>/ (temp name, renamed
when complete). Progress is reported from FFmpeg's -progress output. Renders run at lower CPU priority than
stream ingestion, with a capped thread count, inside the renderer worker's concurrency limit.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from . import captions, clip_storage, db
from .config import settings
from .media_tools import MediaError, probe
from .smart_crop import best_center

ASPECTS = {"9:16": (9, 16), "16:9": (16, 9), "1:1": (1, 1)}
SIZES = {("9:16", "high"): (1080, 1920), ("9:16", "fast"): (720, 1280),
         ("16:9", "high"): (1920, 1080), ("16:9", "fast"): (1280, 720),
         ("1:1", "high"): (1080, 1080), ("1:1", "fast"): (720, 720)}
RENDER_THREADS = int(os.environ.get("RENDER_THREADS", "2"))
RENDER_TIMEOUT = int(os.environ.get("RENDER_TIMEOUT_SECONDS", "600"))      # a 40 s Short takes 1-3 min

DEFAULT_PARAMS = {
    "start": 0.0, "end": 0.0, "aspect": "9:16", "fit": "crop", "crop_x": None, "crop_y": 0.5, "zoom": 1.0,
    "smart_crop": True, "quality": "high",
    "captions": {"enabled": True, "style": "bounce", "position": 0.72, "font_scale": 1.0, "words": None,
                 "highlight_words": []},
    "hook": {"enabled": True, "text": "", "position": 0.14},
    "audio": {"volume_db": 0.0, "normalize": True, "mute": False},
    "title": "", "description": "", "hashtags": [],
}


def merge_params(base: dict, override: dict | None) -> dict:
    out = {**base}
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **{a: b for a, b in v.items()}}
        else:
            out[k] = v
    return out


def even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def video_filter(p: dict, src_w: int, src_h: int, out_w: int, out_h: int, crop_x: float) -> str:
    aw, ah = ASPECTS.get(p["aspect"], (9, 16))
    target = aw / ah
    zoom = max(1.0, min(3.0, float(p.get("zoom") or 1.0)))
    if p.get("fit") == "blur" and abs(src_w / max(src_h, 1) - target) > 0.02:
        # whole frame, centred, over a blurred and darkened copy that fills the rest
        return (f"[0:v]split=2[a][b];"
                f"[a]scale={even(out_w / 4)}:{even(out_h / 4)}:force_original_aspect_ratio=increase,"
                f"crop={even(out_w / 4)}:{even(out_h / 4)},boxblur=6:2,scale={out_w}:{out_h},"
                f"eq=brightness=-0.12:saturation=1.1[bg];"
                f"[b]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
                f"scale=trunc(iw/2)*2:trunc(ih/2)*2[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30[v0]")
    if src_w / max(src_h, 1) > target:
        cw, ch = src_h * target, src_h
    else:
        cw, ch = src_w, src_w / target
    # sizes are expressions of the frame actually decoded (iw/ih), not of the probed size: a live recording can
    # change resolution part-way (Twitch switching 1080p -> 720p), and a fixed 608x1080 crop then fails on 720p
    cy = float(p.get("crop_y") if p.get("crop_y") is not None else 0.5)
    t = f"{target:.6f}"
    w = f"trunc(min(iw\\,ih*{t})/{zoom:.4f}/2)*2"
    h = f"trunc(min(ih\\,iw/{t})/{zoom:.4f}/2)*2"
    x = f"max(0\\,min(iw-ow\\,iw*{crop_x:.4f}-ow/2))"
    y = f"max(0\\,min(ih-oh\\,ih*{cy:.4f}-oh/2))"
    return f"[0:v]crop=w={w}:h={h}:x={x}:y={y},scale={out_w}:{out_h}:flags=lanczos,setsar=1,fps=30[v0]"


def audio_filter(p: dict) -> str:
    a = p.get("audio") or {}
    if a.get("mute"):
        return "volume=0"
    parts = []
    vol = float(a.get("volume_db") or 0)
    if abs(vol) > 0.01:
        parts.append(f"volume={max(-30.0, min(15.0, vol)):.1f}dB")
    if a.get("normalize", True):
        parts.append("loudnorm=I=-14:TP=-1.5:LRA=11")
    parts.append("aresample=48000")
    return ",".join(parts)


def render(edit_id: str, on_progress: Callable[[float], None] | None = None) -> dict:
    edit = db.get(db.Edit, edit_id)
    if edit is None:
        raise MediaError("edit not found")
    clip = db.get(db.Clip, edit.clip_id)
    if clip is None or clip.status != "ready":
        raise MediaError("source clip is not available")
    src = clip_storage.source_file(clip)
    if not src.is_file():
        raise MediaError("source file is missing")
    p = merge_params(DEFAULT_PARAMS, edit.params)
    info = probe(src)
    start = max(0.0, float(p.get("start") or 0))
    end = float(p.get("end") or 0) or info["duration"]
    end = min(end, info["duration"])
    if end - start < 1:
        raise MediaError("the selected range is shorter than one second")
    out_w, out_h = SIZES.get((p["aspect"], p.get("quality", "high")), (1080, 1920))
    aw, ah = ASPECTS.get(p["aspect"], (9, 16))
    crop_x = p.get("crop_x")
    if crop_x is None:
        crop_x = best_center(str(src), start, end, info["width"], info["height"], aw / ah, p.get("zoom") or 1) \
            if p.get("smart_crop", True) else 0.5
    crop_x = min(1.0, max(0.0, float(crop_x)))

    cap = p.get("captions") or {}
    hook = p.get("hook") or {}
    words = cap.get("words") if cap.get("words") is not None else clip_storage.read_words(clip)
    ass_text = captions.build_ass(words if cap.get("enabled", True) else [], start, end, out_w, out_h,
                                  style=cap.get("style", "bounce"),
                                  hook=(hook.get("text") or clip.hook or "") if hook.get("enabled", True) else "",
                                  highlight=cap.get("highlight_words") or [],
                                  caption_y=float(cap.get("position", 0.72)),
                                  hook_y=float(hook.get("position", 0.14)),
                                  font_scale=float(cap.get("font_scale", 1.0)))
    out_dir = settings.edited_dir / clip.id
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"v{edit.version:03d}-{edit.id}"
    final = out_dir / f"{name}.mp4"
    sub_final = settings.subtitles_dir / clip.id / f"{name}.ass"
    clip_storage.atomic_write_text(sub_final, ass_text)
    clip_storage.atomic_write_text(sub_final.with_suffix(".srt"), captions.to_srt(words, start, end))

    # FFmpeg's subtitle filter is given short relative paths inside a private work folder (no escaping issues)
    work = Path(tempfile.mkdtemp(prefix="r", dir=settings.work_dir))
    try:
        shutil.copyfile(sub_final, work / "c.ass")
        (work / "f").mkdir()
        for fnt in captions.FONTS_DIR.glob("*.ttf"):
            shutil.copyfile(fnt, work / "f" / fnt.name)
        vf = video_filter(p, info["width"], info["height"], out_w, out_h, crop_x) + ";[v0]ass=c.ass:fontsdir=f[v]"
        crf, preset = ("20", "veryfast") if p.get("quality", "high") == "high" else ("24", "superfast")
        tmp_out = work / "out.mp4"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-progress", "pipe:1", "-nostats",
               "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(src),
               "-filter_complex", vf, "-map", "[v]"]
        if info["has_audio"]:
            cmd += ["-map", "0:a:0", "-af", audio_filter(p), "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
        cmd += ["-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p", "-profile:v", "high",
                "-threads", str(RENDER_THREADS), "-movflags", "+faststart", str(tmp_out)]
        t0 = time.time()
        proc = subprocess.Popen(cmd, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                preexec_fn=lambda: os.nice(10))
        total = end - start
        last = 0.0
        # error output is read all the time (a full pipe would freeze FFmpeg), and a timer stops a render that
        # hangs without printing anything (the progress loop alone can't notice that)
        err_tail: list[str] = []

        def drain():
            for ln in proc.stderr:
                err_tail.append(ln)
                del err_tail[:-40]
        threading.Thread(target=drain, daemon=True).start()
        timed_out = threading.Event()

        def watchdog():
            if proc.poll() is None:
                timed_out.set()
                proc.kill()
        timer = threading.Timer(RENDER_TIMEOUT, watchdog)
        timer.daemon = True
        timer.start()
        try:
            for line in proc.stdout:
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    try:
                        done = int(line.split("=", 1)[1]) / 1e6
                    except ValueError:
                        continue
                    pct = max(0.0, min(99.0, done / total * 100))
                    if on_progress and pct - last >= 2:
                        last = pct
                        on_progress(pct)
                if time.time() - t0 > RENDER_TIMEOUT:
                    proc.kill()
                    raise MediaError(f"render took longer than {RENDER_TIMEOUT}s and was stopped")
            proc.wait(timeout=60)
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
        if timed_out.is_set():
            raise MediaError(f"render took longer than {RENDER_TIMEOUT}s and was stopped")
        if proc.returncode != 0:
            time.sleep(0.2)
            raise MediaError("FFmpeg render failed: " + "".join(err_tail)[-600:])
        tmp_final = clip_storage.tmp_name(final)
        shutil.move(str(tmp_out), tmp_final)
        os.replace(tmp_final, final)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{min(1.0, total / 2):.2f}",
                        "-i", str(final), "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "4",
                        str(out_dir / f"{name}.jpg")], capture_output=True, timeout=60)
        return {"output_rel": f"{clip.id}/{name}.mp4", "duration": round(total, 2),
                "render_seconds": round(time.time() - t0, 2), "crop_x": round(crop_x, 3)}
    finally:
        shutil.rmtree(work, ignore_errors=True)
