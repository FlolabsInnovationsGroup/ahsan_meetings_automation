"""The clips data store: one SQLite file holding sources, videos, clips, events and flags.

Each record is kept as its JSON (so every model round-trips exactly) plus the few
columns we filter and sort by. A short connection is opened per call, the file is
in WAL mode, and writes take the write lock up front, so the loop and the web page
can use the same file at the same time.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from clips.models import Clip, ClipStatus, Source, SourceVideo, VideoStatus

_BOOKED = (ClipStatus.SCHEDULED, ClipStatus.UPLOADING, ClipStatus.UPLOADED)
_NOT_YET_UP = (ClipStatus.DRAFT, ClipStatus.APPROVED, ClipStatus.SCHEDULED, ClipStatus.UPLOADING)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, added_at TEXT, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, status TEXT NOT NULL, seen_at TEXT, native_id TEXT NOT NULL,
    json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS videos_source ON videos (source_id);
CREATE TABLE IF NOT EXISTS clips (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, status TEXT NOT NULL, publish_at TEXT, created_at TEXT,
    json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS clips_status ON clips (status);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL,
    clip_id TEXT NOT NULL DEFAULT '', source_id TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS flags (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def new_clip_id() -> str:
    """A fresh clip id: 12 hex characters."""
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    """A sortable UTC text form. Naive datetimes are taken to be UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple, set, frozenset)) else [value]


