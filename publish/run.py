"""A publishing run: the shorts first, then the full video.

Why this order. The full video's description lists its shorts with links, and
a short's link exists only once the short is uploaded. Editing the full video
afterwards would need a wider Google permission than the app asks for (it only
asks to upload). So the shorts go up first, each saying in words where its
clip plays in the full video; then the full video goes up with a list of the
shorts that made it. A short posted after the full video is already online is
not in that list (the page offers the list to paste into the description).

Each short and the full video are separate uploads with their own state on the
job, so one failing never loses the others. Whether something may go up at
all is the ledger's call (ledger/uploads.py), not the job's: it remembers
every video on the channel across jobs and restarts, and a cut-off upload is
finished through its own upload link instead of being sent again.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from core.logging import get_logger
from core.models import JobRecord, YoutubePrivacy, YoutubeUpload
from core.timeline import fmt_clock
from ledger import uploads
from ledger.uploads import get_ledger

from . import describe, media, once, youtube
from .cleanup import delete_job_files

logger = get_logger(__name__)

Update = Callable[[JobRecord], None]

INTERRUPTED = ("Upload interrupted by a server restart. Publish again to finish it: it carries on where it "
               "stopped and never uploads a video twice.")

# Serialises "may this go up?" with marking it queued, for the Publish button
# and the autopilot alike, so two starts can't queue the same upload twice.
PUBLISH_LOCK = threading.Lock()


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
    # the stretch of the recording it shows (seconds), kept in the posted list
    clip: tuple[float, float] | None = None


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
    # who posted it, for the posted list: "person" or "autopilot"
    by: str = "person"


# ---------------------------------------------------------------- the plan


def build_plan(
    job: JobRecord,
    *,
    title: str | None = None,
    description: str | None = None,
    privacy: YoutubePrivacy = "private",
    shorts: Sequence[tuple[int, str | None, str | None]] = (),
    delete_after: bool = True,
    by: str = "person",
) -> PublishPlan:
    """Check a request against the job and the ledger, and fill in what it
    left out.

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
        _not_blocked(uploads.full_item(job))
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
        _not_blocked(_short_item(job, info), f"Short {index}")
        if info.file not in on_hand:
            raise PublishRefused(410, f"Short {index} is no longer on the server.")
        short_title = describe.short_title(info, job) if short_title is None else short_title.strip()
        short_text = describe.short_description(info, job, full_url) if short_text is None else short_text
        _valid(describe.title_problem(short_title), f"title of short {index}")
        _valid(describe.description_problem(short_text), f"description of short {index}")
        plans[index] = ShortPlan(index, short_title, short_text, (info.source_start, info.source_end))

    return PublishPlan(privacy, tuple(plans[i] for i in sorted(plans)), full, delete_after, by)


def _valid(problem: str | None, what: str) -> None:
    if problem:
        raise PublishRefused(400, f"Invalid {what}: {problem}.")


def _short_item(job: JobRecord, info: describe.ShortInfo) -> uploads.Item:
    return uploads.short_item(job, info.index, info.source_start, info.source_end)


def _not_blocked(item: uploads.Item, what: str = "The full video") -> None:
    blocker = get_ledger().blocker(item)
    if blocker is not None:
        raise PublishRefused(409, blocked_text(blocker, item, what))


def blocked_text(blocker: uploads.Upload, item: uploads.Item, what: str) -> str:
    """Why the ledger won't let `item` go up, in the words the page shows."""
    if blocker.key != item.key:
        return (f"{what} can't be posted: this meeting's shorts already come from another processing run "
                f"(job {blocker.job_id}), and a meeting's shorts are only ever posted from one run.")
    if blocker.state == "published":
        return f"{what} is already on YouTube: {blocker.url}"
    if blocker.state == "unsure":
        return f"{what} needs a check on YouTube first: see the Posted panel."
    if blocker.state in ("interrupted", "uploading") and blocker.job_id != item.job_id and blocker.session_uri:
        return (f"{what} was cut off mid-upload in job {blocker.job_id}: publish that job again to finish it, "
                "or settle it in the Posted panel.")
    return f"{what} is being uploaded right now."


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


def publish_job(job: JobRecord, update: Update, plan: PublishPlan) -> str | None:
    """Background task: the shorts, then the full video, then (if asked) the
    removal of the files, but only once everything that can be published is.
    Returns why the run stopped early (quota, the connection), or None."""
    stopped = None
    try:
        stopped = _upload_all(job, update, plan)
        if plan.delete_after and stopped is None and everything_published(job):
            delete_job_files(job)
    except Exception as exc:
        logger.exception("job %s: publishing crashed", job.job_id)
        stopped = f"Publishing stopped unexpectedly: {exc}"[:300]
        _fail_waiting(job, stopped)
    finally:
        update(job)
    return stopped


