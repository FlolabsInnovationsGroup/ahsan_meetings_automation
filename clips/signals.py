"""Cheap signals for finding candidate moments: loudness, scene changes, and
the windows built from them (plus YouTube's replay heatmap and chapters).

The ffmpeg calls are two small functions; everything else is pure and tested
with sample output.
"""

from __future__ import annotations

import re
from pathlib import Path

from clips.config import clips_settings
from clips.models import ClipKind, Fetched, Window

_EBU_LINE = re.compile(r"\bt:\s*(?P<t>\d+(?:\.\d+)?)\b.*?\bM:\s*(?P<m>-?\d+(?:\.\d+)?|-inf)")
_PTS_TIME = re.compile(r"pts_time:\s*(?P<t>\d+(?:\.\d+)?)")

# Momentary loudness is mapped so -60 LUFS (near silence) is 0 and -10 LUFS (very loud) is 1.
_QUIET_LUFS = -60.0
_LOUD_LUFS = -10.0
_LEAD_S = {ClipKind.SHORT: 5.0, ClipKind.LONG: 30.0}  # start this long before the peak


# ------------------------------------------------------------------ ffmpeg calls


def _run_ffmpeg_stderr(args: list[str]) -> str:
    """Run ffmpeg and return its stderr text (the filters print there). Tests replace this."""
    from core.config import get_settings
    from core.proc import run_checked

    settings = get_settings()
    cmd = [settings.ffmpeg_bin, "-hide_banner", "-nostats", *args]
    return run_checked(cmd, timeout=settings.ffmpeg_timeout_seconds).stderr or ""


# ------------------------------------------------------------------ parsing


