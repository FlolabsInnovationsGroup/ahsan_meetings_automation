"""The team (series) registry: one list of teams that decides which teams may
post, gives each team one stable name and colour, and holds its playlist IDs.

A team is what is left of a Zoom meeting name once dates and filler words go
("Internet of Things Meeting (2026-11-03)" -> "internet of things"). Each team
has a tier:

- public:  its videos go up as public;
- archive: its videos go up unlisted;
- never:   shown as held back (the never-post list stays the real guard);
- unknown: not decided yet (stored as null).

Saved in storage_dir/state/series.json. Pure logic plus file reads and writes,
no network. A file that can't be read is never overwritten: lookups answer
"unknown" and the problem is logged.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import asdict, dataclass, field
from typing import Any

from autopilot import never_post
from core.logging import get_logger
from ledger import files
from publish import describe

logger = get_logger(__name__)

FILE = "series.json"
TIERS = ("public", "archive", "never")
PLAYLIST_KINDS = ("full", "cuts", "shorts")
MAX_LABEL = 60
MAX_EXAMPLES = 3
MAX_ALIASES = 20
COLOURS = 6

_PLAYLIST_ID = re.compile(r"^PL[A-Za-z0-9_-]{10,64}$")
_HASHTAG = re.compile(r"^#[A-Za-z0-9]+$")
_FIELDS = {"label", "tier", "hashtags", "playlists", "aliases"}

# one lock for the file; re-entrant because seeding runs inside a first read
_lock = threading.RLock()
_DAMAGED = object()


@dataclass
class Row:
    key: str
    label: str
    tier: str | None = None  # 'public' | 'archive' | 'never' | None (unknown)
    hashtags: list[str] = field(default_factory=list)
    playlists: dict[str, str] = field(default_factory=lambda: {k: "" for k in PLAYLIST_KINDS})
    aliases: list[str] = field(default_factory=list)
    seen: int = 0
    last_seen: str = ""
    examples: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ names


def series_key(topic: str | None) -> str:
    """'Internet of Things Meeting (2026-11-03)' -> 'internet of things';
    '' when nothing is left (a filler-only title such as 'Team Meeting')."""
    return never_post._words(describe.series_of(topic or ""))


def _label_of(topic: str) -> str:
    series = describe.series_of(topic or "")
    if series.lower().startswith("flolabs") and series[len("flolabs"):].strip():
        series = series[len("flolabs"):].strip()
    return series


def colour_index(key: str) -> int:
    """0-5, the same for a team every time (the team chip on thumbnails)."""
    digest = hashlib.sha256(never_post._words(key).encode("utf-8")).hexdigest()
    return int(digest, 16) % COLOURS


# ------------------------------------------------------------------ file


def _path():
    return files.state_dir() / FILE


def _row_from(raw: Any) -> Row:
    if not isinstance(raw, dict) or not isinstance(raw.get("key"), str) or not raw["key"]:
        raise ValueError("row without a key")
    tier = raw.get("tier")
    playlists = {k: "" for k in PLAYLIST_KINDS}
    if isinstance(raw.get("playlists"), dict):
        for k in PLAYLIST_KINDS:
            value = raw["playlists"].get(k, "")
            playlists[k] = value if isinstance(value, str) else ""
    return Row(
        key=raw["key"],
        label=str(raw.get("label") or raw["key"]),
        tier=tier if tier in TIERS else None,
        hashtags=[h for h in raw.get("hashtags", []) if isinstance(h, str)][:2],
        playlists=playlists,
        aliases=[a for a in raw.get("aliases", []) if isinstance(a, str)],
        seen=int(raw.get("seen") or 0),
        last_seen=str(raw.get("last_seen") or ""),
        examples=[e for e in raw.get("examples", []) if isinstance(e, str)][-MAX_EXAMPLES:],
    )


def _load() -> tuple[bool, dict[str, Row]] | object:
    """(seeded, rows by key), or _DAMAGED when the file is there but unreadable."""
    path = _path()
    data = files.read_json(path, _DAMAGED)
    if data is _DAMAGED:
        if path.exists():
            logger.error("%s can't be read; the team list is treated as empty and left as it is", path)
            return _DAMAGED
        return False, {}
    try:
        if not isinstance(data, dict) or not isinstance(data.get("rows", []), list):
            raise TypeError("unexpected shape")
        rows = {}
        for raw in data.get("rows", []):
            row = _row_from(raw)
            rows[row.key] = row
        return bool(data.get("seeded_v1")), rows
    except (ValueError, TypeError) as exc:
        logger.error("%s can't be understood (%s); the team list is treated as empty and left as it is", path, exc)
        return _DAMAGED


def _save(seeded: bool, rows: dict[str, Row]) -> None:
    files.write_json(_path(), {"seeded_v1": seeded, "rows": [asdict(r) for r in rows.values()]})


def _state() -> dict[str, Row] | None:
    """The rows, or None when the file is damaged. Seeding is done by the caller
    BEFORE it takes _lock (seed_once), so no other store is read under _lock."""
    with _lock:
        loaded = _load()
        if loaded is _DAMAGED:
            return None
        return loaded[1]


# ------------------------------------------------------------------ lookup


def _find(rows: dict[str, Row], key: str) -> Row | None:
    if not key:
        return None
    if key in rows:
        return rows[key]
    for row in rows.values():
        if key in row.aliases:
            return row
    return None


def rows() -> list[Row]:
    seed_once()
    with _lock:
        state = _state()
        return [_copy(r) for r in (state or {}).values()]


def _copy(row: Row) -> Row:
    return _row_from(asdict(row))


def lookup_key(key: str) -> Row | None:
    key = never_post._words(key)
    seed_once()
    with _lock:
        state = _state()
        row = _find(state, key) if state is not None else None
        return _copy(row) if row else None


def lookup(topic: str | None) -> Row | None:
    return lookup_key(series_key(topic))


def tier_of(topic: str | None, meeting_id: object = None) -> str:
    """'public' | 'archive' | 'never' | 'unknown'. A title with no team in it
    is always unknown; a meeting the never-post list catches is always never."""
    key = series_key(topic)
    if not key:
        return "unknown"
    if never_post.match(topic, meeting_id) is not None:
        return "never"
    row = lookup_key(key)
    return row.tier if row and row.tier else "unknown"


def tier_for_job(job: object) -> str:
    """The tier of a job's meeting: its Zoom topic first, then its title (like
    never_post.match_job); a job the never-post list catches is 'never'."""
    info = getattr(job, "zoom_meeting", None)
    info = info if isinstance(info, dict) else {}
    zoom_id = info.get("meeting_id") or info.get("id")
    if never_post.match_job(job) is not None:
        return "never"
    for topic in (info.get("topic"), getattr(job, "title", None)):
        if series_key(topic):
            return tier_of(topic, zoom_id)
    return "unknown"


def held_keys(meetings) -> set[str]:
    """Teams among `meetings` (objects with a topic, dicts, or plain topics)
    whose videos would not go up as they stand: 'never' or not decided yet."""
    held: set[str] = set()
    for meeting in meetings or ():
        if isinstance(meeting, str):
            topic, zoom_id = meeting, None
        elif isinstance(meeting, dict):
            topic, zoom_id = meeting.get("topic"), meeting.get("meeting_id")
        else:
            topic, zoom_id = getattr(meeting, "topic", None), getattr(meeting, "meeting_id", None)
        key = series_key(topic)
        if key and tier_of(topic, zoom_id) in ("never", "unknown"):
            held.add(key)
    return held


# ------------------------------------------------------------------ changes


def note_seen(topic: str | None) -> Row | None:
    """A meeting of this team was seen: count it and keep the last 3 topics.
    Never decides a tier (a new team starts as unknown)."""
    key = series_key(topic)
    if not key:
        return None
    seed_once()
    with _lock:
        if _state() is None:
            return None
        seeded, rows = _load()
        row = _find(rows, key)
        if row is None:
            row = rows[key] = Row(key=key, label=_label_of(topic) or key)
        row.seen += 1
        row.last_seen = files.now_iso()
        text = " ".join((topic or "").split())[:200]
        row.examples = [e for e in row.examples if e != text][-(MAX_EXAMPLES - 1):] + [text]
        _save(seeded, rows)
        return _copy(row)


def _check_playlist(value: Any) -> str:
    value = (value or "").strip() if isinstance(value, str) or value is None else value
    if not isinstance(value, str) or (value and not _PLAYLIST_ID.match(value)):
        raise ValueError("That is not a YouTube playlist ID (it starts with PL).")
    return value


def _check_hashtags(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > 2:
        raise ValueError("A team has at most 2 hashtags.")
    out = []
    for tag in value:
        tag = (tag or "").strip() if isinstance(tag, str) else tag
        if not isinstance(tag, str) or not _HASHTAG.match(tag) or len(tag) > 30:
            raise ValueError(f'"{tag}" is not a hashtag: a # then letters and digits, at most 30 characters.')
        if tag not in out:
            out.append(tag)
    return out


def _never_post_hit(row: Row) -> str | None:
    for text in (row.label, row.key, *row.examples):
        why = never_post.match(text)
        if why is not None:
            return why
    return None


def set_row(key: str, **fields: Any) -> Row:
    """Create or change a team. Raises ValueError for a bad value."""
    unknown = set(fields) - _FIELDS
    if unknown:
        raise ValueError(f"Can't set {', '.join(sorted(unknown))}.")
    key = never_post._words(key)
    if not key:
        raise ValueError("A team needs a name with letters or numbers in it.")
    seed_once()
    with _lock:
        if _state() is None:
            raise ValueError("The team list can't be read, so it was left as it is.")
        seeded, rows = _load()
        row = rows.get(key)
        if row is None:
            owner = _find(rows, key)
            if owner is not None:
                # the name is an alias: the change applies to the team that owns it
                row = owner
                key = owner.key
            else:
                row = Row(key=key, label=key)
        draft = _copy(row)
        if "label" in fields:
            label = " ".join(str(fields["label"] or "").split())
            if not label or "<" in label or ">" in label or len(label) > MAX_LABEL:
                raise ValueError(f"The name must be 1-{MAX_LABEL} characters, without < or >.")
            draft.label = label
        if "hashtags" in fields:
            draft.hashtags = _check_hashtags(fields["hashtags"])
        if "playlists" in fields:
            given = fields["playlists"]
            if not isinstance(given, dict) or set(given) - set(PLAYLIST_KINDS):
                raise ValueError("Playlists are full, cuts and shorts.")
            for kind, value in given.items():
                draft.playlists[kind] = _check_playlist(value)
        if "aliases" in fields:
            aliases = []
            for alias in fields["aliases"] or []:
                alias = never_post._words(alias)
                if not alias or alias == key:
                    continue
                other = _find(rows, alias)
                if other is not None and other.key != key:
                    raise ValueError(f'"{alias}" already belongs to another team.')
                if alias not in aliases:
                    aliases.append(alias)
            if len(aliases) > MAX_ALIASES:
                raise ValueError(f"At most {MAX_ALIASES} aliases.")
            draft.aliases = aliases
        if "tier" in fields:
            tier = fields["tier"]
            if tier in (None, "", "unknown"):
                draft.tier = None
            elif tier in TIERS:
                draft.tier = tier
            else:
                raise ValueError("The tier is public, archive or never.")
        if draft.tier in ("public", "archive") and "tier" in fields and _never_post_hit(draft) is not None:
            raise ValueError("never-post always wins")
        rows[key] = draft
        _save(seeded, rows)
        return _copy(draft)


def set_playlist(key: str, which: str, playlist_id: str) -> Row:
    """Remember a playlist the app made for a team ('full', 'cuts' or 'shorts')."""
    if which not in PLAYLIST_KINDS:
        raise ValueError("Playlists are full, cuts and shorts.")
    key = never_post._words(key)
    seed_once()
    with _lock:
        row = lookup_key(key)
        if row is None:
            raise ValueError("No such team.")
        return set_row(row.key, playlists={which: playlist_id})


def delete_row(key: str) -> bool:
    """Remove a team. It is not added back by seeding."""
    key = never_post._words(key)
    seed_once()
    with _lock:
        if _state() is None:
            return False
        seeded, rows = _load()
        if rows.pop(key, None) is None:
            return False
        _save(seeded, rows)
        return True


def merge(alias_key: str, into_key: str) -> Row:
    """Fold one team into another: its name becomes an alias of the target,
    its count and playlists carry over, and its own row goes."""
    alias_key, into_key = never_post._words(alias_key), never_post._words(into_key)
    seed_once()
    with _lock:
        if _state() is None:
            raise ValueError("The team list can't be read, so it was left as it is.")
        seeded, rows = _load()
        src, dst = rows.get(alias_key), rows.get(into_key)
        if src is None or dst is None:
            raise ValueError("Both teams must exist.")
        if src.key == dst.key:
            raise ValueError("A team can't be merged into itself.")
        for name in [src.key, *src.aliases]:
            if name != dst.key and name not in dst.aliases:
                dst.aliases.append(name)
        dst.seen += src.seen
        dst.last_seen = max(dst.last_seen, src.last_seen)
        for example in src.examples:
            if example not in dst.examples:
                dst.examples.append(example)
        dst.examples = dst.examples[-MAX_EXAMPLES:]
        for kind in PLAYLIST_KINDS:
            dst.playlists[kind] = dst.playlists[kind] or src.playlists[kind]
        del rows[src.key]
        _save(seeded, rows)
        return _copy(dst)


# ------------------------------------------------------------------ seeding


def seed_once() -> None:
    """On first use: every team already seen or posted becomes public, and one
    the never-post list catches becomes never. Runs once (flag seeded_v1); a
    team the owner deletes is not added back. Only reads its sources; one that
    can't be read is skipped and logged."""
    loaded = _load()  # a cheap look without the lock; the sources are read lock-free
    if loaded is _DAMAGED or loaded[0]:
        return
    found = _gather_sources()
    with _lock:
        loaded = _load()
        if loaded is _DAMAGED:
            return
        seeded, rows = loaded
        if seeded:
            return
        for key, item in found.items():
            if _find(rows, key) is not None:
                continue
            rows[key] = Row(key=key, label=item["label"], tier="never" if item["never"] and not item["miss"] else "public",
                            seen=item["seen"], last_seen=item["last"],
                            examples=item["examples"][-MAX_EXAMPLES:])
        _save(True, rows)
        logger.info("series seeded: %s", ", ".join(f"{r.key} ({r.tier})" for r in rows.values()) or "none")


