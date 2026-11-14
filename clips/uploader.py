"""Connecting the clips channel and putting one clip on it.

Upload path: the app's own at-most-once path (publish.once.upload_once with the
ledger in the clips folder), through an Item that carries an empty meeting and
no topic. It already claims the clip before any byte, records YouTube's upload
link, resumes a cut-off upload, and looks the video up by its hidden tag when
the link is lost, so none of that is rewritten here.

Every guard runs here, in a fixed order, before anything is sent.
"""

from __future__ import annotations

import importlib
import logging
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from clips.config import clips_settings
from clips.guard import (
    GuardError,
    assert_isolated,
    breaker_open,
    breaker_reason,
    check_channel,
    kill_reason,
    kill_switch_on,
    note_upload_result,
)
from clips.models import Clip, ClipKind, ClipStatus, Source, Uploaded

logger = logging.getLogger(__name__)

TITLE_MAX = 100
DESCRIPTION_MAX = 4900  # YouTube allows 5000 bytes; leave room for multi-byte text
MIN_AHEAD = timedelta(minutes=15)  # a clip is uploaded only when its publish time is at least this far ahead
CONNECT_FLAG = "CONNECTING"  # a file in the clips folder while a sign-in is being checked: uploads wait
CONNECT_FLAG_TTL_S = 600
_last_state = ""
_connect_lock = threading.Lock()


class UploadError(RuntimeError):
    """An upload failed in a way that may work on a retry. The message is plain words."""


class NotScheduledError(RuntimeError):
    """The clip has no publish time, or its time is less than 15 minutes ahead (or past). It is not a
    guard refusal and not an upload failure: nothing was sent. The scheduler gives the clip a new slot."""


class ClipBusy(RuntimeError):
    """Another worker (or process) already took this clip. Skip it and leave its status alone;
    this is not a failure and must not count toward the breaker."""


# ------------------------------------------------------------------ connecting

def auth_url(state: str = "") -> str:
    """The Google sign-in address for connecting the clips channel."""
    global _last_state
    from publish import youtube

    url = youtube.start_connect()
    generated = parse_qs(urlparse(url).query).get("state", [""])[0]
    if state and generated and state != generated:
        # keep the caller's own state value: re-key the pending sign-in under it
        with youtube._pending_lock:
            youtube._pending[state] = youtube._pending.pop(generated)
        url = url.replace(f"state={generated}", f"state={state}")
        generated = state
    _last_state = generated
    return url


def finish_auth(code: str, state: str = "") -> tuple[str, str]:
    """Store the token. Returns (channel_id, channel_title).

    The new token is held in memory while its channel is checked and is written to the token
    file only when it is the configured clips channel. Uploads wait while this runs."""
    from publish import youtube

    settings = clips_settings()
    flag = settings.storage_dir / CONNECT_FLAG
    with _connect_lock:
        settings.storage_dir.mkdir(parents=True, exist_ok=True)
        flag.write_text(str(time.time()), encoding="utf-8")
        held: list[tuple[object, str]] = []
        real_write = youtube._write_secret
        token_path = youtube._token_path()

        def hold(path, text, *args, **kwargs):
            if Path(path) == Path(token_path):
                held.append((path, text))
                return None
            return real_write(path, text, *args, **kwargs)

        youtube._write_secret = hold
        try:
            youtube.finish_connect(state or _last_state, code)
        finally:
            youtube._write_secret = real_write
        try:
            found = _channel_of_token(held[-1][1]) if held else connected_channel()
            if found is None:
                youtube.disconnect()
                raise GuardError("Signed in, but YouTube shows no channel for this account.")
            try:
                check_channel(found[0], settings)  # raises with the plain reason
            except GuardError:
                youtube.disconnect()  # a held token was never written; this also clears an older one
                raise
            if held:
                real_write(*held[-1])
            return found
        finally:
            flag.unlink(missing_ok=True)


def _channel_of_token(token_json: str) -> tuple[str, str] | None:
    """The channel a not-yet-saved token belongs to."""
    import json

    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    from publish import youtube

    creds = Credentials.from_authorized_user_info(json.loads(token_json))
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    youtube._count(1, "channels.list")
    return connected_channel(service)


