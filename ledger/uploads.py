"""The channel's memory: which videos are on YouTube, and which uploads were
started and may still finish. This is what stops a video or a short from
being posted twice: by the autopilot, by a person pressing Publish, after a
crash or a restart, whichever job the cut came from.

Two files in storage_dir/state (see ledger/files.py):
- posted.jsonl is the list people read: one line per video on the channel,
  with the meeting, the full video or short, its title, link, when and by whom.
- uploads.jsonl is the journal. A line before anything is sent ("claim"),
  one with YouTube's upload link before the first byte ("session"), and how
  the upload ended ("published", "stopped", "unsure", "resolved").

Why the upload link matters: one YouTube upload link can create at most one
video. Every retry of an upload goes through the link it started with, so
after a crash we ask YouTube how far that link got (finished, or how many
bytes it holds) instead of starting a second upload. Only when YouTube no
longer knows the link do we look for the video on the channel, by a hidden
tag every upload of the item carries; when that finds nothing either, the
item stops as "check needed" for a person. It is never guessed.

What counts as the same video (the owner's rule, 2026-10-26):
- the full video: one per meeting (per Zoom recording; per job for an upload
  or a link);
- shorts: a meeting's shorts all come from one processing run. Once a short
  of a meeting is on the channel (or on its way), shorts that another job cut
  from the same meeting are never posted; the first job's other shorts still
  can be.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from core.logging import get_logger
from core.models import JobRecord

from . import files

logger = get_logger(__name__)

Kind = Literal["full", "short"]
# uploading: a thread of this process is sending it (or the process died doing so)
# interrupted: stopped after YouTube gave an upload link; resuming uses that link
# failed: stopped before any byte left; uploading again is safe
# unsure: the link is gone and the video wasn't found on the channel; a person checks
State = Literal["uploading", "interrupted", "failed", "published", "unsure"]

JOURNAL = "uploads.jsonl"
POSTED = "posted.jsonl"


class Refused(Exception):
    """The item can't be uploaded now; `upload` is what stands in the way."""

    def __init__(self, message: str, upload: Upload) -> None:
        super().__init__(message)
        self.upload = upload


class AlreadyPosted(Refused):
    pass


class NeedsCheck(Refused):
    pass


class InFlight(Refused):
    pass


@dataclass(frozen=True)
class Item:
    """One video to put on the channel."""

    key: str
    source: str
    kind: Kind
    job_id: str
    meeting: str
    meeting_date: str | None = None
    index: int | None = None  # the short's number in its job (shorts/short_01.mp4 = 1)
    clip: tuple[float, float] | None = None  # the short's stretch of the recording, seconds


@dataclass
class Upload:
    key: str
    source: str
    kind: Kind
    job_id: str
    meeting: str
    meeting_date: str | None = None
    index: int | None = None
    clip: tuple[float, float] | None = None
    state: State = "uploading"
    title: str = ""
    privacy: str = ""
    by: str = ""
    session_uri: str | None = None
    video_id: str | None = None
    url: str | None = None
    posted_at: str | None = None
    error: str | None = None
    updated_at: str = ""

    @property
    def marker(self) -> str:
        return marker_for(self.key)


@dataclass(frozen=True)
class Attempt:
    upload: Upload
    # the upload link to carry on with, None for a fresh upload
    resume_uri: str | None


# ------------------------------------------------------------------ identity


def source_of(job: JobRecord) -> str:
    return f"zoom:{job.zoom_meeting_uuid}" if job.zoom_meeting_uuid else f"job:{job.job_id}"


def full_item(job: JobRecord) -> Item:
    source = source_of(job)
    return Item(f"{source}|full", source, "full", job.job_id, job.title, job.meeting_date)


def short_item(job: JobRecord, index: int, start: float | None = None, end: float | None = None) -> Item:
    source = source_of(job)
    clip = (float(start), float(end)) if start is not None and end is not None else None
    return Item(f"{source}|short|{job.job_id}|{index}", source, "short", job.job_id, job.title, job.meeting_date,
                index, clip)


def marker_for(key: str) -> str:
    """The hidden tag every upload of an item carries, so the video can be
    found on the channel when its upload link is lost."""
    return "hc" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def video_url(kind: Kind, video_id: str) -> str:
    return f"https://youtube.com/shorts/{video_id}" if kind == "short" else f"https://youtu.be/{video_id}"


