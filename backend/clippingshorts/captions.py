"""Word-by-word captions and a hook title as an ASS subtitle file, for any output size.

Text is measured with the exact font that FFmpeg's libass will draw, so every word can be placed by hand: the word
being spoken grows and changes colour without pushing its neighbours, long lines shrink to fit, and the hook sits
in a rounded box sized to its text. Sizes are defined for a 1080-wide frame and scaled for other outputs.
"""
from __future__ import annotations

import re
import struct
from pathlib import Path

FONTS_DIR = Path(__file__).resolve().parent / "fonts"

# ASS colours are &HBBGGRR
STYLES = {
    "bounce": {"label": "Bounce", "font": "Poppins ExtraBold", "size": 132, "base": "FFFFFF", "active": "00F0FF",
               "strong": "FFFFFF", "outline": 7, "pop": 112, "upper": True, "per": 3},
    "hype": {"label": "Hype", "font": "Anton", "size": 152, "base": "FFFFFF", "active": "3C3CFF",
             "strong": "00D7FF", "outline": 7, "pop": 114, "upper": True, "per": 2},
    "clean": {"label": "Clean", "font": "Poppins ExtraBold", "size": 100, "base": "FFFFFF", "active": "6BD7FF",
              "strong": "FFFFFF", "outline": 5, "pop": 106, "upper": False, "per": 4},
}
HOOK = {"font": "Poppins ExtraBold", "size": 96, "ink": "14100E", "box": "FFFFFF", "pad_x": 36, "pad_y": 22,
        "radius": 26, "max_w": 900}
FONT_FILES = {"Poppins ExtraBold": "Poppins-ExtraBold.ttf", "Anton": "Anton-Regular.ttf",
              "Archivo Black": "ArchivoBlack-Regular.ttf"}


class Font:
    _cache: dict = {}

    def __init__(self, name: str):
        path = FONTS_DIR / FONT_FILES.get(name, FONT_FILES["Poppins ExtraBold"])
        d = path.read_bytes()
        tables = {}
        for i in range(struct.unpack(">H", d[4:6])[0]):
            tag, _, off, _ = struct.unpack(">4sIII", d[12 + 16 * i: 28 + 16 * i])
            tables[tag] = off
        self.upm = struct.unpack(">H", d[tables[b"head"] + 18: tables[b"head"] + 20])[0]
        o = tables[b"OS/2"]
        self.asc, self.desc = struct.unpack(">HH", d[o + 74: o + 78])
        self.cap = struct.unpack(">h", d[o + 88: o + 90])[0] or int(self.upm * 0.7)
        self.path = str(path)
        self._pil = None

    @classmethod
    def get(cls, name: str) -> "Font":
        if name not in cls._cache:
            cls._cache[name] = cls(name)
        return cls._cache[name]

    def em(self, size: float) -> float:
        return size * self.upm / (self.asc + self.desc)

    def width(self, text: str, size: float) -> float:
        try:
            from PIL import ImageFont
            if self._pil is None:
                self._pil = ImageFont.truetype(self.path, 200)
            return self._pil.getlength(text) * self.em(size) / 200
        except Exception:  # noqa: BLE001 - without Pillow a fair estimate still renders
            return len(text) * self.em(size) * 0.62

    def cap_shift(self, size: float) -> float:
        return ((self.asc - self.desc) / 2 - self.cap / 2) * size / (self.asc + self.desc)