def _connecting() -> bool:
    flag = clips_settings().storage_dir / CONNECT_FLAG
    try:
        return time.time() - float(flag.read_text(encoding="utf-8")) < CONNECT_FLAG_TTL_S
    except (OSError, ValueError):
        return False


def connected_channel(service=None) -> tuple[str, str] | None:
    """(channel_id, title) of the signed-in channel, or None when not connected."""
    from publish import youtube

    if service is None:
        try:
            creds = youtube._load_credentials()
        except youtube.YoutubeNotConnectedError:
            return None
        from googleapiclient.discovery import build

        service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        youtube._count(1, "channels.list")
    items = service.channels().list(part="id,snippet", mine=True).execute().get("items", [])
    if not items:
        return None
    return items[0]["id"], items[0].get("snippet", {}).get("title", "")


# -------------------------------------------------------------------- uploading

def upload_clip(clip: Clip, source: Source | None, store, *, service=None, dry_run: bool = False) -> Uploaded:
    """Run every guard, then upload the clip once. `dry_run` checks and sends nothing.

    Raises GuardError when a guard refuses (the clip is held), UploadError for a
    failure worth retrying."""
    if kill_switch_on():
        raise GuardError(f"The kill switch is on: {kill_reason() or 'posting is stopped'}.")
    assert_isolated()
    if breaker_open(store):
        raise GuardError(f"The breaker is open: {breaker_reason(store) or 'too many problems'}. Reset it to go on.")
    verdict = importlib.import_module("clips.rights").check_clip(clip, source)
    if not verdict.ok:
        raise GuardError("Rights check failed: " + "; ".join(verdict.reasons))
    if clip.youtube_id:
        return _uploaded(clip, clip.youtube_id)  # never a second copy
    problem = _clip_problem(clip)
    if problem:
        raise GuardError(problem)
    privacy, publish_at = _privacy(clip)  # refuses a slot that has passed
    if _connecting():
        raise GuardError("A YouTube sign-in is being checked right now; uploads wait for it.")
    found = connected_channel(service)
    if found is None:
        raise GuardError("No YouTube channel is connected, so nothing is uploaded.")
    check_channel(found[0])
    if dry_run:
        return Uploaded(youtube_id="", url="", publish_at=publish_at, privacy=privacy)
    return _send(clip, store)


def _clip_problem(clip: Clip) -> str:
    if clip.text is None:
        return "The clip has no title or description yet."
    if not clip.file or not Path(clip.file).is_file():
        return "The clip's video file is missing."
    return ""


def _privacy(clip: Clip) -> tuple[str, datetime | None]:
    """("private", publish_at): a video always goes up private and YouTube releases it at publish_at.

    A clip with no publish time, or one less than 15 minutes ahead (or past), is refused with
    NotScheduledError: a late clip is booked again by the scheduler, never made public at once."""
    when = clip.publish_at
    if when is None:
        raise NotScheduledError(f"Clip {clip.id} has no publish time, so it is not uploaded. It needs a slot first.")
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if when < datetime.now(UTC) + MIN_AHEAD:
        raise NotScheduledError(
            f"The publish time {when.isoformat(timespec='minutes')} is past or less than 15 minutes away. "
            "The clip needs a new slot; it is not posted at once."
        )
    return "private", when


def _fresh_ledger() -> None:
    """Reload the upload journal from disk: another process may have written to it since this one read it."""
    from ledger import files, uploads

    folder = files.state_dir().resolve()
    with uploads._registry_lock:
        uploads._registry[folder] = uploads.Ledger(folder)


