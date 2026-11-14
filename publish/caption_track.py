"""Our own English caption track for each full video (and, if switched on,
each Short), uploaded later through the follow-up queue (publish/extras.py).

The text is the kept transcript: what final.mp4 actually says. Speech that was
cut out is not in it, and neither are speaker names. The intro, the title card
and the outro have no speech, so no caption lands on them.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from core.logging import get_logger
from core.models import Word
from publish import extras, optional

logger = get_logger(__name__)

UNITS = 400  # YouTube quota cost of captions.insert
TRACK_NAME = "English (FloLabs)"
MAX_CHARS = 42


def _job_dir(job_id: str) -> Path:
    from ingest.store import job_dir

    return job_dir(job_id)


def _read_text(job_id: str, name: str) -> str | None:
    """A file of the job from its folder, or from bundle.zip when the folder no
    longer has it. None when neither does."""
    folder = _job_dir(job_id)
    try:
        path = folder / name
        if path.is_file():
            return path.read_text(encoding="utf-8")
        bundle = folder / "bundle.zip"
        if bundle.is_file():
            with zipfile.ZipFile(bundle) as zf:
                return zf.read(name).decode("utf-8")
    except (OSError, KeyError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        logger.warning("could not read %s for job %s: %s", name, job_id, exc)
    return None


def _json(job_id: str, name: str) -> dict:
    text = _read_text(job_id, name)
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _job_id(job) -> str:
    return job if isinstance(job, str) else (getattr(job, "job_id", None) or getattr(job, "id", None) or "")


def full_srt(job) -> str:
    """SRT text for final.mp4 (times are positions in final.mp4), or '' when
    the kept transcript is missing or has nothing to caption."""
    from slice.captions import caption_cues, srt_text

    job_id = _job_id(job)
    clean = _json(job_id, "transcript_clean.json")
    lines = clean.get("lines") or []
    if not lines:
        return ""
    final = _json(job_id, "manifest.json").get("final") or {}

    def number(*values) -> float:
        for value in values:
            try:
                if value is not None:
                    return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0

    intro_s = number(final.get("intro_s"), clean.get("intro_s"))
    outro_s = number(final.get("outro_s"), clean.get("outro_s"))
    ends = [number(row.get("end")) for row in lines if isinstance(row, dict)]
    duration = number(final.get("duration_s"), (max(ends) + outro_s) if ends else 0.0)
    if duration <= 0:
        return ""

    # one section at a time (highlights reel, then the meeting), so a caption
    # never runs across the title card between them
    sections: dict[str, list[Word]] = {}
    for row in lines:
        try:
            text = str(row.get("text") or "").strip()
            if text:
                sections.setdefault(str(row.get("section") or ""), []).append(
                    Word(word=text, start=float(row["start"]), end=float(row["end"]), speaker=""))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    outro_start = duration - outro_s if outro_s > 0 else duration
    cleaned_start = number(final.get("cleaned_starts_at_s"), clean.get("cleaned_starts_at_s"))
    reel_len = number(final.get("highlights_s"))
    reel_end = intro_s + reel_len if reel_len > 0 else 0.0
    if reel_end <= 0 and sections.get("highlights"):
        reel_end = max(w.end for w in sections["highlights"])  # lines are already clamped to the reel
    if cleaned_start > 0:
        reel_end = min(reel_end, cleaned_start) if reel_end > 0 else cleaned_start
    # each section gets its own window; a cue is cut at the window's end, never dropped whole
    cues = []
    for name, words in sections.items():
        if name == "highlights" and reel_end > 0:
            start_at, end_at = intro_s, min(reel_end, outro_start)
        elif name == "meeting" and cleaned_start > 0:
            start_at, end_at = max(cleaned_start, intro_s), outro_start
        else:
            start_at, end_at = intro_s, outro_start
        if end_at <= start_at:
            continue
        cues += [c for c in caption_cues(words, 0.0, end_at, max_chars=MAX_CHARS, max_words=12, max_pause_s=0.8)
                 if c.start >= start_at - 1e-6 and c.end > c.start]
    cues.sort(key=lambda c: c.start)
    # a cue never overlaps the next one
    fixed = []
    for i, cue in enumerate(cues):
        if i + 1 < len(cues) and cue.end > cues[i + 1].start:
            cue = type(cue)(cue.start, cues[i + 1].start, cue.text)
        if cue.end > cue.start:
            fixed.append(cue)
    return srt_text(fixed) if fixed else ""


def short_srt(job, index: int) -> str:
    """The SRT already written next to short number `index`, or ''."""
    return _read_text(_job_id(job), f"shorts/short_{int(index):02d}.srt") or ""


# ------------------------------------------------------------------ the queue


def _skipped(item, video_kind: str, reason: str) -> extras.Task:
    return extras.Task(kind="captions", item_key=getattr(item, "key", ""), video_kind=video_kind,
                       state=extras.SKIPPED, reason=reason)


def build(job, item, posted, *, video_kind: str, index: int | None = None, cfg=None) -> extras.Task | None:
    if video_kind == "full":
        if not getattr(cfg, "captions_full", True):
            return None
    elif video_kind == "short":
        if not getattr(cfg, "captions_shorts", False):
            return None
    else:
        return None  # topic cuts are not covered
    privacy = (getattr(posted, "privacy", "") or getattr(item, "privacy", "") or "").lower()
    if privacy == "private" and not getattr(posted, "publish_at", None):
        return _skipped(item, video_kind, "plain private upload; no caption track")
    job_id = getattr(item, "job_id", "") or _job_id(job)
    if video_kind == "full":
        srt = full_srt(job_id)
    else:
        srt = short_srt(job_id, index if index is not None else getattr(item, "index", None) or 0)
    if not srt.strip():
        return _skipped(item, video_kind, "no transcript to make captions from")
    task = extras.Task(kind="captions", item_key=getattr(item, "key", ""), video_kind=video_kind)
    dest = extras.files_dir() / f"{task.id}.srt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(srt, encoding="utf-8")
    task.file = str(dest)
    return task


def run(task: extras.Task) -> optional.Outcome:
    from publish import settings as publishing_settings
    from publish import youtube_extra

    if not task.file or not Path(task.file).is_file():
        return optional.Outcome(optional.SKIPPED, reason="the caption file is gone")
    language = publishing_settings.current().language or "en"
    return optional.call(
        "captions", UNITS,
        lambda: youtube_extra.insert_caption(task.video_id, task.file, language=language, name=TRACK_NAME),
        attempt=task.attempts, forbidden_reason="YouTube would not take a caption track for this video")


extras.register("captions", build, run)
