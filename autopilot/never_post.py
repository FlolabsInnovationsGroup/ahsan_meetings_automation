"""Meetings the autopilot must never process or post (owner, 2026-11-05):
Operations, Project Management, Legal, HR and 1-to-1 meetings are internal.

They share a Zoom account with the meetings that do go up, so the account
can't tell them apart. A meeting is recognised by its title (whole words,
capitals and punctuation don't matter) or by its Zoom meeting ID, which stays
the same when a repeating meeting is renamed. The list is built in, so it
works from the moment it is deployed, and the side panel can change it (saved
in storage_dir/state/prefs.json).
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from ledger import prefs

DEFAULT_ENTRIES = (
    "operation",
    "operations",
    "ops",
    "project management",
    "project manager",
    "project managers",
    "pm",  # also catches "Demo at 5 PM": it says why, and a private meeting posted can't be taken back
    "pmo",
    "legal",
    "hr",
    "h r",
    "human resources",
    "1 to 1",
    "1 on 1",
    "1:1",
    "1to1",
    "1on1",
    "one to one",
    "one on one",
    "84116996599",  # Operations team meeting
    "85138528372",  # Project Management Team meeting
    "89716953476",  # Ethics & Legal Team meeting
)
MAX_ENTRIES = 500
MAX_ENTRY_CHARS = 100

# a dotted number stays one word: "Release 1.1.1" holds no "1:1"
_WORD = re.compile(r"\d+(?:\.\d+)+|[^\W_]+")
_ZOOM_ID = re.compile(r"\d{9,12}")


@lru_cache(maxsize=4096)  # every open meeting is checked against every entry at every step
def _words(text: str) -> str:
    """'1-to-1: Opérations' -> '1 to 1 operations'."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c))
    return " ".join(_WORD.findall(plain.casefold()))


def _as_zoom_id(entry: str) -> str | None:
    """An entry that is only a Zoom meeting ID, written with or without its spaces."""
    digits = re.sub(r"[\s-]", "", entry)
    return digits if _ZOOM_ID.fullmatch(digits) else None


def entries() -> list[str]:
    """The list in force: the side panel's, else the built-in one."""
    saved = prefs.never_post()
    return list(DEFAULT_ENTRIES) if saved is None else saved


def set_entries(raw: list[str]) -> list[str]:
    """Save the side panel's list. Raises ValueError for an entry that could
    never match, rather than leave someone thinking it protects a meeting."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        entry = " ".join((item or "").split())
        if not entry:
            continue
        if len(entry) > MAX_ENTRY_CHARS:
            raise ValueError(f'"{entry[:40]}…" is too long (at most {MAX_ENTRY_CHARS} characters).')
        if not _words(entry):
            raise ValueError(f'"{entry}" has no letters or numbers to look for.')
        key = _as_zoom_id(entry) or _words(entry)
        if key not in seen:
            seen.add(key)
            cleaned.append(entry)
    if len(cleaned) > MAX_ENTRIES:
        raise ValueError(f"The list can hold at most {MAX_ENTRIES} entries.")
    prefs.set_never_post(cleaned)
    return cleaned


def match(topic: str | None, meeting_id: object = None, listed: list[str] | None = None) -> str | None:
    """Why this meeting must not be posted, or None. `listed` saves reading
    the saved list again when many meetings are checked in a row."""
    title = f" {_words(topic or '')} "
    wanted_id = str(meeting_id).replace(" ", "") if meeting_id else ""
    for entry in entries() if listed is None else listed:
        zoom_id = _as_zoom_id(entry)
        if zoom_id is not None:
            if zoom_id == wanted_id:
                return f"its Zoom meeting ID {zoom_id} is on the never-post list"
        elif _words(entry) and f" {_words(entry)} " in title:
            return f'its title has "{entry}", which is on the never-post list'
    return None
