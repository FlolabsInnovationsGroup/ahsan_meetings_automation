"""The meetings the autopilot follows, and its own few settings, in
storage_dir/state/autopilot.json (rewritten atomically on every change and
copied to the backup folder, see ledger/files.py).

A meeting goes:
  waiting_recording  Zoom is still making the video
  waiting_transcript the video is ready; Zoom's transcript isn't, and the
                     wait (AUTOPILOT_TRANSCRIPT_WAIT_HOURS after the
                     recording ended) isn't over
  ready              to be processed when the server is free (with Zoom's
                     transcript, or with WhisperX once the wait is over)
  processing         its job runs (or one a person started)
  posting            the job is done; its videos go up
  posted             the full video and its shorts are on the channel
  needs_attention    something a person has to look at (the panel says what)
  skipped            a person said no, or the recording was deleted in Zoom
Nothing ever moves a meeting back from "posted".
"""

from __future__ import annotations

import threading
from typing import Literal

from pydantic import BaseModel, Field

from core.logging import get_logger
from ledger import files

logger = get_logger(__name__)

FILE = "autopilot.json"

MeetingState = Literal[
    "waiting_recording", "waiting_transcript", "ready", "processing", "posting", "posted",
    "needs_attention", "skipped",
]
# a person can't skip a meeting whose job or uploads are running
ACTIVE = ("processing", "posting")


class Meeting(BaseModel):
    uuid: str
    host_email: str = ""
    topic: str = ""
    start_time: str | None = None
    recording_end: str | None = None
    state: MeetingState = "waiting_recording"
    # "zoom" (its own transcript) or "whisperx" (we transcribe), once known
    transcript: str | None = None
    # when the wait for Zoom's transcript ends
    deadline_at: str | None = None
    job_id: str | None = None
    # a person pressed Process before the autopilot did: it posts that job
    by_hand: bool = False
    # processing runs started (the first one included) and posting rounds
    attempts: int = 0
    post_attempts: int = 0
    # not before this time (a retry's wait)
    next_try_at: str | None = None
    error: str | None = None
    first_seen_at: str = ""
    updated_at: str = ""
    posted_at: str | None = None


class ScheduledMeeting(BaseModel):
    email: str
    topic: str = ""
    end_at: str
    # a check ran after it ended
    checked: bool = False


class State(BaseModel):
    # the side panel's on/off switch (off until someone turns it on)
    enabled: bool = False
    # meetings recorded before this are never posted by the autopilot
    start_after: str | None = None
    paused: bool = False
    last_check_at: str | None = None
    last_check_error: str | None = None
    schedule: list[ScheduledMeeting] = Field(default_factory=list)
    schedule_checked_at: str | None = None
    schedule_error: str | None = None
    # the last check that listed every watched account without an error
    last_ok_check_at: str | None = None
    # the app last started with the autopilot off: the start moves to the
    # day it is switched on again
    switched_off: bool = False
    # meetings Zoom's webhook told us about, to look at in the next check,
    # and those Zoom couldn't be asked about then (looked at in later checks)
    nudges: list[str] = Field(default_factory=list)
    nudges_later: list[str] = Field(default_factory=list)
    meetings: dict[str, Meeting] = Field(default_factory=dict)


class Store:
    """The state in memory, saved after every change. Callers change it only
    inside `with store.editing() as state:`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: State | None = None
        self._folder = None

    def _ensure(self) -> State:
        folder = files.state_dir().resolve()
        if self._state is None or self._folder != folder:
            data = files.read_json(folder / FILE, None)
            try:
                self._state = State.model_validate(data) if data else State()
            except ValueError as exc:
                logger.error("autopilot.json can't be read (%s); starting from an empty list", exc)
                self._state = State()
            self._folder = folder
        return self._state

    def read(self) -> State:
        with self._lock:
            return self._ensure().model_copy(deep=True)

    def editing(self):
        return _Editing(self)

    def _save(self) -> None:
        files.write_json(files.state_dir() / FILE, self._ensure().model_dump(mode="json"))


class _Editing:
    def __init__(self, store: Store) -> None:
        self.store = store

    def __enter__(self) -> State:
        self.store._lock.acquire()
        try:
            return self.store._ensure()
        except BaseException:
            self.store._lock.release()  # __exit__ won't run: never keep the lock
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.store._save()
            else:  # drop half-made changes: read the file again next time
                self.store._state = None
        finally:
            self.store._lock.release()


store = Store()
