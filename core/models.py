from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Word(BaseModel):
    """A single ASR word with diarization, per spec section 3.1."""

    word: str
    start: float
    end: float
    speaker: str
    overlap_candidate: bool = False


class Segment(BaseModel):
    """A contiguous speaker turn built from adjacent same-speaker words (section 3.3 input)."""

    speaker: str
    start: float
    end: float
    text: str
    overlap_candidate: bool = False


class Decision(BaseModel):
    """One LLM edit decision for a segment, per spec section 3.3 output schema."""

    start: float
    end: float
    decision: Literal["keep", "remove"]
    reason: str = ""


class EDLRange(BaseModel):
    """A single validated KEEP range in the final Edit Decision List (section 3.4)."""

    start: float
    end: float


class EditDecisionList(BaseModel):
    """Sorted, non-overlapping KEEP ranges ready for slicing."""

    ranges: list[EDLRange] = Field(default_factory=list)
    source_duration: float = 0.0


class JobStatus(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    TRANSCRIBING = "transcribing"
    DECIDING = "deciding"
    BUILDING_EDL = "building_edl"
    SLICING = "slicing"
    DONE = "done"
    FAILED = "failed"
    SKIPPED_DESYNC = "skipped_desync"


EditMode = Literal["crosstalk", "highlights"]

TranscriptSource = Literal["asr", "youtube_captions", "uploaded_transcript", "zoom_transcript"]

YoutubeStatus = Literal["uploading", "published", "failed"]

YoutubePrivacy = Literal["private", "unlisted", "public"]


class JobRecord(BaseModel):
    job_id: str
    status: JobStatus = JobStatus.QUEUED
    mode: EditMode = "highlights"
    source_path: str = ""
    source_url: str | None = None
    # Set for meetings imported from the company Zoom account (ingest/zoom.py).
    zoom_meeting_uuid: str | None = None
    # Human name of the source (e.g. the Zoom meeting topic and date), used
    # as the default YouTube title.
    source_title: str | None = None
    native_transcript_path: str | None = None
    transcript_source: TranscriptSource = "asr"
    # Real, measured progress within the current status (e.g. segments judged so
    # far / total, or clips rendered so far / total) — 0/0 whenever the current
    # stage has no countable unit of work (e.g. transcribing), so the UI can
    # honestly fall back to a plain "in progress" indicator instead of faking a
    # percentage for a stage we can't actually measure.
    progress_current: int = 0
    progress_total: int = 0
    error: str | None = None
    output_video_path: str | None = None
    edl_path: str | None = None
    transcript_path: str | None = None
    clean_transcript_path: str | None = None
    # Publishing the finished cut to YouTube (publish/youtube.py).
    youtube_status: YoutubeStatus | None = None
    youtube_progress: int = 0
    youtube_video_id: str | None = None
    youtube_url: str | None = None
    youtube_error: str | None = None
    # True once the job's videos/transcripts were removed from the server
    # (after publishing, or on explicit request). job.json itself is kept so
    # the YouTube link stays visible.
    files_deleted: bool = False