def _send(clip: Clip, store) -> Uploaded:
    from ledger import uploads
    from publish import once, youtube

    privacy, publish_at = _privacy(clip)
    copy = clip.text
    item = uploads.Item(
        key=f"clips:{clip.id}", source=f"clips:{clip.id}", kind="short" if clip.kind is ClipKind.SHORT else "full",
        job_id=clip.id, meeting="",
    )
    if not store.claim_upload(clip.id):  # one claim across every process, before any network call
        raise ClipBusy(f"Clip {clip.id} is already being uploaded, or is no longer scheduled.")
    try:
        _fresh_ledger()
    except Exception as exc:  # noqa: BLE001 - a stale journal view is not worth stopping for
        logger.warning("could not reload the upload journal: %s", exc)
    try:
        done = once.upload_once(
            item, lambda: _open_file(clip.file),
            title=_title(copy.title), description=_description(clip), privacy=privacy, by="clips",
            publish_at=publish_at, notify=True, category_id=copy.category_id or None, language=copy.language or None,
        )
    except uploads.AlreadyPosted as exc:
        return _uploaded(clip, exc.upload.video_id or "", when=publish_at)
    except (uploads.Refused, once.NeverPost) as exc:
        store.set_clip_status(clip.id, ClipStatus.SCHEDULED)
        raise GuardError(str(exc)) from exc
    except Exception as exc:
        message = youtube.friendly_error(exc)
        store.set_clip_status(clip.id, ClipStatus.SCHEDULED, error=message)
        note_upload_result(store, False, message)
        raise UploadError(message) from exc
    note_upload_result(store, True)
    return _uploaded(clip, done.video_id or "", when=publish_at, privacy=privacy)


def _open_file(path: str):
    from contextlib import nullcontext

    return nullcontext(Path(path))


def _uploaded(clip: Clip, video_id: str, *, when: datetime | None = None, privacy: str | None = None) -> Uploaded:
    return Uploaded(
        youtube_id=video_id, url=f"https://youtube.com/shorts/{video_id}" if clip.kind is ClipKind.SHORT
        else f"https://youtu.be/{video_id}",
        publish_at=when or clip.publish_at, privacy=privacy or "private",
    )


def _title(text: str) -> str:
    text = re.sub(r"[<>]", "", text).strip() or "Clip"
    return text if len(text) <= TITLE_MAX else text[: TITLE_MAX - 1].rstrip() + "…"


def _description(clip: Clip) -> str:
    """The copy's description plus the credit and hashtags, when not already in it."""
    copy = clip.text
    text = re.sub(r"[<>]", "", copy.description).strip()
    if clip.credit and clip.credit not in text:
        text += f"\n\n{clip.credit}"
    tags = [t for t in copy.hashtags if t not in text]
    if tags:
        text += "\n\n" + " ".join(tags)
    return text[:DESCRIPTION_MAX]


# ----------------------------------------------------------------- pulling back

def pull_back_pending(store, service=None) -> tuple[list[str], list[str]]:
    """Take every uploaded clip that is not public yet back to private with no release time.

    Used when posting is stopped: videos already on YouTube would otherwise still go public at
    their publish time. Each clip that worked becomes HELD. Returns (clip ids pulled back,
    clip ids that could not be, with the reason in the clip's error)."""
    from clips.guard import pending_releases
    from publish import youtube

    pending = pending_releases(store)
    done: list[str] = []
    failed: list[str] = []
    if not pending:
        return done, failed
    assert_isolated()  # off the clips folder the token on disk is the company's: never read or refresh it
    try:
        if service is None:
            from googleapiclient.discovery import build

            service = build("youtube", "v3", credentials=youtube._load_credentials(), cache_discovery=False)
    except Exception as exc:  # noqa: BLE001
        message = youtube.friendly_error(exc)
        for clip in pending:
            store.set_clip_status(clip.id, ClipStatus.UPLOADED, error=f"Could not pull back: {message}")
            failed.append(clip.id)
        store.add_event("kill", f"Could not pull back {len(failed)} scheduled video(s): {message}")
        return done, failed
    for clip in pending:
        try:
            service.videos().update(
                part="status",
                body={"id": clip.youtube_id, "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False}},
            ).execute()
            youtube._count(50, "videos.update")
        except Exception as exc:  # noqa: BLE001
            message = youtube.friendly_error(exc)
            store.set_clip_status(clip.id, ClipStatus.UPLOADED, error=f"Could not pull back: {message}")
            store.add_event("kill", f"{clip.id} could not be pulled back: {message}", clip_id=clip.id)
            failed.append(clip.id)
            continue
        store.set_clip_status(clip.id, ClipStatus.HELD, error="Pulled back to private when posting was stopped.")
        store.add_event("kill", f"{clip.id} pulled back to private", clip_id=clip.id)
        done.append(clip.id)
    return done, failed
