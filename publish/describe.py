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
import unicodedata
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger
from core.models import JobRecord, RenderManifest
from core.naming import _strip_dates, date_in_text, meeting_date
from core.timeline import fmt_clock, parse_rate
from publish import settings as pub_settings

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
    # what is said in the clip, when the render recorded it ("" when not): used
    # to check that a title really is about the clip
    text: str = ""


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
                                    str(row.get("hook") or ""), start, end, at_s,
                                    str(row.get("text") or "")))
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


def _title_meeting_date(job: JobRecord) -> str:
    """The meeting's name, with the day when the name has none."""
    topic = one_line(job.title or "", TITLE_LIMIT)
    if not topic:
        return one_line(f"Meeting {meeting_date(job)}")
    if date_in_text(topic) is None:
        day = meeting_date(job)
        return f"{one_line(topic, TITLE_LIMIT - len(day) - 3)} — {day}"
    return topic


def _chapters_text(job: JobRecord) -> str:
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


ORG_LINE = "A real R&D team meeting from FloLabs Innovations Group."
# Room kept free for the shorts list that run.py appends (a heading and about
# four lines of "• title — link", each up to ~150 bytes).
SHORTS_ROOM = 800
_FILLER = {"meeting", "meetings", "weekly", "team", "sync", "call", "standup", "update", "updates"}
_STOP = {
    "this", "that", "with", "from", "have", "what", "when", "your", "about", "into", "than", "then", "them",
    "they", "their", "there", "these", "those", "were", "will", "would", "could", "should", "which", "while",
    "where", "does", "more", "most", "some", "only", "just", "also", "been", "being", "over", "after",
    "before", "here", "make", "makes", "made", "like", "very", "much", "many", "need", "needs", "how",
}
_LEADING_TIME = re.compile(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s*")
_ANY_TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?!\d)")
_CLOCK_TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?!\d)(?:\s*[ap]\.?m\b\.?)?|\b\d{1,2}\s*[ap]m\b", re.IGNORECASE)
_CHAPTER_LINE = re.compile(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s+(.+?)\s*$")
_HASHTAG_MAX = 3
_BRAND_TAG = "#FloLabs"


def series_of(title: str) -> str:
    """The team or series in a Zoom meeting name: "IoT Team Meeting (2026-11-04)"
    -> "IoT". Dates, filler words (meeting, weekly, team ...) and stray
    punctuation go; the other words stay as written. "" when nothing is left."""
    text = _CLOCK_TIME.sub(" ", _strip_dates(unicodedata.normalize("NFKC", title or "")))
    words = []
    for raw in text.split():
        word = raw.strip(" \t\"'()[]{}<>|,;:.!?/\\-–—_*&+=~`@#$%^")
        if word and any(c.isalnum() for c in word) and word.lower() not in _FILLER:
            words.append(word)
    return " ".join(_ANGLE.sub(" ", " ".join(words)).split())


def series_label(job: JobRecord) -> str:
    """"FloLabs <Series>", or just "FloLabs" when the meeting has no series."""
    series = series_of(job.title or "")
    if series.lower().startswith("flolabs"):
        series = series[len("flolabs"):].strip()
    return f"FloLabs {series}" if series else "FloLabs"


def _series_tag(job: JobRecord) -> str:
    """"Multi-modal perception" -> "#MultiModalPerception"; "" when empty or
    the same as the brand tag."""
    words = re.findall(r"[^\W_]+", series_of(job.title or ""))
    tag = "".join(w[0].upper() + w[1:] for w in words)[:30]
    return f"#{tag}" if tag and f"#{tag}".lower() != _BRAND_TAG.lower() else ""


def _main_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[^\W\d_]{4,}", text.lower()) if w not in _STOP}


_NUMBER_WORDS = {
    w: str(i) for i, w in enumerate((
        "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
        "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
        "nineteen", "twenty"))
}
_NUMBER_WORDS.update({"thirty": "30", "forty": "40", "fifty": "50", "sixty": "60",
                      "seventy": "70", "eighty": "80", "ninety": "90", "hundred": "100"})
# a number standing on its own in a title ("800", "3.3V", "12 ms"); the digits
# inside a name like "ESP32" are not a number the title states
_TITLE_NUMBER = re.compile(r"(?<![A-Za-z0-9.,])\d+(?:[.,]\d+)*(?!\d|(?:st|nd|rd|th)\b)", re.IGNORECASE)
_ANY_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _numbers_said(text: str) -> set[str]:
    said = {n.replace(",", "") for n in _ANY_NUMBER.findall(text)}
    said.update(_NUMBER_WORDS[w] for w in re.findall(r"[a-z]+", text.lower()) if w in _NUMBER_WORDS)
    return said


def _grounded(title: str, text: str) -> bool:
    """With the clip's own text at hand, a title counts only when at least half
    of its main words are said in the clip and every number it states was said
    too (as digits, or as a word for 0 to 20); without text the re-rank title
    is trusted."""
    if not text.strip():
        return True
    said = _numbers_said(text)
    if any(n.replace(",", "") not in said for n in _TITLE_NUMBER.findall(title)):
        return False
    words = _main_words(title)
    if not words:
        return True
    spoken = _main_words(text)
    stems = {w[:5] for w in spoken}
    return sum(1 for w in words if w in spoken or w[:5] in stems) * 2 >= len(words)


_GENERIC_SPEAKER = re.compile(r"(?:speaker|spk|guest|unknown|participant)\W*\d*|\d+", re.IGNORECASE)


def _speaker_names(job: JobRecord) -> set[str]:
    """The name parts (3 letters or more, lower-cased) of the people who spoke in
    the meeting, from the transcript the job kept. Empty when there is no
    transcript left or its speakers are only labels like "SPEAKER_01"."""
    data = _read_json(get_settings().jobs_dir / job.job_id / "segments.json")
    if not isinstance(data, list):
        return set()
    names: set[str] = set()
    for row in data:
        label = str(row.get("speaker") or "").strip() if isinstance(row, dict) else ""
        if not label or _GENERIC_SPEAKER.fullmatch(label) or label in names:
            continue
        names.add(label)
    parts = set()
    for label in names:
        parts.update(w.lower() for w in re.findall(r"[^\W\d_]{3,}", label))
    return parts


def _names_someone(title: str, names: set[str]) -> bool:
    """Whether a title names one of `names`: a capitalised word that is a part of
    a speaker's name ("Ahsan explains the cache")."""
    return any(w[0].isupper() and w.lower() in names for w in re.findall(r"[^\W\d_]{3,}", title))


def _best_moment(job: JobRecord) -> tuple[str, str]:
    """(title, why) of the moment the full video is best known for: the first
    short that passes the grounding check, else the top highlight of the reel
    (both stay in manifest.json after the files are removed). ("", "") if none."""
    # a short title that failed the check must not come back through the reel,
    # whose rows keep no clip text to check against
    # A title never names a person: one that does is passed over like one that
    # fails the check (the speakers are known only when the job kept them).
    names = _speaker_names(job)
    rejected: set[str] = set()
    for short in load_shorts(job):
        title = one_line(short.title)
        if title and _grounded(title, short.text) and not _names_someone(title, names):
            return title, one_line(_LEADING_TIME.sub("", short.hook), 200)
        if title:
            rejected.add(title.lower())
    data = _read_json(get_settings().jobs_dir / job.job_id / "manifest.json")
    rows = data.get("highlights") if isinstance(data, dict) else None
    if isinstance(rows, list):
        for row in rows:
            title = one_line(row.get("title") or "") if isinstance(row, dict) else ""
            if title and not rejected and not _names_someone(title, names):
                return title, ""
    return "", ""


def _posted_titles() -> set[str]:
    try:
        # imported here because the ledger sits beside this package
        from ledger.uploads import get_ledger
        return {u.title.strip().lower() for u in get_ledger().posted() if u.title}
    except Exception:  # a title check must never stop a publish
        logger.warning("could not read the posted list to check for a repeated title", exc_info=True)
        return set()


def _title_candidates(job: JobRecord) -> list[str]:
    """The topic-first titles this meeting could get, best first: the plain
    "<moment> | FloLabs <Series>", then the same with the day added."""
    label = series_label(job)
    room = TITLE_LIMIT - len(label) - 3
    moment, _ = _best_moment(job)
    if not moment or room < 15:
        return []
    day = meeting_date(job)
    return [f"{one_line(moment, room)} | {label}", f"{one_line(moment, room - len(day) - 3)} ({day}) | {label}"]


def title_variants(job: JobRecord) -> set[str]:
    """Every title the full video of this job may carry, lower-cased, ignoring
    what is already posted. The autopilot's duplicate check compares these, so
    the day-adding dodge in full_title cannot hide a repeat."""
    if pub_settings.current().title_style != "topic_first":
        return {_title_meeting_date(job).lower()}
    return {t.lower() for t in _title_candidates(job)} | {_title_meeting_date(job).lower()}


def full_title(job: JobRecord) -> str:
    """The full video's title: "<best moment> | FloLabs <Series>" (topic_first),
    or the meeting's name and day (meeting_date)."""
    if pub_settings.current().title_style != "topic_first":
        return _title_meeting_date(job)
    candidates = _title_candidates(job)
    if candidates:
        posted = _posted_titles()
        for title in candidates:
            if title.lower() not in posted:
                return title
    return _title_meeting_date(job)


def _chapter_entries(job: JobRecord) -> list[str]:
    """Chapter titles in order (from chapters.json, else the chapter text)."""
    data = _read_json(get_settings().jobs_dir / job.job_id / "chapters.json")
    entries = data.get("entries") if isinstance(data, dict) else None
    try:
        if isinstance(entries, list):
            return [one_line(e["title"]) for e in entries]
    except (KeyError, TypeError):
        pass
    found = (_CHAPTER_LINE.match(line) for line in _chapters_text(job).splitlines())
    return [one_line(m[1]) for m in found if m]


def _hashtags(job: JobRecord, extra: str = "") -> str:
    tags: list[str] = []
    for tag in (_BRAND_TAG, _series_tag(job), extra):
        if tag and tag.lower() not in {t.lower() for t in tags} and tag.lower() != "#shorts":
            tags.append(tag)
    return " ".join(tags[:_HASHTAG_MAX])


def _no_times(text: str) -> str:
    """YouTube reads any m:ss in the description as a timestamp, and the chapter
    list only works when the first one is 0:00, so "9:30" becomes "9.30"."""
    return _ANY_TIME.sub(lambda m: m[0].replace(":", "."), text)


def _summary_lines(job: JobRecord) -> list[str]:
    """The lines above the chapters. None starts with a time, which would break
    YouTube's chapter list."""
    lines = []
    _, why = _best_moment(job)
    if why:
        lines.append(_no_times(why))
    titles = [_no_times(t) for t in _chapter_entries(job)[1:] if t]
    if titles:
        lines.append(one_line("Also in this meeting: " + ", ".join(titles[:3]), 250))
    series = series_of(job.title or "")
    try:
        d = date.fromisoformat(meeting_date(job))
        day = f"{d.day} {d:%B %Y}"
    except ValueError:
        day = ""
    if series and day:
        lines.append(f"{series} team meeting, {day}. {ORG_LINE}")
    elif series:
        lines.append(f"{series} team meeting. {ORG_LINE}")
    else:
        lines.append(ORG_LINE)
    return lines


def full_description(job: JobRecord) -> str:
    """What a viewer and a search engine read about the full video: (topic_first)
    a short summary, then the chapters, then links and hashtags. The chapters
    are never cut; extras give way so the text, plus the shorts list run.py
    adds later, stays within YouTube's 5000 bytes."""
    cfg = pub_settings.current()
    chapters = _chapters_text(job)
    summary = "\n".join(_summary_lines(job)) if cfg.title_style == "topic_first" else ""
    tags = _hashtags(job) if cfg.hashtags else ""
    links = cfg.links_text.strip()

    def build(summary_text: str, links_text: str) -> str:
        return "\n\n".join(p for p in (summary_text, chapters, links_text, tags) if p)

    budget = DESCRIPTION_LIMIT - SHORTS_ROOM
    text = build(summary, links)
    if len(text.encode("utf-8")) > budget and links:  # links go first: keep as many whole lines as fit
        kept: list[str] = []
        for line in links.splitlines():
            if len(build(summary, "\n".join([*kept, line])).encode("utf-8")) > budget:
                break
            kept.append(line)
        text = build(summary, "\n".join(kept))
    if len(text.encode("utf-8")) > budget and summary:
        text = build("", "")
    return text


def _topic_tag(title: str) -> str:
    """A hashtag from a proper noun or tool name in a Short's title ("ESP32",
    "FloBrain", "IoT"); "" when there is none."""
    for raw in title.split():
        word = "".join(c for c in raw if c.isalnum())
        has_digit = any(c.isdigit() for c in word) and any(c.isalpha() for c in word)
        camel = re.fullmatch(r"[A-Z]?[a-z]+[A-Z][A-Za-z0-9]*", word) is not None
        if len(word) >= 3 and (has_digit or camel):
            return f"#{word}"
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
    if pub_settings.current().hashtags:
        tags = _hashtags(job, _topic_tag(info.title))
        if tags:
            lines += ["", tags]
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