def plan_everything(job: JobRecord, privacy: YoutubePrivacy, by: str) -> PublishPlan | None:
    """Every video of the job that may still go up, with the suggested words
    (what the autopilot posts), or None when nothing is left. Leaves out what
    is posted, waits for a check, belongs to another run, or has no file."""
    ledger = get_ledger()
    infos = describe.load_shorts(job)
    final = media.final_member(job)
    on_hand = media.available(job, ([final] if final else []) + [s.file for s in infos])
    full_url = job.youtube_url if job.youtube_status == "published" else None
    shorts = []
    for info in infos:
        state = job.youtube_shorts.get(info.index)
        if (state is not None and state.status == "published") or info.file not in on_hand:
            continue
        if ledger.blocker(_short_item(job, info)) is not None:
            continue
        title, text = describe.short_title(info, job), describe.short_description(info, job, full_url)
        if describe.title_problem(title) or describe.description_problem(text):
            logger.warning("job %s: short %d has no usable suggested words; left out", job.job_id, info.index)
            continue
        shorts.append(ShortPlan(info.index, title, text, (info.source_start, info.source_end)))
    full = None
    if (job.youtube_status != "published" and final is not None and final in on_hand
            and ledger.blocker(uploads.full_item(job)) is None):
        full = FullPlan(describe.full_title(job), describe.full_description(job))
    if not shorts and full is None:
        return None
    return PublishPlan(privacy, tuple(shorts), full, delete_after=True, by=by)


def posting_verdict(job: JobRecord) -> str:
    """Where the job's videos stand: "done" (the full video and every short
    that may go up from this job are on the channel), "check" (one waits for
    a person to look on YouTube), "left" (some can still go up) or "missing"
    (some can't: their files are gone)."""
    ledger = get_ledger()
    infos = {s.index: s for s in describe.load_shorts(job)}
    final = media.final_member(job)
    on_hand = media.available(job, ([final] if final else []) + [s.file for s in infos.values()])
    verdicts = [_item_verdict(ledger, uploads.full_item(job), final is not None and final in on_hand)]
    for index in sorted(describe.rendered_shorts(job)):
        info = infos.get(index)
        item = _short_item(job, info) if info is not None else uploads.short_item(job, index)
        verdicts.append(_item_verdict(ledger, item, info is not None and info.file in on_hand))
    for verdict in ("check", "left", "missing"):
        if verdict in verdicts:
            return verdict
    return "done"


def _item_verdict(ledger, item: uploads.Item, file_on_hand: bool) -> str:
    blocker = ledger.blocker(item)
    if blocker is not None:
        if blocker.key != item.key:
            return "done"  # this meeting's shorts come from another run: nothing to post from here
        if blocker.state == "published":
            return "done"
        return "check" if blocker.state == "unsure" else "left"
    return "left" if file_on_hand else "missing"


def _upload_all(job: JobRecord, update: Update, plan: PublishPlan) -> str | None:
    """Uploads everything in the plan. Returns why it stopped early (an error
    every further upload would hit too: quota, a revoked connection), or None."""
    stop: str | None = None
    for short in plan.shorts:
        if stop is not None:
            _not_uploaded(job.youtube_shorts[short.index], stop)
            continue
        stop = _upload_short(job, update, short, plan)
    if plan.full is not None:
        if stop is not None:
            job.youtube_status, job.youtube_error = "failed", f"Not uploaded: {stop}"
        else:
            stop = _upload_full(job, update, plan.full, plan)
    return stop


def _upload_short(job: JobRecord, update: Update, short: ShortPlan, plan: PublishPlan) -> str | None:
    state = job.youtube_shorts[short.index]
    state.status, state.progress, state.error = "uploading", 0, None
    update(job)

    def progress(pct: int) -> None:
        state.progress = max(0, min(99, pct))
        update(job)

    item = uploads.short_item(job, short.index, *(short.clip or (None, None)))
    try:
        posted = once.upload_once(
            item, lambda: media.open_media(job, media.short_member(short.index)),
            title=short.title, description=short.description, privacy=plan.privacy, by=plan.by,
            on_progress=progress,
        )
    except uploads.Refused as exc:
        if exc.upload.key == item.key and exc.upload.state == "published":
            posted = exc.upload  # it is on the channel already (e.g. a click and the autopilot raced)
        else:
            state.status, state.error = "failed", blocked_text(exc.upload, item, f"Short {short.index}")
            update(job)
            return None
    except Exception as exc:
        logger.exception("job %s: YouTube upload of short %d failed", job.job_id, short.index)
        state.status, state.error = "failed", _error_text(exc)
        update(job)
        return state.error if youtube.is_account_wide_error(exc) else None
    state.status, state.progress, state.error = "published", 100, None
    state.video_id, state.url = posted.video_id, posted.url
    update(job)
    return None


