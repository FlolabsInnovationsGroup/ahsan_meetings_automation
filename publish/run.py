"""A publishing run: the shorts first, then the full video.

Why this order. The full video's description lists its shorts with links, and
a short's link exists only once the short is uploaded. Editing the full video
afterwards would need a wider Google permission than the app asks for (it only
asks to upload). So the shorts go up first, each saying in words where its
clip plays in the full video; then the full video goes up with a list of the
shorts that made it. A short posted after the full video is already online is
not in that list (the page offers the list to paste into the description).

Each short and the full video are separate uploads with their own state on the
job, so one failing never loses the others, and a retry never uploads a video
twice (a published one is refused).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from core.logging import get_logger
from core.models import JobRecord, YoutubePrivacy, YoutubeUpload
from core.timeline import fmt_clock

from . import describe, media, youtube
from .cleanup import delete_job_files

logger = get_logger(__name__)

Update = Callable[[JobRecord], None]

INTERRUPTED = "Upload interrupted by a server restart. Please publish again."


class PublishRefused(Exception):
    """The request can't be carried out; `status_code` is the HTTP answer."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ShortPlan:
    index: int
    title: str
    description: str


@dataclass(frozen=True)
class FullPlan:
    title: str
    # without the shorts list: that is added when the full video goes up
    description: str


@dataclass(frozen=True)
class PublishPlan:
    privacy: YoutubePrivacy
    shorts: tuple[ShortPlan, ...] = ()
    full: FullPlan | None = None
    # Remove the files from the server once everything that can be published
    # is on the channel (never while something is still to post or failed).
    delete_after: bool = True


# ---------------------------------------------------------------- the plan


def build_plan(
    job: JobRecord,
    *,
    title: str | None = None,
    description: str | None = None,
    privacy: YoutubePrivacy = "private",
    shorts: Sequence[tuple[int, str | None, str | None]] = (),
    delete_after: bool = True,
) -> PublishPlan:
    """Check a request against the job and fill in what it left out.

    `title` None = no full video. `shorts` are (number, title, description);
    a title or description left None is the suggested one (publish_options).
    Raises PublishRefused, whose status code is the HTTP answer."""
    if job.files_deleted:
        raise PublishRefused(409, "The files were already removed from the server.")
    if job.youtube_busy:
        raise PublishRefused(409, "An upload for this job is already running.")
    if not youtube.youtube_status()["connected"]:
        raise PublishRefused(409, "No YouTube channel connected yet.")
    if title is None and not shorts:
        raise PublishRefused(400, "Nothing to publish: choose the full video, some shorts, or both.")

    infos = {s.index: s for s in describe.load_shorts(job)}
    final = media.final_member(job)
    wanted = ([final] if final else []) + [infos[i].file for i, _, _ in shorts if i in infos]
    on_hand = media.available(job, wanted)

    full = None
    if title is not None:
        if job.youtube_status == "published":
            raise PublishRefused(409, f"The full video is already on YouTube: {job.youtube_url}")
        full_title = title.strip()
        _valid(describe.title_problem(full_title), "title")
        full_text = describe.full_description(job) if description is None else description
        _valid(describe.description_problem(full_text), "description")
        if final is None or final not in on_hand:
            raise PublishRefused(410, "The final video is no longer on the server.")
        full = FullPlan(full_title, full_text)

    full_url = job.youtube_url if job.youtube_status == "published" else None
    plans: dict[int, ShortPlan] = {}
    for index, short_title, short_text in shorts:
        info = infos.get(index)
        if info is None:
            raise PublishRefused(404, f"This job has no short {index}.")
        state = job.youtube_shorts.get(index)
        if state is not None and state.status == "published":
            raise PublishRefused(409, f"Short {index} is already on YouTube: {state.url}")
        if info.file not in on_hand:
            raise PublishRefused(410, f"Short {index} is no longer on the server.")
        short_title = describe.short_title(info, job) if short_title is None else short_title.strip()
        short_text = describe.short_description(info, job, full_url) if short_text is None else short_text
        _valid(describe.title_problem(short_title), f"title of short {index}")
        _valid(describe.description_problem(short_text), f"description of short {index}")
        plans[index] = ShortPlan(index, short_title, short_text)

    return PublishPlan(privacy, tuple(plans[i] for i in sorted(plans)), full, delete_after)


