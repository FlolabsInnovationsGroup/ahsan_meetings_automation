"""Picks a good, safe frame for a thumbnail and cuts it as a clean 1280x720 base
JPEG (the text and badges go on top later, in other modules).

One cheap ffmpeg pass over the keyframes of the source scores every keyframe
(brightness, detail, blur, edges, a tiny grey copy). From those numbers we tell
shared screens from camera views and blank frames, pick the best frame near a
moment, and crop a shared screen down to the part that changes. A whole frame
would leak the presenter's bookmarks bar, local file paths and Zoom's clock, so
only the changing part of a shared screen is ever used. Faces are left out
unless the owner switches them on; with nothing safe to show, the answer is
'card' (a plain designed card, made elsewhere).

No text is drawn here and nothing is uploaded.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from core.config import get_settings
from core.proc import run_checked

# ---------------------------------------------------------------- constants
# These cut-offs come from ONE 101-minute recording (shares had ylow 41-50 and
# blur 3.98-4.71; the gallery view had ylow 1 and blur 6.0-6.7). Re-check them
# if another recording classifies badly.
BLANK_YAVG = 20.0
BLANK_ENTROPY = 0.2
SHARE_YLOW_MIN = 20.0
SHARE_BLUR_MAX = 5.2

SMALL_W, SMALL_H = 64, 34  # the grey copy kept per keyframe
CHANGE_STD = 12.0  # a pixel "changes" when its std across the run is above this
CHANGE_LINE_FRACTION = 0.10  # a row/column counts when more than this share changes
MIN_RUN = 3  # fewer share keyframes than this: use the fixed safe crop
BOTTOM_DROP = 0.04  # Zoom's timestamp lives here
SAFE_TOP, SAFE_LEFT = 0.20, 0.05  # fixed safe crop (bottom is BOTTOM_DROP)
MIN_WINDOW_HEIGHT = 0.50  # of the frame
CANDIDATE_MARGIN_S = 20.0
INSIDE_BONUS = 10.0
SHARE_BONUS = 25.0

THUMB_W, THUMB_H = 1280, 720

# ----------------------------------------------------- thumbs.json row schema
VIDEO_FULL, VIDEO_SHORT, VIDEO_TOPIC = "full", "short", "topic"
VIDEOS = (VIDEO_FULL, VIDEO_SHORT, VIDEO_TOPIC)
KIND_SHARE, KIND_CAMERA, KIND_CARD = "share", "camera", "card"
KINDS = (KIND_SHARE, KIND_CAMERA, KIND_CARD)
THUMBS_FILE = "thumbs.json"
THUMBS_DIR = "thumbnails"


def row(
    video: str,
    index: int,
    source_start: float,
    source_end: float,
    t: float | None,
    kind: str,
    crop: list[int] | tuple[int, ...] | None = None,
    file: str | None = None,
    scores: dict | None = None,
) -> dict:
    """One entry of thumbs.json. `index` is the Short or topic number, or the
    candidate rank for the full video. `file` is 'thumbnails/base_....jpg' or
    None (a card has no base picture)."""
    if video not in VIDEOS:
        raise ValueError(f"video must be one of {VIDEOS}, not {video!r}")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, not {kind!r}")
    return {
        "video": video,
        "index": int(index),
        "source_start": float(source_start),
        "source_end": float(source_end),
        "t": None if t is None else float(t),
        "kind": kind,
        "crop": None if crop is None else [int(v) for v in crop],
        "file": file,
        "scores": dict(scores or {}),
    }


# --------------------------------------------------------------------- types
@dataclass
class Row:
    t: float
    yavg: float
    ylow: float
    entropy: float
    blur: float
    edge: float
    small: np.ndarray = field(repr=False, compare=False)  # (34, 64) uint8


@dataclass
class Pick:
    t: float | None
    kind: str  # 'share' | 'camera' | 'card'
    crop: tuple[int, int, int, int] | None
    score: float


# ------------------------------------------------------------------- scoring
def _blocks(text: str) -> list[tuple[float, dict[str, float]]]:
    """Split ffmpeg metadata=print text into (time, {key: value}) per frame."""
    out: list[tuple[float, dict[str, float]]] = []
    cur: dict[str, float] | None = None
    t = 0.0
    for line in text.splitlines():
        m = re.match(r"frame:\d+\s+pts:\S+\s+pts_time:(\S+)", line)
        if m:
            if cur is not None:
                out.append((t, cur))
            t, cur = float(m.group(1)), {}
            continue
        if cur is not None and "=" in line:
            k, _, v = line.strip().partition("=")
            try:
                cur[k] = float(v)
            except ValueError:
                pass
    if cur is not None:
        out.append((t, cur))
    return out


def parse_rows(metrics: str, edges: str, raw: bytes) -> list[Row]:
    """Join the two metadata texts and the raw grey frames into rows."""
    size = SMALL_W * SMALL_H
    a, b = _blocks(metrics), _blocks(edges)
    n = min(len(a), len(b), len(raw) // size)
    rows: list[Row] = []
    for i in range(n):
        t, m = a[i]
        e = b[i][1]
        small = np.frombuffer(raw, np.uint8, size, i * size).reshape(SMALL_H, SMALL_W).copy()
        rows.append(
            Row(
                t=t,
                yavg=m.get("lavfi.signalstats.YAVG", 0.0),
                ylow=m.get("lavfi.signalstats.YLOW", 0.0),
                entropy=m.get("lavfi.entropy.entropy.normal.Y", 0.0),
                blur=m.get("lavfi.blur", 0.0),
                edge=e.get("lavfi.signalstats.YAVG", 0.0),
                small=small,
            )
        )
    return rows


_GRAPH = (
    "[0:v]scale=480:-2,split=3[a][b][c];"
    "[a]signalstats,entropy,blurdetect=block_width=32:block_height=32,"
    "metadata=print:file=m.txt[o1];"
    "[b]edgedetect=low=0.1:high=0.3,signalstats,metadata=print:file=e.txt[o2];"
    f"[c]scale={SMALL_W}:{SMALL_H},format=gray[o3]"
)


def score_keyframes(source: str | Path, timeout: float = 120) -> list[Row]:
    """Score every keyframe of the source in one ffmpeg pass (about 4 s for a
    101-minute recording)."""
    src = str(Path(source).resolve())
    ffmpeg = get_settings().ffmpeg_bin
    with tempfile.TemporaryDirectory(prefix="hc_kf_") as tmp:
        # Relative metadata names and cwd=tmp: a Windows drive colon would break
        # the filter option parsing.
        run_checked(
            [
                ffmpeg, "-hide_banner", "-nostdin", "-y", "-skip_frame", "nokey", "-i", src,
                "-filter_complex", _GRAPH,
                "-map", "[o1]", "-f", "null", "-",
                "-map", "[o2]", "-f", "null", "-",
                "-map", "[o3]", "-f", "rawvideo", "-fps_mode", "passthrough", "g.raw",
            ],
            timeout=timeout,
            cwd=tmp,
        )
        p = Path(tmp)
        return parse_rows(
            (p / "m.txt").read_text(errors="replace"),
            (p / "e.txt").read_text(errors="replace"),
            (p / "g.raw").read_bytes(),
        )


def classify(r: Row) -> str:
    """'blank', 'share' (a shared screen) or 'camera' (people on camera)."""
    if r.yavg < BLANK_YAVG or r.entropy < BLANK_ENTROPY:
        return "blank"
    if r.ylow >= SHARE_YLOW_MIN and r.blur < SHARE_BLUR_MAX:
        return KIND_SHARE
    return KIND_CAMERA


def frame_score(r: Row, inside: bool) -> float:
    s = 3 * r.edge + 10 * r.entropy - 2 * r.blur
    if classify(r) == KIND_SHARE:
        s += SHARE_BONUS
    if inside:
        s += INSIDE_BONUS
    return s


# --------------------------------------------------------------------- crop
def _even(v: float) -> int:
    return int(v) // 2 * 2


def content_box(
    rows: list[Row], src_w: int, src_h: int, focus: Row | None = None
) -> tuple[int, int, int, int]:
    """Crop (x, y, w, h) in source pixels for a run of share keyframes: the
    part of the screen that changes, as a 16:9 window. Fewer than MIN_RUN
    keyframes gives the fixed safe crop (top 20%, bottom 4%, left 5% removed)."""
    usable_h = src_h * (1 - BOTTOM_DROP)
    if len(rows) >= MIN_RUN:
        stack = np.stack([r.small.astype(np.float32) for r in rows])
        mask = stack.std(axis=0) > CHANGE_STD
        rws = np.where(mask.mean(axis=1) > CHANGE_LINE_FRACTION)[0]
        cls = np.where(mask.mean(axis=0) > CHANGE_LINE_FRACTION)[0]
    else:
        rws = cls = np.array([], dtype=int)
    if len(rws) and len(cls):
        sx, sy = src_w / SMALL_W, src_h / SMALL_H
        bx0, bx1 = cls[0] * sx, (cls[-1] + 1) * sx
        by0, by1 = rws[0] * sy, min((rws[-1] + 1) * sy, usable_h)
    else:
        bx0, by0, bx1, by1 = src_w * SAFE_LEFT, src_h * SAFE_TOP, float(src_w), usable_h
    if by1 <= by0 or bx1 <= bx0:
        bx0, by0, bx1, by1 = src_w * SAFE_LEFT, src_h * SAFE_TOP, float(src_w), usable_h

    # Window: as tall as the box (at least half the frame), 16:9, inside the frame.
    h = max(by1 - by0, src_h * MIN_WINDOW_HEIGHT)
    # Never reach above the box top (static chrome) or into the timestamp strip.
    h = min(h, usable_h - by0)
    w = h * 16 / 9
    if w > src_w:
        w = float(src_w)
        h = w * 9 / 16

    # Centre on the edge centroid of the chosen frame (the box centre if unknown).
    cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
    if focus is not None:
        g = np.abs(np.diff(focus.small.astype(np.float32), axis=1))[:-1, :] + np.abs(
            np.diff(focus.small.astype(np.float32), axis=0)
        )[:, :-1]
        if g.sum() > 0:
            ys, xs = np.mgrid[0 : g.shape[0], 0 : g.shape[1]]
            cx = float((xs * g).sum() / g.sum() + 0.5) * src_w / SMALL_W
            cy = float((ys * g).sum() / g.sum() + 0.5) * src_h / SMALL_H

    def place(c: float, size: float, lo: float, hi: float, f_lo: float, f_hi: float) -> float:
        # Stay inside the box when the window fits in it, else inside the frame.
        if size > hi - lo + 0.5:
            lo, hi = f_lo, f_hi
        return max(min(c - size / 2, hi - size), lo)

    x = place(cx, w, bx0, bx1, 0.0, float(src_w))
    y = place(cy, h, by0, by1, by0, usable_h)  # never above the box top
    cw, ch = _even(w), _even(h)
    cx0, cy0 = _even(x), _even(y)
    # Rounding must never push the window out of the frame or into the timestamp.
    cx0 = min(cx0, _even(src_w - cw))
    cy0 = min(cy0, _even(usable_h - ch))
    return (cx0, max(cy0, 0), cw, ch)


# ---------------------------------------------------------------------- pick
def pick(
    rows: list[Row],
    start: float,
    end: float,
    *,
    faces: bool = False,
    peak: float | None = None,
    src_w: int,
    src_h: int,
) -> Pick:
    """The best frame for a moment [start, end]. Keyframes in [start-20, end+20]
    compete; camera frames are out unless `faces`; nothing left gives 'card'.
    Ties go to the frame nearest `peak` (the middle of the moment by default)."""
    peak = (start + end) / 2 if peak is None else peak
    best: tuple[float, float, int] | None = None
    for i, r in enumerate(rows):
        if not (start - CANDIDATE_MARGIN_S <= r.t <= end + CANDIDATE_MARGIN_S):
            continue
        kind = classify(r)
        if kind == "blank" or (kind == KIND_CAMERA and not faces):
            continue
        key = (frame_score(r, start <= r.t <= end), -abs(r.t - peak), i)
        if best is None or key[:2] > best[:2]:
            best = key
    if best is None:
        return Pick(None, KIND_CARD, None, 0.0)
    i = best[2]
    r = rows[i]
    kind = classify(r)
    if kind != KIND_SHARE:
        return Pick(r.t, KIND_CAMERA, None, best[0])
    # The run of consecutive share keyframes around the chosen one.
    lo = hi = i
    while lo > 0 and classify(rows[lo - 1]) == KIND_SHARE:
        lo -= 1
    while hi + 1 < len(rows) and classify(rows[hi + 1]) == KIND_SHARE:
        hi += 1
    crop = content_box(rows[lo : hi + 1], src_w, src_h, focus=r)
    return Pick(r.t, KIND_SHARE, crop, best[0])


# ------------------------------------------------------------------- extract
def extract(
    source: str | Path,
    t: float,
    crop: tuple[int, int, int, int] | list[int] | None,
    out_jpg: str | Path,
    timeout: float = 60,
) -> Path:
    """One frame at `t`, cropped, as a 1280x720 JPEG (about 130 KB). No text."""
    if crop:
        x, y, w, h = (int(v) for v in crop)
        pre = f"crop={w}:{h}:{x}:{y},"
    else:
        pre = ""
    vf = (
        f"{pre}scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={THUMB_W}:{THUMB_H},unsharp=5:5:0.5"
    )
    out = Path(out_jpg)
    out.parent.mkdir(parents=True, exist_ok=True)
    run_checked(
        [
            get_settings().ffmpeg_bin, "-hide_banner", "-nostdin", "-y",
            "-ss", f"{max(t, 0):.3f}", "-i", str(source),
            "-frames:v", "1", "-vf", vf, "-q:v", "3", str(out),
        ],
        timeout=timeout,
    )
    return out


def extract_vertical(
    short_mp4: str | Path, t: float, out_jpg: str | Path, timeout: float = 60
) -> Path:
    """One 1080x1920 frame from a rendered Short."""
    out = Path(out_jpg)
    out.parent.mkdir(parents=True, exist_ok=True)
    run_checked(
        [
            get_settings().ffmpeg_bin, "-hide_banner", "-nostdin", "-y",
            "-ss", f"{max(t, 0):.3f}", "-i", str(short_mp4),
            "-frames:v", "1",
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
            "-q:v", "3", str(out),
        ],
        timeout=timeout,
    )
    return out
