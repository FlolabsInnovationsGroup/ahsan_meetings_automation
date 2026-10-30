"""Settings people change on the page, kept with the rest of the server's
state (storage_dir/state/prefs.json, backed up like the posted list) so they
survive restarts and deploys. A setting nobody changed comes from .env."""

from __future__ import annotations

from core.config import get_settings

from . import files

FILE = "prefs.json"
# a year: anything longer is "never" (0)
MAX_RETENTION_HOURS = 24 * 365


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
    data = _read()
    data["job_retention_hours"] = hours
    files.write_json(_path(), data)
