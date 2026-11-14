"""When to post: the next free publish times.

Pure functions, no clock and no files. The pace is a setting: `ramp_steps` gives
the allowance for each week since the first post (the last value repeats), never
below `posts_per_day` (0 pauses posting) and never above `max_posts_per_day`.
A `ramp_per_week` above 0 replaces the steps with a straight-line climb. Slots sit on quarter hours inside
the daily window (in `settings.timezone`), keep `min_gap_minutes` from every
booked time and from each other, and never pass the day's allowance.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clips.config import ClipsSettings

LOOK_AHEAD_DAYS = 30
_QUARTER = timedelta(minutes=15)


def allowance_on(day: date, first_post: date | None, settings: ClipsSettings) -> int:
    """How many posts are allowed on `day` (a local date)."""
    base = settings.posts_per_day
    if base <= 0:
        return 0  # posts_per_day 0 pauses posting
    if first_post is None or day < first_post:
        weeks = 0
    else:
        weeks = (day - first_post).days // 7
    if settings.ramp_per_week > 0:
        wanted = base + settings.ramp_per_week * weeks
    else:
        steps = settings.ramp_list
        step = steps[min(weeks, len(steps) - 1)] if steps else base
        wanted = max(base, step)
    return min(wanted, settings.max_posts_per_day)


def next_slots(
    now: datetime,
    booked: list[datetime],
    settings: ClipsSettings,
    first_post: date | None,
    count: int = 1,
) -> list[datetime]:
    """Up to `count` free publish times (UTC), earliest first.

    Fewer than `count` come back when the next 30 days are full."""
    tz = _zone(settings.timezone)
    now = _utc(now)
    taken = sorted(_utc(b) for b in booked)
    earliest = _ceil_quarter(now + timedelta(minutes=max(settings.lead_minutes, 20)))  # upload needs 15 minutes of room
    gap = timedelta(minutes=settings.min_gap_minutes)
    per_day: dict[date, int] = {}
    for moment in taken:
        local_day = moment.astimezone(tz).date()
        per_day[local_day] = per_day.get(local_day, 0) + 1

    chosen: list[datetime] = []
    last_day = (now + timedelta(days=LOOK_AHEAD_DAYS)).astimezone(tz).date()
    day = earliest.astimezone(tz).date()
    while day <= last_day and len(chosen) < count:
        room = allowance_on(day, first_post, settings) - per_day.get(day, 0)
        if room > 0:
            for slot in _free_on_day(day, tz, settings, earliest, taken, gap, min(room, count - len(chosen))):
                chosen.append(slot)
                taken.append(slot)
        day += timedelta(days=1)
    return sorted(chosen)


def _free_on_day(day, tz, settings, earliest, taken, gap, limit) -> list[datetime]:
    """Walk the day's window in quarter hours and take the free spots."""
    start = datetime.combine(day, time(settings.window_start_hour), tzinfo=tz).astimezone(UTC)
    if settings.window_end_hour >= 24:
        end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz).astimezone(UTC)
    else:
        end = datetime.combine(day, time(settings.window_end_hour), tzinfo=tz).astimezone(UTC)
    current = _ceil_quarter(max(start, earliest))
    found: list[datetime] = []
    local_taken = list(taken)
    while current < end and len(found) < limit:
        if all(abs(current - other) >= gap for other in local_taken):
            found.append(current)
            local_taken.append(current)
        current += _QUARTER
    return found


def _zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC  # a mistyped zone must not stop posting; UTC is a safe fallback


def _utc(moment: datetime) -> datetime:
    """Treat a naive time as UTC and move an aware one to UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _ceil_quarter(moment: datetime) -> datetime:
    """Round up to the next quarter hour (seconds count: 10:00:01 goes to 10:15)."""
    moment = moment.astimezone(UTC)
    floor = moment.replace(minute=moment.minute - moment.minute % 15, second=0, microsecond=0)
    return floor if floor == moment else floor + _QUARTER