def _upload_full(job: JobRecord, update: Update, full: FullPlan, plan: PublishPlan) -> str | None:
    description, listed = describe.with_shorts_block(full.description, published_shorts(job))
    job.youtube_status, job.youtube_progress, job.youtube_error = "uploading", 0, None
    update(job)

    def progress(pct: int) -> None:
        job.youtube_progress = max(0, min(99, pct))
        update(job)

    item = uploads.full_item(job)
    try:
        member = media.final_member(job)
        if member is None:
            raise media.MediaMissing("The final video is no longer on the server.")
        posted = once.upload_once(
            item, lambda: media.open_media(job, member),
            title=full.title, description=description, privacy=plan.privacy, by=plan.by, on_progress=progress,
        )
    except uploads.Refused as exc:
        if exc.upload.key == item.key and exc.upload.state == "published":
            posted = exc.upload
        else:
            job.youtube_status, job.youtube_error = "failed", blocked_text(exc.upload, item, "The full video")
            update(job)
            return None
    except Exception as exc:
        logger.exception("job %s: YouTube upload of the full video failed", job.job_id)
        job.youtube_status, job.youtube_error = "failed", _error_text(exc)
        update(job)
        return job.youtube_error if youtube.is_account_wide_error(exc) else None
    job.youtube_video_id = posted.video_id
    job.youtube_url = posted.url
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


def import_published_jobs(jobs) -> int:
    """Put on the posted list what job records say went up before the list
    existed (they only had their own youtube_* fields), so a later job of
    the same meeting can't post it again. Safe to run at every start: what
    the list knows is skipped. Returns how many videos were added."""
    ledger = get_ledger()
    added = 0
    for job in jobs:
        if job is None:
            continue
        if job.youtube_status == "published" and job.youtube_url:
            added += ledger.record_existing(uploads.full_item(job), title=describe.full_title(job),
                                            url=job.youtube_url)
        for index, state in job.youtube_shorts.items():
            if state.status == "published" and state.url:
                added += ledger.record_existing(uploads.short_item(job, index), title=state.title, url=state.url)
    return added


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
    full = {
        "available": final is not None and final in on_hand,
        "title": describe.full_title(job),
        "description": describe.full_description(job),
        "status": job.youtube_status,
        "progress": job.youtube_progress,
        "url": job.youtube_url,
        "error": job.youtube_error,
        "blocked": None,
    }
    if job.youtube_status != "published":
        _apply_ledger(full, uploads.full_item(job), "The full video")
    return {
        "youtube": youtube.youtube_status(),
        "files_deleted": job.files_deleted,
        "busy": job.youtube_busy,
        "full": full,
        "shorts": rows,
        "shorts_block": _unlisted_block(job),
    }


def _apply_ledger(row: dict, item: uploads.Item, what: str) -> None:
    """What the ledger knows that the job doesn't: the video went up from
    another job of the same meeting, waits for a check, or (for a short)
    the meeting's shorts come from another run. `blocked` keeps the row from
    being picked, whatever the job's own state says later."""
    blocker = get_ledger().blocker(item)
    if blocker is None:
        return
    if blocker.key == item.key and blocker.state == "published":
        row["status"], row["url"] = "published", blocker.url
    row["blocked"] = blocked_text(blocker, item, what)


def _short_row(
    job: JobRecord, index: int, info: describe.ShortInfo | None, available: bool, full_url: str | None
) -> dict:
    """One short for the panel. `info` is None once the manifest is gone: only
    what the job remembers about the upload is left."""
    state = job.youtube_shorts.get(index)
    at_s = info.at_s if info is not None else None
    row = {
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
        "blocked": None,
    }
    if info is not None and (state is None or state.status != "published"):
        _apply_ledger(row, _short_item(job, info), f"Short {index}")
    return row


def _unlisted_block(job: JobRecord) -> str:
    """The shorts list to paste into the full video's description when the
    full video went up before some of its published shorts."""
    if job.youtube_status != "published":
        return ""
    posted = published_shorts(job)
    if all(index in job.youtube_listed_shorts for index, _, _ in posted):
        return ""
    return describe.shorts_block(posted)
