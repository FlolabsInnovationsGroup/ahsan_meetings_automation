"""A YouTube channel in memory, standing in for publish/youtube.py's
upload_video, session_status and find_by_marker, that can die at any step of
an upload. Shared by the ledger, publishing and autopilot tests.

What it models, as YouTube does it: an upload link is made first (no video
yet); bytes arrive through that link; the video exists once the last byte is
in; one link makes at most one video; a link YouTube forgot ("expired")
answers 404; a finished link answers with its video.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from publish import youtube as yt

# Where an upload can die:
# - "before_link": asking for the upload link fails (nothing on YouTube)
# - "after_link": the link is made and recorded, then the process dies
# - "mid_upload": half the bytes are in
# - "after_last_byte": YouTube made the video, the answer never arrived
CRASH_POINTS = ("before_link", "after_link", "mid_upload", "after_last_byte")


class Crash(BaseException):
    """The process dies: not an Exception, so no handler records anything."""


class TransientError(OSError):
    """A dropped connection (what upload_video's own retries give up on)."""


@dataclass
class Session:
    uri: str
    size: int
    title: str
    tags: list[str]
    privacy: str
    received: int = 0
    video_id: str | None = None
    expired: bool = False


@dataclass
class FakeChannel:
    sessions: dict[str, Session] = field(default_factory=dict)
    videos: dict[str, dict] = field(default_factory=dict)  # id -> {"title", "tags", "privacy", "session"}
    crash_at: str | None = None
    # an Exception class to raise instead of Crash (a handled failure)
    raise_with: type[BaseException] = Crash
    status_error: BaseException | None = None  # session_status raises this once
    calls: list[str] = field(default_factory=list)

    def install(self, monkeypatch) -> FakeChannel:
        monkeypatch.setattr(yt, "upload_video", self.upload_video)
        monkeypatch.setattr(yt, "session_status", self.session_status)
        monkeypatch.setattr(yt, "find_by_marker", self.find_by_marker)
        return self

    def _die(self, point: str) -> None:
        if self.crash_at == point:
            self.crash_at = None  # it dies once; the next attempt runs normally
            raise self.raise_with(f"died at {point}")

    # ------------------------------------------------------------- upload_video
    def upload_video(self, path, title, description, privacy, on_progress=None, *, tags=(), session_uri=None,
                     start_byte=0, on_session=None):
        size = Path(path).stat().st_size
        if session_uri is None:
            self.calls.append("new_link")
            self._die("before_link")
            uri = f"https://upload.example/s{len(self.sessions) + 1}"
            self.sessions[uri] = Session(uri, size, title, list(tags), privacy)
            if on_session is not None:
                on_session(uri)
            self._die("after_link")
        else:
            self.calls.append(f"resume@{start_byte}")
            uri = session_uri
            session = self.sessions[uri]
            if session.expired:
                raise RuntimeError("404 upload link expired")
            if session.video_id is not None:
                return session.video_id
            assert start_byte == session.received, "resumed from the wrong byte"
        session = self.sessions[uri]
        session.received = max(session.received, size // 2)
        if on_progress is not None:
            on_progress(50)
        self._die("mid_upload")
        session.received = size
        session.video_id = f"v{len(self.videos) + 1:010d}"
        self.videos[session.video_id] = {"title": session.title, "tags": session.tags,
                                         "privacy": session.privacy, "session": uri}
        self._die("after_last_byte")
        return session.video_id

    # ------------------------------------------------------------- the rest
    def session_status(self, uri, size):
        self.calls.append("status")
        if self.status_error is not None:
            error, self.status_error = self.status_error, None
            raise error
        session = self.sessions.get(uri)
        if session is None or session.expired:
            return "gone", None
        if session.video_id is not None:
            return "done", session.video_id
        return "partial", session.received

    def find_by_marker(self, marker):
        self.calls.append("search")
        for video_id, video in self.videos.items():
            if marker in video["tags"]:
                return video_id
        return None

    def forget_links(self) -> None:
        """YouTube expires every upload link."""
        for session in self.sessions.values():
            session.expired = True
