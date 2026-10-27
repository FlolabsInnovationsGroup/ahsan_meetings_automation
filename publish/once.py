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
from pathlib import Path

from core.logging import get_logger
from ledger import uploads
from ledger.uploads import Item, NeedsCheck, Upload, get_ledger

from . import youtube

logger = get_logger(__name__)

UNSURE = ("The upload was cut off and YouTube no longer knows it, and the video isn't on the channel "
          "(yet). Look in YouTube Studio, then tell the app whether it is there.")


def upload_once(
    item: Item,
    open_file: Callable[[], AbstractContextManager[Path]],
    *,
    title: str,
    description: str,
    privacy: str,
    by: str,
    on_progress: Callable[[int], None] | None = None,
) -> Upload:
    """Upload `item` unless the ledger says it is (or may be) on the channel.
    Returns the published record. Raises uploads.Refused subclasses when it
    must not go up, and the upload's own error when it failed (the journal
    then knows whether a retry resumes it or starts afresh)."""
    ledger = get_ledger()
    attempt = ledger.begin(item, title=title, privacy=privacy, by=by)
    key = item.key
    try:
        with open_file() as path:
            start = 0
            if attempt.resume_uri is not None:
                outcome = _resume_point(ledger, attempt, path)
                if isinstance(outcome, Upload):
                    return outcome
                start = outcome
            try:
                video_id = youtube.upload_video(
                    path, title, description, privacy, on_progress,  # type: ignore[arg-type]
                    tags=[attempt.upload.marker],
                    session_uri=attempt.resume_uri,
                    start_byte=start,
                    on_session=lambda link: ledger.session_started(key, link),
                )
            except Exception as exc:
                ledger.stopped(key, youtube.friendly_error(exc))
                raise
            return ledger.published(key, video_id)
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
