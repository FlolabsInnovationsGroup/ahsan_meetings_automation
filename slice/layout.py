"""Per-clip picture analysis for the Shorts layout.

Zoom's recording_type labels a whole meeting, but a screen is often shared for
only a few minutes of it. This module looks at the actual frames of one clip
and says whether it shows a shared screen, a speaker or a gallery, plus a crop
box for the renderer. It never raises: any trouble gives the 'fit' layout (the
whole meeting frame, as before).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import numpy as np

from slice import ffmpeg_wrapper, profile

SAMPLE_FPS = 2.0
SAMPLE_WIDTH = 480
MAX_FRAMES = 120  # 120 gray frames at 480x270 is about 15 MB

MIN_FRAMES = 4
CONTENT_PIXEL_LEVEL = 24  # a pixel brighter than this is picture, not letterbox
CONTENT_LINE_SHARE = 0.15  # share of a row/column that must be picture
CHANGE_LEVEL = 12  # a step-to-step difference above this is a real change
LIVE_LEVEL = 3  # a smaller difference, used to see camera video (sensor noise)
LIVE_STEP_SHARE = 0.30  # a pixel is "live" when it moves in more than this share of steps

# Screen-or-not limits. They come from ONE real recording (a screen share
# measured 0.014 / 0.0, gallery clips 0.20-0.38 / 0.08-0.11), so treat them as
# a first guess: every Short logs its metrics so they can be recalibrated.
SCREEN_MAX_CHANGED_SHARE = 0.06
SCREEN_MAX_LIVE_SHARE = 0.03

EDGE_LEVEL = 40  # brightness jump across a line that counts as an edge
VERTICAL_EDGE_MIN_HEIGHT = 0.35  # long vertical edge: this much of the height
VERTICAL_EDGE_MIN_GAP = 0.40  # the two panel edges are this far apart (of width)
HORIZONTAL_EDGE_MIN_WIDTH = 0.85  # of the panel's width
HORIZONTAL_EDGE_MIN_GAP = 0.25  # of the height
MIN_PANEL_AREA = 0.25  # a panel smaller than this share of the frame -> fit
INTERIOR_MARGIN = 0.05

CLOCK_X = 0.85  # a ticking clock sits in the bottom right corner
CLOCK_Y = 0.95
CLOCK_STEP_SHARE = 0.30
CLOCK_MIN_PIXELS = 0.004  # of the corner region
CLOCK_MAX_PIXELS = 0.5  # more than this is video (a gallery tile), not a clock
WINDOW_S = 5.0
MAX_DISAGREE = 0.20

# Zoom burns the recording's date and time into the bottom right of the frame (white
# text on the black bar under the tiles). It is found on the sampled frames and blurred.
STAMP_X = 0.80  # the stamp is looked for right of this share of the width...
STAMP_Y = 0.93  # ...and below this share of the height
STAMP_BG_MAX = 50  # the area around it is dark (the black bar), else it is picture
STAMP_TEXT_LEVEL = 90
STAMP_MIN_PIXELS = 30
STAMP_PAD = 8  # source pixels around the text


@dataclass(frozen=True)
class Layout:
    kind: str  # 'screen' | 'speaker' | 'gallery' | 'fit'
    panel: tuple[int, int, int, int]  # (x, y, w, h) in source pixels
    change_box: tuple[int, int, int, int] | None = None
    focus_x: int | None = None
    metrics: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    stamp: tuple[int, int, int, int] | None = None  # (x, y, w, h): Zoom's timestamp, to be blurred


def sample_frames(
    src,
    start_s: float,
    end_s: float,
    fps: float = SAMPLE_FPS,
    width: int = SAMPLE_WIDTH,
    *,
    src_size: tuple[int, int],
) -> np.ndarray:
    """Gray frames of [start_s, end_s) as a uint8 array (n, h, w), one ffmpeg call.

    src_size is the source (width, height) from the caller's MediaInfo; it fixes
    the output height so the raw bytes can be reshaped.
    """
    src_w, src_h = src_size
    dur = max(0.0, end_s - start_s)
    if dur <= 0 or src_w <= 0 or src_h <= 0:
        raise ValueError("empty clip or unknown source size")
    h = max(2, round(src_h * width / src_w / 2.0) * 2)
    rate = min(float(fps), MAX_FRAMES / dur)
    cmd = [
        ffmpeg_wrapper.ffmpeg_bin(),
        *profile.BASE_ARGS,
        "-ss", f"{start_s:.3f}",
        "-t", f"{dur:.3f}",
        "-i", str(src),
        "-vf", f"fps={rate:.4f},scale={width}:{h},format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=profile.ffmpeg_timeout(dur), check=True)
    n = len(proc.stdout) // (width * h)
    if n == 0:
        return np.zeros((0, h, width), dtype=np.uint8)
    return np.frombuffer(proc.stdout, dtype=np.uint8, count=n * width * h).reshape(n, h, width)


def _fit(src_w: int, src_h: int, note: str, metrics: dict | None = None) -> Layout:
    return Layout(
        kind="fit", panel=(0, 0, max(2, src_w // 2 * 2), max(2, src_h // 2 * 2)), metrics=metrics or {}, notes=[note]
    )


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    out, start = [], None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(mask) - 1))
    return out


def _content_box(med: np.ndarray) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1), x1/y1 exclusive: the picture without letterbox bars."""
    h, w = med.shape
    lit = med > CONTENT_PIXEL_LEVEL
    rows = np.flatnonzero(lit.mean(axis=1) >= CONTENT_LINE_SHARE)
    cols = np.flatnonzero(lit.mean(axis=0) >= CONTENT_LINE_SHARE)
    if rows.size == 0 or cols.size == 0:
        return 0, 0, w, h
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


