"""The rights rules. A source is clipped and posted only when these say yes.

There is no override here and there must never be one: a source with no recorded
basis (or missing evidence for its basis) is never clipped, never posted.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path

from clips.models import (
    POSTABLE_BASES,
    Clip,
    Platform,
    RightsBasis,
    RightsVerdict,
    Source,
    SourceVideo,
)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# What each basis needs on file besides the basis itself.
_NEEDS: dict[RightsBasis, tuple[str, ...]] = {
    RightsBasis.OWN: (),
    RightsBasis.WRITTEN_PERMISSION: ("evidence_note", "evidence_date"),
    RightsBasis.PUBLIC_PERMISSION: ("evidence_url", "evidence_note", "evidence_date"),
    RightsBasis.CAMPAIGN: ("evidence_url", "evidence_date"),
    RightsBasis.CREATIVE_COMMONS: (),
    RightsBasis.OFFICIAL: ("evidence_url", "evidence_date"),
}

_HINTS = {
    "evidence_note": "evidence note (who said yes and where the proof is kept, or the sentence that grants it)",
    "evidence_url": "evidence link (the page where the permission, campaign terms or policy can be read)",
    "evidence_date": "evidence date (the day the proof was read or received, as YYYY-MM-DD)",
}


def _check_date(text: str) -> str | None:
    """A problem with the evidence date, or None when it is fine."""
    text = text.strip()
    if not _ISO_DATE.match(text):
        return f"The evidence date '{text}' is not a date written as YYYY-MM-DD."
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return f"The evidence date '{text}' is not a real calendar date."
    if day > datetime.now(UTC).date():
        return f"The evidence date {text} is in the future."
    return None


def _company_folder(folder: Path) -> str:
    """Why this folder may hold the company's own data (meeting recordings), or ''."""
    from clips.config import clips_settings
    from clips.guard import looks_like_company_data

    try:
        here = folder.expanduser().resolve()
    except OSError:
        return "its path cannot be read"
    for candidate in (here, *here.parents):
        why = looks_like_company_data(candidate)
        if why:
            return f"it is, or sits inside, the company app's data folder ({why})"
    try:
        from core.config import get_settings

        app_dir = Path(get_settings().storage_dir).expanduser().resolve()
    except Exception:  # noqa: BLE001 - settings trouble must not widen what is allowed, see below
        return ""
    own = Path(clips_settings().storage_dir).expanduser().resolve()
    if app_dir == own:
        return ""  # isolated: the app's folder is the clips folder
    if here == app_dir or app_dir in here.parents or here in app_dir.parents:
        return "it is, or holds, the company app's storage folder"
    return ""


def _check_location(source: Source) -> list[str]:
    if source.platform is Platform.FOLDER:
        if not source.folder.strip():
            return ["A folder source needs a folder."]
        if not Path(source.folder).is_dir():
            return [f"The folder '{source.folder}' does not exist."]
        why = _company_folder(Path(source.folder))
        if why:
            return [f"The folder '{source.folder}' cannot be a source: {why}."]
    elif not source.url.strip():
        return [f"A {source.platform.value} source needs its channel link."]
    return []


def check_source(source: Source) -> RightsVerdict:
    """Ok only when the basis is postable and its evidence is on file."""
    basis = source.rights
    if basis not in POSTABLE_BASES or basis not in _NEEDS:
        return RightsVerdict(
            ok=False,
            reasons=["No rights basis is recorded, so this source can be listed but never clipped or posted."],
        )
    reasons = [
        f"Missing {_HINTS[name]}." for name in _NEEDS[basis] if not str(getattr(source, name)).strip()
    ]
    if "evidence_date" in _NEEDS[basis] and source.evidence_date.strip():
        problem = _check_date(source.evidence_date)
        if problem:
            reasons.append(problem)
    reasons.extend(_check_location(source))
    return RightsVerdict(ok=not reasons, reasons=reasons)


def _check_release_gate(source: Source, video: SourceVideo) -> list[str]:
    """Nothing dated before CLIPS_NOT_BEFORE (the game's release date), unless the source is OFFICIAL.
    A video with no known date passes. A setting that is not a date refuses (never crashes)."""
    if source.rights is RightsBasis.OFFICIAL or video.published_at is None:
        return []
    from clips.config import ConfigError, clips_settings

    try:
        gate = clips_settings().not_before_date
    except ConfigError as exc:
        return [str(exc)]
    if gate is None:
        return []
    when = video.published_at
    day = (when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)).date()
    if day < gate:
        return ["dated before CLIPS_NOT_BEFORE (the game's release date)"]
    return []


def check_video(source: Source, video: SourceVideo) -> RightsVerdict:
    """check_source plus the rules that apply to one video."""
    reasons = list(check_source(source).reasons)
    if video.is_live:
        reasons.append("This is a live stream that is still running; wait until it has ended.")
    reasons.extend(_check_release_gate(source, video))
    if source.rights is RightsBasis.CREATIVE_COMMONS and video.license != "creativeCommon":
        reasons.append("This video is not marked Creative Commons on YouTube, so it cannot be clipped.")
    return RightsVerdict(ok=not reasons, reasons=reasons)


def check_clip(clip: Clip, source: Source | None) -> RightsVerdict:
    """The last gate before upload."""
    if source is None:
        return RightsVerdict(ok=False, reasons=["The source of this clip no longer exists."])
    reasons: list[str] = []
    if not source.enabled:
        reasons.append("The source is switched off.")
    reasons.extend(check_source(source).reasons)
    if clip.rights not in POSTABLE_BASES:
        reasons.append("The clip has no rights basis recorded.")
    elif clip.rights != source.rights:
        reasons.append(
            f"The source's rights basis changed from {clip.rights.value} to {source.rights.value} since this clip was made."
        )
    if clip.rights is not RightsBasis.OWN and not clip.credit.strip():
        reasons.append("The clip has no credit line.")
    if clip.evidence_url.strip() != source.evidence_url.strip():
        reasons.append("The source's evidence link is not the one this clip was made under (the source was changed).")
    return RightsVerdict(ok=not reasons, reasons=reasons)


def credit_line(source: Source, video: SourceVideo | None = None) -> str:
    """Who to thank: 'Clip from <credit or name>' and the video link. Empty for our own footage."""
    if source.rights is RightsBasis.OWN:
        return ""
    line = f"Clip from {source.credit.strip() or source.name}"
    if video is not None and video.url.strip():
        line += f" - {video.url.strip()}"
    return line
