"""A publishing run: the shorts first, then the full video (or, for public
videos on the release plan, the full video first: see the end of this note).

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

The release plan (publish/schedule.py). A video that would go up public is,
when the owner's "scheduled" setting is on, uploaded private with a time
YouTube makes it public, so a meeting's full video and its Shorts don't all
appear at once and each can be reviewed first. Then the order is the other
way round: the full video first (it gets the earliest slot), then the Shorts
(each at least `shorts_after_full_hours` after it), because a Short's
description links to the full video. The full video's description leaves the
Shorts list out, since the Shorts aren't public yet. A Short with no free
slot is "skipped": the autopilot doesn't retry it, and its files are kept so a
person can still publish it by hand. Private and unlisted uploads, and
"public right away", work as described above.

Topic cuts (topic/topic_NN.mp4). A cut is a topic of the meeting made into its
own video. On the release plan it is the meeting's "front door": it gets the
earliest slot and the day's subscriber notification, and the full video follows
in a later slot, quietly. The full video is nevertheless uploaded FIRST, so the
cut's description can link to it ("Full meeting: ...?t=<where the topic starts>")
and the cut never goes public pointing at nothing: when the full video's upload
fails, the cut's booking is given back and the next round tries both again. The
Shorts come after both and link to the cut when their clip lies inside it. A
cut with no free slot is "skipped" (kept for a person); the full video then
books normally and notifies. Every upload of a cut goes through the same
never-post checks and the release plan as any other video.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from autopilot import never_post
from core import naming
from core.logging import get_logger
from core.models import JobRecord, YoutubePrivacy, YoutubeUpload
from core.timeline import fmt_clock
from ledger import uploads
from ledger.uploads import get_ledger

from . import describe, media, once, schedule, youtube
from . import settings as pub_settings
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
    # False when a person typed the description: it is never rewritten
    suggested: bool = True


@dataclass(frozen=True)
class TopicPlan:
    index: int
    title: str
    description: str
    # the stretch of the recording it shows (seconds), kept in the posted list
    span: tuple[float, float] | None = None
    # False when a person typed the description: it is never rewritten
    suggested: bool = True


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
    # Public at once instead of on the release plan (only matters for public).
    right_away: bool = False
    topics: tuple[TopicPlan, ...] = ()


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
    right_away: bool = False,
    topics: Sequence[tuple[int, str | None, str | None]] = (),
) -> PublishPlan:
    """Check a request against the job and the ledger, and fill in what it
    left out.

    `title` None = no full video. `shorts` and `topics` (topic cuts) are
    (number, title, description); a title or description left None is the
    suggested one (publish_options).
    Raises PublishRefused, whose status code is the HTTP answer."""
    why = never_post.match_job(job)
    if why is not None:
        raise PublishRefused(403, f"This meeting can't be posted: {why}. Take it off the never-post list first.")
    from . import series as team_series

    if team_series.tier_for_job(job) == "never":
        raise PublishRefused(403, "This team is set to never in the Teams list.")
    if job.files_deleted:
        raise PublishRefused(409, "The files were already removed from the server.")
    if job.youtube_busy:
        raise PublishRefused(409, "An upload for this job is already running.")
    if not youtube.youtube_status()["connected"]:
        raise PublishRefused(409, "No YouTube channel connected yet.")
    if title is None and not shorts and not topics:
        raise PublishRefused(400, "Nothing to publish: choose the full video, some shorts or topic cuts, or all.")

    infos = {s.index: s for s in describe.load_shorts(job)}
    cuts = {t.index: t for t in describe.load_topics(job)} if topics else {}
    final = media.final_member(job)
    wanted = ([final] if final else []) + [infos[i].file for i, _, _ in shorts if i in infos]
    wanted += [cuts[i].file for i, _, _ in topics if i in cuts]
    on_hand = media.available(job, wanted)

    full = None
    if title is not None:
        if job.youtube_status == "published":
            raise PublishRefused(409, f"The full video is already on YouTube: {job.youtube_url}")
        full_title = title.strip()
        _valid(describe.title_problem(full_title), "title")
        full_text = describe.full_description(job) if description is None else description
        _valid(describe.description_problem(full_text), "description")
        _not_listed(full_title, full_text, "the full video")
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
        # the page sends the words it shows: text equal to the suggested one (as it
        # reads now or before the full video had a link) is still the suggested text
        suggested = short_text is None or short_text in (describe.short_description(info, job, full_url),
                                                         describe.short_description(info, job, None))
        short_title = describe.short_title(info, job) if short_title is None else short_title.strip()
        short_text = describe.short_description(info, job, full_url) if short_text is None else short_text
        _valid(describe.title_problem(short_title), f"title of short {index}")
        _valid(describe.description_problem(short_text), f"description of short {index}")
        _not_listed(short_title, short_text, f"short {index}")
        plans[index] = ShortPlan(index, short_title, short_text, (info.source_start, info.source_end), suggested)

    cut_plans: dict[int, TopicPlan] = {}
    for index, cut_title, cut_text in topics:
        cut = cuts.get(index)
        if cut is None:
            raise PublishRefused(404, f"This job has no topic cut {index}.")
        state = job.youtube_topics.get(index)
        if state is not None and state.status == "published":
            raise PublishRefused(409, f"Topic cut {index} is already on YouTube: {state.url}")
        _not_blocked(_topic_item(job, cut), f"Topic cut {index}")
        if cut.file not in on_hand:
            raise PublishRefused(410, f"Topic cut {index} is no longer on the server.")
        suggested = cut_text is None or cut_text in (describe.topic_description(cut, job, full_url),
                                                     describe.topic_description(cut, job, None))
        cut_title = describe.topic_title(cut, job) if cut_title is None else cut_title.strip()
        cut_text = describe.topic_description(cut, job, full_url) if cut_text is None else cut_text
        _valid(describe.title_problem(cut_title), f"title of topic cut {index}")
        _valid(describe.description_problem(cut_text), f"description of topic cut {index}")
        _not_listed(cut_title, cut_text, f"topic cut {index}")
        cut_plans[index] = TopicPlan(index, cut_title, cut_text, (cut.source_start, cut.source_end), suggested)

    return PublishPlan(privacy, tuple(plans[i] for i in sorted(plans)), full, delete_after, by, right_away,
                       tuple(cut_plans[i] for i in sorted(cut_plans)))


def listed_words(title: str, description: str) -> str | None:
    """A never-post word in the words that would go up, or None. What the app
    puts in every description (the team line, the owner's links) is left out."""
    text = description.replace(describe.ORG_LINE, " ")
    for line in pub_settings.current().links_text.splitlines():
        if line.strip():
            text = text.replace(line.strip(), " ")
    return never_post.match_words(f"{title} . {text}")


def suggested_words_listed(job: JobRecord) -> str | None:
    """Why the words the autopilot would post for this job (the full video's and
    every Short's suggested title and description) hold a never-post word, or
    None. The meeting passed the list, but a title the AI wrote can still say
    "investor" or "private"; the channel check would then report the app's
    own upload as a leak."""
    texts = [("full video", describe.full_title(job), describe.full_description(job))]
    for info in describe.load_shorts(job):
        texts.append((f"short {info.index}", describe.short_title(info, job),
                      describe.short_description(info, job, None)))
    if _topics_apply(job):
        for cut in describe.load_topics(job):
            texts.append((f"topic cut {cut.index}", describe.topic_title(cut, job),
                          describe.topic_description(cut, job, None)))
    for what, title, text in texts:
        word = listed_words(title, text)
        if word is not None:
            return f'the {what} would say "{word}", which is on the never-post list'
    return None


def _topics_apply(job: JobRecord) -> bool:
    """Whether the job's topic cuts are part of what gets posted: the owner has
    them on and the team is a public one (an archive team gets no cuts; the
    never tier never reaches here). Cuts rendered before a setting changed are
    left alone rather than held forever."""
    if not pub_settings.current().topic_cuts:
        return False
    from . import series as team_series

    return team_series.tier_for_job(job) == "public"


def _not_listed(title: str, description: str, what: str) -> None:
    word = listed_words(title, description)
    if word is not None:
        raise PublishRefused(
            400, f'The title or description of {what} has "{word}", which is on the never-post list. '
                 "Reword it, or take the word off the list first.")


def _valid(problem: str | None, what: str) -> None:
    if problem:
        raise PublishRefused(400, f"Invalid {what}: {problem}.")


def _short_item(job: JobRecord, info: describe.ShortInfo) -> uploads.Item:
    return uploads.short_item(job, info.index, info.source_start, info.source_end,
                              style=info.style or None, layout=info.layout or None)


def _topic_item(job: JobRecord, info: describe.TopicInfo) -> uploads.Item:
    return uploads.topic_item(job, info.index, info.source_start, info.source_end)


def _topic_member(index: int) -> str:
    return f"topic/topic_{index:02d}.mp4"


def _not_blocked(item: uploads.Item, what: str = "The full video") -> None:
    blocker = get_ledger().blocker(item)
    if blocker is not None:
        raise PublishRefused(409, blocked_text(blocker, item, what))


def blocked_text(blocker: uploads.Upload, item: uploads.Item, what: str) -> str:
    """Why the ledger won't let `item` go up, in the words the page shows."""
    if blocker.key != item.key:
        kind = "topic cuts" if item.kind == "topic" else "shorts"
        return (f"{what} can't be posted: this meeting's {kind} already come from another processing run "
                f"(job {blocker.job_id}), and a meeting's {kind} are only ever posted from one run.")
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
    if plan.topics:
        job.youtube_topics = {
            **job.youtube_topics,
            **{cut.index: YoutubeUpload(status="queued", title=cut.title) for cut in plan.topics},
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


def plan_everything(
    job: JobRecord, privacy: YoutubePrivacy, by: str, kinds: set[str] | None = None,
) -> PublishPlan | None:
    """Every video of the job that may still go up, with the suggested words
    (what the autopilot posts), or None when nothing is left. Leaves out what
    is posted, waits for a check, belongs to another run, or has no file.
    `kinds` limits it ({'full'}: no Shorts; None: everything).
    A meeting on the never-post list gets no plan at all."""
    if never_post.match_job(job) is not None:
        return None
    ledger = get_ledger()
    infos = describe.load_shorts(job)
    final = media.final_member(job)
    on_hand = media.available(job, ([final] if final else []) + [s.file for s in infos])
    full_url = job.youtube_url if job.youtube_status == "published" else None
    shorts = []
    for info in infos if kinds is None or "shorts" in kinds else ():
        state = job.youtube_shorts.get(info.index)
        # a "skipped" short (no release slot) is left for a person: not retried
        if (state is not None and state.status in ("published", "skipped")) or info.file not in on_hand:
            continue
        if ledger.blocker(_short_item(job, info)) is not None:
            continue
        title, text = describe.short_title(info, job), describe.short_description(info, job, full_url)
        if describe.title_problem(title) or describe.description_problem(text):
            logger.warning("job %s: short %d has no usable suggested words; left out", job.job_id, info.index)
            continue
        shorts.append(ShortPlan(info.index, title, text, (info.source_start, info.source_end)))
    cuts = _suggested_topics(job, ledger, full_url) if kinds is None or "topics" in kinds else []
    full = None
    if ((kinds is None or "full" in kinds)
            and job.youtube_status != "published" and final is not None and final in on_hand
            and ledger.blocker(uploads.full_item(job)) is None):
        full = FullPlan(describe.full_title(job), describe.full_description(job))
    if not shorts and full is None and not cuts:
        return None
    return PublishPlan(privacy, tuple(shorts), full, delete_after=True, by=by, topics=tuple(cuts))


def _suggested_topics(job: JobRecord, ledger, full_url: str | None) -> list[TopicPlan]:
    """The job's topic cuts that may still go up, with the suggested words. Only for
    a public team with topic cuts on (never an archive team's)."""
    if not _topics_apply(job):
        return []
    infos = describe.load_topics(job)
    on_hand = media.available(job, [t.file for t in infos])
    cuts = []
    for info in infos:
        state = job.youtube_topics.get(info.index)
        # a "skipped" cut (no release slot) is left for a person: not retried
        if (state is not None and state.status in ("published", "skipped")) or info.file not in on_hand:
            continue
        if ledger.blocker(_topic_item(job, info)) is not None:
            continue
        title, text = describe.topic_title(info, job), describe.topic_description(info, job, full_url)
        if describe.title_problem(title) or describe.description_problem(text):
            logger.warning("job %s: topic cut %d has no usable suggested words; left out", job.job_id, info.index)
            continue
        cuts.append(TopicPlan(info.index, title, text, (info.source_start, info.source_end)))
    return cuts


def posting_verdict(job: JobRecord) -> str:
    """Where the job's videos stand: "done" (the full video and every short
    that may go up from this job are on the channel), "check" (one waits for
    a person to look on YouTube), "left" (some can still go up) or "missing"
    (some can't: their files are gone). A topic cut counts like a short: posted
    or skipped is done."""
    ledger = get_ledger()
    infos = {s.index: s for s in describe.load_shorts(job)}
    final = media.final_member(job)
    on_hand = media.available(job, ([final] if final else []) + [s.file for s in infos.values()])
    verdicts = [_item_verdict(ledger, uploads.full_item(job), final is not None and final in on_hand)]
    for index in sorted(describe.rendered_shorts(job)):
        state = job.youtube_shorts.get(index)
        if state is not None and state.status == "skipped":
            verdicts.append("done")  # no release slot: left for a person, never retried
            continue
        info = infos.get(index)
        item = _short_item(job, info) if info is not None else uploads.short_item(job, index)
        verdicts.append(_item_verdict(ledger, item, info is not None and info.file in on_hand))
    if _topics_apply(job):
        cuts = {t.index: t for t in describe.load_topics(job)}
        cut_files = media.available(job, [t.file for t in cuts.values()])
        for index in sorted(describe.rendered_topics(job)):
            state = job.youtube_topics.get(index)
            if state is not None and state.status == "skipped":
                verdicts.append("done")  # no release slot: left for a person, never retried
                continue
            cut = cuts.get(index)
            item = _topic_item(job, cut) if cut is not None else uploads.topic_item(job, index)
            verdicts.append(_item_verdict(ledger, item, cut is not None and cut.file in cut_files))
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
    cfg = pub_settings.current()
    if plan.privacy == "public" and cfg.scheduled and not plan.right_away:
        return _upload_scheduled(job, update, plan, cfg)
    stop: str | None = None
    for short in plan.shorts:
        # the list can change while uploads run, so look again before each one
        stop = stop or _listed_now(job)
        if stop is not None:
            _not_uploaded(job.youtube_shorts[short.index], stop)
            continue
        stop = _upload_short(job, update, short, plan, cfg)
    if plan.full is not None:
        stop = stop or _listed_now(job)
        if stop is not None:
            job.youtube_status, job.youtube_error = "failed", f"Not uploaded: {stop}"
        else:
            stop = _upload_full(job, update, plan.full, plan, cfg)
    for cut in plan.topics:  # the full video is up (or failed): a cut never goes up pointing at nothing
        state = job.youtube_topics[cut.index]
        stop = stop or _listed_now(job)
        if stop is not None:
            _not_uploaded(state, stop)
        elif plan.full is not None and job.youtube_status != "published":
            _waits_for_full(state)
        else:
            stop = _upload_topic(job, update, cut, plan, cfg)
    return stop


def _waits_for_full(state: YoutubeUpload) -> None:
    state.status, state.progress = "failed", 0
    state.error = "Not uploaded: the full video isn't on the channel yet, and its topic cut waits for it. Publish again."


def _upload_scheduled(job: JobRecord, update: Update, plan: PublishPlan, cfg) -> str | None:
    """The release plan's order. Without topic cuts: the full video, then the Shorts,
    each in its own slot (publish/schedule.py). With a topic cut and the owner's
    "front door" setting: the cut is booked first (it takes the day's notification),
    the full video second (quietly, after it), but the full video is uploaded first so
    the cut can link to it; then the cut, then the other cuts and the Shorts. Every item
    is booked right before its own upload, and the never-post list is looked at again
    before each one."""
    stop: str | None = None
    full_item = uploads.full_item(job)
    later = list(plan.topics)
    front = later.pop(0) if later and plan.full is not None and cfg.topic_front_door else None
    front_item = front_slot = None
    if front is not None:
        stop = _listed_now(job)
        if stop is None:
            front_item = _topic_item_of(job, front)
            front_slot = _slot_for(front_item, None, cfg)
            if front_slot is None:  # kept for a person; the full video books normally and notifies
                _skip_topic(job.youtube_topics[front.index], cfg)
                logger.warning("job %s: no release slot for topic cut %d; skipped", job.job_id, front.index)
                update(job)
                front = front_item = None
    if plan.full is not None:
        stop = _listed_now(job)
        if stop is not None:
            job.youtube_status, job.youtube_error = "failed", f"Not uploaded: {stop}"
        else:
            after_cut = (front_slot.at + timedelta(hours=cfg.min_gap_hours)) if front_slot is not None else None
            slot = _slot_for(full_item, after_cut, cfg, quiet=front_slot is not None)
            if slot is None:
                job.youtube_status, job.youtube_error = "failed", _no_slot_text(cfg, full=True)
                logger.warning("job %s: no release slot for the full video", job.job_id)
                update(job)
            else:
                stop = _upload_full(job, update, plan.full, plan, cfg, slot)
    full_waiting = plan.full is not None and job.youtube_status != "published"
    if front is not None:
        state = job.youtube_topics[front.index]
        stop = stop or _listed_now(job)
        if stop is not None:
            if front_item is not None:
                _release_slot(front_item)
            _not_uploaded(state, stop)
            update(job)
        elif full_waiting:  # the cut must never go public pointing at nothing: give its slot back
            _release_slot(front_item)
            _waits_for_full(state)
            update(job)
        else:
            stop = _upload_topic(job, update, front, plan, cfg, front_slot)
    # the other cuts (all of them when there is no front door) come after the full video
    for cut in later:
        state = job.youtube_topics[cut.index]
        stop = stop or _listed_now(job)
        if stop is not None:
            _not_uploaded(state, stop)
            continue
        if full_waiting:
            _waits_for_full(state)
            update(job)
            continue
        item = _topic_item_of(job, cut)
        slot = _slot_for(item, _after_published(cfg.min_gap_hours, full_item, front_item), cfg)
        if slot is None:
            _skip_topic(state, cfg)
            logger.warning("job %s: no release slot for topic cut %d; skipped", job.job_id, cut.index)
            update(job)
            continue
        stop = _upload_topic(job, update, cut, plan, cfg, slot)
    # The Shorts wait until the full video (and the front door) is public, or at least booked
    not_before = _after_full(full_item, cfg, front_item)
    shorts = _link_to_full(job, plan, full_waiting, cfg)
    for short in shorts:
        state = job.youtube_shorts[short.index]
        stop = stop or _listed_now(job)
        if stop is not None:
            _not_uploaded(state, stop)
            continue
        if full_waiting:
            state.status = "failed"
            state.error = "Not uploaded: the full video isn't posted yet, and its Shorts wait for it. Publish again."
            update(job)
            continue
        item = _short_item_of(job, short)
        slot = _slot_for(item, not_before, cfg)
        if slot is None:
            state.status, state.progress = "skipped", 0
            state.error = _no_slot_text(cfg, full=False)
            logger.warning("job %s: no release slot for short %d; skipped", job.job_id, short.index)
            update(job)
            continue
        stop = _upload_short(job, update, short, plan, cfg, slot)
    return stop


def _skip_topic(state: YoutubeUpload, cfg) -> None:
    """No free slot for a cut: it counts as done, so nothing retries it, and its file
    is kept so a person can still publish it by hand."""
    state.status, state.progress = "skipped", 0
    state.error = _no_slot_text(cfg, full=False)


def _after_published(gap_hours: float, *items: uploads.Item | None) -> datetime | None:
    """`gap_hours` after the latest of these items that is on the channel with a
    release time (None when none is)."""
    ledger = get_ledger()
    times = []
    for item in items:
        held = ledger.get(item.key) if item is not None else None
        if held is not None and held.state == "published" and held.publish_at:
            times.append(datetime.fromisoformat(held.publish_at))
    return max(times) + timedelta(hours=gap_hours) if times else None


def _slot_for(item: uploads.Item, not_before: datetime | None, cfg, *, quiet: bool = False) -> schedule.Booking | None:
    """The slot an item goes public in. A cut-off upload carries on through
    the link it started with, so it keeps the time that was fixed then and
    books nothing new, but only while that time still leaves the full review
    time (a video must never go public without review). When it doesn't, a
    new slot is booked here and the upload (publish/once.py) drops the old link
    for a new one, unless YouTube turns out to hold the video already. None
    when there is no free slot. `quiet`: the video never tells subscribers (the
    full video that follows its topic cut, which took the day's notification)."""
    ledger = get_ledger()
    now = datetime.now(UTC)
    if ledger.blocker(item) is not None:
        # upload_once will refuse it: book nothing, and move no one else's slot
        return schedule.get(item.key) or schedule.Booking(item.key, item.kind, item.source, now, False, now, item.meeting)
    held = ledger.get(item.key)
    if (held is not None and held.state in ("interrupted", "uploading") and held.session_uri
            and held.job_id == item.job_id and held.publish_at):
        at = datetime.fromisoformat(held.publish_at)
        earliest = now + timedelta(hours=cfg.review_hours)
        if not_before is not None:
            earliest = max(earliest, not_before)
        if at >= earliest:
            return schedule.Booking(item.key, item.kind, item.source, at, bool(held.notify) and not quiet, now,
                                    item.meeting)
    return schedule.book(item.key, item.kind, item.source, not_before=not_before, meeting=item.meeting, quiet=quiet)


def _settle_slot(item: uploads.Item, slot: schedule.Booking | None, posted: uploads.Upload) -> None:
    """After an upload that is on the channel: the schedule must say what
    YouTube holds. A video found already there (a cut-off upload that had
    finished, or one another run posted) keeps the time it has, not the slot
    just booked for this attempt, which is given back. A video with no time to
    go public holds no slot (one left from a cut-off scheduled upload is freed)."""
    if not posted.publish_at:  # public at once or private: no time to go public, so no slot is held
        schedule.release(item.key)
        return
    if slot is None:
        return
    if posted.publish_at != schedule.rfc3339(slot.at):
        at = datetime.fromisoformat(posted.publish_at)
        schedule.keep(schedule.Booking(item.key, item.kind, item.source, at, bool(posted.notify), datetime.now(UTC), item.meeting))


def _after_full(full_item: uploads.Item, cfg, front_item: uploads.Item | None = None) -> datetime | None:
    """The earliest a Short may go public: `shorts_after_full_hours` after the
    meeting's full video (and its topic cut, the front door), whichever is
    later, when that one is on the channel with a release time. None = no such
    wait (it went up right away, or isn't posted from here)."""
    # Also after the full video went public: schedule.book takes the later of
    # this and now + review_hours, so a long-past full video adds no wait.
    return _after_published(cfg.shorts_after_full_hours, full_item, front_item)


def _cut_link_for(job: JobRecord, cfg, info: describe.ShortInfo) -> tuple[str, int] | None:
    """(the topic cut's link, seconds into it) when the Short's clip lies inside a cut of
    this job that is on the channel, and the owner links Shorts to the cut."""
    if cfg.shorts_link_target != "topic":
        return None
    for cut in describe.load_topics(job):
        state = job.youtube_topics.get(cut.index)
        if state is not None and state.status == "published" and state.url:
            at = describe.short_cut_position(info, cut, job)
            if at is not None:
                return state.url, at
    return None


def _link_to_full(job: JobRecord, plan: PublishPlan, full_waiting: bool, cfg=None) -> tuple[ShortPlan, ...]:
    """The plan's Shorts, those with a suggested description rewritten to link
    to the topic cut their clip lies in, else to the full video, now that they
    are on the channel (the plan was made before they had links)."""
    full_url = job.youtube_url if job.youtube_status == "published" else None
    if full_waiting:
        return plan.shorts
    cfg = cfg or pub_settings.current()
    infos = {info.index: info for info in describe.load_shorts(job)}
    rewritten = []
    for short in plan.shorts:
        info = infos.get(short.index)
        if short.suggested and info is not None:
            cut = _cut_link_for(job, cfg, info)
            if cut is not None:
                text = describe.short_description(info, job, full_url, cut_url=cut[0], cut_at_s=cut[1])
            elif plan.full is not None and full_url:
                text = describe.short_description(info, job, full_url)
            else:
                text = None
            if text is not None and not describe.description_problem(text):
                short = replace(short, description=text)
        rewritten.append(short)
    return tuple(rewritten)


def _no_slot_text(cfg, *, full: bool) -> str:
    days = f"{cfg.max_days_ahead} day" + ("" if cfg.max_days_ahead == 1 else "s")
    if full:
        return (f"Not uploaded: no free release slot in the next {days}. "
                "Try again later or publish it right away.")
    return f"Not posted: no free release slot in the next {days}. Publish it by hand if you want it."


def _release_slot(item: uploads.Item) -> None:
    """An upload failed: give its slot back, unless YouTube already holds a
    video (or a cut-off upload) for it. Those keep the slot for the retry."""
    held = get_ledger().get(item.key)
    if held is None or held.state == "failed":
        schedule.release(item.key)


def _upload_fields(job: JobRecord, cfg, slot: schedule.Booking | None, *, item: uploads.Item | None = None,
                   title: str = "", description: str = "") -> dict:
    """What every upload carries besides its words: the owner's category and
    language, the meeting's day, and (release plan) when it goes public."""
    fields = {"category_id": cfg.category_id, "language": cfg.language,
              "recording_date": naming.meeting_date(job)}
    if slot is not None:
        fields.update(publish_at=slot.at, notify=slot.notify)
    if item is not None and getattr(cfg, "translations", False) and cfg.language:
        # asked for only when a new upload is about to start, after the never-post check
        # (publish/once.py); the words checked there are the English ones
        fields["localizations"] = lambda: _translations(item, cfg, title, description)
    return fields


def _translations(item: uploads.Item, cfg, title: str, description: str) -> dict | None:
    """The title and description in the owner's languages, or None: on any error or
    after 90 seconds the video goes up in English only."""
    try:
        from . import translate

        langs = [c.strip() for c in str(cfg.translation_languages).split(",") if c.strip()]
        found = translate.localizations(title, description, langs, cache_key=item.key, timeout_s=90,
                                        job_id=item.job_id, meeting=item.meeting)
        return found or None
    except Exception:
        logger.exception("translations skipped for %s", item.key)
        return None


def _start_extras_worker() -> None:
    threading.Thread(target=_run_extras_due, name="extras", daemon=True).start()


def _run_extras_due() -> None:
    from . import extras

    extras.run_due()


def _queue_extras(job: JobRecord, item: uploads.Item, posted: uploads.Upload, video_kind: str,
                  index: int | None, cfg) -> None:
    """After an upload is on the channel: queue its follow-ups (captions, playlists,
    thumbnail ...) and start the worker. Nothing in the upload depends on this and
    nothing here may raise into it."""
    try:
        from . import extras

        extras.enqueue_for_upload(job, item, posted, video_kind=video_kind, index=index, cfg=cfg)
        _start_extras_worker()
    except Exception:
        logger.exception("job %s: follow-ups could not be queued for %s", job.job_id, item.key)


def _listed_now(job: JobRecord) -> str | None:
    """Why the job's meeting is on the never-post list right now, or None."""
    why = never_post.match_job(job)
    return f"{why}." if why else None


def _short_item_of(job: JobRecord, short: ShortPlan) -> uploads.Item:
    style = layout = None
    try:
        info = next((i for i in describe.load_shorts(job) if i.index == short.index), None)
        if info is not None:
            style, layout = info.style or None, info.layout or None
    except Exception:  # noqa: BLE001 - the look is only bookkeeping
        logger.warning("job %s: short %s: its look could not be read", job.job_id, short.index)
    return uploads.short_item(job, short.index, *(short.clip or (None, None)), style=style, layout=layout)


def _upload_short(job: JobRecord, update: Update, short: ShortPlan, plan: PublishPlan, cfg,
                  slot: schedule.Booking | None = None) -> str | None:
    state = job.youtube_shorts[short.index]
    state.status, state.progress, state.error = "uploading", 0, None
    state.publish_at = schedule.rfc3339(slot.at) if slot is not None else None
    update(job)

    def progress(pct: int) -> None:
        state.progress = max(0, min(99, pct))
        update(job)

    item = _short_item_of(job, short)
    try:
        posted = once.upload_once(
            item, lambda: media.open_media(job, media.short_member(short.index)),
            title=short.title, description=short.description, privacy=plan.privacy, by=plan.by,
            on_progress=progress,
            **_upload_fields(job, cfg, slot, item=item, title=short.title, description=short.description),
        )
    except uploads.Refused as exc:
        if exc.upload.key == item.key and exc.upload.state == "published":
            posted = exc.upload  # it is on the channel already (e.g. a click and the autopilot raced)
        else:
            if slot is not None:
                _release_slot(item)
            state.status, state.error, state.publish_at = "failed", blocked_text(exc.upload, item, f"Short {short.index}"), None
            update(job)
            return None
    except Exception as exc:
        logger.exception("job %s: YouTube upload of short %d failed", job.job_id, short.index)
        if slot is not None:
            _release_slot(item)
        state.status, state.error = "failed", _error_text(exc)
        state.publish_at = None
        update(job)
        return state.error if youtube.is_account_wide_error(exc) else None
    _settle_slot(item, slot, posted)
    state.status, state.progress, state.error = "published", 100, posted.warning
    state.video_id, state.url = posted.video_id, posted.url
    state.publish_at = posted.publish_at
    update(job)
    _record_views(item, posted, job)
    _queue_extras(job, item, posted, "short", short.index, cfg)
    return None


def _topic_item_of(job: JobRecord, cut: TopicPlan) -> uploads.Item:
    return uploads.topic_item(job, cut.index, *(cut.span or (None, None)))


def _cut_description(job: JobRecord, cut: TopicPlan) -> str:
    """What the cut goes up with: a suggested description gains the link to the full
    video (at the moment the topic starts) once that video is on the channel; one a
    person typed is never rewritten."""
    if cut.suggested and job.youtube_status == "published" and job.youtube_url:
        info = next((i for i in describe.load_topics(job) if i.index == cut.index), None)
        if info is not None:
            text = describe.topic_description(info, job, job.youtube_url)
            if not describe.description_problem(text):
                return text
    return cut.description


def _upload_topic(job: JobRecord, update: Update, cut: TopicPlan, plan: PublishPlan, cfg,
                  slot: schedule.Booking | None = None) -> str | None:
    state = job.youtube_topics[cut.index]
    state.status, state.progress, state.error = "uploading", 0, None
    state.publish_at = schedule.rfc3339(slot.at) if slot is not None else None
    update(job)

    def progress(pct: int) -> None:
        state.progress = max(0, min(99, pct))
        update(job)

    item = _topic_item_of(job, cut)
    description = _cut_description(job, cut)
    try:
        posted = once.upload_once(
            item, lambda: media.open_media(job, _topic_member(cut.index)),
            title=cut.title, description=description, privacy=plan.privacy, by=plan.by,
            on_progress=progress,
            **_upload_fields(job, cfg, slot, item=item, title=cut.title, description=description),
        )
    except uploads.Refused as exc:
        if exc.upload.key == item.key and exc.upload.state == "published":
            posted = exc.upload  # it is on the channel already (e.g. a click and the autopilot raced)
        else:
            if slot is not None:
                _release_slot(item)
            state.status, state.error, state.publish_at = (
                "failed", blocked_text(exc.upload, item, f"Topic cut {cut.index}"), None)
            update(job)
            return None
    except Exception as exc:
        logger.exception("job %s: YouTube upload of topic cut %d failed", job.job_id, cut.index)
        if slot is not None:
            _release_slot(item)
        state.status, state.error = "failed", _error_text(exc)
        state.publish_at = None
        update(job)
        return state.error if youtube.is_account_wide_error(exc) else None
    _settle_slot(item, slot, posted)
    state.status, state.progress, state.error = "published", 100, posted.warning
    state.video_id, state.url = posted.video_id, posted.url
    state.publish_at = posted.publish_at
    update(job)
    _record_views(item, posted, job)
    _queue_extras(job, item, posted, "topic", cut.index, cfg)
    # TODO(description refresher, left out): the full video went up before this cut, so its
    # description can't list the cut; adding it later needs the force-ssl permission.
    return None


def _upload_full(job: JobRecord, update: Update, full: FullPlan, plan: PublishPlan, cfg,
                 slot: schedule.Booking | None = None) -> str | None:
    if slot is not None:
        # the release plan's Shorts come after it and aren't public yet: no list
        description, listed = full.description, []
    else:
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
            **_upload_fields(job, cfg, slot, item=item, title=full.title, description=description),
        )
    except uploads.Refused as exc:
        if exc.upload.key == item.key and exc.upload.state == "published":
            posted = exc.upload
        else:
            if slot is not None:
                _release_slot(item)
            job.youtube_status, job.youtube_error = "failed", blocked_text(exc.upload, item, "The full video")
            update(job)
            return None
    except Exception as exc:
        logger.exception("job %s: YouTube upload of the full video failed", job.job_id)
        if slot is not None:
            _release_slot(item)
        job.youtube_status, job.youtube_error = "failed", _error_text(exc)
        update(job)
        return job.youtube_error if youtube.is_account_wide_error(exc) else None
    _settle_slot(item, slot, posted)
    job.youtube_video_id = posted.video_id
    job.youtube_url = posted.url
    job.youtube_status = "published"
    job.youtube_progress = 100
    job.youtube_error = posted.warning
    job.youtube_listed_shorts = listed
    update(job)
    _record_views(item, posted, job)
    _queue_extras(job, item, posted, "full", None, cfg)
    return None


def _error_text(exc: Exception) -> str:
    return str(exc) if isinstance(exc, (media.MediaMissing, once.NeverPost)) else youtube.friendly_error(exc)


def _not_uploaded(state: YoutubeUpload, reason: str) -> None:
    state.status, state.error = "failed", f"Not uploaded: {reason}"


def published_shorts(job: JobRecord) -> list[tuple[int, str, str]]:
    """(number, title, link) of the shorts that are on the channel."""
    return [(i, s.title, s.url) for i, s in sorted(job.youtube_shorts.items()) if s.status == "published" and s.url]


def everything_published(job: JobRecord) -> bool:
    """The full video, every short and every topic cut the job rendered are on the
    channel. A short or cut the release plan skipped is not: its files stay so a person can still
    publish it by hand (the autopilot is finished with it all the same, see
    posting_verdict)."""
    if job.youtube_status != "published":
        return False
    for index in describe.rendered_shorts(job):
        state = job.youtube_shorts.get(index)
        if state is None or state.status != "published":
            return False
    if _topics_apply(job):
        for index in describe.rendered_topics(job):
            state = job.youtube_topics.get(index)
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
        for index, state in job.youtube_topics.items():
            if state.status == "published" and state.url:
                added += ledger.record_existing(uploads.topic_item(job, index), title=state.title, url=state.url)
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
    for state in (*job.youtube_shorts.values(), *job.youtube_topics.values()):
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
    topic_infos = describe.load_topics(job)
    topic_files = media.available(job, [t.file for t in topic_infos])
    cut_rows = [_topic_row(job, info, info.file in topic_files, full_url, n == 0)
                for n, info in enumerate(topic_infos)]
    known_cuts = {info.index for info in topic_infos}
    cut_rows += [_topic_row(job, None, False, full_url, False, index) for index in job.youtube_topics
                 if index not in known_cuts]
    cut_rows.sort(key=lambda row: row["index"])
    full = {
        "available": final is not None and final in on_hand,
        "title": describe.full_title(job),
        "description": describe.full_description(job),
        "status": job.youtube_status,
        "progress": job.youtube_progress,
        "url": job.youtube_url,
        "error": job.youtube_error,
        "blocked": None,
        "publish_at": _ledger_publish_at(uploads.full_item(job)) if job.youtube_status == "published" else None,
    }
    if job.youtube_status != "published":
        _apply_ledger(full, uploads.full_item(job), "The full video")
    cfg = pub_settings.current()
    return {
        "release": {"scheduled": cfg.scheduled, "review_hours": cfg.review_hours,
                    "timezone": schedule.team_zone_name()},
        "youtube": youtube.youtube_status(),
        "files_deleted": job.files_deleted,
        "busy": job.youtube_busy,
        "full": full,
        "shorts": rows,
        "topics": cut_rows,
        "topic_cuts": {"on": cfg.topic_cuts, "front_door": cfg.topic_front_door and cfg.scheduled},
        "shorts_block": _unlisted_block(job),
        "never_post": never_post.match_job(job),
        "series": _series_info(job),
    }


def _series_info(job: JobRecord) -> dict:
    """The job's team for the publish panel: {key, label, tier}, so the page
    can warn for a new (unknown) or archive team."""
    from . import series as team_series

    info = job.zoom_meeting if isinstance(job.zoom_meeting, dict) else {}
    topic = next((t for t in (info.get("topic"), job.title) if team_series.series_key(t)), None)
    key = team_series.series_key(topic)
    row = team_series.lookup_key(key) if key else None
    label = (row.label if row else "") or (team_series._label_of(topic) if topic else "") or key
    return {"key": row.key if row else key, "label": label, "tier": team_series.tier_for_job(job)}


def _record_views(item: uploads.Item, upload: uploads.Upload, job: JobRecord | None = None) -> None:
    """Keep what the views database needs about a video the ledger shows as posted.
    Never raises."""
    try:
        from analytics import record

        record.record_upload(item, upload, job)
    except Exception:
        logger.exception("views: could not record %s", item.key)


def _apply_ledger(row: dict, item: uploads.Item, what: str) -> None:
    """What the ledger knows that the job doesn't: the video went up from
    another job of the same meeting, waits for a check, or (for a short)
    the meeting's shorts come from another run. `blocked` keeps the row from
    being picked, whatever the job's own state says later."""
    blocker = get_ledger().blocker(item)
    if blocker is None:
        return
    if blocker.key == item.key and blocker.state == "published":
        # the job's own failure text is stale: the video is on the channel
        row["status"], row["url"], row["error"] = "published", blocker.url, None
        row["publish_at"] = blocker.publish_at
        _record_views(item, blocker)
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
        "publish_at": state.publish_at if state else None,
    }
    if info is not None and (state is None or state.status != "published"):
        _apply_ledger(row, _short_item(job, info), f"Short {index}")
    return row