_VIDEO_ID = re.compile(r"(?:youtu\.be/|/shorts/|/live/|[?&]v=)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")


def video_id_from_url(url: str) -> str | None:
    """The 11-character id in a youtu.be, /shorts/, /live/ or watch?v= link
    (or a bare id)."""
    url = (url or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    match = _VIDEO_ID.search(url)
    return match.group(1) if match else None


# ------------------------------------------------------------------ the ledger


class Ledger:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.journal_path = folder / JOURNAL
        self.posted_path = folder / POSTED
        self._lock = threading.Lock()
        self._uploads: dict[str, Upload] = {}
        # keys a thread of this process is uploading right now
        self._active: set[str] = set()
        self._load()

    # ------------------------------------------------------------- reading
    def _load(self) -> None:
        for event in files.read_jsonl(self.journal_path):
            try:
                self._apply(event)
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("uploads journal: skipping unreadable event %r: %s", event.get("id"), exc)
        listed = set()
        for line in files.read_jsonl(self.posted_path):
            key = line.get("key")
            if not key:
                continue
            listed.add(key)
            upload = self._uploads.get(key)
            if upload is None or upload.state != "published":
                # the list is the final word on what is on the channel
                self._uploads[key] = _from_posted(line)
        for upload in self._uploads.values():
            if upload.state == "uploading":  # the process stopped while it was being sent
                upload.state = "interrupted" if upload.session_uri else "failed"
                upload.error = upload.error or "The server stopped during this upload."
            if upload.state == "published" and upload.key not in listed:
                # the journal got "published" in, the list line didn't
                files.append_jsonl(self.posted_path, _posted_line(upload))

    def _apply(self, event: dict) -> None:
        kind, key, at = event["event"], event["key"], event.get("at", "")
        if kind == "claim":
            self._uploads[key] = Upload(
                key=key, source=event["source"], kind=event["kind"], job_id=event.get("job_id") or "",
                meeting=event.get("meeting") or "", meeting_date=event.get("meeting_date"),
                index=event.get("index"), clip=tuple(event["clip"]) if event.get("clip") else None,
                state="uploading", title=event.get("title") or "", privacy=event.get("privacy") or "",
                by=event.get("by") or "", updated_at=at,
            )
            return
        upload = self._uploads.get(key)
        if upload is None:
            raise KeyError(f"{kind} for unknown upload {key}")
        upload.updated_at = at
        if kind == "session":
            upload.session_uri, upload.state = event["uri"], "uploading"
        elif kind == "published":
            upload.state, upload.error = "published", None
            upload.video_id, upload.url = event["video_id"], event["url"]
            upload.posted_at = at
            upload.by = event.get("by") or upload.by
        elif kind == "stopped":
            upload.state = "interrupted" if upload.session_uri else "failed"
            upload.error = event.get("error")
        elif kind == "unsure":
            upload.state, upload.error = "unsure", event.get("error")
        elif kind == "resolved":
            if event["outcome"] == "posted":
                upload.state, upload.error = "published", None
                upload.video_id, upload.url, upload.posted_at = event["video_id"], event["url"], at
                upload.by = "person"
            else:  # a person checked: it is not on the channel, so a new upload is safe
                upload.state, upload.session_uri = "failed", None
                upload.error = "Checked by a person: not on YouTube."
        else:
            raise ValueError(f"unknown event {kind!r}")

    def get(self, key: str) -> Upload | None:
        with self._lock:
            upload = self._uploads.get(key)
            return replace(upload) if upload else None

    def blocker(self, item: Item) -> Upload | None:
        """What stops `item` from being uploaded as a new video: itself, already
        on the channel or waiting for a check; an upload of it that another job
        started and that was cut off (only that job's file may finish that
        upload link); or for a short, a short of the same meeting from another
        job that is on the channel, being uploaded, cut off or waiting for a
        check. None when it can go up (or be resumed by this job)."""
        with self._lock:
            found = self._blocker(item)
            return replace(found) if found else None

    def _blocker(self, item: Item) -> Upload | None:
        own = self._uploads.get(item.key)
        if own is not None and (own.state in ("published", "unsure") or item.key in self._active):
            return own
        if own is not None and own.session_uri and own.job_id != item.job_id                 and self._state(own) == "interrupted":
            return own
        if item.kind == "short":
            for other in self._uploads.values():
                if (other.source == item.source and other.kind == "short" and other.job_id != item.job_id
                        and (self._state(other) in ("published", "unsure", "interrupted")
                             or other.key in self._active)):
                    return other
        return None

    def _state(self, upload: Upload) -> State:
        """"uploading" with no thread left on it (one died without saying how
        it ended) is cut off: interrupted with a link, failed without."""
        if upload.state == "uploading" and upload.key not in self._active:
            return "interrupted" if upload.session_uri else "failed"
        return upload.state

    def posted(self) -> list[Upload]:
        """Everything on the channel, newest first."""
        with self._lock:
            done = [replace(u) for u in self._uploads.values() if u.state == "published"]
        return sorted(done, key=lambda u: u.posted_at or "", reverse=True)

    def needing_check(self) -> list[Upload]:
        """Uploads a person may have to settle: waiting for a check, or cut
        off mid-upload (publishing their job again finishes those; when that
        job's files are gone, only a person can say whether it got there)."""
        with self._lock:
            return [replace(u, state=self._state(u)) for u in self._uploads.values()
                    if self._state(u) in ("unsure", "interrupted") and u.key not in self._active]

    def for_source(self, source: str) -> list[Upload]:
        with self._lock:
            return [replace(u) for u in self._uploads.values() if u.source == source]

    def meeting_state(self, source: str) -> str | None:
        """"posted" once the full video is on the channel, "partly" when only
        some shorts are, else None."""
        with self._lock:
            mine = [u for u in self._uploads.values() if u.source == source and u.state == "published"]
        if any(u.kind == "full" for u in mine):
            return "posted"
        return "partly" if mine else None

    def busy(self) -> bool:
        with self._lock:
            return bool(self._active)

    # ------------------------------------------------------------- writing
    def begin(self, item: Item, *, title: str, privacy: str, by: str) -> Attempt:
        """Claim `item` for this thread before anything is sent. Raises
        AlreadyPosted, NeedsCheck or InFlight when it must not be sent. A
        cut-off upload of the same item is resumed through its upload link
        (the title it started with stays: YouTube fixed it then)."""
        with self._lock:
            own = self._uploads.get(item.key)
            if item.key in self._active:
                raise InFlight("This video is being uploaded right now.", replace(own))
            blocker = self._blocker(item)
            if blocker is not None:
                raise _refusal(blocker, item)
            if (own is not None and own.state in ("interrupted", "uploading") and own.session_uri
                    and own.job_id == item.job_id):
                self._active.add(item.key)
                return Attempt(replace(own), own.session_uri)
            record = files.append_jsonl(self.journal_path, {
                "at": files.now_iso(), "event": "claim", "key": item.key, "source": item.source,
                "kind": item.kind, "job_id": item.job_id, "meeting": item.meeting,
                "meeting_date": item.meeting_date, "index": item.index,
                "clip": list(item.clip) if item.clip else None, "title": title, "privacy": privacy, "by": by,
            })
            self._apply(record)
            self._active.add(item.key)
            return Attempt(replace(self._uploads[item.key]), None)

    def session_started(self, key: str, uri: str) -> None:
        """Record YouTube's upload link. Must succeed before the first byte is
        sent: an exception here stops the upload."""
        self._write(key, "session", uri=uri)

    def published(self, key: str, video_id: str, *, by: str | None = None) -> Upload:
        with self._lock:
            upload = self._uploads[key]
            record = files.append_jsonl(self.journal_path, {
                "at": files.now_iso(), "event": "published", "key": key, "video_id": video_id,
                "url": video_url(upload.kind, video_id), **({"by": by} if by else {}),
            })
            self._apply(record)
            self._active.discard(key)
            files.append_jsonl(self.posted_path, _posted_line(upload))
            return replace(upload)

    def stopped(self, key: str, error: str) -> None:
        self._write(key, "stopped", error=error[:500])

    def unsure(self, key: str, error: str) -> None:
        self._write(key, "unsure", error=error[:500])

    def record_existing(self, item: Item, *, title: str, url: str, privacy: str = "") -> bool:
        """Put a video that went up before this list existed (a job.json said
        "published") on the list. Returns whether it was new to the list."""
        video_id = video_id_from_url(url)
        with self._lock:
            own = self._uploads.get(item.key)
            if own is not None and own.state == "published":
                return False
            upload = Upload(
                key=item.key, source=item.source, kind=item.kind, job_id=item.job_id, meeting=item.meeting,
                meeting_date=item.meeting_date, index=item.index, clip=item.clip, state="published",
                title=title, privacy=privacy, by="person", video_id=video_id,
                url=video_url(item.kind, video_id) if video_id else url, posted_at=files.now_iso(),
            )
            files.append_jsonl(self.posted_path, _posted_line(upload))
            self._uploads[item.key] = upload
            return True

    def end(self, key: str) -> None:
        """The thread is done with `key`, whatever happened (always called)."""
        with self._lock:
            self._active.discard(key)

    def resolve(self, key: str, *, on_youtube: bool, url: str = "") -> Upload:
        """A person checked YouTube for an item waiting for a check (or one
        whose upload was cut off): it is there (`url`), or it is not, and then
        it may be uploaded again."""
        with self._lock:
            upload = self._uploads.get(key)
            if upload is None:
                raise KeyError(key)
            if key in self._active:
                raise InFlight("This video is being uploaded right now.", replace(upload))
            if upload.state == "published":
                raise AlreadyPosted("This video is already on the list.", replace(upload))
            if on_youtube:
                video_id = video_id_from_url(url)
                if video_id is None:
                    raise ValueError("That doesn't look like a YouTube link.")
                fields = {"outcome": "posted", "video_id": video_id, "url": video_url(upload.kind, video_id)}
            else:
                fields = {"outcome": "not_posted"}
            record = files.append_jsonl(self.journal_path, {
                "at": files.now_iso(), "event": "resolved", "key": key, **fields})
            self._apply(record)
            if on_youtube:
                files.append_jsonl(self.posted_path, _posted_line(upload))
            return replace(upload)

    def _write(self, key: str, event: str, **fields) -> None:
        with self._lock:
            record = files.append_jsonl(self.journal_path, {
                "at": files.now_iso(), "event": event, "key": key, **fields})
            self._apply(record)
            if event in ("stopped", "unsure"):
                self._active.discard(key)


def _refusal(blocker: Upload, item: Item) -> Refused:
    if blocker.key != item.key:
        # shorts of this meeting belong to another processing run
        message = (f"Shorts of this meeting already come from another processing run (job {blocker.job_id}); "
                   "a meeting's shorts are only ever posted from one run.")
        return (InFlight if blocker.state == "uploading" else AlreadyPosted)(message, replace(blocker))
    what = "This video"
    if blocker.state == "published":
        return AlreadyPosted(f"{what} is already on YouTube: {blocker.url}", replace(blocker))
    if blocker.state == "unsure":
        return NeedsCheck(f"{what} needs a check on YouTube first (see the Posted panel).", replace(blocker))
    if blocker.state == "interrupted":
        return NeedsCheck(f"{what} was cut off mid-upload; publish its job ({blocker.job_id}) again to finish it.",
                          replace(blocker))
    return InFlight(f"{what} is being uploaded right now.", replace(blocker))


def _posted_line(upload: Upload) -> dict:
    return {
        "posted_at": upload.posted_at or files.now_iso(),
        "meeting": upload.meeting,
        "meeting_date": upload.meeting_date,
        "kind": upload.kind,
        "short": upload.index,
        "title": upload.title,
        "url": upload.url,
        "video_id": upload.video_id,
        "privacy": upload.privacy,
        "by": upload.by,
        "source": upload.source,
        "job_id": upload.job_id,
        "clip": list(upload.clip) if upload.clip else None,
        "key": upload.key,
    }


def _from_posted(line: dict) -> Upload:
    return Upload(
        key=line["key"], source=line.get("source") or "", kind=line.get("kind") or "full",
        job_id=line.get("job_id") or "", meeting=line.get("meeting") or "",
        meeting_date=line.get("meeting_date"), index=line.get("short"),
        clip=tuple(line["clip"]) if line.get("clip") else None, state="published",
        title=line.get("title") or "", privacy=line.get("privacy") or "", by=line.get("by") or "",
        video_id=line.get("video_id"), url=line.get("url"), posted_at=line.get("posted_at"),
        updated_at=line.get("posted_at") or "",
    )


# ------------------------------------------------------------------ one per folder

_registry_lock = threading.Lock()
_registry: dict[Path, Ledger] = {}


def get_ledger() -> Ledger:
    """The ledger of this server's storage folder (one per process)."""
    folder = files.state_dir().resolve()
    with _registry_lock:
        ledger = _registry.get(folder)
        if ledger is None:
            ledger = _registry[folder] = Ledger(folder)
        return ledger
