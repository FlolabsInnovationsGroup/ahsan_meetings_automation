"""Choosing the moments: candidate windows go to the language model (when there
is one), and whatever it answers is validated and repaired in code.

Without a model the best-scoring windows become moments with a neutral summary
and no hook, so the pipeline still produces something a person can review.
"""

from __future__ import annotations

import difflib
import math
import tempfile
from pathlib import Path

from clips.config import clips_settings
from clips.models import Caption, ClipKind, Fetched, Moment, Source, SourceVideo, Window
from core.logging import get_logger

logger = get_logger(__name__)

HOOK_MAX = 40
SUMMARY_MAX = 300
MAX_OVERLAP = 0.3  # of the shorter moment; more than this counts as the same moment
MIN_START_GAP_S = 5.0  # two moments starting this close are near-duplicates
SIMILAR_SUMMARY = 0.85
VISION_CANDIDATES = 10  # only the best few candidates are shown to a vision model
VISION_FRAMES = 2
CAPTION_CHARS = 400
MAX_SHARE_OF_SOURCE = 0.5  # a clip is at most this fraction of its source video

SYSTEM = """You choose the best moments of a gameplay video to cut into clips for YouTube.
Rules:
- A good moment has a set-up, a peak and a pay-off, and makes sense to someone who has not seen the rest of the video.
- Start 2 to 4 seconds before the peak, and end shortly after the pay-off.
- Describe only what the signals, the title and the words spoken support. Never invent what is on screen. If unsure, say less.
- No back-references to things outside the clip ("as I said earlier").
- Flag spoilers: set "spoiler" true for main-story plot reveals, endings and mission outcomes.
- Set "has_music" true when licensed music seems to be playing.
- Prefer chaos, crashes, chases, glitches, funny reactions and skilful plays.
- Moments must not overlap each other.
- "hook" is on-screen text of at most 40 characters, plain and true to the footage.
- "score" is 0 to 1: how likely a stranger is to watch to the end.
Answer with JSON only: {"moments": [{"start_s": number, "end_s": number, "kind": "short"|"long", "summary": string,
"hook": string, "reason": string, "score": number, "spoiler": boolean, "has_music": boolean}]}"""


# ------------------------------------------------------------------ public


def pick_moments(
    video: SourceVideo,
    fetched: Fetched,
    source: Source,
    *,
    llm=None,
    max_shorts: int | None = None,
    max_long: int | None = None,
    candidates: list[Window] | None = None,
) -> list[Moment]:
    """Choose up to `max_shorts` Shorts and `max_long` long clips from a video, best first."""
    settings = clips_settings()
    n_short = settings.shorts_per_video if max_shorts is None else max_shorts
    n_long = settings.long_per_video if max_long is None else max_long
    duration = fetched.duration_s or video.duration_s
    if duration <= 0 or (n_short <= 0 and n_long <= 0):
        return []
    if candidates is None:
        candidates = _gather_candidates(fetched, n_short, n_long)
    caps = {ClipKind.SHORT: max(n_short, 0), ClipKind.LONG: max(n_long, 0)}
    raw: list[dict] = []
    if llm is not None and candidates:
        try:
            raw = _ask_model(llm, video, fetched, source, candidates, caps)
        except Exception as exc:  # noqa: BLE001  a bad answer or a model outage must not stop the video
            logger.warning("clips: the language model could not pick moments for %s: %s", video.id, exc)
    moments = validate_moments(raw, duration, caps, source=source) if raw else []
    if not moments:
        moments = fallback_moments(candidates, video, duration, caps)
    return moments


# ------------------------------------------------------------------ candidates


def _gather_candidates(fetched: Fetched, n_short: int, n_long: int) -> list[Window]:
    """Windows for each wanted kind, using loudness and cuts when the file can be read."""
    from clips import signals

    loud: list[tuple[float, float]] | None = None
    scenes: list[float] | None = None
    path = Path(fetched.video_path) if fetched.video_path else None
    if path is not None and path.exists():
        loud = _safe(signals.loudness_curve, path) if fetched.has_audio else None
        scenes = _safe(signals.scene_changes, path)
    out: list[Window] = []
    for kind, wanted in ((ClipKind.SHORT, n_short), (ClipKind.LONG, n_long)):
        if wanted > 0:
            out += signals.candidate_windows(fetched, kind=kind, max_candidates=30, loudness=loud, scenes=scenes)
    return out


