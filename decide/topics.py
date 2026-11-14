"""Topic cuts: a 5-20 minute single-topic stretch of the cleaned meeting that
is worth its own video.

Pure choosing, no I/O and no AI call: it reuses the chapters (cleaned
timeline) and the re-rank's moments (source timeline). `pick_cuts` returns the
best non-overlapping stretches; `edl.builder.slice_edl` then slices the
cleaned EDL for each one.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from fractions import Fraction

from autopilot.never_post import match_words
from core.models import Chapter, Moment, RenderManifest
from core.timeline import parse_rate, round_half_up
from edl.highlights import LEARNING_CATEGORIES

ALLOWED_CATEGORIES = tuple(LEARNING_CATEGORIES) + ("decision",)
MIN_OVERLAP_SHARE = 0.6  # of the moment's own length
MIN_STANDS_ALONE = 6.0
GOOD_MOMENT_SCORE = 5.0  # "worth watching" for the coverage share
OPEN_MIN_S = 15.0
OPEN_TARGET_S = 30.0
OPEN_MAX_S = 40.0


@dataclass
class Span:
    """One chapter on the cleaned timeline."""

    start: float
    end: float
    title: str


@dataclass
class Candidate:
    start: float  # cleaned seconds
    end: float
    titles: list[str] = field(default_factory=list)

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass
class TopicCut:
    index: int
    cleaned_start_s: float
    cleaned_end_s: float
    src_start_frame: int
    src_end_frame: int
    chapter_titles: list[str]
    best: Moment
    score_tuple: tuple
    open_span: tuple[float, float] | None  # source seconds of the cold open
    title: str
    hook: str
    thumb_src_s: float


# ------------------------------------------------------------- the timeline


def _out_len(piece) -> int:
    return piece.src_end_frame - piece.src_start_frame


def cleaned_to_source_frame(manifest: RenderManifest, cleaned_s: float) -> int:
    """The source frame that plays `cleaned_s` seconds into the cleaned video:
    the inverse of publish.describe.clip_position. An output frame inside a
    piece maps to src_start_frame + (out - out_start); inside a dissolve (two
    pieces overlap) it maps to the later piece. A time before the first piece
    or past the last one is clamped to the first or last source frame."""
    pieces = manifest.pieces
    if not pieces:
        raise ValueError("the manifest has no pieces")
    out = round_half_up(Fraction(cleaned_s) * parse_rate(manifest.fps))
    for piece in reversed(pieces):
        if piece.out_start_frame <= out < piece.out_start_frame + _out_len(piece):
            return piece.src_start_frame + (out - piece.out_start_frame)
    if out < pieces[0].out_start_frame:
        return pieces[0].src_start_frame
    for piece in pieces:  # in a hole between pieces: the next piece's start
        if out < piece.out_start_frame:
            return piece.src_start_frame
    return pieces[-1].src_end_frame


def _end_frame(manifest: RenderManifest, cleaned_s: float) -> int:
    """The exclusive source end frame for a cleaned end time."""
    fps = parse_rate(manifest.fps)
    out = round_half_up(Fraction(cleaned_s) * fps)
    if out <= 0:
        return cleaned_to_source_frame(manifest, 0.0)
    return cleaned_to_source_frame(manifest, float(Fraction(out - 1) / fps)) + 1


def source_to_cleaned_s(manifest: RenderManifest, src_s: float, *, end: bool = False) -> float | None:
    """Cleaned seconds at which source second `src_s` plays. A time in a
    removed stretch maps to the start of the next kept one (`end`: to the end
    of the previous one). None when nothing is kept on that side."""
    fps = parse_rate(manifest.fps)
    frame = Fraction(src_s) * fps
    pieces = manifest.pieces
    for piece in pieces:
        if piece.src_start_frame <= frame <= piece.src_end_frame and (frame < piece.src_end_frame or end):
            return float((piece.out_start_frame + (frame - piece.src_start_frame)) / fps)
    if end:
        before = [p for p in pieces if p.src_end_frame <= frame]
        if not before:
            return None
        last = max(before, key=lambda p: p.src_end_frame)
        return float(Fraction(last.out_start_frame + _out_len(last)) / fps)
    after = [p for p in pieces if p.src_start_frame >= frame]
    if not after:
        return None
    first = min(after, key=lambda p: p.src_start_frame)
    return float(Fraction(first.out_start_frame) / fps)


def chapter_spans(chapters: list[Chapter], cleaned_s: float) -> list[Span]:
    """[start_i, start_{i+1}) for each chapter; the last one runs to the end."""
    ordered = sorted((c for c in chapters or [] if c.start < cleaned_s), key=lambda c: c.start)
    spans = []
    for i, c in enumerate(ordered):
        end = ordered[i + 1].start if i + 1 < len(ordered) else cleaned_s
        if end > c.start:
            spans.append(Span(float(c.start), float(end), c.title))
    return spans


def candidates(spans: list[Span], min_s: float, max_s: float, max_chapters: int = 3) -> list[Candidate]:
    """Every run of 1-3 neighbouring chapters whose length is in [min_s, max_s]."""
    found = []
    for i in range(len(spans)):
        for n in range(1, max_chapters + 1):
            if i + n > len(spans):
                break
            run = spans[i : i + n]
            length = run[-1].end - run[0].start
            if min_s <= length <= max_s:
                found.append(Candidate(run[0].start, run[-1].end, [s.title for s in run]))
    return found


# ------------------------------------------------------------------ choosing


def _score(m: Moment) -> float:
    return float(m.raw_score if m.raw_score is not None else m.score)


@dataclass
class _Placed:
    moment: Moment
    start: float  # cleaned seconds
    end: float


def _place(manifest: RenderManifest, moments: list[Moment]) -> list[_Placed]:
    placed = []
    for m in moments:
        a = source_to_cleaned_s(manifest, m.start)
        b = source_to_cleaned_s(manifest, m.end, end=True)
        if a is not None and b is not None and b > a:
            placed.append(_Placed(m, a, b))
    return placed


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _best(cand: Candidate, placed: list[_Placed]) -> _Placed | None:
    best = None
    for p in placed:
        if _overlap(cand.start, cand.end, p.start, p.end) < MIN_OVERLAP_SHARE * (p.end - p.start):
            continue
        if best is None or _score(p.moment) > _score(best.moment):
            best = p
    return best


def qualify(
    cand: Candidate, placed: list[_Placed], cfg, never_words: list[str] | None = None
) -> tuple[_Placed, tuple] | None:
    """The candidate's best moment and its rank key, or None when it is not
    worth a video. A missing stands_alone counts as unknown, not as a fail."""
    best = _best(cand, placed)
    if best is None:
        return None
    m = best.moment
    if _score(m) < cfg.topic_min_score or m.category not in ALLOWED_CATEGORIES:
        return None
    if m.stands_alone is not None and m.stands_alone < MIN_STANDS_ALONE:
        return None
    if any(match_words(title, never_words) for title in cand.titles):
        return None
    good = sorted(
        (max(p.start, cand.start), min(p.end, cand.end))
        for p in placed
        if _score(p.moment) >= GOOD_MOMENT_SCORE and _overlap(cand.start, cand.end, p.start, p.end) > 0
    )
    covered, edge = 0.0, cand.start
    for a, b in good:
        a = max(a, edge)
        if b > a:
            covered += b - a
            edge = b
    share = covered / cand.length if cand.length > 0 else 0.0
    key = (_score(m), m.stands_alone if m.stands_alone is not None else 5.0, share,
           -len(cand.titles), -cand.length)
    return best, key


def _segment_edges(segments) -> tuple[list[float], list[float]]:
    starts, ends = [], []
    for s in segments or []:
        starts.append(float(s["start"] if isinstance(s, dict) else s.start))
        ends.append(float(s["end"] if isinstance(s, dict) else s.end))
    return starts, ends


def _peak_s(m: Moment, segments) -> float:
    if m.peak_index is not None and segments and 0 <= m.peak_index < len(segments):
        s = segments[m.peak_index]
        a, b = (s["start"], s["end"]) if isinstance(s, dict) else (s.start, s.end)
        return (float(a) + float(b)) / 2
    return (m.start + m.end) / 2


def cold_open(m: Moment, lo: float, hi: float, segments=None) -> tuple[float, float] | None:
    """15-40 s of source time around the best moment's peak (from its
    short_start when the re-rank gave one), snapped to segment edges and kept
    inside [lo, hi] (the cut's own source span). The open never starts in the
    cut's first OPEN_MAX_S: the body begins at the cut's first frame, so such an
    open would play the same words twice back to back. None when the moment is
    under 15 s or no 15 s window is left.
    """
    lo, hi = max(lo + OPEN_MAX_S, m.start), min(hi, m.end)
    if m.end - m.start < OPEN_MIN_S or hi - lo < OPEN_MIN_S:
        return None
    length = min(OPEN_TARGET_S, hi - lo)
    start = m.short_start if m.short_start is not None else _peak_s(m, segments) - length / 2
    start = min(max(start, lo), hi - length)
    end = start + length
    starts, ends = _segment_edges(segments)
    if starts:
        i = bisect.bisect_right(starts, start + 1e-6) - 1  # the segment that holds `start`
        if i >= 0 and starts[i] >= lo - 1e-6:
            start = starts[i]
        j = bisect.bisect_left(ends, end - 1e-6)  # the segment that holds `end`
        if j < len(ends) and ends[j] <= hi + 1e-6:
            end = ends[j]
        if end - start > OPEN_MAX_S:  # snapping stretched it: keep the start, trim the end
            end = start + OPEN_MAX_S
            k = bisect.bisect_right(ends, end + 1e-6) - 1
            if k >= 0 and ends[k] - start >= OPEN_MIN_S:
                end = ends[k]
    start, end = max(start, lo), min(end, hi)
    if end - start < OPEN_MIN_S:
        return None
    return round(start, 3), round(end, 3)


def pick_cuts(
    manifest: RenderManifest,
    chapters: list[Chapter] | None,
    moments: list[Moment],
    cfg,
    segments=None,
    never_words: list[str] | None = None,
) -> list[TopicCut]:
    """The top `cfg.topic_cuts_per_meeting` non-overlapping topic cuts ([] when
    topic cuts are off). `segments` (the transcript's segments, in the order
    the moments' peak_index counts) lets the cold open snap to sentence edges."""
    if not cfg.topic_cuts or cfg.topic_cuts_per_meeting < 1 or not manifest.pieces:
        return []
    fps = parse_rate(manifest.fps)
    total_s = float(Fraction(max(p.out_start_frame + _out_len(p) for p in manifest.pieces)) / fps)
    min_s, max_s = cfg.topic_min_minutes * 60.0, cfg.topic_max_minutes * 60.0
    placed = _place(manifest, moments)
    cands = candidates(chapter_spans(chapters or [], total_s), min_s, max_s)
    if not cands:  # no (usable) chapters: single moments that fit on their own
        cands = [Candidate(p.start, p.end, []) for p in placed if min_s <= p.end - p.start <= max_s]
    ranked = []
    for cand in cands:
        got = qualify(cand, placed, cfg, never_words)
        if got is not None:
            ranked.append((got[1], cand, got[0]))
    ranked.sort(key=lambda r: r[0], reverse=True)
    chosen: list[TopicCut] = []
    for key, cand, best in ranked:
        if len(chosen) >= cfg.topic_cuts_per_meeting:
            break
        if any(_overlap(cand.start, cand.end, c.cleaned_start_s, c.cleaned_end_s) > 0 for c in chosen):
            continue
        sf = cleaned_to_source_frame(manifest, cand.start)
        ef = _end_frame(manifest, cand.end)
        m = best.moment
        opening = None
        if cfg.topic_cold_open:
            opening = cold_open(m, float(Fraction(sf) / fps), float(Fraction(ef) / fps), segments)
        chosen.append(TopicCut(
            index=len(chosen), cleaned_start_s=cand.start, cleaned_end_s=cand.end,
            src_start_frame=sf, src_end_frame=ef, chapter_titles=list(cand.titles), best=m,
            score_tuple=key, open_span=opening, title=m.title, hook=m.screen_hook or m.hook,
            thumb_src_s=_peak_s(m, segments),
        ))
    return chosen