def _edge_centres(share: np.ndarray, min_share: float) -> list[float]:
    return [(a + b) / 2.0 for a, b in _runs(share >= min_share)]


def _vertical_edges(img: np.ndarray) -> list[float]:
    """Positions (in img columns) of long vertical edges."""
    f = img.astype(np.float32)
    _h, w = f.shape
    k = 3
    if w < 4 * k:
        return []
    cs = np.cumsum(np.pad(f, ((0, 0), (1, 0))), axis=1)
    left = (cs[:, k : w - k + 1] - cs[:, : w - 2 * k + 1]) / k
    right = (cs[:, 2 * k : w + 1] - cs[:, k : w - k + 1]) / k
    share = (np.abs(right - left) > EDGE_LEVEL).mean(axis=0)
    return [c + k for c in _edge_centres(share, VERTICAL_EDGE_MIN_HEIGHT)]


def _horizontal_edges(img: np.ndarray, x0: int, x1: int) -> list[float]:
    f = img[:, x0:x1].astype(np.float32)
    h, w = f.shape
    k = 3
    if h < 4 * k or w < 4:
        return []
    cs = np.cumsum(np.pad(f, ((1, 0), (0, 0))), axis=0)
    up = (cs[k : h - k + 1] - cs[: h - 2 * k + 1]) / k
    down = (cs[2 * k : h + 1] - cs[k : h - k + 1]) / k
    share = (np.abs(down - up) > EDGE_LEVEL).mean(axis=1)
    return [c + k for c in _edge_centres(share, HORIZONTAL_EDGE_MIN_WIDTH)]


def _best_pair(
    edges: list[float],
    activity: np.ndarray,
    extent: float,
    min_gap: float,
    widest: bool = False,
    enclose: tuple[float, float] | None = None,
) -> tuple[float, float] | None:
    """The pair of edges (at least min_gap apart) that holds most of the change.

    activity is the per-column (or per-row) change count. Pairs holding at least
    90% of the best pair's change compete. Sideways, the tightest wins, so a
    sidebar does not stretch the slide box; vertically (widest=True) the widest
    wins, so a title bar inside the slide is not taken for its top edge.

    enclose is the (low, high) span of the change box: a pair that does not
    contain it (give or take a few columns) is dropped, so an edge inside the
    slide is never taken for its border. A pair that holds no change at all
    (nothing moved) is no evidence of a tight panel, so then the widest wins.
    """
    total = np.concatenate([[0.0], np.cumsum(activity, dtype=np.float64)])
    pairs = []
    for i, a in enumerate(edges):
        for b in edges[i + 1 :]:
            if b - a >= min_gap and b <= extent:
                if enclose is not None:
                    slack = max(2.0, 0.03 * extent)
                    if enclose[0] < a - slack or enclose[1] > b + slack:
                        continue
                lo, hi = round(a), round(b)
                pairs.append((total[min(hi, len(activity))] - total[lo], a, b))
    if not pairs:
        return None
    top = max(p[0] for p in pairs)
    near = [p for p in pairs if p[0] >= 0.9 * top]
    pick = max if (widest or top <= 0) else min
    _, a, b = pick(near, key=lambda p: p[2] - p[1])
    return a, b