def _valid(problem: str | None, what: str) -> None:
    if problem:
        raise PublishRefused(400, f"Invalid {what}: {problem}.")


def prepare(job: JobRecord, plan: PublishPlan) -> None:
    """Mark every upload of the plan as waiting, so the page shows them at once
    (before the background thread starts)."""
    if plan.shorts:
        # a new dict swapped in at once, so a request serialising the job on
        # another thread never sees this one change size
        job.youtube_shorts = {
            **job.youtube_shorts,
            **{short.index: YoutubeUpload(status="queued", title=short.title) for short in plan.shorts},
        }
    if plan.full is not None:
        job.youtube_status = "queued"
        job.youtube_progress = 0
        job.youtube_error = None


# ------------------------------------------------------------------ the run


def publish_job(job: JobRecord, update: Update, plan: PublishPlan) -> None:
    """Background task: the shorts, then the full video, then (if asked) the
    removal of the files, but only once everything that can be published is."""
    try:
        stopped = _upload_all(job, update, plan)
        if plan.delete_after and stopped is None and everything_published(job):
            delete_job_files(job)
    except Exception as exc:
        logger.exception("job %s: publishing crashed", job.job_id)
        _fail_waiting(job, f"Publishing stopped unexpectedly: {exc}"[:300])
    finally:
        update(job)


def _upload_all(job: JobRecord, update: Update, plan: PublishPlan) -> str | None:
    """Uploads everything in the plan. Returns why it stopped early (an error
    every further upload would hit too: quota, a revoked connection), or None."""
    stop: str | None = None
    for short in plan.shorts:
        if stop is not None:
            _not_uploaded(job.youtube_shorts[short.index], stop)
            continue
        stop = _upload_short(job, update, short, plan.privacy)
    if plan.full is not None:
        if stop is not None:
            job.youtube_status, job.youtube_error = "failed", f"Not uploaded: {stop}"
        else:
            stop = _upload_full(job, update, plan.full, plan.privacy)
    return stop


def _upload_short(job: JobRecord, update: Update, short: ShortPlan, privacy: YoutubePrivacy) -> str | None:
    state = job.youtube_shorts[short.index]
    state.status, state.progress, state.error = "uploading", 0, None
    update(job)

    def progress(pct: int) -> None:
        state.progress = max(0, min(99, pct))
        update(job)

    try:
        with media.open_media(job, media.short_member(short.index)) as path:
            video_id = youtube.upload_video(path, short.title, short.description, privacy, on_progress=progress)
    except Exception as exc:
        logger.exception("job %s: YouTube upload of short %d failed", job.job_id, short.index)
        state.status, state.error = "failed", _error_text(exc)
        update(job)
        return state.error if youtube.is_account_wide_error(exc) else None
    state.status, state.progress, state.error = "published", 100, None
    state.video_id, state.url = video_id, f"https://youtube.com/shorts/{video_id}"
    update(job)
    return None


def _upload_full(job: JobRecord, update: Update, full: FullPlan, privacy: YoutubePrivacy) -> str | None:
    description, listed = describe.with_shorts_block(full.description, published_shorts(job))
    job.youtube_status, job.youtube_progress, job.youtube_error = "uploading", 0, None
    update(job)

    def progress(pct: int) -> None:
        job.youtube_progress = max(0, min(99, pct))
        update(job)

    try:
        member = media.final_member(job)
        if member is None:
            raise media.MediaMissing("The final video is no longer on the server.")
        with media.open_media(job, member) as path:
            video_id = youtube.upload_video(path, full.title, description, privacy, on_progress=progress)
    except Exception as exc:
        logger.exception("job %s: YouTube upload of the full video failed", job.job_id)
        job.youtube_status, job.youtube_error = "failed", _error_text(exc)
        update(job)
        return job.youtube_error if youtube.is_account_wide_error(exc) else None
    job.youtube_video_id = video_id
    job.youtube_url = f"https://youtu.be/{video_id}"
    job.youtube_status = "published"
    job.youtube_progress = 100
    job.youtube_error = None
    job.youtube_listed_shorts = listed
    update(job)
    return None


