"""The words that go on YouTube with a clip: title, description, tags.

The language model only writes the title, the first line and the tags. The rest
of the description is built here in code so the credit line, the fan-channel
line and the hashtags are always right. Pure: no network except the model call.
"""

from __future__ import annotations

import re

from clips.config import ClipsSettings, clips_settings
from clips.models import LLM, ClipKind, Copy, Moment, Source, SourceVideo

TITLE_MAX = {ClipKind.SHORT: 60, ClipKind.LONG: 70}
MAX_HASHTAGS = 3
MAX_TAG_CHARS = 500
MAX_DESCRIPTION = 4500
MAX_TAGS = 12
CATEGORY_GAMING = "20"
FAN_LINE = "Fan channel, not affiliated with Rockstar Games or Take-Two Interactive."
BASE_TAGS = ["GTA 6", "GTA VI", "Grand Theft Auto 6", "gameplay"]
TRIES = 2  # model attempts before we fall back to a plain title

_BAD_CHARS = re.compile(r"[<>]")
_OFFICIAL = re.compile(r"\bofficial(ly)?\b", re.IGNORECASE)
_HASHTAG = re.compile(r"(?<!\w)#\w+")
_SPACES = re.compile(r"\s+")

SYSTEM = (
    "You write YouTube copy for a fan channel that posts GTA 6 gameplay clips. "
    "Answer with a JSON object with keys title, description_first_line and tags (a list of short strings). "
    "Rules: the title is true to the summary you are given and never claims anything that is not in it; "
    "start the title with the game name (GTA 6) followed by a colon; no all-capitals words for shouting; no story spoilers; "
    "never say or imply the clip or channel is official or from Rockstar; no angle brackets; "
    "no hashtags in the title. English only."
)


def _clean(text: object) -> str:
    """One line, no angle brackets, single spaces."""
    return _SPACES.sub(" ", _BAD_CHARS.sub("", str(text or ""))).strip()


def _strip_official(text: str) -> str:
    return _SPACES.sub(" ", _OFFICIAL.sub("", text)).strip()


def _is_shouting(title: str) -> bool:
    letters = [c for c in title if c.isalpha()]
    return len(letters) >= 4 and all(c.isupper() for c in letters)


def _tag_chars(tags: list[str]) -> int:
    """Characters YouTube counts for tags: quotes around multi-word tags, commas between."""
    total = sum(len(t) + (2 if " " in t else 0) for t in tags)
    return total + max(len(tags) - 1, 0)


def check_copy(copy: Copy, *, kind: ClipKind, credit: str) -> list[str]:
    """Problems with the copy in plain words; empty when it is fine."""
    problems: list[str] = []
    title = copy.title.strip()
    limit = TITLE_MAX[kind]
    if not title:
        problems.append("The title is empty.")
    if len(title) > limit:
        problems.append(f"The title is {len(title)} characters; the limit for this clip is {limit}.")
    if _is_shouting(title):
        problems.append("The title is all capitals.")
    if _BAD_CHARS.search(title) or _BAD_CHARS.search(copy.description):
        problems.append("The title or description has < or >, which YouTube refuses.")
    credit = credit.strip()
    if credit and credit not in copy.description:
        problems.append("The description is missing the credit line.")
    hashtags = {h.lower() for h in copy.hashtags} | {h.lower() for h in _HASHTAG.findall(copy.description)}
    if len(hashtags) > MAX_HASHTAGS:
        problems.append(f"There are {len(hashtags)} hashtags; the most allowed is {MAX_HASHTAGS}.")
    if _tag_chars(copy.tags) > MAX_TAG_CHARS:
        problems.append(f"The tags are longer than {MAX_TAG_CHARS} characters in total.")
    if len(copy.description) > MAX_DESCRIPTION:
        problems.append(f"The description is over {MAX_DESCRIPTION} characters.")
    # The credit line may legitimately name an official source, so leave it out of this check.
    body = copy.description.replace(credit, "") if credit else copy.description
    if _OFFICIAL.search(title) or _OFFICIAL.search(body) or any(_OFFICIAL.search(t) for t in copy.tags):
        problems.append("The text claims to be official.")
    return problems


