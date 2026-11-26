"""Bring videos already on YouTube with a time to go public forward into free
release slots.

A video is booked the earliest slot free when it is uploaded, but the slots
in front of it can free up later: the release rules changed (owner,
2026-11-17: one hour between videos, up to 24 a day), or a video booked
earlier was released. Without this, videos booked under old, slower rules
stay days out while the channel posts nothing.

Every few minutes (the server's sweep loop) the upcoming bookings are looked
at, soonest first, and each one that could go public at least MIN_GAIN sooner
is moved: the new slot is held in the release plan, then YouTube is told
(videos.update, which needs the "edit" permission, youtube.force-ssl), then the
upload list is told. When YouTube can't be changed the old slot is put back.

A meeting keeps its own order: nothing moves ahead of a video of the same
meeting that is booked before it, and a Short stays `shorts_after_full_hours`
behind its full video or topic cut. A video that tells subscribers only moves
to a day no other video tells them on. Nothing on the never-post list is
brought forward.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from core.config import get_settings
from core.logging import get_logger
from ledger.uploads import get_ledger

from . import optional, schedule, youtube
from . import settings as pub_settings

logger = get_logger(__name__)

# Only move a video when it goes public at least this much sooner.
MIN_GAIN = timedelta(minutes=45)
# A moved video goes public at least this long after the move, so YouTube
# never gets a time that is already past by the time the call lands.
LEAD = timedelta(minutes=30)
# At most this many moves in one round (each costs 51 quota units).
MAX_MOVES = 25
UNITS = 51  # videos.list (1) + videos.update (50)

_running = threading.Lock()
# What the last round found, for the release plan on the page.
_last: dict = {}


def last_round() -> dict:
    return dict(_last)


def _blocked(upload) -> str | None:
    """Why this video must not be brought forward (never-post list), or None."""
    from autopilot import never_post

    why = never_post.match_words(upload.title)
    if why is not None:
        return f'its title has "{why}"'
    job = None
    try:
        from core.models import JobRecord

        path = get_settings().jobs_dir / upload.job_id / "job.json"
        if upload.job_id and path.is_file():
            job = JobRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable job falls back to the meeting's name
        job = None
    if job is not None:
        return never_post.match_job(job)
    return never_post.match(upload.meeting)


def _not_before(booking: schedule.Booking, cfg) -> datetime | None:
    """The earliest this booking may go public so its meeting keeps its order:
    after every video of the same meeting booked before it, and a Short at
    least `shorts_after_full_hours` after the meeting's full video or cut."""
    times = []
    for other in schedule.for_source(booking.source):
        if other.key == booking.key or other.at >= booking.at:
            continue
        wait = cfg.shorts_after_full_hours if booking.kind == "short" and other.kind != "short" else 0.0
        times.append(other.at + timedelta(hours=wait))
    return max(times) if times else None


def pull_forward(now: datetime | None = None) -> dict:
    """One round. Returns {"moved": n, "note": plain words or ""}. Never raises."""
    if not _running.acquire(blocking=False):
        return {"moved": 0, "note": "already running"}
    try:
        result = _round(now or datetime.now(UTC))
    except Exception as exc:  # the next round tries again
        logger.exception("pulling scheduled videos forward failed")
        result = {"moved": 0, "note": f"Could not run: {str(exc)[:200]}"}
    finally:
        _running.release()
    _last.clear()
    _last.update(result, at=datetime.now(UTC).isoformat(timespec="seconds"))
    return result


def _round(now: datetime) -> dict:
    cfg = pub_settings.current()
    if not cfg.scheduled:
        return {"moved": 0, "note": ""}
    granted = youtube.granted_scopes()  # empty when no channel is connected
    if not granted:
        return {"moved": 0, "note": ""}
    if youtube.FEATURE_SCOPES["edit"] not in granted:
        waiting = sum(1 for b in schedule.upcoming(now) if b.at - now > MIN_GAIN + LEAD)
        note = ("Reconnect YouTube to let the app move videos booked under the old, slower rules into earlier "
                "slots.") if waiting else ""
        return {"moved": 0, "note": note}
    ledger = get_ledger()
    pulled = pull_back_listed(now)
    moved = 0
    for booking in schedule.upcoming(now):
        if moved >= MAX_MOVES:
            break
        held = ledger.get(booking.key)
        if held is None or held.state != "published" or not held.video_id or not held.publish_at:
            continue  # not on YouTube yet: it is booked afresh when it is uploaded
        why = _blocked(held)
        if why is not None:
            logger.info("not bringing %s forward: %s", booking.key, why)
            continue
        swap = schedule.hold_earlier(booking.key, not_before=_not_before(booking, cfg), lead_time=LEAD,
                                     min_gain=MIN_GAIN, now=now)
        if swap is None:
            continue
        old, new = swap
        when = schedule.rfc3339(new.at)
        outcome = optional.call("edit", UNITS, lambda vid=held.video_id, w=when: youtube_set(vid, w))
        if outcome.status != optional.DONE or not outcome.value:
            schedule.keep(old)  # YouTube still has the old time
            if outcome.status in (optional.STOP_ALL, optional.NEEDS_RECONNECT) or (
                    outcome.status == optional.RETRY and outcome.retry_at is not None
                    and "quota" in outcome.reason):
                return {"moved": moved, "note": outcome.reason}
            continue
        ledger.rescheduled(booking.key, when)
        moved += 1
        logger.info("brought %s forward: %s -> %s", booking.key, schedule.rfc3339(old.at), when)
    return {"moved": moved, "note": "", **({"pulled_back": pulled} if pulled else {})}


# How far back the pull-back looks at what the app posted.
PULL_BACK_DAYS = 30
_pulled: set[str] = set()  # video ids already made private by this process


def pull_back_listed(now: datetime) -> int:
    """Make private every video the app posted in the last PULL_BACK_DAYS
    whose meeting is now on the never-post list (or is a personal meeting
    room), scheduled ones too, and free their release slots. Returns how many
    were changed. Needs the edit permission (checked by the caller)."""
    from autopilot import never_post

    cutoff = now - timedelta(days=PULL_BACK_DAYS)
    changed = 0
    for upload in get_ledger().posted():
        if not upload.video_id or upload.video_id in _pulled:
            continue
        try:
            posted_at = datetime.fromisoformat(upload.posted_at) if upload.posted_at else None
        except ValueError:
            posted_at = None
        if posted_at is not None and (posted_at if posted_at.tzinfo else posted_at.replace(tzinfo=UTC)) < cutoff:
            continue
        why = _blocked(upload) or never_post.personal_room(upload.meeting)
        if why is None:
            continue
        outcome = optional.call("edit", UNITS, lambda vid=upload.video_id: _make_private(vid))
        if outcome.status != optional.DONE:
            if outcome.status in (optional.STOP_ALL, optional.NEEDS_RECONNECT):
                break
            continue
        _pulled.add(upload.video_id)
        schedule.release(upload.key)
        if outcome.value:
            changed += 1
            logger.warning("made %s (%s) private: %s", upload.video_id, upload.key, why)
    return changed


def _make_private(video_id: str) -> bool:
    from . import youtube_extra

    return youtube_extra.make_private(video_id)


def youtube_set(video_id: str, when: str) -> bool:
    from . import youtube_extra

    return youtube_extra.set_publish_at(video_id, when)
