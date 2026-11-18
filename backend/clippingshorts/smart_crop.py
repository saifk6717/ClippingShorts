"""Where to crop a landscape video for a vertical (or square) output.

A few low-resolution grey frames are sampled from the chosen range. Each column of the picture gets a score from
motion (what changes between frames) plus detail (edges), with a slight preference for the centre. The crop window
with the highest total score wins. Cheap: about 1 frame per second at 160 px wide, a few MB of RAM at most.
"""
from __future__ import annotations

import subprocess

import numpy as np

SAMPLE_W = 160


def best_center(src: str, start: float, end: float, src_w: int, src_h: int, target_aspect: float,
                zoom: float = 1.0) -> float:
    if src_w <= 0 or src_h <= 0:
        return 0.5
    sample_h = max(2, int(round(SAMPLE_W * src_h / src_w / 2)) * 2)
    dur = max(1.0, end - start)
    fps = min(2.0, 30.0 / dur) if dur > 15 else 1.0
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}",
                        "-i", src, "-vf", f"fps={fps:.3f},scale={SAMPLE_W}:{sample_h},format=gray",
                        "-f", "rawvideo", "pipe:1"], capture_output=True, timeout=120)
    frame = SAMPLE_W * sample_h
    if p.returncode != 0 or len(p.stdout) < frame * 2:
        return 0.5
    n = len(p.stdout) // frame
    v = np.frombuffer(p.stdout[: n * frame], dtype=np.uint8).reshape(n, sample_h, SAMPLE_W).astype(np.float32)
    motion = np.abs(np.diff(v, axis=0)).mean(axis=(0, 1))
    edges = np.abs(np.diff(v, axis=2)).mean(axis=(0, 1))
    edges = np.append(edges, edges[-1])
    score = motion / (motion.mean() + 1e-6) + 0.5 * edges / (edges.mean() + 1e-6)
    x = np.linspace(-1, 1, SAMPLE_W)
    score = score * (1.0 - 0.25 * x ** 2)                     # gentle centre bias
    crop_w = min(SAMPLE_W, int(round(sample_h * target_aspect / max(zoom, 1.0))))
    if crop_w >= SAMPLE_W:
        return 0.5
    csum = np.concatenate([[0], np.cumsum(score)])
    sums = csum[crop_w:] - csum[:-crop_w]
    left = int(np.argmax(sums))
    return float((left + crop_w / 2) / SAMPLE_W)