def _shorten(text: str, limit: int) -> str:
    """Cut at a word boundary so the text fits in `limit` characters."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-|")
    return cut or text[:limit]


def _plain_title(moment: Moment, kind: ClipKind) -> str:
    """A safe title from the hook or summary, with the game named when it fits."""
    limit = TITLE_MAX[kind]
    base = _strip_official(_clean(moment.hook) or _clean(moment.summary)) or "GTA 6 gameplay moment"
    if _is_shouting(base):
        base = base.capitalize()
    base = base[:1].upper() + base[1:]
    if "gta" not in base.lower():
        prefix = "GTA 6: "
        return prefix + _shorten(base, limit - len(prefix))
    return _shorten(base, limit)


def _hashtag_list(kind: ClipKind, settings: ClipsSettings) -> list[str]:
    """Settings hashtags, at most three, no repeats. #shorts is not added: YouTube sorts Shorts by shape."""
    tags: list[str] = []
    for raw in settings.hashtags:
        tag = "#" + re.sub(r"\W", "", raw.lstrip("#"))
        if len(tag) > 1 and tag.lower() not in {t.lower() for t in tags}:
            tags.append(tag)
    return [t for t in tags if t.lower() != "#shorts"][:MAX_HASHTAGS]


def _clean_tags(raw: object, settings: ClipsSettings) -> list[str]:
    """A short deduplicated tag list that fits the 500 character limit."""
    items = raw if isinstance(raw, list) else []
    out: list[str] = []
    seen: set[str] = set()
    for item in [*items, *BASE_TAGS, *(h.lstrip("#") for h in settings.hashtags)]:
        tag = _clean(item).lstrip("#")
        if not tag or len(tag) > 40 or _OFFICIAL.search(tag) or tag.lower() in seen:
            continue
        if len(out) >= MAX_TAGS or _tag_chars([*out, tag]) > MAX_TAG_CHARS:
            continue
        seen.add(tag.lower())
        out.append(tag)
    return out


def _build(
    title: str, first_line: str, tags: list[str], *, kind: ClipKind, credit: str, settings: ClipsSettings
) -> Copy:
    hashtags = _hashtag_list(kind, settings)
    parts = [first_line, ""]
    if credit.strip():
        parts += [credit.strip(), ""]
    parts += [FAN_LINE, "", " ".join(hashtags)]
    description = "\n".join(parts).strip()
    return Copy(
        title=title,
        description=description[:MAX_DESCRIPTION],
        tags=tags,
        hashtags=hashtags,
        category_id=CATEGORY_GAMING,
        language="en",
    )


def _ask_model(
    llm: LLM, moment: Moment, source: Source, video: SourceVideo, kind: ClipKind, problems: list[str]
) -> dict:
    user = (
        f"Clip type: {kind.value} (title at most {TITLE_MAX[kind]} characters)\n"
        f"What happens: {moment.summary or moment.hook}\n"
        f"On-screen hook: {moment.hook}\n"
        f"Source video title: {video.title}\n"
        f"Source channel: {source.name}\n"
    )
    if problems:
        user += "Your last answer had these problems, fix them: " + "; ".join(problems) + "\n"
    answer = llm.json(SYSTEM, user, max_tokens=600)
    return answer if isinstance(answer, dict) else {}


def write_copy(
    moment: Moment,
    source: Source,
    video: SourceVideo,
    *,
    kind: ClipKind,
    credit: str,
    llm: LLM | None = None,
) -> Copy:
    """Title, description and tags for a clip. Falls back to plain words without a usable model."""
    settings = clips_settings()
    if llm is not None:
        problems: list[str] = []
        for _ in range(TRIES):
            try:
                answer = _ask_model(llm, moment, source, video, kind, problems)
            except Exception:  # noqa: BLE001 - a failed model call must never stop a clip
                break
            title = _clean(answer.get("title", ""))
            first = _clean(answer.get("description_first_line", "")) or _clean(moment.summary)
            tags = _clean_tags(answer.get("tags"), settings)
            copy = _build(title, first, tags, kind=kind, credit=credit, settings=settings)
            problems = check_copy(copy, kind=kind, credit=credit)
            if not problems:
                return copy
    first = _strip_official(_clean(moment.summary) or _clean(moment.hook)) or "GTA 6 gameplay clip."
    return _build(
        _plain_title(moment, kind),
        first,
        _clean_tags([], settings),
        kind=kind,
        credit=credit,
        settings=settings,
    )
