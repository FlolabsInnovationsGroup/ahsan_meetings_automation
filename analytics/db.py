"""The views database: storage_dir/state/views.sqlite.

Same pattern as costs/db.py: WAL, a short connection per call, one lock.
Job files are deleted after a day or so, so what a video was (team, Short
style, title style, hook, thumbnail arm) is stored here when it is uploaded.
Everything here comes from YouTube's API, which must not be kept for long
without a refresh: purge_all() empties it (pull.purge_if_stale calls it).
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from core.config import get_settings

KINDS = ("full", "short", "topic", "other")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    ours INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'other',
    source TEXT, job_id TEXT, meeting TEXT, series_key TEXT, series_label TEXT, category TEXT,
    title TEXT, title_style TEXT, short_style TEXT, layout TEXT, thumb_arm TEXT,
    stands_alone REAL, screen_hook TEXT,
    published_at TEXT, publish_at TEXT, privacy TEXT, duration_s REAL,
    first_seen TEXT NOT NULL, updated_at TEXT NOT NULL,
    missing_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS snapshots (
    video_id TEXT NOT NULL, day TEXT NOT NULL, at TEXT NOT NULL,
    views INTEGER, likes INTEGER, comments INTEGER,
    PRIMARY KEY (video_id, day)
);
CREATE TABLE IF NOT EXISTS yta_daily (
    video_id TEXT NOT NULL, day TEXT NOT NULL,
    engaged_views INTEGER, views INTEGER, minutes_watched REAL,
    avg_view_duration_s REAL, avg_view_pct REAL, subs_gained INTEGER,
    PRIMARY KEY (video_id, day)
);
CREATE TABLE IF NOT EXISTS pulls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, kind TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
    units INTEGER NOT NULL DEFAULT 0, videos INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_VIDEO_COLUMNS = (
    "ours", "kind", "source", "job_id", "meeting", "series_key", "series_label", "category", "title",
    "title_style", "short_style", "layout", "thumb_arm", "stands_alone", "screen_hook", "published_at",
    "publish_at", "privacy", "duration_s", "missing_count",
)

_lock = threading.RLock()
_ready: set[Path] = set()


def db_path() -> Path:
    return get_settings().storage_dir / "state" / "views.sqlite"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextmanager
def connect():
    """A short connection with the tables in place; commits on success."""
    path = db_path()
    with _lock:
        if path not in _ready:
            path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(path, timeout=30)
            try:
                con.execute("PRAGMA journal_mode=WAL")
                con.executescript(_SCHEMA)
                con.commit()
            finally:
                con.close()
            _ready.add(path)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=30000")
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def upsert_video(con: sqlite3.Connection, video_id: str, *, only_missing: bool = False, **fields) -> None:
    """Add the video or update the named columns. Fields that are None are left
    alone. With only_missing, a column that already has a value (a kind other
    than 'other', ours=1) is kept: the ledger fills gaps, it never overwrites
    what the API or the upload recorded."""
    unknown = set(fields) - set(_VIDEO_COLUMNS)
    if unknown:
        raise ValueError(f"unknown video column(s): {sorted(unknown)}")
    stamp = now_iso()
    con.execute("INSERT OR IGNORE INTO videos (video_id, first_seen, updated_at) VALUES (?,?,?)",
                (video_id, stamp, stamp))
    sets, args = ["updated_at=?"], [stamp]
    for col, value in fields.items():
        if value is None:
            continue
        if only_missing:
            if col == "missing_count":
                continue
            if col == "kind":
                empty = "kind IS NULL OR kind='other'"
            elif col == "ours":
                empty = "ours IS NULL OR ours=0"
            else:
                empty = f"{col} IS NULL OR {col}=''"
            sets.append(f"{col}=CASE WHEN {empty} THEN ? ELSE {col} END")
        else:
            sets.append(f"{col}=?")
        args.append(value)
    con.execute(f"UPDATE videos SET {', '.join(sets)} WHERE video_id=?", (*args, video_id))


def get_video(video_id: str) -> dict | None:
    with connect() as con:
        row = con.execute("SELECT * FROM videos WHERE video_id=?", (video_id,)).fetchone()
        return dict(row) if row else None


def upsert_snapshot(con: sqlite3.Connection, video_id: str, day: str, views, likes, comments) -> None:
    con.execute("INSERT OR REPLACE INTO snapshots (video_id, day, at, views, likes, comments) VALUES (?,?,?,?,?,?)",
                (video_id, day, now_iso(), views, likes, comments))


def delete_video(con: sqlite3.Connection, video_id: str) -> None:
    for table in ("videos", "snapshots", "yta_daily"):
        con.execute(f"DELETE FROM {table} WHERE video_id=?", (video_id,))


def purge_all() -> None:
    """Delete every video, snapshot and statistic (the API data policy)."""
    with connect() as con:
        for table in ("videos", "snapshots", "yta_daily", "meta"):
            con.execute(f"DELETE FROM {table}")


def get_meta(key: str) -> str | None:
    with connect() as con:
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value))


def add_pull(*, kind: str, calls: int, units: int, videos: int, ok: bool, error: str = "") -> None:
    with connect() as con:
        con.execute("INSERT INTO pulls (at, kind, calls, units, videos, ok, error) VALUES (?,?,?,?,?,?,?)",
                    (now_iso(), kind, calls, units, videos, 1 if ok else 0, error[:300]))


def last_pull() -> dict | None:
    with connect() as con:
        row = con.execute("SELECT * FROM pulls ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
