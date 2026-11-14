"""Burned-in text for a clip, as one ASS subtitle file.

Three kinds of text: the hook (a bold bar at the top for the whole clip), the
credit line (small, bottom-left) and the spoken captions (a few words at a
time). Everything stays inside the area the Shorts player leaves free: nothing
above y=288 or below y=1248; the one exception is the face-camera stack, where the captions
sit lower in the gameplay panel (bottom edge at y=1450) so they do not cover the hook at the seam. Times are relative to the clip start.
"""

from __future__ import annotations

import math
import textwrap
from pathlib import Path

from clips.models import Caption, Layout

BASE_W, BASE_H = 1080, 1920
HOOK_CHARS_PER_LINE = 22
HOOK_MAX_CHARS = 44
CAPTION_MAX_WORDS = 3
CAPTION_MAX_CHARS = 20
BRAND = "&H0000E5FF"  # ASS colours are BGR: this is a bright yellow
BLACK = "&H00000000"
WHITE = "&H00FFFFFF"
STYLE_FORMAT = (
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
    "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
    "Alignment, MarginL, MarginR, MarginV, Encoding"
)


def ass_time(t: float) -> str:
    """Seconds as h:mm:ss.cc."""
    cs = max(0, math.floor(t * 100 + 0.5))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    """Make text safe inside an ASS line: no override blocks, no tags, no line breaks."""
    cleaned = text.replace("\\", "/").replace("{", "(").replace("}", ")")
    return " ".join(cleaned.split())


def wrap_hook(hook: str) -> str:
    """The hook in at most two lines (joined with the ASS line break), cut with
    an ellipsis if it is longer than that."""
    text = " ".join(hook.split())
    lines = textwrap.wrap(text, HOOK_CHARS_PER_LINE, break_long_words=True)
    if len(lines) > 2 or len(text) > HOOK_MAX_CHARS:
        lines = lines[:2]
        lines[-1] = lines[-1][: HOOK_CHARS_PER_LINE - 1].rstrip() + "…"
    return "\\N".join(ass_escape(line) for line in lines)


def _words_with_times(captions: list[Caption], start_s: float, end_s: float) -> list[tuple[float, float, str]]:
    """Each caption split into words, the caption's time shared out by word length.
    Words whose middle falls outside the clip are dropped. Times are source seconds."""
    out: list[tuple[float, float, str]] = []
    for cap in sorted(captions, key=lambda c: c.start_s):
        words = cap.text.split()
        if not words or cap.end_s <= start_s or cap.start_s >= end_s:
            continue
        total = sum(len(w) + 1 for w in words)
        span = max(cap.end_s - cap.start_s, 0.2 * len(words))
        t = cap.start_s
        for w in words:
            d = span * (len(w) + 1) / total
            if start_s <= t + d / 2 < end_s:
                out.append((max(t, start_s), min(t + d, end_s), w))
            t += d
    return out


def caption_groups(captions: list[Caption], start_s: float, end_s: float) -> list[tuple[float, float, str]]:
    """Short groups of words with times relative to the clip start."""
    groups: list[list[tuple[float, float, str]]] = []
    current: list[tuple[float, float, str]] = []
    for tok in _words_with_times(captions, start_s, end_s):
        if current:
            joined = " ".join(w for _, _, w in current)
            gap = tok[0] - current[-1][1]
            if len(current) >= CAPTION_MAX_WORDS or len(joined) + 1 + len(tok[2]) > CAPTION_MAX_CHARS or gap > 0.8:
                groups.append(current)
                current = []
        current.append(tok)
        if tok[2].endswith((".", "!", "?")):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return [(g[0][0] - start_s, g[-1][1] - start_s, " ".join(w for _, _, w in g)) for g in groups]


def _font() -> tuple[str, bool]:
    """(family name for libass, whether a font file was found)."""
    from slice.fonts import find_font, font_family

    path = find_font(bold=True)
    if path is None:
        return "Arial", False
    return font_family(path), True


def _placement(layout: Layout, out_h: int, k: float) -> tuple[int, int, int]:
    """Bottom margins (credit, captions) and top margin (hook), in pixels from the edges."""
    if layout is Layout.FACECAM_STACK:
        # hook just below the seam; captions low in the gameplay panel (bottom edge at y=1450), clear of
        # the hook and of the Shorts player's own labels; credit where it always is
        return out_h - round(1240 * k), out_h - round(1450 * k), round(660 * k)
    return out_h - round(1240 * k), out_h - round(1190 * k), round(300 * k)


def _style(name: str, font: str, size: int, primary: str, outline: str, border: int, outline_w: int,
           align: int, margin_lr: int, margin_v: int) -> str:
    return (f"Style: {name},{font},{size},{primary},&H000000FF,{outline},&H64000000,-1,0,0,0,100,100,0,0,"
            f"{border},{outline_w},0,{align},{margin_lr},{margin_lr},{margin_v},1")


def write_ass(
    out_path: Path,
    *,
    hook: str,
    credit: str,
    captions: list[Caption],
    start_s: float,
    end_s: float,
    layout: Layout,
    out_w: int = BASE_W,
    out_h: int = BASE_H,
) -> Path:
    """Write the subtitle file for one clip and return its path."""
    portrait = out_h > out_w
    k = out_w / BASE_W if portrait else out_h / 1080
    duration = max(0.0, end_s - start_s)
    font, _ = _font()
    margin = round(60 * k)
    if portrait:
        credit_v, caption_v, hook_v = _placement(layout, out_h, k)
    else:  # landscape: only the credit and an optional hook, kept clear of the edges
        credit_v, caption_v, hook_v = round(36 * k), round(120 * k), round(40 * k)
    styles = [
        _style("Hook", font, round(64 * k), BLACK, BRAND, 3, round(18 * k), 8, margin, hook_v),
        _style("Credit", font, round(34 * k), WHITE, BLACK, 1, max(1, round(3 * k)), 1, round(48 * k), credit_v),
        _style("Caption", font, round(66 * k), WHITE, BLACK, 1, max(1, round(5 * k)), 2, margin, caption_v),
    ]
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {out_w}",
        f"PlayResY: {out_h}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        STYLE_FORMAT,
        *styles,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    whole = f"{ass_time(0)},{ass_time(duration)}"
    if hook.strip():
        lines.append(f"Dialogue: 1,{whole},Hook,,0,0,0,,{wrap_hook(hook)}")
    if credit.strip():
        lines.append(f"Dialogue: 0,{whole},Credit,,0,0,0,,{ass_escape(credit)}")
    for begin, end, text in caption_groups(captions or [], start_s, end_s):
        if end > begin:
            lines.append(f"Dialogue: 0,{ass_time(begin)},{ass_time(end)},Caption,,0,0,0,,{ass_escape(text)}")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path
