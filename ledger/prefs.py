"""Settings people change on the page, kept with the rest of the server's
state (storage_dir/state/prefs.json, backed up like the posted list) so they
survive restarts and deploys. A setting nobody changed comes from .env."""

from __future__ import annotations

import threading

from core.config import get_settings

from . import files

FILE = "prefs.json"
# a year: anything longer is "never" (0)
MAX_RETENTION_HOURS = 24 * 365
# two saves at once must not lose one: each rewrites the whole file
_write_lock = threading.Lock()


def _path():
    return files.state_dir() / FILE


def _read() -> dict:
    data = files.read_json(_path(), {})
    return data if isinstance(data, dict) else {}


def retention_hours() -> float:
    """Finished jobs nobody touched for this many hours are deleted (0 = never):
    the side panel's choice, else JOB_RETENTION_HOURS."""
    value = _read().get("job_retention_hours")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= MAX_RETENTION_HOURS:
        return float(value)
    return get_settings().job_retention_hours


def set_retention_hours(hours: float) -> None:
    if not 0 <= hours <= MAX_RETENTION_HOURS:
        raise ValueError(f"Choose between 0 (never) and {MAX_RETENTION_HOURS} hours.")
    with _write_lock:
        data = _read()
        data["job_retention_hours"] = hours
        files.write_json(_path(), data)


def never_post() -> list[str] | None:
    """The side panel's never-post list, or None when nobody changed it (the
    built-in list applies, see autopilot/never_post.py). An empty list is a
    choice and is kept; a damaged one counts as unchanged, so nothing that
    must stay private starts going up because of a bad file."""
    value = _read().get("never_post")
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return None


def set_never_post(entries: list[str]) -> None:
    with _write_lock:
        data = _read()
        data["never_post"] = entries
        files.write_json(_path(), data)