def parse_ebur128(text: str, step_s: float = 1.0) -> list[tuple[float, float]]:
    """Turn ebur128 log lines into (time_s, 0..1) points, one per `step_s`, keeping the loudest reading in each step."""
    step = max(step_s, 0.1)
    best: dict[int, float] = {}
    for line in text.splitlines():
        match = _EBU_LINE.search(line)
        if not match:
            continue
        raw = match.group("m")
        lufs = -120.0 if raw == "-inf" else float(raw)
        bucket = int(float(match.group("t")) // step)
        best[bucket] = max(best.get(bucket, -120.0), lufs)
    return [(round(b * step, 3), _scale_lufs(v)) for b, v in sorted(best.items())]


def _scale_lufs(lufs: float) -> float:
    return max(0.0, min(1.0, (lufs - _QUIET_LUFS) / (_LOUD_LUFS - _QUIET_LUFS)))


def parse_scene_times(text: str) -> list[float]:
    """Times (seconds) of scene changes from showinfo lines."""
    times = []
    for line in text.splitlines():
        match = _PTS_TIME.search(line) if "showinfo" in line else None
        if match:
            times.append(float(match.group("t")))
    return sorted(times)


def loudness_curve(path: Path, *, step_s: float = 1.0) -> list[tuple[float, float]]:
    """Loudness over time of a file, as (time_s, 0..1) points."""
    text = _run_ffmpeg_stderr(["-i", str(path), "-vn", "-af", "ebur128=framelog=info", "-f", "null", "-"])
    return parse_ebur128(text, step_s)


def scene_changes(path: Path, *, threshold: float = 0.35) -> list[float]:
    """Times where the picture cuts to something quite different."""
    vf = f"select='gt(scene,{threshold})',showinfo"
    text = _run_ffmpeg_stderr(["-i", str(path), "-an", "-vf", vf, "-f", "null", "-"])
    return parse_scene_times(text)


# ------------------------------------------------------------------ peaks


def _loud_peaks(curve: list[tuple[float, float]]) -> list[tuple[float, float, str]]:
    """Local maxima that stand clearly above the usual level."""
    if len(curve) < 3:
        return []
    values = [v for _, v in curve]
    mean = sum(values) / len(values)
    spread = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
    floor = mean + max(spread, 0.05)
    out = []
    for i in range(1, len(curve) - 1):
        t, v = curve[i]
        if v >= floor and v >= curve[i - 1][1] and v > curve[i + 1][1]:
            out.append((t, min(1.0, v), "loud"))
    return out


def _heat_peaks(heatmap: list[tuple[float, float, float]]) -> list[tuple[float, float, str]]:
    """Replay-heatmap segments that are local maxima and at least half as hot as the best."""
    pts = [((a + b) / 2, v) for a, b, v in heatmap]
    out = []
    for i, (t, v) in enumerate(pts):
        left = pts[i - 1][1] if i > 0 else -1.0
        right = pts[i + 1][1] if i + 1 < len(pts) else -1.0
        if v >= 0.5 and v >= left and v > right:
            out.append((t, min(1.0, v), "replayed"))
    return out


def _scene_bursts(scenes: list[float], span_s: float = 10.0, min_cuts: int = 3) -> list[tuple[float, float, str]]:
    """Places with many cuts in a short span (chases, crashes, fights). One peak per burst."""
    out: list[tuple[float, float, str]] = []
    ts = sorted(scenes)
    i = 0
    while i < len(ts):
        j = i
        while j + 1 < len(ts) and ts[j + 1] - ts[i] <= span_s:
            j += 1
        count = j - i + 1
        if count >= min_cuts:
            out.append(((ts[i] + ts[j]) / 2, min(1.0, count / 8), "cuts"))
            i = j + 1
        else:
            i += 1
    return out


def _chapter_peaks(chapters: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    return [(a + min(10.0, (b - a) / 2), 0.3, "chapter") for a, b, _ in chapters]


def _cluster(peaks: list[tuple[float, float, str]], gap_s: float) -> list[tuple[float, float, dict[str, float]]]:
    """Merge peaks closer than `gap_s`; the group keeps its best time and every signal's best value."""
    groups: list[list[tuple[float, float, str]]] = []
    for peak in sorted(peaks):
        if groups and peak[0] - groups[-1][-1][0] <= gap_s:
            groups[-1].append(peak)
        else:
            groups.append([peak])
    out = []
    for group in groups:
        signals: dict[str, float] = {}
        for _, value, name in group:
            signals[name] = max(signals.get(name, 0.0), round(value, 3))
        best_t = max(group, key=lambda p: p[1])[0]
        score = min(1.0, max(signals.values()) + 0.15 * (len(signals) - 1))
        out.append((best_t, round(score, 3), signals))
    return out


# ------------------------------------------------------------------ windows


def _length_for(kind: ClipKind) -> tuple[float, float, float]:
    """(min, max, target) length in seconds for the kind."""
    s = clips_settings()
    lo, hi = (s.short_min_s, s.short_max_s) if kind is ClipKind.SHORT else (s.long_min_s, s.long_max_s)
    return lo, hi, (lo + hi) / 2


def _overlap(a: Window, b: Window) -> float:
    return max(0.0, min(a.end_s, b.end_s) - max(a.start_s, b.start_s))


def _place(peak_t: float, kind: ClipKind, duration: float) -> tuple[float, float]:
    _, _, target = _length_for(kind)
    start = max(0.0, peak_t - _LEAD_S[kind])
    end = min(duration, start + target)
    return max(0.0, end - target), end


def _evenly_spaced(duration: float, kind: ClipKind, count: int) -> list[Window]:
    lo, _, target = _length_for(kind)
    if duration < lo:
        return []  # too short to cut a clip of this kind; never offer the whole video
    n = min(count, max(1, int(duration // target)))
    stride = (duration - target) / (n - 1) if n > 1 else 0.0
    return [
        Window(start_s=round(i * stride, 2), end_s=round(i * stride + target, 2), score=0.1, signals={"even": 0.1})
        for i in range(n)
    ]


def candidate_windows(
    fetched: Fetched,
    *,
    kind: ClipKind,
    max_candidates: int = 30,
    loudness: list[tuple[float, float]] | None = None,
    scenes: list[float] | None = None,
) -> list[Window]:
    """Scored windows that might hold a good moment, best first.

    Uses whatever signals exist (heatmap, loudness, cut bursts, chapters); with
    none it falls back to evenly spaced windows so the picker always has some.
    """
    seen_ends = [b for _, b, _ in fetched.heatmap] + [t for t, _ in (loudness or [])]
    duration = fetched.duration_s or max(seen_ends + [0.0])
    if duration <= 0 or max_candidates <= 0:
        return []
    peaks = (
        _heat_peaks(fetched.heatmap)
        + _loud_peaks(loudness or [])
        + _scene_bursts(scenes or [])
        + _chapter_peaks(fetched.chapters)
    )
    if not peaks:
        return _evenly_spaced(duration, kind, max_candidates)
    lo, _, target = _length_for(kind)
    if duration < lo:
        return []
    windows: list[Window] = []
    for t, score, signals in _cluster(peaks, target / 2):
        start, end = _place(t, kind, duration)
        if end - start < lo:
            continue
        windows.append(
            Window(start_s=round(start, 2), end_s=round(end, 2), score=score, signals=signals, note=f"peak at {t:.0f}s")
        )
    kept: list[Window] = []
    for win in sorted(windows, key=lambda w: w.score, reverse=True):
        if all(_overlap(win, k) <= 0.5 * (win.end_s - win.start_s) for k in kept):
            kept.append(win)
    return kept[:max_candidates]