def _error_text(exc: Exception) -> str:
    return str(exc) if isinstance(exc, media.MediaMissing) else youtube.friendly_error(exc)


def _not_uploaded(state: YoutubeUpload, reason: str) -> None:
    state.status, state.error = "failed", f"Not uploaded: {reason}"


def published_shorts(job: JobRecord) -> list[tuple[int, str, str]]:
    """(number, title, link) of the shorts that are on the channel."""
    return [(i, s.title, s.url) for i, s in sorted(job.youtube_shorts.items()) if s.status == "published" and s.url]


def everything_published(job: JobRecord) -> bool:
    """The full video and every short the job rendered are on the channel."""
    if job.youtube_status != "published":
        return False
    for index in describe.rendered_shorts(job):
        state = job.youtube_shorts.get(index)
        if state is None or state.status != "published":
            return False
    return True


# ---------------------------------------------------- restarts and crashes


def fail_interrupted_uploads(job: JobRecord) -> bool:
    """A restart killed the upload thread: whatever was waiting or running
    will never finish. The cut itself is fine, so publishing can simply be
    tried again. Returns whether anything changed."""
    return _fail_waiting(job, INTERRUPTED)


def _fail_waiting(job: JobRecord, message: str) -> bool:
    changed = False
    if job.youtube_status in ("queued", "uploading"):
        job.youtube_status, job.youtube_error = "failed", message
        changed = True
    for state in job.youtube_shorts.values():
        if state.status in ("queued", "uploading"):
            state.status, state.error = "failed", message
            changed = True
    return changed


# ---------------------------------------------------------- what the page needs


def publish_options(job: JobRecord) -> dict:
    """What the publish panel shows: the channel, and for the full video and
    each short its suggested title and description, whether its file can still
    be uploaded, and where it stands. Nothing here is a promise: the request
    to publish carries the final words."""
    shorts = describe.load_shorts(job)
    final = media.final_member(job)
    on_hand = media.available(job, ([final] if final else []) + [s.file for s in shorts])
    full_url = job.youtube_url if job.youtube_status == "published" else None
    rows = [_short_row(job, info.index, info, info.file in on_hand, full_url) for info in shorts]
    # shorts that are on the channel but whose files, and manifest, are gone
    known = {info.index for info in shorts}
    rows += [_short_row(job, index, None, False, full_url) for index in job.youtube_shorts if index not in known]
    rows.sort(key=lambda row: row["index"])
    return {
        "youtube": youtube.youtube_status(),
        "files_deleted": job.files_deleted,
        "busy": job.youtube_busy,
        "full": {
            "available": final is not None and final in on_hand,
            "title": describe.full_title(job),
            "description": describe.full_description(job),
            "status": job.youtube_status,
            "progress": job.youtube_progress,
            "url": job.youtube_url,
            "error": job.youtube_error,
        },
        "shorts": rows,
        "shorts_block": _unlisted_block(job),
    }


def _short_row(
    job: JobRecord, index: int, info: describe.ShortInfo | None, available: bool, full_url: str | None
) -> dict:
    """One short for the panel. `info` is None once the manifest is gone: only
    what the job remembers about the upload is left."""
    state = job.youtube_shorts.get(index)
    at_s = info.at_s if info is not None else None
    return {
        "index": index,
        "available": available,
        "title": describe.short_title(info, job) if info is not None else (state.title if state else ""),
        "description": describe.short_description(info, job, full_url) if info is not None else "",
        "hook": info.hook if info is not None else "",
        "at_s": at_s,
        "at": fmt_clock(at_s) if at_s is not None else None,
        "status": state.status if state else None,
        "progress": state.progress if state else 0,
        "url": state.url if state else None,
        "error": state.error if state else None,
    }


def _unlisted_block(job: JobRecord) -> str:
    """The shorts list to paste into the full video's description when the
    full video went up before some of its published shorts."""
    if job.youtube_status != "published":
        return ""
    posted = published_shorts(job)
    if all(index in job.youtube_listed_shorts for index, _, _ in posted):
        return ""
    return describe.shorts_block(posted)
