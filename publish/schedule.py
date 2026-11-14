"""The release plan: when each public video goes public.

Posting a meeting's full video and all its Shorts at once means up to about 15
public videos a day, five within minutes. YouTube may pause subscriber
notifications for a day after more than 3 publishes in a short window, and a
burst of near-identical automated uploads looks like spam. So a public video
is uploaded private with a time YouTube makes it public (status.publishAt),
and this module hands out those times, one slot per video.

The rules (all from the owner's Publishing settings, publish/settings.py):
- a slot is at least `review_hours` after now, so a mistake can be caught in
  YouTube Studio before anyone sees the video;
- on a quarter hour, inside the daily window in the team's time zone
  (settings.local_timezone; daylight saving is handled by zoneinfo);
- at least `min_gap_hours` from every other booked video;
- at most `max_per_day` videos on a team-time calendar day, of which at most
  `shorts_per_day` are Shorts;
- no further than `max_days_ahead` days out (else no slot: None).

Only the first full video booked on a day may notify subscribers; Shorts never
do. That keeps to one notification a day.

Bookings live in storage_dir/state/schedule.json. The app can't cancel a
scheduled video afterwards (that would need a wider YouTube permission); the
owner can, in YouTube Studio, during the review window.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.config import get_settings
from core.logging import get_logger
from ledger import files

from . import settings as pub_settings

logger = get_logger(__name__)

Kind = Literal["full", "short"]

FILE = "schedule.json"
# Bookings older than this are forgotten on every write.
KEEP_DAYS = 8
_STEP = timedelta(minutes=15)

# Read-modify-write of the file happens under this lock, so two uploads
# booking at once can't be given the same slot.
_lock = threading.Lock()


@dataclass(frozen=True)
class Booking:
    key: str  # the ledger item key of the video
    kind: Kind
    source: str  # the meeting
    at: datetime  # when it goes public (UTC)
    notify: bool  # whether subscribers are told
    booked_at: datetime
    meeting: str = ""  # the meeting's readable name (older rows have none)


# ------------------------------------------------------------------ time zone


def team_zone() -> tzinfo:
    """The team's time zone (settings.local_timezone), else UTC."""
    name = get_settings().local_timezone
    if name:
        try:
            return ZoneInfo(str(name))
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("unknown time zone %r; release slots use UTC", name)
    return UTC


def team_zone_name() -> str:
    """The zone's name ("Asia/Karachi"), for the page to show times in it."""
    return getattr(team_zone(), "key", "UTC")


def team_time_text(moment: datetime) -> str:
    """"Nov 15, 2:30 PM" in the team's time zone."""
    local = moment.astimezone(team_zone())
    hour = local.hour % 12 or 12
    return f"{local:%b} {local.day}, {hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"