def _topic_row(job: JobRecord, info: describe.TopicInfo | None, available: bool, full_url: str | None,
               first: bool, index: int | None = None) -> dict:
    """One topic cut for the panel. `info` is None once the manifest is gone: only
    what the job remembers about the upload is left. `ok` says whether the suggested
    words may go up (the page ticks the cut then)."""
    index = info.index if info is not None else (index or 0)
    state = job.youtube_topics.get(index)
    cfg = pub_settings.current()
    title = describe.topic_title(info, job) if info is not None else (state.title if state else "")
    description = describe.topic_description(info, job, full_url) if info is not None else ""
    at_s = info.at_s if info is not None else None
    row = {
        "index": index,
        "available": available,
        "title": title,
        "description": description,
        "hook": info.hook if info is not None else "",
        "at_s": at_s,
        "at": fmt_clock(at_s) if at_s is not None else None,
        "duration_s": round(info.duration_s) if info is not None else None,
        "score": info.best_score if info is not None else None,
        "ok": info is not None and not describe.title_problem(title) and not describe.description_problem(description)
              and listed_words(title, description) is None,
        "front_door": bool(first and cfg.topic_front_door and cfg.scheduled),
        "status": state.status if state else None,
        "progress": state.progress if state else 0,
        "url": state.url if state else None,
        "error": state.error if state else None,
        "blocked": None,
        "publish_at": state.publish_at if state else None,
    }
    if info is not None and (state is None or state.status != "published"):
        _apply_ledger(row, _topic_item(job, info), f"Topic cut {index}")
    return row


def _ledger_publish_at(item: uploads.Item) -> str | None:
    """When the posted video goes public (ISO, UTC), if it was scheduled."""
    held = get_ledger().get(item.key)
    return held.publish_at if held is not None and held.state == "published" else None


def _unlisted_block(job: JobRecord) -> str:
    """The shorts list to paste into the full video's description when the
    full video went up before some of its published shorts."""
    if job.youtube_status != "published":
        return ""
    posted = published_shorts(job)
    if all(index in job.youtube_listed_shorts for index, _, _ in posted):
        return ""
    return describe.shorts_block(posted)
