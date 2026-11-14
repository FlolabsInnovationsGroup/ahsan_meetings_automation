"""YouTube's daily quota, counted by the app.

Google gives the project a number of units a day (10,000 by default) and
resets it at midnight Pacific time. The app keeps its own count so optional
extras (thumbnails, caption tracks, playlists, analytics) can never use up
what the uploads need: "essential" calls are always allowed, optional ones
only while the day's use plus the call stays under the daily quota minus a
reserve kept for uploads. When Google itself says the quota is used up,
mark_exhausted() stops every optional call until the next Pacific midnight.

Uploads count in their own bucket: YouTube allows a fixed number of upload
calls a day, so each upload start is recorded as an upload call, not units.

State: storage_dir/state/quota.json. A damaged file starts the day at 0.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from core.logging import get_logger
from ledger import files

logger = get_logger(__name__)

PACIFIC = ZoneInfo("America/Los_Angeles")

_lock = threading.RLock()


def _path():
    return files.state_dir() / "quota.json"


def _now() -> datetime:
    return datetime.now(UTC)


def day_of(now: datetime | None = None) -> str:
    """The quota day: the date in Los Angeles (handles daylight saving)."""
    return (now or _now()).astimezone(PACIFIC).date().isoformat()


def next_reset(now: datetime | None = None) -> datetime:
    """The next midnight in Los Angeles, as a UTC time."""
    local = (now or _now()).astimezone(PACIFIC)
    # build the next date's midnight from wall-clock parts, so a 23 or 25 hour day is right
    next_day = local.date() + timedelta(days=1)
    midnight = datetime(next_day.year, next_day.month, next_day.day, tzinfo=PACIFIC)
    return midnight.astimezone(UTC)


def _fresh(day: str) -> dict:
    return {"day": day, "used": 0, "by_kind": {}, "upload_calls": 0, "exhausted_until": None}


def _load(now: datetime) -> dict:
    day = day_of(now)
    data = files.read_json(_path(), None)
    if not isinstance(data, dict) or data.get("day") != day:
        # a new day, or a file that is missing or damaged: start at 0
        exhausted = data.get("exhausted_until") if isinstance(data, dict) else None
        state = _fresh(day)
        if isinstance(exhausted, str) and _parse(exhausted) and _parse(exhausted) > now:
            state["exhausted_until"] = exhausted
        return state
    state = _fresh(day)
    used = data.get("used")
    state["used"] = used if isinstance(used, int) and used >= 0 else 0
    by_kind = data.get("by_kind")
    if isinstance(by_kind, dict):
        state["by_kind"] = {str(k): v for k, v in by_kind.items() if isinstance(v, int)}
    calls = data.get("upload_calls")
    state["upload_calls"] = calls if isinstance(calls, int) and calls >= 0 else 0
    exhausted = data.get("exhausted_until")
    state["exhausted_until"] = exhausted if isinstance(exhausted, str) and _parse(exhausted) else None
    return state


def _parse(text: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _limits() -> tuple[int, int]:
    from publish import settings as publishing_settings

    p = publishing_settings.current()
    return p.quota_daily_units, p.quota_reserve_units


def _exhausted(state: dict, now: datetime) -> bool:
    until = _parse(state["exhausted_until"]) if state.get("exhausted_until") else None
    return until is not None and until > now


def spend(units: int, kind: str, essential: bool = False) -> None:
    """Count `units` used by a call of this kind."""
    if units <= 0:
        return
    with _lock:
        now = _now()
        state = _load(now)
        state["used"] += int(units)
        state["by_kind"][kind] = state["by_kind"].get(kind, 0) + int(units)
        files.write_json(_path(), state)


def spend_upload_call() -> None:
    """Count the start of one upload (its own daily bucket, not units)."""
    with _lock:
        state = _load(_now())
        state["upload_calls"] += 1
        files.write_json(_path(), state)


def may_spend(units: int, essential: bool = False) -> bool:
    """Essential calls always; optional ones only while the reserve for
    uploads stays untouched and Google has not said the quota is gone."""
    if essential:
        return True
    with _lock:
        now = _now()
        state = _load(now)
        if _exhausted(state, now):
            return False
        daily, reserve = _limits()
        return state["used"] + int(units) <= daily - reserve


def mark_exhausted() -> datetime:
    """Google said the quota is used up: no optional call until the next
    Pacific midnight. Returns that time (UTC)."""
    with _lock:
        now = _now()
        state = _load(now)
        until = next_reset(now)
        state["exhausted_until"] = until.isoformat(timespec="seconds")
        files.write_json(_path(), state)
    logger.warning("YouTube quota used up; optional calls wait until %s", until.isoformat(timespec="minutes"))
    return until


def remaining() -> int:
    """Units left today before the daily quota (the reserve is part of it)."""
    with _lock:
        state = _load(_now())
        daily, _ = _limits()
        return max(0, daily - state["used"])


def today() -> dict:
    """What the page shows."""
    with _lock:
        now = _now()
        state = _load(now)
        daily, reserve = _limits()
        return {
            "day": state["day"],
            "used": state["used"],
            "by_kind": dict(state["by_kind"]),
            "upload_calls": state["upload_calls"],
            "daily_units": daily,
            "reserve_units": reserve,
            "remaining": max(0, daily - state["used"]),
            "remaining_for_extras": max(0, daily - reserve - state["used"]),
            "exhausted_until": state["exhausted_until"] if _exhausted(state, now) else None,
            "resets_at": next_reset(now).isoformat(timespec="seconds"),
        }