def rfc3339(moment: datetime) -> str:
    """The UTC form YouTube's publishAt wants: 2026-11-15T09:30:00Z."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ the file


def _path():
    return files.state_dir() / FILE


def _parse(value) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _load() -> list[Booking]:
    data = files.read_json(_path(), {})
    rows = data.get("bookings") if isinstance(data, dict) else None
    found: list[Booking] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            at, booked = _parse(row["at"]), _parse(row.get("booked_at"))
            if at is None or row["kind"] not in ("full", "short"):
                continue
            found.append(Booking(str(row["key"]), row["kind"], str(row.get("source") or ""), at,
                                 bool(row.get("notify")), booked or at, str(row.get("meeting") or "")))
        except (KeyError, TypeError):
            continue  # one damaged row must not lose the others
    return found


def _save(bookings: list[Booking], now: datetime) -> None:
    keep_from = now - timedelta(days=KEEP_DAYS)
    rows = [
        {"key": b.key, "kind": b.kind, "source": b.source, "at": b.at.astimezone(UTC).isoformat(timespec="seconds"),
         "notify": b.notify, "booked_at": b.booked_at.astimezone(UTC).isoformat(timespec="seconds"),
         "meeting": b.meeting}
        for b in sorted(bookings, key=lambda b: b.at) if b.at >= keep_from
    ]
    files.write_json(_path(), {"bookings": rows})


def _now(now: datetime | None) -> datetime:
    moment = now or datetime.now(UTC)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


# ------------------------------------------------------------------ booking


def book(
    key: str,
    kind: Kind,
    source: str,
    *,
    not_before: datetime | None = None,
    now: datetime | None = None,
    meeting: str = "",
) -> Booking | None:
    """The slot for the video `key`, or None when there is none within
    `max_days_ahead` days. A video that already has a slot in the future keeps
    it (a retry after a failure must not move to a later day)."""
    now = _now(now)
    cfg = pub_settings.current()
    with _lock:
        bookings = _load()
        mine = next((b for b in bookings if b.key == key), None)
        # Keep an old slot only while it still leaves the full review time (and
        # respects not_before). One left over from a cut-off upload could be
        # minutes away now, and a video must never go public without review.
        earliest = now + timedelta(hours=cfg.review_hours)
        if not_before is not None:
            earliest = max(earliest, _now(not_before))
        if mine is not None and mine.at >= earliest:
            return mine
        others = [b for b in bookings if b.key != key]  # an old slot of its own is simply replaced
        zone = team_zone()
        slot = _find(others, kind, cfg, zone, now, not_before)
        if slot is None:
            return None
        booking = Booking(key, kind, source, slot, _notifies(others, kind, slot, zone), now, meeting)
        _save([*others, booking], now)
        return booking


def _find(others, kind, cfg, zone, now, not_before) -> datetime | None:
    earliest = now + timedelta(hours=cfg.review_hours)
    if not_before is not None:
        earliest = max(earliest, _now(not_before))
    last = now + timedelta(days=cfg.max_days_ahead)
    gap = timedelta(hours=cfg.min_gap_hours)
    start_min, end_min = cfg.window_start_hour * 60, cfg.window_end_hour * 60
    t = _round_up(earliest)
    while t <= last:
        local = t.astimezone(zone)
        if start_min <= local.hour * 60 + local.minute < end_min and _fits(others, kind, cfg, zone, t, gap):
            return t
        t += _STEP
    return None


def _round_up(moment: datetime) -> datetime:
    """The next quarter hour at or after `moment` (UTC)."""
    moment = moment.astimezone(UTC)
    floor = moment.replace(minute=moment.minute - moment.minute % 15, second=0, microsecond=0)
    return floor if floor == moment else floor + _STEP


def _fits(others, kind, cfg, zone, t: datetime, gap: timedelta) -> bool:
    if any(abs(b.at - t) < gap for b in others):
        return False
    day = t.astimezone(zone).date()
    same_day = [b for b in others if _day(b, zone) == day]
    if len(same_day) >= cfg.max_per_day:
        return False
    return not (kind == "short" and sum(1 for b in same_day if b.kind == "short") >= cfg.shorts_per_day)


def _day(booking: Booking, zone: tzinfo) -> date:
    return booking.at.astimezone(zone).date()


def _notifies(others, kind, slot: datetime, zone: tzinfo) -> bool:
    """Only the first full video of a team-time day tells subscribers."""
    if kind != "full":
        return False
    day = slot.astimezone(zone).date()
    return not any(b.notify and _day(b, zone) == day for b in others)


def keep(booking: Booking, *, now: datetime | None = None) -> None:
    """Record `booking` exactly as given, replacing any other booking of its
    key: for a video YouTube already holds at that time."""
    now = _now(now)
    with _lock:
        _save([*[b for b in _load() if b.key != booking.key], booking], now)


def release(key: str, *, now: datetime | None = None) -> bool:
    """Give the slot of `key` back (its upload failed before YouTube made a
    video). Returns whether there was one."""
    now = _now(now)
    with _lock:
        bookings = _load()
        left = [b for b in bookings if b.key != key]
        if len(left) == len(bookings):
            return False
        _save(left, now)
        return True


def upcoming(now: datetime | None = None) -> list[Booking]:
    """Bookings that have not happened yet, soonest first."""
    now = _now(now)
    with _lock:
        return sorted((b for b in _load() if b.at > now), key=lambda b: b.at)


def get(key: str) -> Booking | None:
    """The booking of `key`, if any (even a past one still kept)."""
    with _lock:
        return next((b for b in _load() if b.key == key), None)
