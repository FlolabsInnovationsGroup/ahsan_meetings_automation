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
import threading
import unicodedata
from functools import lru_cache

from ledger import files, prefs

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
    # Added 2026-11-14 (YouTube growth research): meetings that are about other
    # people's business or say so in the title. A title that says it is
    # internal or confidential must never reach the channel.
    "internal",
    "confidential",
    "private",
    "nda",
    "interview",
    "interviews",
    "investor",
    "investors",
    "partner",
    "partners",
)
# The words added on 2026-11-14. A list the owner saved before that has none of
# them and would never get them, so they are added to it once (see
# _add_new_defaults_once); the flag below records that it was done.
NEW_DEFAULTS_V2 = DEFAULT_ENTRIES[-10:]
DEFAULTS_V2_FLAG = "never_post_defaults_v2"
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
    _add_new_defaults_once()
    saved = prefs.never_post()
    return list(DEFAULT_ENTRIES) if saved is None else saved


# prefs files already looked at by this process (their folder), so the check
# below reads the file once, not at every title that is matched
_defaults_checked: set = set()
_defaults_lock = threading.Lock()


def _add_new_defaults_once() -> None:
    """On first use (per process and storage folder): a saved list that predates
    the 2026-11-14 words gets the missing ones added, and the flag is set so it
    never happens again. Without it a list saved earlier would keep letting an
    "internal" or "investor" meeting through. After the flag is set, whatever
    the owner changes in the side panel stays as they left it."""
    path = files.state_dir() / prefs.FILE
    with _defaults_lock:
        if path in _defaults_checked:
            return
        # the same lock prefs uses, so a save from the side panel can't be lost
        with prefs._write_lock:  # prefs has no public helper for a flag in the same file
            data = files.read_json(path, None)
            if data is None and not path.exists():
                data = {}  # nothing saved yet: only the flag is written
            # a file that exists but can't be read is left alone (rewriting it would lose what is in it)
            if isinstance(data, dict) and not data.get(DEFAULTS_V2_FLAG):
                saved = data.get("never_post")
                if isinstance(saved, list) and all(isinstance(item, str) for item in saved):
                    have = {_as_zoom_id(item) or _words(item) for item in saved}
                    data["never_post"] = saved + [w for w in NEW_DEFAULTS_V2 if _words(w) not in have]
                data[DEFAULTS_V2_FLAG] = True
                files.write_json(path, data)
        _defaults_checked.add(path)


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
    # a list the owner saves is theirs as it stands: the 2026-11-14 words must
    # not be added to it later, over a word they chose to take out
    _add_new_defaults_once()
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


def match_words(text: str | None, listed: list[str] | None = None) -> str | None:
    """The first word or phrase of the never-post list found in `text` (a
    title or a description that is about to go out), or None. Zoom IDs are not
    looked for: they name a meeting, not words."""
    found = f" {_words(text or '')} "
    for entry in entries() if listed is None else listed:
        if _as_zoom_id(entry) is None and _words(entry) and f" {_words(entry)} " in found:
            return entry
    return None


def match_job(job: object, listed: list[str] | None = None) -> str | None:
    """Why a job's meeting must not be posted, or None. Looks at what the job
    kept of the meeting: the Zoom topic and ID, and the title it carries.
    Both titles are checked, since a rename in either place must not hide it."""
    info = getattr(job, "zoom_meeting", None)
    info = info if isinstance(info, dict) else {}
    zoom_id = info.get("meeting_id") or info.get("id")
    for topic in (info.get("topic"), getattr(job, "title", None)):
        why = match(topic, zoom_id, listed)
        if why is not None:
            return why
    return None