def _ts(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def clean(text) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", re.sub(r"[{}\\]", "", str(text))).strip()


def _key(word: str) -> str:
    return re.sub(r"[^a-z0-9']", "", word.lower())


def _groups(words: list, per: int) -> list:
    out, cur = [], []
    for w in words:
        if cur and (len(cur) >= per or sum(len(x["w"]) for x in cur) + len(w["w"]) > 16
                    or w["s"] - cur[-1]["e"] > 0.45 or cur[-1]["w"][-1:] in ".?!"):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def _drawable(text: str) -> bool:
    """The bundled caption fonts only cover Latin script."""
    return all(ord(c) < 0x250 or c in "’‘“”…–—" for c in text)


def _hook_events(hook: str, dur: float, y: float, W: int, k: float) -> list:
    f, size = Font.get(HOOK["font"]), HOOK["size"] * k
    words = hook.split()
    if not words:
        return []
    max_w = HOOK["max_w"] * k
    lines = [" ".join(words)]
    if f.width(lines[0], size) > max_w and len(words) > 1:
        cut = min(range(1, len(words)),
                  key=lambda i: max(f.width(" ".join(words[:i]), size), f.width(" ".join(words[i:]), size)))
        lines = [" ".join(words[:cut]), " ".join(words[cut:])]
    widest = max(f.width(line, size) for line in lines)
    if widest > max_w:
        size = max(30 * k, size * max_w / widest)
        widest = max(f.width(line, size) for line in lines)
    line_h = f.em(size) * 1.16
    bw, bh = widest + 2 * HOOK["pad_x"] * k, line_h * len(lines) + 2 * HOOK["pad_y"] * k
    x0, y0, r = (W - bw) / 2, y - bh / 2, min(HOOK["radius"] * k, bh / 2)
    x1, y1 = x0 + bw, y0 + bh
    n = lambda v: f"{v:.0f}"  # noqa: E731
    box = (f"m {n(x0 + r)} {n(y0)} l {n(x1 - r)} {n(y0)} b {n(x1)} {n(y0)} {n(x1)} {n(y0)} {n(x1)} {n(y0 + r)} "
           f"l {n(x1)} {n(y1 - r)} b {n(x1)} {n(y1)} {n(x1)} {n(y1)} {n(x1 - r)} {n(y1)} "
           f"l {n(x0 + r)} {n(y1)} b {n(x0)} {n(y1)} {n(x0)} {n(y1)} {n(x0)} {n(y1 - r)} "
           f"l {n(x0)} {n(y0 + r)} b {n(x0)} {n(y0)} {n(x0)} {n(y0)} {n(x0 + r)} {n(y0)}")
    span = f"{_ts(0)},{_ts(dur)}"
    ev = [f"Dialogue: 3,{span},Hook,,0,0,0,,{{\\an7\\pos(0,0)\\fad(160,0)\\c&H{HOOK['box']}&\\p1}}{box}{{\\p0}}"]
    for i, line in enumerate(lines):
        cy = y - line_h * (len(lines) - 1) / 2 + i * line_h - f.cap_shift(size)
        ev.append(f"Dialogue: 4,{span},Hook,,0,0,0,,{{\\an5\\pos({W // 2},{cy:.0f})\\fad(160,0)\\fs{size:.0f}}}{line}")
    return ev


def build_ass(words: list, start: float, end: float, W: int, H: int, style: str = "bounce", hook: str = "",
              highlight: list | None = None, caption_y: float = 0.72, hook_y: float = 0.14,
              font_scale: float = 1.0) -> str:
    """ASS for [start, end] of the source. `words` are on the source clock; output times start at 0.
    caption_y / hook_y are fractions of the frame height."""
    st = STYLES.get(style, STYLES["bounce"])
    k = (W / 1080.0) * font_scale
    if W > H:                                   # landscape: smaller captions relative to width
        k *= 0.62
    strong = {_key(h) for h in (highlight or []) if _key(h)}
    cap_max_w = W * 0.89
    outline = max(2, round(st["outline"] * k))
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{st['font']},{st['size'] * k:.0f},&H00{st['base']},&H00{st['base']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{outline},0,5,0,0,0,1
Style: Hook,{HOOK['font']},{HOOK['size'] * k:.0f},&H00{HOOK['ink']},&H00{HOOK['ink']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dur = end - start
    ev = _hook_events(clean(hook), dur, hook_y * H, W, k) if hook else []
    cap_y = caption_y * H
    f = Font.get(st["font"])
    inside = [dict(w, w=clean(w["w"])) for w in words if start - 0.05 <= w["s"] < end]
    inside = sorted((w for w in inside if w["w"]), key=lambda w: w["s"])
    for i, w in enumerate(inside[:-1]):
        w["e"] = max(w["s"], min(w["e"], inside[i + 1]["s"]))
    groups = _groups(inside, st["per"])
    for gi, g in enumerate(groups):
        g_start = max(0.0, g[0]["s"] - start)
        g_end = min(g[-1]["e"] + 0.25, groups[gi + 1][0]["s"] if gi + 1 < len(groups) else end) - start
        if g_end - g_start < 0.02:
            continue
        texts = [re.sub(r"[.,;:]+$", "", x["w"]) for x in g]
        texts = [t.upper() if st["upper"] else t for t in texts]
        if not all(_drawable(t) for t in texts):
            line = " ".join(texts)
            fs = max(30 * k, min(st["size"] * 0.7 * k, 1500 * k / max(1, len(line)) * 1.6))
            ev.append(f"Dialogue: 1,{_ts(g_start)},{_ts(g_end)},Cap,,60,60,0,,"
                      f"{{\\an5\\pos({W // 2},{cap_y:.0f})\\q0\\fs{fs:.0f}}}{line}")
            continue
        size = st["size"] * k

        def gap_for(sz):
            return f.width("n n", sz) - 2 * f.width("n", sz) + (2 * outline + 6) * sz / (st["size"] * k)
        gap = gap_for(size)
        total = sum(f.width(t, size) for t in texts) + gap * (len(texts) - 1)
        if total > cap_max_w:
            size = max(26 * k, size * cap_max_w / total)
            gap = gap_for(size)
            total = sum(f.width(t, size) for t in texts) + gap * (len(texts) - 1)
        cy = cap_y - f.cap_shift(size)
        x = (W - total) / 2
        for i, (w, t) in enumerate(zip(g, texts)):
            wd = f.width(t, size)
            p = max(104, min(st["pop"], 100 + int(2200 * k / max(wd, 1))))
            pos = f"\\an5\\pos({x + wd / 2:.0f},{cy:.0f})\\fs{size:.0f}"
            x += wd + gap
            a = max(g_start, w["s"] - start)
            b = (g[i + 1]["s"] - start) if i + 1 < len(g) else g_end
            base = st["strong"] if _key(w["w"]) in strong else st["base"]
            if a - g_start >= 0.02:
                ev.append(f"Dialogue: 1,{_ts(g_start)},{_ts(a)},Cap,,0,0,0,,{{{pos}\\c&H{base}&}}{t}")
            if b - a >= 0.02:
                ev.append(f"Dialogue: 2,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{{{pos}\\c&H{st['active']}&\\fscx94\\fscy94"
                          f"\\t(0,90,\\fscx{p}\\fscy{p})\\t(90,200,\\fscx{p - 5}\\fscy{p - 5})}}{t}")
            if g_end - b >= 0.02:
                ev.append(f"Dialogue: 1,{_ts(b)},{_ts(g_end)},Cap,,0,0,0,,{{{pos}\\c&H{base}&\\fscx{p - 5}"
                          f"\\fscy{p - 5}\\t(0,90,\\fscx100\\fscy100)}}{t}")
    return head + "\n".join(ev) + "\n"


def to_srt(words: list, start: float, end: float, per: int = 6) -> str:
    """Plain SRT of the same words (uploaded alongside, or for other editors)."""
    inside = [w for w in words if start <= w["s"] < end]
    out = []
    for i, g in enumerate(_groups(inside, per), 1):
        a, b = g[0]["s"] - start, g[-1]["e"] - start

        def f(t):
            t = max(0, t)
            return f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d},{int(t * 1000) % 1000:03d}"
        out.append(f"{i}\n{f(a)} --> {f(b)}\n{' '.join(clean(x['w']) for x in g)}\n")
    return "\n".join(out)