def _bbox(counts: np.ndarray, min_count: int = 1) -> tuple[int, int, int, int] | None:
    hit = counts > 0
    rows = np.flatnonzero(hit.sum(axis=1) >= min_count)
    cols = np.flatnonzero(hit.sum(axis=0) >= min_count)
    if rows.size == 0 or cols.size == 0:
        return None
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


def _main_run(profile: np.ndarray, bridge: int = 2) -> tuple[int, int] | None:
    """(first, last) of the run of the profile (a per-column or per-row count of changed pixels)
    that holds the most change. Counts under 2 are ignored and gaps of up to `bridge` are bridged."""
    runs = _runs(profile >= 2)
    if not runs:
        return None
    merged = [list(runs[0])]
    for a, b in runs[1:]:
        if a - merged[-1][1] - 1 <= bridge:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    a, b = max(merged, key=lambda r: profile[r[0] : r[1] + 1].sum())
    return int(a), int(b)


def _main_bbox(counts: np.ndarray) -> tuple[int, int, int, int] | None:
    """(x0, y0, x1, y1), x1/y1 exclusive: the box of the biggest block of change. Small changes on its
    own (a viewer's page counter, a thumbnail highlight beside the slide) do not stretch it."""
    hit = counts > 0
    cols = _main_run(hit.sum(axis=0))
    if cols is None:
        return None
    rows = _main_run(hit[:, cols[0] : cols[1] + 1].sum(axis=1))
    if rows is None:
        return None
    cols = _main_run(hit[rows[0] : rows[1] + 1].sum(axis=0)) or cols
    return cols[0], rows[0], cols[1] + 1, rows[1] + 1


def _is_screen(changed_share: float, live_share: float) -> bool:
    return changed_share <= SCREEN_MAX_CHANGED_SHARE and live_share <= SCREEN_MAX_LIVE_SHARE


def _panel(x0: float, y0: float, x1: float, y1: float, src_w: int, src_h: int) -> tuple[int, int, int, int]:
    """(x, y, w, h) in source pixels: even numbers, inside the frame."""
    ax = max(0, round(x0)) // 2 * 2
    ay = max(0, round(y0)) // 2 * 2
    bx = min(src_w, round(x1)) // 2 * 2
    by = min(src_h, round(y1)) // 2 * 2
    return ax, ay, max(2, bx - ax), max(2, by - ay)


def _stamp_box(med: np.ndarray, src_w: int, src_h: int) -> tuple[int, int, int, int] | None:
    """(x, y, w, h) in source pixels around Zoom's timestamp, or None when there is none.
    It is white text on the dark bar in the bottom right corner of the median frame."""
    h, w = med.shape
    x0, y0 = int(w * STAMP_X), int(h * STAMP_Y)
    reg = med[y0:, x0:]
    if reg.size == 0 or float(np.median(reg)) > STAMP_BG_MAX:
        return None
    ys, xs = np.nonzero(reg > STAMP_TEXT_LEVEL)
    if len(ys) < STAMP_MIN_PIXELS or len(ys) > 0.25 * reg.size:
        return None
    bx0, bx1, by0, by1 = int(xs.min()) + x0, int(xs.max()) + 1 + x0, int(ys.min()) + y0, int(ys.max()) + 1 + y0
    if not (0.05 * w <= bx1 - bx0 <= 0.2 * w) or by1 - by0 > 0.06 * h:
        return None
    sx, sy = src_w / w, src_h / h
    return _panel(bx0 * sx - STAMP_PAD, by0 * sy - STAMP_PAD, bx1 * sx + STAMP_PAD, by1 * sy + STAMP_PAD, src_w, src_h)


