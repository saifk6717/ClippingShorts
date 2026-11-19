"""Real FFmpeg: extraction from buffer segments, rendering all aspects, captions, smart crop."""
import subprocess

import pytest

from clippingshorts import captions, db, rolling_buffer, transcript_store
from clippingshorts.clip_extractor import extract
from clippingshorts.clip_renderer import render
from clippingshorts.config import settings
from clippingshorts.jobs import create_edit
from clippingshorts.media_tools import probe
from clippingshorts.smart_crop import best_center

from .conftest import make_video


@pytest.fixture(scope="module")
def stream_with_buffer():
    """A stream whose rolling buffer holds 60 s of real 4-second segments."""
    with db.session() as s:
        st = db.Stream(url="https://www.twitch.tv/media_test", platform="twitch", channel="media_test",
                       creator_name="Media Test")
        s.add(st)
    session = "sessx"
    bid = f"{st.id}.{session}"
    buf = rolling_buffer.RollingBuffer(bid)
    buf.reset()
    src = make_video(settings.work_dir / "long.mp4", seconds=60, loud_at=30)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-c", "copy", "-f",
                    "segment", "-segment_time", "4", "-reset_timestamps", "1", "-segment_format", "mpegts",
                    "-segment_list", str(buf.dir / "p001.csv"), "-segment_list_type", "csv",
                    str(buf.dir / "p001_%06d.ts")], check=True)
    buf.new_part("p001", 0)
    buf.scan()
    transcript_store.add_words(st.id, [{"w": f"word{i}", "s": i * 0.5, "e": i * 0.5 + 0.4} for i in range(120)])
    return st, session, bid


def test_extract_source_clip(stream_with_buffer):
    st, session, bid = stream_with_buffer
    pin = rolling_buffer.add_pin(bid, 10, 50, "test")
    with db.session() as s:
        h = db.Highlight(stream_id=st.id, session_id=session, event_t=30, start_t=25, end_t=40, pin_id=pin,
                         status="accepted", title="Test highlight", hook="TEST")
        s.add(h)
    clip_id = extract(h.id)
    c = db.get(db.Clip, clip_id)
    assert c.status == "ready"
    assert 38 <= c.duration <= 46                 # segment-aligned range around [10, 50]
    d = settings.source_dir / c.rel_dir
    for f in ("source.mp4", "thumb.jpg", "sprite.jpg", "waveform.json", "words.json"):
        assert (d / f).is_file(), f
    assert rolling_buffer.get_pin(bid, pin) is None      # released after extraction
    assert 0 < c.suggested_start < c.suggested_end <= c.duration
    pytest.clip_id = clip_id


@pytest.mark.parametrize("aspect,fit,size", [("9:16", "crop", (1080, 1920)), ("16:9", "blur", (1920, 1080)),
                                              ("1:1", "crop", (1080, 1080)), ("9:16", "blur", (720, 1280))])
def test_render_aspects(stream_with_buffer, aspect, fit, size):
    c = db.get(db.Clip, pytest.clip_id)
    quality = "fast" if size[0] == 720 else "high"
    e = create_edit(c, {"start": 2, "end": 8, "aspect": aspect, "fit": fit, "quality": quality,
                        "audio": {"volume_db": 3, "normalize": True},
                        "captions": {"enabled": True, "style": "hype",
                                     "words": [{"w": "HELLO", "s": 2.5, "e": 3}, {"w": "world", "s": 3.1, "e": 3.6}]},
                        "hook": {"enabled": True, "text": "Edited hook"}})
    progress = []
    out = render(e.id, progress.append)
    f = settings.edited_dir / out["output_rel"]
    info = probe(f)
    assert (info["width"], info["height"]) == size
    assert 5.5 <= info["duration"] <= 6.6
    assert info["has_audio"]
    src = settings.source_dir / c.rel_dir / "source.mp4"
    assert src.is_file()                            # source untouched


def test_captions_scale_to_output():
    words = [{"w": "This", "s": 0, "e": 0.3}, {"w": "is", "s": 0.3, "e": 0.5}, {"w": "insane!", "s": 0.5, "e": 1}]
    for w, h in [(1080, 1920), (1920, 1080), (1080, 1080)]:
        ass = captions.build_ass(words, 0, 2, w, h, hook="WOW")
        assert f"PlayResX: {w}" in ass and "INSANE" in ass and "WOW" in ass
    assert "1\n00:00:00,000 --> " in captions.to_srt(words, 0, 2)


def test_smart_crop_finds_motion(tmp_path):
    # a static left half and a moving box on the right: the crop should move right of centre
    out = tmp_path / "motion.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=gray:s=1280x720:d=6", "-vf",
                    "drawbox=x='1000+40*sin(t*6)':y=300:w=120:h=120:color=white:t=fill", "-c:v", "libx264",
                    "-preset", "ultrafast", str(out)], check=True)
    x = best_center(str(out), 0, 6, 1280, 720, 9 / 16)
    assert x > 0.6


def test_crop_survives_a_resolution_change_mid_clip(tmp_path):
    """Live recordings can switch from 1080p to 720p part-way; the crop must follow the decoded frame size."""
    from clippingshorts.clip_renderer import video_filter
    parts = []
    for i, size in enumerate(("1920x1080", "1280x720")):
        ts = tmp_path / f"p{i}.ts"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"testsrc2=size={size}:rate=30:duration=2", "-c:v", "libx264", "-preset", "ultrafast",
                        "-f", "mpegts", str(ts)], check=True)
        parts.append(ts)
    joined = tmp_path / "joined.ts"
    joined.write_bytes(b"".join(x.read_bytes() for x in parts))
    vf = video_filter({"aspect": "9:16", "fit": "crop", "zoom": 1.0, "crop_y": 0.5}, 1920, 1080, 1080, 1920, 0.5)
    out = tmp_path / "out.mp4"
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(joined),
                        "-filter_complex", vf, "-map", "[v0]", "-c:v", "libx264", "-preset", "ultrafast", str(out)],
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-400:]
    info = probe(out)
    assert (info["width"], info["height"]) == (1080, 1920) and info["duration"] > 3.5