class Store:
    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            from clips.config import clips_settings

            path = clips_settings().db_path
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn(write=False) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ---- plumbing

    @contextmanager
    def _conn(self, write: bool = True) -> Iterator[sqlite3.Connection]:
        """A short connection. Writes take the write lock first so read-modify-write is safe."""
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            if conn.in_transaction:
                conn.execute("COMMIT")
        finally:
            conn.close()

    def _rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._conn(write=False) as conn:
            return conn.execute(sql, params).fetchall()

    # ---- sources

    def save_source(self, source: Source) -> Source:
        if source.added_at is None:
            source = source.model_copy(update={"added_at": _now()})
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sources (id, enabled, added_at, json) VALUES (?, ?, ?, ?)",
                (source.id, int(source.enabled), _iso(source.added_at), source.model_dump_json()),
            )
        return source

    def get_source(self, source_id: str) -> Source | None:
        rows = self._rows("SELECT json FROM sources WHERE id = ?", (source_id,))
        return Source.model_validate_json(rows[0][0]) if rows else None

    def list_sources(self, enabled_only: bool = False) -> list[Source]:
        sql = "SELECT json FROM sources" + (" WHERE enabled = 1" if enabled_only else "")
        rows = self._rows(sql + " ORDER BY added_at, rowid")
        return [Source.model_validate_json(r[0]) for r in rows]

    def delete_source(self, source_id: str) -> bool:
        """Remove a source. Everything it left behind is stopped in the same locked step:
        its NEW videos are skipped and every clip not yet on YouTube is held, so a later
        source can never inherit the queue (and the id is never handed out again, see
        source_id_used)."""
        with self._conn() as conn:
            gone = conn.execute("DELETE FROM sources WHERE id = ?", (source_id,)).rowcount > 0
            if not gone:
                return False
            for (raw,) in conn.execute(
                "SELECT json FROM videos WHERE source_id = ? AND status = ?", (source_id, VideoStatus.NEW.value)
            ).fetchall():
                video = SourceVideo.model_validate_json(raw)
                data = video.model_dump()
                data.update(status=VideoStatus.SKIPPED, skip_reason="The source was removed.")
                moved = SourceVideo.model_validate(data)
                conn.execute(
                    "UPDATE videos SET status = ?, json = ? WHERE id = ?",
                    (moved.status.value, moved.model_dump_json(), moved.id),
                )
            open_states = [s.value for s in _NOT_YET_UP]
            marks = ",".join("?" * len(open_states))
            for (raw,) in conn.execute(
                f"SELECT json FROM clips WHERE source_id = ? AND status IN ({marks})", (source_id, *open_states)
            ).fetchall():
                data = Clip.model_validate_json(raw).model_dump()
                data.update(status=ClipStatus.HELD, error="The source was removed.", updated_at=_now())
                self._write_clip(conn, Clip.model_validate(data))
        return True

    def source_id_used(self, source_id: str) -> bool:
        """True when a source, a video or a clip already carries this source id (ids are never reused)."""
        for table in ("sources", "videos", "clips"):
            column = "id" if table == "sources" else "source_id"
            if self._rows(f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1", (source_id,)):
                return True
        return False

    # ---- videos

    def save_video(self, video: SourceVideo) -> SourceVideo:
        if video.seen_at is None:
            video = video.model_copy(update={"seen_at": _now()})
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO videos (id, source_id, status, seen_at, native_id, json) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    video.id,
                    video.source_id,
                    video.status.value,
                    _iso(video.seen_at),
                    video.native_id,
                    video.model_dump_json(),
                ),
            )
        return video

    def get_video(self, video_id: str) -> SourceVideo | None:
        rows = self._rows("SELECT json FROM videos WHERE id = ?", (video_id,))
        return SourceVideo.model_validate_json(rows[0][0]) if rows else None

    def list_videos(
        self, source_id: str | None = None, status: VideoStatus | None = None, limit: int = 200
    ) -> list[SourceVideo]:
        where, params = [], []
        if source_id is not None:
            where.append("source_id = ?")
            params.append(source_id)
        if status is not None:
            where.append("status = ?")
            params.append(VideoStatus(status).value)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        rows = self._rows(f"SELECT json FROM videos{clause} ORDER BY seen_at, rowid LIMIT ?", (*params, limit))
        return [SourceVideo.model_validate_json(r[0]) for r in rows]

    def known_native_ids(self, source_id: str) -> set[str]:
        rows = self._rows("SELECT native_id FROM videos WHERE source_id = ?", (source_id,))
        return {r[0] for r in rows}

    # ---- clips

    def save_clip(self, clip: Clip) -> Clip:
        now = _now()
        updates: dict[str, Any] = {"updated_at": now}
        if clip.created_at is None:
            updates["created_at"] = now
        clip = clip.model_copy(update=updates)
        with self._conn() as conn:
            self._write_clip(conn, clip)
        return clip

    @staticmethod
    def _write_clip(conn: sqlite3.Connection, clip: Clip) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO clips (id, source_id, status, publish_at, created_at, json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                clip.id,
                clip.source_id,
                clip.status.value,
                _iso(clip.publish_at),
                _iso(clip.created_at),
                clip.model_dump_json(),
            ),
        )

    def get_clip(self, clip_id: str) -> Clip | None:
        rows = self._rows("SELECT json FROM clips WHERE id = ?", (clip_id,))
        return Clip.model_validate_json(rows[0][0]) if rows else None

    def list_clips(
        self,
        status: ClipStatus | list[ClipStatus] | None = None,
        source_id: str | None = None,
        limit: int = 500,
    ) -> list[Clip]:
        where, params = [], []
        if status is not None:
            values = [ClipStatus(s).value for s in _as_list(status)]
            where.append(f"status IN ({','.join('?' * len(values))})")
            params.extend(values)
        if source_id is not None:
            where.append("source_id = ?")
            params.append(source_id)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        rows = self._rows(f"SELECT json FROM clips{clause} ORDER BY created_at, rowid LIMIT ?", (*params, limit))
        return [Clip.model_validate_json(r[0]) for r in rows]

    def set_clip_status(self, clip_id: str, status: ClipStatus, **fields: Any) -> Clip:
        """Change a clip's status (and any other fields) in one locked step. KeyError when unknown."""
        unknown = [name for name in fields if name not in Clip.model_fields]
        if unknown:
            raise ValueError(f"Clip has no field {unknown[0]!r}")
        with self._conn() as conn:
            row = conn.execute("SELECT json FROM clips WHERE id = ?", (clip_id,)).fetchone()
            if row is None:
                raise KeyError(clip_id)
            data = Clip.model_validate_json(row[0]).model_dump()
            data.update(fields)
            data["status"] = ClipStatus(status)
            data["updated_at"] = _now()
            clip = Clip.model_validate(data)
            self._write_clip(conn, clip)
        return clip

    def claim_upload(self, clip_id: str) -> bool:
        """Take a SCHEDULED clip for upload: SCHEDULED -> UPLOADING in one locked step.

        True for exactly one caller across every process on this file; False when the
        clip is unknown or no longer SCHEDULED (someone else has it, or it moved on)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT json FROM clips WHERE id = ? AND status = ?", (clip_id, ClipStatus.SCHEDULED.value)
            ).fetchone()
            if row is None:
                return False
            data = Clip.model_validate_json(row[0]).model_dump()
            data.update(status=ClipStatus.UPLOADING, updated_at=_now())
            self._write_clip(conn, Clip.model_validate(data))
            return True

    def recover_stale_uploads(self, older_than_hours: float = 2.0) -> list[str]:
        """Put clips that have sat in UPLOADING for too long back to SCHEDULED (the process
        that held them died). The upload journal then resumes or finds the video. Returns the ids."""
        cutoff = _now() - timedelta(hours=older_than_hours)
        moved: list[str] = []
        with self._conn() as conn:
            rows = conn.execute("SELECT json FROM clips WHERE status = ?", (ClipStatus.UPLOADING.value,)).fetchall()
            for (raw,) in rows:
                clip = Clip.model_validate_json(raw)
                stamp = clip.updated_at or clip.created_at
                if stamp is not None and stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=UTC)
                if stamp is not None and stamp > cutoff:
                    continue
                data = clip.model_dump()
                data.update(status=ClipStatus.SCHEDULED, updated_at=_now())
                self._write_clip(conn, Clip.model_validate(data))
                moved.append(clip.id)
        return moved

    def rebook_overdue(self, before: datetime) -> list[str]:
        """SCHEDULED clips whose publish_at is earlier than `before` go back to APPROVED with
        no time, so the scheduler books them a fresh slot. A missed slot is never posted at once."""
        moved: list[str] = []
        limit = _iso(before)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT json FROM clips WHERE status = ? AND (publish_at IS NULL OR publish_at < ?)",
                (ClipStatus.SCHEDULED.value, limit),
            ).fetchall()
            for (raw,) in rows:
                clip = Clip.model_validate_json(raw)
                if clip.publish_at is None:
                    continue
                data = clip.model_dump()
                data.update(status=ClipStatus.APPROVED, publish_at=None, updated_at=_now())
                self._write_clip(conn, Clip.model_validate(data))
                moved.append(clip.id)
        return moved

    def booked_times(self) -> list[datetime]:
        values = [s.value for s in _BOOKED]
        rows = self._rows(
            f"SELECT publish_at FROM clips WHERE publish_at IS NOT NULL AND status IN ({','.join('?' * len(values))}) "
            "ORDER BY publish_at",
            tuple(values),
        )
        return [_parse(r[0]) for r in rows]

    def first_post_date(self) -> date | None:
        rows = self._rows("SELECT publish_at FROM clips WHERE status = ? AND publish_at IS NOT NULL", (ClipStatus.UPLOADED.value,))
        if not rows:
            return None
        return min(_parse(r[0]) for r in rows).astimezone(UTC).date()

    # ---- events

    def add_event(
        self, kind: str, message: str, *, clip_id: str = "", source_id: str = "", data: dict | None = None
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO events (at, kind, message, clip_id, source_id, data) VALUES (?, ?, ?, ?, ?, ?)",
                (_iso(_now()), kind, message, clip_id, source_id, json.dumps(data or {}, default=str)),
            )

    def list_events(self, limit: int = 200, kind: str | None = None) -> list[dict]:
        sql = "SELECT at, kind, message, clip_id, source_id, data FROM events"
        params: tuple = ()
        if kind is not None:
            sql += " WHERE kind = ?"
            params = (kind,)
        rows = self._rows(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))
        return [
            {"at": r[0], "kind": r[1], "message": r[2], "clip_id": r[3], "source_id": r[4], "data": json.loads(r[5])}
            for r in rows
        ]

    # ---- flags

    def get_flag(self, key: str, default: str = "") -> str:
        rows = self._rows("SELECT value FROM flags WHERE key = ?", (key,))
        return rows[0][0] if rows else default

    def set_flag(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO flags (key, value) VALUES (?, ?)", (key, value))