def _centroid_x(mask: np.ndarray) -> float:
    cols = mask.sum(axis=0).astype(np.float64)
    return float((cols * (np.arange(mask.shape[1]) + 0.5)).sum() / cols.sum())


def analyse(frames, src_w: int, src_h: int, recording_type: str | None) -> Layout:
    """Decide the layout of one clip from its sampled frames. Never raises."""
    try:
        return _analyse(frames, src_w, src_h, recording_type)
    except Exception as e:  # noqa: BLE001 - the renderer must always get a layout
        return _fit(src_w, src_h, f"analysis failed: {type(e).__name__}")


def _analyse(frames, src_w: int, src_h: int, recording_type: str | None) -> Layout:
    if frames is None or getattr(frames, "ndim", 0) != 3 or len(frames) < MIN_FRAMES:
        return _fit(src_w, src_h, "too few frames to judge")
    n, fh, fw = frames.shape
    sx, sy = src_w / fw, src_h / fh
    metrics: dict = {"frames": int(n)}

    median = np.median(frames, axis=0)
    cx0, cy0, cx1, cy1 = _content_box(median)
    metrics["content_box"] = [cx0, cy0, cx1, cy1]
    stamp = _stamp_box(median, src_w, src_h)
    metrics["stamp"] = list(stamp) if stamp else None
    ch, cw = cy1 - cy0, cx1 - cx0
    if ch < 8 or cw < 8:
        return _fit(src_w, src_h, "no picture found", metrics)
    crop = frames[:, cy0:cy1, cx0:cx1]
    steps = n - 1

    # Motion, one step at a time so memory stays flat.
    win_len = max(2, round(WINDOW_S * SAMPLE_FPS))
    cnt12 = np.zeros((ch, cw), np.int32)
    cnt3 = np.zeros((ch, cw), np.int32)
    win12 = np.zeros((ch, cw), np.int32)
    win3 = np.zeros((ch, cw), np.int32)
    win_n = 0
    win_stats: list[tuple[float, float]] = []
    prev = crop[0].astype(np.int16)
    for i in range(1, n):
        cur = crop[i].astype(np.int16)
        d = np.abs(cur - prev)
        prev = cur
        big, small = d > CHANGE_LEVEL, d > LIVE_LEVEL
        cnt12 += big
        cnt3 += small
        win12 += big
        win3 += small
        win_n += 1
        if win_n >= win_len or i == n - 1:
            if win_n >= 4:
                win_stats.append((float((win12 >= 2).mean()), float((win3 > LIVE_STEP_SHARE * win_n).mean())))
            win12[:] = 0
            win3[:] = 0
            win_n = 0

    # A ticking clock in the bottom right corner is not part of the picture.
    clock_top = None
    rx0 = int(max(0, CLOCK_X * fw - cx0))
    ry0 = int(max(0, CLOCK_Y * fh - cy0))
    if rx0 < cw and ry0 < ch:
        ticking = cnt12[ry0:, rx0:] >= CLOCK_STEP_SHARE * steps
        if CLOCK_MIN_PIXELS <= ticking.mean() <= CLOCK_MAX_PIXELS:
            clock_top = cy0 + ry0 + int(np.flatnonzero(ticking.any(axis=1))[0])
            cnt12[ry0:, rx0:] = 0
            cnt3[ry0:, rx0:] = 0
    metrics["clock_top"] = clock_top

    changed_share = float((cnt12 >= 2).mean())
    live_mask = cnt3 > LIVE_STEP_SHARE * steps
    live_share = float(live_mask.mean())
    metrics["changed_px_share"] = round(changed_share, 4)
    metrics["live_share"] = round(live_share, 4)
    screen = _is_screen(changed_share, live_share)
    metrics["screen_like"] = bool(screen)

    # Mixed clip: classify 5-second windows.
    if win_stats:
        votes = [_is_screen(c, lv) for c, lv in win_stats]
        majority = sum(votes) * 2 >= len(votes)
        disagree = sum(1 for v in votes if v != majority) / len(votes)
        metrics["window_disagree"] = round(disagree, 3)
        if disagree > MAX_DISAGREE:
            return _fit(src_w, src_h, "picture switches between screen and people inside the clip", metrics)

    cb_small = _main_bbox(cnt12)
    change_box = None
    if cb_small is not None:
        change_box = _panel(
            (cb_small[0] + cx0) * sx, (cb_small[1] + cy0) * sy, (cb_small[2] + cx0) * sx, (cb_small[3] + cy0) * sy,
            src_w, src_h,
        )

    mean_img = frames.mean(axis=0)[cy0:cy1, cx0:cx1]
    v_edges = _vertical_edges(mean_img)
    interior = [e for e in v_edges if cw * INTERIOR_MARGIN <= e <= cw * (1 - INTERIOR_MARGIN)]
    metrics["interior_vertical_edges"] = len(interior)
    notes: list[str] = []

    if screen:
        pair_x = _best_pair(
            v_edges, cnt12.sum(axis=0), cw, cw * VERTICAL_EDGE_MIN_GAP,
            enclose=(cb_small[0], cb_small[2]) if cb_small else None,
        )
        if pair_x:
            px0, px1 = pair_x
        elif cb_small:
            px0, px1 = cb_small[0], cb_small[2]
            notes.append("no slide edges found, used the area that changes")
        else:
            px0, px1 = 0, cw
            notes.append("nothing changes, used the whole picture")
        h_edges = _horizontal_edges(mean_img, int(px0), int(px1))
        rows_active = cnt12[:, int(px0) : int(px1)].sum(axis=1)
        pair_y = _best_pair(h_edges, rows_active, ch, ch * HORIZONTAL_EDGE_MIN_GAP, widest=True)
        if pair_y:
            py0, py1 = pair_y
        elif cb_small:
            py0, py1 = cb_small[1], cb_small[3]
        else:
            py0, py1 = 0, ch
        y_end = (py1 + cy0) * sy
        if clock_top is not None:
            y_end = min(y_end, (clock_top - 2) * sy)
            notes.append("kept the clock corner out of the slide")
        panel = _panel((px0 + cx0) * sx, (py0 + cy0) * sy, (px1 + cx0) * sx, y_end, src_w, src_h)
        metrics["panel"] = list(panel)
        metrics["panel_from_edges"] = bool(pair_x and pair_y)
        if panel[2] * panel[3] < MIN_PANEL_AREA * src_w * src_h:
            return _fit(src_w, src_h, "slide area too small to crop to", metrics)
        return Layout("screen", panel, change_box, None, metrics, notes, stamp)

    rt = (recording_type or "").lower()
    if "+" in rt or not ("speaker" in rt or "gallery" in rt):
        kind = "gallery" if len(interior) >= 2 else "speaker"
        notes.append("recording type unclear, chose by vertical edges")
    else:
        kind = "speaker" if "speaker" in rt else "gallery"
    panel = _panel(cx0 * sx, cy0 * sy, cx1 * sx, cy1 * sy, src_w, src_h)
    focus = None
    if kind == "speaker" and live_mask.any():
        focus = max(0, min(src_w, int((_centroid_x(live_mask) + cx0) * sx)))
    metrics["panel"] = list(panel)
    return Layout(kind, panel, change_box, focus, metrics, notes, stamp)


def analyse_clip(
    src, start_s: float, end_s: float, src_w: int, src_h: int, recording_type: str | None
) -> Layout:
    """Sample the clip's frames and analyse them. Never raises."""
    try:
        frames = sample_frames(src, start_s, end_s, src_size=(src_w, src_h))
    except Exception as e:  # noqa: BLE001
        return _fit(src_w, src_h, f"could not sample frames: {type(e).__name__}")
    return analyse(frames, src_w, src_h, recording_type)