def _safe(fn, path: Path):
    """Call a signal function; a failure just means one signal fewer."""
    try:
        return fn(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("clips: %s failed on %s: %s", fn.__name__, path.name, exc)
        return None


def _kind_of(window: Window) -> ClipKind:
    """A candidate is long when it is longer than a Short may be."""
    return ClipKind.LONG if window.end_s - window.start_s > clips_settings().short_max_s else ClipKind.SHORT


# ------------------------------------------------------------------ the model

def _ask_model(llm, video: SourceVideo, fetched: Fetched, source: Source, candidates: list[Window], caps: dict) -> list[dict]:
    ranked = sorted(candidates, key=lambda w: w.score, reverse=True)[:30]
    user = build_prompt(video, fetched, source, ranked, caps)
    images = grab_candidate_frames(fetched, ranked) if clips_settings().vision else []
    data = llm.json(SYSTEM, user, images=images or None, max_tokens=2500)
    items = data.get("moments") if isinstance(data, dict) else None
    return [m for m in items if isinstance(m, dict)] if isinstance(items, list) else []


def build_prompt(video: SourceVideo, fetched: Fetched, source: Source, windows: list[Window], caps: dict) -> str:
    """The user message: the video, what we want, and the candidate windows with the words spoken in them."""
    s = clips_settings()
    lines = [
        f"Video title: {video.title or 'untitled'}",
        f"Source: {source.name}",
        f"Length: {fetched.duration_s or video.duration_s:.0f} seconds",
        (
            f"Wanted: up to {caps[ClipKind.SHORT] + 2} Shorts ({s.short_min_s:.0f}-{s.short_max_s:.0f} s each)"
            f" and up to {caps[ClipKind.LONG] + 1} long clips ({s.long_min_s:.0f}-{s.long_max_s:.0f} s each)."
        ),
    ]
    if source.avoid_spoilers:
        lines.append("Story spoilers will be dropped, so mark them honestly.")
    lines.append("Candidate windows (signals are loudness, replays, bursts of cuts; values 0 to 1):")
    for i, win in enumerate(windows, 1):
        sig = ", ".join(f"{k} {v:.2f}" for k, v in win.signals.items()) or "none"
        lines.append(f"{i}. {win.start_s:.1f}-{win.end_s:.1f} s, {_kind_of(win).value}, score {win.score:.2f}, signals: {sig}")
        words = captions_text(fetched.captions, win.start_s, win.end_s)
        if words:
            lines.append(f"   said: {words}")
    return "\n".join(lines)


def captions_text(captions: list[Caption], start_s: float, end_s: float, limit: int = CAPTION_CHARS) -> str:
    """The words spoken inside a window, cut to `limit` characters."""
    text = " ".join(c.text.strip() for c in captions if c.end_s > start_s and c.start_s < end_s and c.text.strip())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ------------------------------------------------------------------ pictures (optional)


def grab_frame(video_path: Path, at_s: float, out_path: Path) -> bytes:
    """One small JPEG frame from the file. Tests replace this."""
    from core.config import get_settings
    from core.proc import run_checked

    cmd = [get_settings().ffmpeg_bin, "-hide_banner", "-y", "-ss", f"{at_s:.2f}", "-i", str(video_path),
           "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "6", str(out_path)]
    run_checked(cmd, timeout=60)
    return out_path.read_bytes()


def grab_candidate_frames(fetched: Fetched, windows: list[Window]) -> list[bytes]:
    """A few small frames from the best candidate windows. Empty when the file is missing or ffmpeg fails."""
    path = Path(fetched.video_path) if fetched.video_path else None
    if path is None or not path.exists():
        return []
    frames: list[bytes] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for win in windows[:VISION_CANDIDATES]:
                span = win.end_s - win.start_s
                for k in range(VISION_FRAMES):
                    at = win.start_s + span * (k + 1) / (VISION_FRAMES + 1)
                    frames.append(grab_frame(path, at, Path(tmp) / f"f{len(frames)}.jpg"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("clips: could not grab frames: %s", exc)
        return []
    return frames


# ------------------------------------------------------------------ validation


def _num(value, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _flag(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return bool(value)


def _short_text(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return (cut.rsplit(" ", 1)[0] if " " in cut[limit // 2 :] else cut).rstrip(" ,.;:-")


def _fit_length(start: float, end: float, kind: ClipKind, duration: float) -> tuple[float, float] | None:
    """Clamp to the video and into the kind's length range; None when it cannot fit."""
    s = clips_settings()
    lo, hi = (s.short_min_s, s.short_max_s) if kind is ClipKind.SHORT else (s.long_min_s, s.long_max_s)
    hi = min(hi, duration * MAX_SHARE_OF_SOURCE)  # never re-upload (most of) a creator's whole video
    if hi < lo or start >= duration or end <= 0:
        return None
    asked = end - start
    start, end = max(0.0, start), min(duration, end)
    if end - start < 0.5 * asked:
        return None  # most of the asked range lies outside the video: a made-up time, not a nudge
    if end - start > hi:
        end = start + hi
    if end - start < lo:
        end = min(duration, start + lo)
        start = max(0.0, end - lo)
    if end - start < lo - 1e-6 or end <= start:
        return None
    return round(start, 2), round(end, 2)


def _to_moment(item: dict, duration: float) -> Moment | None:
    start, end = _num(item.get("start_s")), _num(item.get("end_s"))
    if start is None or end is None or end <= start:
        return None
    kind_raw = str(item.get("kind") or "").strip().lower()
    kind = ClipKind(kind_raw) if kind_raw in {k.value for k in ClipKind} else None
    if kind is None:
        kind = ClipKind.LONG if end - start > clips_settings().short_max_s else ClipKind.SHORT
    fitted = _fit_length(start, end, kind, duration)
    if fitted is None:
        return None
    score = max(0.0, min(1.0, _num(item.get("score"), 0.5) or 0.0))
    return Moment(
        start_s=fitted[0], end_s=fitted[1], kind=kind,
        summary=_short_text(item.get("summary"), SUMMARY_MAX),
        hook=_short_text(item.get("hook"), HOOK_MAX),
        reason=_short_text(item.get("reason"), SUMMARY_MAX),
        score=round(score, 3), spoiler=_flag(item.get("spoiler")), has_music=_flag(item.get("has_music")),
    )


def _same_moment(a: Moment, b: Moment, by_text: bool = True) -> bool:
    shorter = min(a.end_s - a.start_s, b.end_s - b.start_s)
    overlap = min(a.end_s, b.end_s) - max(a.start_s, b.start_s)
    if overlap > MAX_OVERLAP * shorter:
        return True
    if abs(a.start_s - b.start_s) < MIN_START_GAP_S:
        return True
    if by_text and a.summary and b.summary:
        return difflib.SequenceMatcher(None, a.summary.lower(), b.summary.lower()).ratio() >= SIMILAR_SUMMARY
    return False


def allowed(moment: Moment, source: Source | None) -> bool:
    """False for a story spoiler or licensed music when the source says to drop them."""
    if source is None:
        return True
    if moment.spoiler and source.avoid_spoilers:
        return False
    return not (moment.has_music and source.skip_music)


def validate_moments(
    raw: list[dict], duration: float, caps: dict[ClipKind, int], source: Source | None = None
) -> list[Moment]:
    """Turn the model's items into safe Moments: range-checked, de-duplicated and capped per kind.

    Spoilers and licensed music are dropped before the cap, so the spare moments
    the model offered take their place."""
    moments = [m for m in (_to_moment(item, duration) for item in raw) if m is not None]
    return _select([m for m in moments if allowed(m, source)], caps)


def _select(moments: list[Moment], caps: dict[ClipKind, int], by_text: bool = True) -> list[Moment]:
    kept: list[Moment] = []
    count = {ClipKind.SHORT: 0, ClipKind.LONG: 0}
    for m in sorted(moments, key=lambda x: x.score, reverse=True):
        if count[m.kind] >= caps.get(m.kind, 0):
            continue
        if any(_same_moment(m, k, by_text) for k in kept if k.kind is m.kind):
            continue
        kept.append(m)
        count[m.kind] += 1
    return kept


def fallback_moments(candidates: list[Window], video: SourceVideo, duration: float, caps: dict[ClipKind, int]) -> list[Moment]:
    """With no model answer: the best-scoring windows, neutral summary, empty hook."""
    title = _short_text(video.title, 120) or "the video"
    moments = []
    for win in candidates:
        kind = _kind_of(win)
        fitted = _fit_length(win.start_s, win.end_s, kind, duration)
        if fitted is None:
            continue
        signals = ", ".join(win.signals) or "even spacing"
        moments.append(Moment(
            start_s=fitted[0], end_s=fitted[1], kind=kind, summary=f"A stretch of {title}",
            reason=f"picked from signals: {signals}", score=round(max(0.0, min(1.0, win.score)), 3),
        ))
    return _select(moments, caps, by_text=False)  # neutral summaries are all alike