def _gather_sources() -> dict[str, dict]:
    """Read the autopilot list, the uploads and the jobs. Never called with
    _lock held: those stores have their own locks."""
    found: dict[str, dict] = {}

    def note(topic, meeting_id=None, *, counted=False, when=""):
        key = series_key(topic)
        if not key:
            return
        item = found.setdefault(key, {"label": _label_of(topic) or key, "never": False, "seen": 0,
                                      "last": "", "examples": [], "miss": False})
        # display only (never_post stays the real guard): a team is "never" only when every
        # title seen for it is caught by the list's words. A match by Zoom ID, or by words
        # the team key does not keep, is for those meetings alone: the rest keep posting.
        if never_post.match(topic) is not None:
            item["never"] = True
        else:
            item["miss"] = True
        if counted:
            item["seen"] += 1
            item["last"] = max(item["last"], when or "")
        text = " ".join((topic or "").split())[:200]
        if text and text not in item["examples"]:
            item["examples"].append(text)

    try:
        from autopilot.store import store
        for meeting in store.read().meetings.values():
            note(meeting.topic, meeting.meeting_id, counted=True, when=meeting.start_time or "")
    except Exception as exc:  # noqa: BLE001 - seed what can be read
        logger.warning("series seeding: the autopilot list can't be read: %s", exc)
    try:
        from ledger.uploads import get_ledger
        for upload in get_ledger().posted():
            if upload.kind == "full":
                note(upload.meeting)
    except Exception as exc:  # noqa: BLE001
        logger.warning("series seeding: the uploads list can't be read: %s", exc)
    try:
        from api.jobs import job_store
        for job_id in job_store.list_ids():
            try:
                job = job_store.get(job_id)
            except Exception as exc:  # noqa: BLE001 - one bad job must not stop the rest
                logger.warning("series seeding: job %s can't be read: %s", job_id, exc)
                continue
            if job is None:
                continue
            info = job.zoom_meeting if isinstance(job.zoom_meeting, dict) else {}
            zoom_id = info.get("meeting_id") or info.get("id")
            for topic in (info.get("topic"), job.title):
                note(topic, zoom_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("series seeding: the job list can't be read: %s", exc)
    return found
