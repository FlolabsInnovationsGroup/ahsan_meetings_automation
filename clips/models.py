"""Shared data shapes for the clips subsystem.

Every clips module takes its types from here, and nothing here imports from the
rest of the app, so each module can be built and tested on its own.

All datetimes are timezone-aware UTC. All times inside a video are seconds from
the start of the source file.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field


class RightsBasis(StrEnum):
    """Why we are allowed to clip a source. NONE means we are not."""

    OWN = "own"  # footage we recorded ourselves
    WRITTEN_PERMISSION = "written_permission"  # the creator said yes in writing; proof kept
    PUBLIC_PERMISSION = "public_permission"  # the creator's public statement lets anyone clip
    CAMPAIGN = "campaign"  # a clipping campaign that supplies or licenses the footage
    CREATIVE_COMMONS = "creative_commons"  # CC BY, checked on every single video
    OFFICIAL = "official"  # publisher material, used inside the publisher's own policy
    NONE = "none"  # nothing on file: may be listed, may never be clipped or posted


POSTABLE_BASES: frozenset[RightsBasis] = frozenset(b for b in RightsBasis if b is not RightsBasis.NONE)


class Platform(StrEnum):
    YOUTUBE = "youtube"
    TWITCH = "twitch"
    FOLDER = "folder"  # files dropped into a watched folder (own recordings, files a creator sent)


class VideoStatus(StrEnum):
    NEW = "new"  # seen, nothing done yet
    FETCHED = "fetched"  # the file is on disk
    PICKED = "picked"  # moments were chosen
    DONE = "done"  # its clips were rendered
    SKIPPED = "skipped"  # not usable (see skip_reason)
    FAILED = "failed"


class ClipKind(StrEnum):
    SHORT = "short"  # vertical 1080x1920, under a minute
    LONG = "long"  # landscape, several minutes


class Layout(StrEnum):
    AUTO = "auto"  # let the renderer choose
    SQUARE = "square"  # 1:1 gameplay crop under a title bar
    FULL_WIDTH = "full_width"  # the whole 16:9 frame over a blurred copy of itself
    FACECAM_STACK = "facecam_stack"  # face camera on top, gameplay below
    CENTER_SLICE = "center_slice"  # a 9:16 slice from the middle (sharp sources only)


class ClipStatus(StrEnum):
    DRAFT = "draft"  # rendered, waiting for a person to look at it
    APPROVED = "approved"  # a person said yes; waiting for a slot
    REJECTED = "rejected"
    SCHEDULED = "scheduled"  # has a publish time; not on YouTube yet
    UPLOADING = "uploading"
    UPLOADED = "uploaded"  # on YouTube: private until its publish time, then public
    FAILED = "failed"
    HELD = "held"  # stopped by a guard (rights, wrong channel, kill switch, breaker)


class Source(BaseModel):
    """A place clips may come from, with the reason we may use it."""

    id: str  # short slug, unique
    name: str  # display name
    platform: Platform
    url: str = ""  # channel URL for youtube and twitch
    channel_id: str = ""  # YouTube channel id (UC...) when known
    folder: str = ""  # absolute folder for a FOLDER source
    rights: RightsBasis = RightsBasis.NONE
    evidence_url: str = ""  # where the permission can be read (policy page, post, campaign terms)
    evidence_note: str = ""  # the sentence that grants it, or where the email or file is kept
    evidence_date: str = ""  # ISO date (YYYY-MM-DD) the evidence was read or received
    credit: str = ""  # credit text shown on screen and in descriptions; empty means use name
    enabled: bool = False  # only an enabled source with a valid rights record is ever clipped
    skip_music: bool = True  # drop moments where licensed music is playing
    avoid_spoilers: bool = True  # drop story spoilers
    facecam_box: tuple[float, float, float, float] | None = None  # x, y, w, h as fractions of the frame
    notes: str = ""
    added_at: datetime | None = None


class SourceVideo(BaseModel):
    """One video (or file) of a source."""

    id: str  # "<platform>:<native_id>", unique
    source_id: str
    platform: Platform
    native_id: str  # YouTube video id, Twitch id, or the file name for a folder source
    url: str = ""
    path: str = ""  # local file, once we have one
    title: str = ""
    published_at: datetime | None = None
    duration_s: float = 0.0
    license: str = ""  # YouTube's value: "creativeCommon" or "youtube"; empty when unknown
    is_live: bool = False
    is_short: bool = False
    status: VideoStatus = VideoStatus.NEW
    skip_reason: str = ""
    error: str = ""
    seen_at: datetime | None = None


class Caption(BaseModel):
    """A piece of speech with its time. May be one word or a whole sentence."""

    start_s: float
    end_s: float
    text: str


class Fetched(BaseModel):
    """What we know about a source file once it is on disk."""

    video_path: str
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration_s: float = 0.0
    has_audio: bool = True
    heatmap: list[tuple[float, float, float]] = Field(default_factory=list)  # start_s, end_s, value 0..1
    chapters: list[tuple[float, float, str]] = Field(default_factory=list)  # start_s, end_s, title
    captions: list[Caption] = Field(default_factory=list)


class Window(BaseModel):
    """A stretch of the source that might hold a good moment, and why."""

    start_s: float
    end_s: float
    score: float = 0.0
    signals: dict[str, float] = Field(default_factory=dict)  # for example {"loud": 0.8, "replayed": 0.6}
    note: str = ""


class Moment(BaseModel):
    """A chosen clip: where it starts and ends, and what it is."""

    start_s: float
    end_s: float
    kind: ClipKind = ClipKind.SHORT
    summary: str = ""  # what happens, in plain words, true to the footage
    hook: str = ""  # short text for the top of the screen (40 characters at most)
    reason: str = ""  # why it was picked
    score: float = 0.0  # 0..1
    spoiler: bool = False
    has_music: bool = False
    layout: Layout = Layout.AUTO


class Copy(BaseModel):
    """The words that go on YouTube with a clip."""

    title: str
    description: str
    tags: list[str] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)
    category_id: str = "20"  # Gaming
    language: str = "en"


class Rendered(BaseModel):
    """A finished video file."""

    file: str
    thumb: str = ""
    duration_s: float = 0.0
    width: int = 0
    height: int = 0
    layout: Layout = Layout.AUTO


class Clip(BaseModel):
    """One video we made, from draft to posted."""

    id: str  # 12 hex characters
    source_id: str
    video_id: str
    kind: ClipKind = ClipKind.SHORT
    moment: Moment
    file: str = ""
    thumb: str = ""
    duration_s: float = 0.0
    text: Copy | None = None
    credit: str = ""  # the credit line shown on screen and written in the description
    rights: RightsBasis = RightsBasis.NONE  # the source's basis when the clip was made
    evidence_url: str = ""
    status: ClipStatus = ClipStatus.DRAFT
    reject_reason: str = ""
    publish_at: datetime | None = None
    youtube_id: str = ""
    error: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Uploaded(BaseModel):
    youtube_id: str
    url: str
    publish_at: datetime | None = None
    privacy: str = "private"


class RightsVerdict(BaseModel):
    ok: bool
    reasons: list[str] = Field(default_factory=list)  # why not, in plain words; empty when ok


class LLM(Protocol):
    """A language model that answers with JSON. Tests pass a fake."""

    def json(
        self,
        system: str,
        user: str,
        *,
        images: list[bytes] | None = None,
        max_tokens: int = 2000,
    ) -> dict:
        """Return the model's answer parsed as a JSON object. `images` are JPEG bytes."""
        ...
