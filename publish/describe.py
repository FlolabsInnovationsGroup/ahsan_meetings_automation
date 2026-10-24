"""The words and the times that go with each upload (pure: no network, no ffmpeg).

A short is a clip of the SOURCE recording. Where it plays in the full video
is worked out through the render manifest of the cleaned meeting, the same
table the transcript remap uses: final.mp4 = intro + highlights reel + title
card + the cleaned meeting + outro, and the cleaned meeting starts at
job.final_offset_s.

YouTube's rules for what is written here: a title has at most 100 characters
and a description at most 5000 bytes, and neither may contain < or >.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger
from core.models import JobRecord, RenderManifest
from core.naming import date_in_text, meeting_date
from core.timeline import fmt_clock, parse_rate

logger = get_logger(__name__)

TITLE_LIMIT = 100  # characters
DESCRIPTION_LIMIT = 5000  # bytes
# A clip counts as playing in a kept piece of the meeting from this much on.
MIN_OVERLAP_S = 1.0
SHORTS_HEADING = "Shorts from this meeting:"

_ANGLE = re.compile(r"[<>]")
_SHORT_FILE = re.compile(r"shorts/short_(\d+)\.mp4")


@dataclass(frozen=True)
class ShortInfo:
    index: int
    file: str  # its name in job.artifacts and in the zip: shorts/short_01.mp4
    title: str
    hook: str
    source_start: float
    source_end: float
    # whole seconds into the full video where the clip first plays, None when
    # none of it is in the cleaned meeting (a short may use material the
    # cleanup cut)
    at_s: int | None


# ------------------------------------------------------------------ checks


def one_line(text: str, limit: int = TITLE_LIMIT) -> str:
    """One line without < or >, at most `limit` characters."""
    text = " ".join(_ANGLE.sub(" ", str(text or "")).split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def title_problem(title: str) -> str | None:
    if not title.strip():
        return "the title is empty"
    if _ANGLE.search(title):
        return "a title can't contain < or >"
    if len(title) > TITLE_LIMIT:
        return f"a title has at most {TITLE_LIMIT} characters"
    return None


def description_problem(description: str) -> str | None:
    if _ANGLE.search(description):
        return "a description can't contain < or >"
    if len(description.encode("utf-8")) > DESCRIPTION_LIMIT:
        return f"a description has at most {DESCRIPTION_LIMIT} bytes"
    return None


# ----------------------------------------------------------- where it plays


def clip_position(manifest: RenderManifest, start: float, end: float, offset_s: float) -> int | None:
    """Whole seconds into final.mp4 where the source stretch [start, end] first
    plays, or None when none of it was kept.

    `manifest` is the cleaned meeting's: each piece maps source frames
    [src_start, src_end) to output frames from out_start, and the cleaned
    meeting starts `offset_s` into final.mp4 (intro + reel + title card). The
    clip's start is used when it was kept; when it was cut, the first kept
    stretch of the clip is (at least MIN_OVERLAP_S of it, so a sliver at a
    cut doesn't count)."""
    fps = parse_rate(manifest.fps)
    for piece in manifest.pieces:
        piece_start = float(Fraction(piece.src_start_frame) / fps)
        piece_end = float(Fraction(piece.src_end_frame) / fps)
        lo, hi = max(piece_start, start), min(piece_end, end)
        if hi - lo >= MIN_OVERLAP_S:
            out_start = float(Fraction(piece.out_start_frame) / fps)
            return math.floor(offset_s + out_start + (lo - piece_start) + 1e-6)
    return None


def load_shorts(job: JobRecord) -> list[ShortInfo]:
    """The job's rendered shorts, from manifest.json (which stays on the server
    when the individual files are removed after zipping), each with the time
    it plays in the full video. Empty for a job without shorts."""
    job_dir = get_settings().jobs_dir / job.job_id
    manifest = _read_json(job_dir / "manifest.json")
    rows = manifest.get("shorts") if isinstance(manifest, dict) else None
    if not isinstance(rows, list):
        return []
    render = _render_manifest(job)
    shorts = []
    for row in rows:
        try:
            match = _SHORT_FILE.fullmatch(str(row["file"]))
            if match is None:
                continue
            artifact = job.artifacts.get(row["file"])
            if artifact is not None and artifact.status != "ok":
                continue
            start, end = float(row["source_start"]), float(row["source_end"])
            at_s = clip_position(render, start, end, job.final_offset_s) if render is not None else None
            shorts.append(ShortInfo(int(match[1]), row["file"], str(row.get("title") or ""),
                                    str(row.get("hook") or ""), start, end, at_s))
        except (KeyError, TypeError, ValueError):
            logger.warning("job %s: skipping an unreadable shorts row in manifest.json: %r", job.job_id, row)
    return sorted(shorts, key=lambda s: s.index)


def rendered_shorts(job: JobRecord) -> set[int]:
    """The number of every short the job rendered: from its artifacts (the
    record of what was delivered) as well as manifest.json, so a manifest row
    that can't be read never makes a short look like it does not exist."""
    found = {short.index for short in load_shorts(job)}
    for name, artifact in job.artifacts.items():
        match = _SHORT_FILE.fullmatch(name)
        if match is not None and artifact.status == "ok":
            found.add(int(match[1]))
    return found


def _render_manifest(job: JobRecord) -> RenderManifest | None:
    if not job.render_manifest_path:
        return None
    try:
        return RenderManifest.model_validate_json(Path(job.render_manifest_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("job %s: no usable render manifest, so no timestamps for the shorts", job.job_id)
        return None


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------- words


def _topic(job: JobRecord) -> str:
    return one_line(job.title or "", 200)


def full_title(job: JobRecord) -> str:
    """The meeting's name, with the day when the name has none."""
    topic = one_line(job.title or "", TITLE_LIMIT)
    if not topic:
        return one_line(f"Meeting {meeting_date(job)}")
    if date_in_text(topic) is None:
        day = meeting_date(job)
        return f"{one_line(topic, TITLE_LIMIT - len(day) - 3)} — {day}"
    return topic


def full_description(job: JobRecord) -> str:
    """The chapter lines for the full video (empty when it has none)."""
    if job.chapters_path and Path(job.chapters_path).is_file():
        return Path(job.chapters_path).read_text(encoding="utf-8").strip()
    # chapters.txt is removed with the other files after zipping; its
    # entries are kept in chapters.json
    data = _read_json(get_settings().jobs_dir / job.job_id / "chapters.json")
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return ""
    try:
        return "\n".join(f"{fmt_clock(e['t'])} {one_line(e['title'])}" for e in entries)
    except (KeyError, TypeError, ValueError):
        return ""


def short_title(info: ShortInfo, job: JobRecord) -> str:
    return one_line(info.title) or one_line(f"{_topic(job) or 'Meeting'} — clip {info.index}")


def short_description(info: ShortInfo, job: JobRecord, full_url: str | None = None) -> str:
    """The hook, then where the clip comes from. `full_url` (the full video's
    link, when it is already on the channel) adds a link to that moment; links
    don't work in a Short's description, so it is a nicety."""
    lines: list[str] = []
    hook = one_line(info.hook, 300)
    if hook:
        lines += [hook, ""]
    topic = _topic(job)
    day = "" if date_in_text(topic) is not None else f" ({meeting_date(job)})"
    lines.append(f"From the full meeting: {topic}{day}" if topic else f"From the full meeting{day}")
    if info.at_s is not None:
        lines.append(f"This clip starts at {fmt_clock(info.at_s)} in the full video.")
        if full_url:
            lines.append(f"{full_url}?t={info.at_s}")
    elif full_url:
        lines.append(full_url)
    return "\n".join(lines)


def shorts_block(shorts: list[tuple[int, str, str]]) -> str:
    """The "Shorts from this meeting" list, one line per (index, title, url)."""
    return "\n".join([SHORTS_HEADING, *(_block_line(title, url) for _, title, url in shorts)])


def with_shorts_block(description: str, shorts: list[tuple[int, str, str]]) -> tuple[str, list[int]]:
    """`description` followed by the shorts list — as many shorts as fit in
    YouTube's 5000 bytes. Returns the text and the indexes that made it in.
    The list's lines don't start with a timestamp, so YouTube can't mistake
    them for chapters."""
    base = description.rstrip()
    lines = [_block_line(title, url) for _, title, url in shorts]
    for count in range(len(lines), 0, -1):
        text = (base + "\n\n" if base else "") + "\n".join([SHORTS_HEADING, *lines[:count]])
        if len(text.encode("utf-8")) <= DESCRIPTION_LIMIT:
            return text, [index for index, _, _ in shorts[:count]]
    return base, []


def _block_line(title: str, url: str) -> str:
    return f"• {one_line(title)} — {url}"
