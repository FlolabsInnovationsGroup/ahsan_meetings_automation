"""Small files that must survive restarts, crashes and a bad write.

Two shapes:
- JSON Lines, only ever appended to. Every record has an "id", and a reader
  merges the file with its backup copy by id, so a record lost from one of
  them (a crash between the two writes, a damaged file) comes back from the
  other. A line cut short by a power loss is skipped, never glued onto the
  next record.
- JSON rewritten whole: written to a temp file, flushed to disk and renamed
  over the old one, so a reader sees the old or the new file, never half of
  one. A file that can't be read falls back to its backup.

Every write is flushed to the disk (fsync) before it returns: the upload
journal relies on "written" meaning "still there after a power cut".
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger

logger = get_logger(__name__)

# One lock for every file here: writes are rare (a few per upload) and tiny.
_lock = threading.RLock()


def state_dir() -> Path:
    return get_settings().storage_dir / "state"


def backup_dir() -> Path:
    return get_settings().storage_dir / "state-backup"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


# ------------------------------------------------------------------ JSON Lines


def append_jsonl(path: Path, record: dict) -> dict:
    """Append one record (given an "id" if it has none) and copy the file to
    the backup folder. Returns the record as written."""
    record = {"id": new_id(), **record} if "id" not in record else record
    data = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab+") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell():
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    fh.write(b"\n")  # end a line a crash cut short; it is skipped on read
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _backup(path)
    return record


def read_jsonl(path: Path) -> list[dict]:
    """Every record of the file and its backup, merged by id, in the order
    they were written. Rewrites the main file when the backup had records it
    lacked (so the next append can't leave them behind)."""
    with _lock:
        primary = _parse_jsonl(path)
        backup = _parse_jsonl(backup_dir() / path.name)
        seen = {r["id"] for r in primary}
        missing = [r for r in backup if r["id"] not in seen]
        if missing:
            logger.warning("%s: %d record(s) restored from the backup copy", path.name, len(missing))
            # The backup is a copy taken after every write, so it holds the
            # records in the order they were written, at most a write behind:
            # keep its order (timestamps have one-second steps and can't order
            # a claim and its upload link), then what only the main file has.
            in_backup = {r["id"] for r in backup}
            merged = backup + [r for r in primary if r["id"] not in in_backup]
            _write_atomic(path, "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n"
                                        for r in merged).encode("utf-8"))
            _backup(path)
            return merged
        return primary


def _parse_jsonl(path: Path) -> list[dict]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    records = []
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            logger.warning("%s line %d can't be read (cut short by a crash?); skipped", path.name, number)
            continue
        if isinstance(record, dict) and record.get("id"):
            records.append(record)
    return records


# ------------------------------------------------------------------ JSON


def write_json(path: Path, data) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with _lock:
        _write_atomic(path, text.encode("utf-8"))
        _backup(path)


def read_json(path: Path, default):
    """The file's content, else its backup's, else `default`."""
    with _lock:
        for candidate in (path, backup_dir() / path.name):
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except ValueError:
                logger.warning("%s can't be read; trying the backup copy", candidate)
        return default


# ------------------------------------------------------------------ disk


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _fsync_dir(folder: Path) -> None:
    """Make the rename itself durable (POSIX; Windows can't open a folder)."""
    if os.name != "posix":
        return
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _backup(path: Path) -> None:
    """Copy the file to the backup folder. A failed copy is logged, never
    raised: the main file is already safely written."""
    try:
        target = backup_dir() / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        shutil.copyfile(path, tmp)
        with tmp.open("rb+") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        logger.warning("could not copy %s to the backup folder: %s", path.name, exc)
