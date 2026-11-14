"""Putting one video on the channel at most once.

The order matters (ledger/uploads.py explains why):
1. claim the item in the journal, before the file is even opened;
2. a cut-off upload of the item is asked about first: finished (record it),
   partly there (carry on through the same link), or forgotten by YouTube
   (look for it on the channel by its hidden tag; not found = a person checks);
3. a new upload records YouTube's upload link before the first byte;
4. the result goes into the journal and the posted list.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path

from autopilot import never_post
from core.logging import get_logger
from ledger import uploads
from ledger.uploads import Item, NeedsCheck, Upload, get_ledger

from . import schedule, youtube

logger = get_logger(__name__)

UNSURE = ("The upload was cut off and YouTube no longer knows it, and the video isn't on the channel "
          "(yet). Look in YouTube Studio, then tell the app whether it is there.")


class NeverPost(Exception):
    """The meeting is on the never-post list; the message says why."""


def upload_once(
    item: Item,
    open_file: Callable[[], AbstractContextManager[Path]],
    *,
    title: str,
    description: str,
    privacy: str,
    by: str,
    on_progress: Callable[[int], None] | None = None,
    publish_at: datetime | None = None,
    notify: bool = True,
    category_id: str | None = None,
    language: str | None = None,
    recording_date: str | None = None,
    localizations: dict | Callable[[], dict | None] | None = None,
) -> Upload:
    """Upload `item` unless the ledger says it is (or may be) on the channel.
    Returns the published record. Raises uploads.Refused subclasses when it
    must not go up, and the upload's own error when it failed (the journal
    then knows whether a retry resumes it or starts afresh).

    With `publish_at` the video goes up private and YouTube makes it public
    then (publish/schedule.py). After a new upload, YouTube is asked once what
    it holds; if that differs from what was asked, the returned record's
    `warning` says so (the video exists, so this never fails the upload).

    A meeting on the never-post list is refused first, before the journal is
    touched, so the refusal leaves nothing claimed or stuck as "uploading"."""
    # the job's title, and what Zoom called the meeting (topic and ID), as match_job looks at them.
    # Every kind of item (full, short, topic cut) carries both, so this last look covers them all.
    why = never_post.match(item.meeting) or never_post.match(item.topic, item.meeting_id)
    if why is not None:
        raise NeverPost(f"Not uploaded: {why}.")
    ledger = get_ledger()
    attempt = ledger.begin(
        item, title=title, privacy=privacy, by=by,
        publish_at=schedule.rfc3339(publish_at) if publish_at is not None else None,
        notify=notify if publish_at is not None else None,
    )
    key = item.key
    asked_at = schedule.rfc3339(publish_at) if publish_at is not None else None
    try:
        with open_file() as path:
            start = 0
            if attempt.resume_uri is not None:
                # YouTube fixed the time to go public when the link was made. A link
                # made for another time (already past, inside the review window,
                # or none at all) must not finish: it would go public then, or
                # ignore what is asked now.
                changed = attempt.upload.publish_at != asked_at
                outcome = _resume_point(ledger, attempt, path)
                if isinstance(outcome, Upload):
                    if changed:  # the video exists, with the old time: say so if it isn't what was asked
                        outcome.warning = _readback(outcome.video_id, privacy, publish_at)
                    return outcome
                if changed:
                    logger.info("upload %s: its cut-off link was made for another time; starting a new one", key)
                    attempt = ledger.restart(
                        item, title=title, privacy=privacy, by=by,
                        publish_at=asked_at, notify=notify if publish_at is not None else None,
                    )
                else:
                    start = outcome
            if callable(localizations):
                # only a new upload asks for translations (after the never-post
                # check and the journal claim); a resumed one keeps what its link
                # was made with and never recomputes them
                wanted = None
                if attempt.resume_uri is None:
                    try:
                        wanted = localizations()
                    except Exception as exc:  # noqa: BLE001 - translations are optional
                        logger.warning("upload %s: translations skipped (%s)", key, type(exc).__name__)
            else:
                wanted = localizations if attempt.resume_uri is None else None
            try:
                video_id = youtube.upload_video(
                    path, title, description, privacy, on_progress,  # type: ignore[arg-type]
                    tags=[attempt.upload.marker],
                    session_uri=attempt.resume_uri,
                    start_byte=start,
                    on_session=lambda link: ledger.session_started(key, link),
                    publish_at=publish_at, notify=notify, category_id=category_id, language=language,
                    recording_date=recording_date, **({"localizations": wanted} if wanted else {}),
                )
            except Exception as exc:
                ledger.stopped(key, youtube.friendly_error(exc))
                raise
            posted = ledger.published(key, video_id)
            if attempt.resume_uri is None:  # a resumed upload keeps what its link was made with
                posted.warning = _readback(video_id, privacy, publish_at)
            return posted
    except uploads.Refused:
        raise
    except Exception as exc:
        current = ledger.get(key)
        if current is not None and current.state == "uploading":
            # failed before or around the upload itself (the file is gone, the
            # disk is full): record how it ended
            ledger.stopped(key, str(exc)[:300] or type(exc).__name__)
        raise
    finally:
        ledger.end(key)


def _readback(video_id: str, privacy: str, publish_at: datetime | None) -> str | None:
    """Ask YouTube what it holds for a new video. None when it matches what was
    asked (or can't be read: the video is there, so that is only logged), else
    a plain sentence for the page."""
    asked_privacy = "private" if publish_at is not None else privacy
    try:
        held = youtube.read_status(video_id)
    except Exception as exc:  # noqa: BLE001 - the video exists; a failed readback must never undo that
        logger.warning("video %s: couldn't read back its visibility: %s", video_id, exc)
        return None
    held_at = _as_utc(held.get("publishAt"))
    asked_at = publish_at.astimezone(UTC) if publish_at is not None else None
    if held.get("privacyStatus") == asked_privacy and held_at == asked_at:
        return None
    what = (f"YouTube shows it as {held.get('privacyStatus')}"
            + (f", going public {schedule.team_time_text(held_at)}" if held_at else "")
            + f", not as asked ({asked_privacy}"
            + (f", going public {schedule.team_time_text(asked_at)}" if asked_at else "") + ").")
    logger.warning("video %s: %s", video_id, what)
    return f"Check this video in YouTube Studio: {what}"


def _as_utc(value) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
    return moment.astimezone(UTC) if moment is not None and moment.tzinfo else moment


def _resume_point(ledger, attempt, path: Path) -> Upload | int:
    """For a cut-off upload: the published record when YouTube already has
    the whole video, else the byte to carry on from. Raises NeedsCheck when
    nobody can tell, and the network error when YouTube can't be asked now."""
    key = attempt.upload.key
    try:
        where, value = youtube.session_status(attempt.resume_uri, path.stat().st_size)
        if where == "done":
            logger.info("upload %s had finished before the interruption: video %s", key, value)
            return ledger.published(key, value)
        if where == "partial":
            logger.info("resuming upload %s from byte %d", key, value)
            return int(value)
        found = youtube.find_by_marker(attempt.upload.marker)
    except Exception as exc:
        ledger.stopped(key, f"Couldn't ask YouTube where the cut-off upload stands: {youtube.friendly_error(exc)}")
        raise
    if found:
        logger.info("upload %s: its link expired but the video is on the channel: %s", key, found)
        return ledger.published(key, found)
    ledger.unsure(key, UNSURE)
    raise NeedsCheck(UNSURE, ledger.get(key))
