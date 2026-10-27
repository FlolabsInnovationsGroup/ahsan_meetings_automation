"""One step of the autopilot's work. loop.py runs it every minute, and right
away when Zoom's webhook calls.

A step, in order:
1. follow the jobs it started: a finished one moves to posting, a failed one
   is tried again later (3 times by default) or waits for a person;
2. post what finished: the shorts, then the full video, all through the
   ledger (ledger/uploads.py), so nothing goes up twice;
3. while no job runs (never during one, the owner's rule): look for new
   recordings when a check is due (every AUTOPILOT_CHECK_MINUTES, after a
   webhook call, after a scheduled meeting ended, or when a meeting's wait
   for Zoom's transcript is over), and refresh Zoom's meeting schedule;
4. start the oldest meeting that is ready, unless a job is running.

Everything it decides is saved (autopilot.json) before it acts, so a restart
carries on where it stopped. Zoom and the job queue are only ever asked
outside the store's lock.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from core.config import get_settings
from core.errors import PipelineError
from core.logging import get_logger
from core.models import JobRecord, JobStatus
from ingest import zoom
from ledger import files
from ledger.uploads import get_ledger
from publish import run as publish_run

from . import emails
from .store import Meeting, ScheduledMeeting, Store
from .store import store as default_store

logger = get_logger(__name__)

# waits before processing a failed meeting again (the owner's rule: about 30 min, 2 h, 6 h)
RETRY_WAITS = (timedelta(minutes=30), timedelta(hours=2), timedelta(hours=6))
# Zoom down or busy while starting a job: not the meeting's fault, no attempt used
SUBMIT_TRY_AGAIN = timedelta(minutes=10)
# posting stopped for the whole channel (quota, sign-in): try again hourly, a day long
ACCOUNT_RETRY = timedelta(hours=1)
ACCOUNT_ROUNDS = 24
# posting failed for this meeting only: 3 more rounds, 30 min apart
POST_RETRY = timedelta(minutes=30)
POST_ROUNDS = 3
SCHEDULE_EVERY = timedelta(hours=6)
# look for the recording this long after a scheduled meeting ends
AFTER_SCHEDULED_END = timedelta(minutes=15)
# Zoom never finished the video
RECORDING_GIVE_UP = timedelta(hours=48)
# each check lists recordings from this long before the last check that worked
LOOKBACK = timedelta(days=1)
# webhook meetings Zoom couldn't be asked about, kept for the next checks
MAX_NUDGES_LATER = 50
# written by deploy/auto-update.sh while it waits to restart the app
DEPLOY_FLAG = ".deploying"
DEPLOY_FLAG_MAX_AGE = timedelta(hours=2)


@dataclass
class Hooks:
    """What the autopilot needs from the API process (api/main.py)."""

    worker_busy: Callable[[], bool]
    # start a job for the meeting (uuid, transcribe-with-WhisperX); its id,
    # or None when the server is busy
    submit: Callable[[str, bool], str | None]
    # a live job someone (or the autopilot) already started for the meeting
    find_job: Callable[[str], JobRecord | None]
    get_job: Callable[[str], JobRecord | None]
    update_job: Callable[[JobRecord], None]


def iso(stamp: datetime) -> str:
    return stamp.astimezone(UTC).isoformat(timespec="seconds")


def parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))  # noqa: FURB162
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _due(next_try_at: str | None, now: datetime) -> bool:
    stamp = parse(next_try_at)
    return stamp is None or stamp <= now


class Engine:
    def __init__(
        self,
        hooks: Hooks,
        *,
        store: Store = default_store,
        zoom_client=None,
        clock: Callable[[], datetime] | None = None,
        run_in_background: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self.hooks = hooks
        self.store = store
        self._zoom = zoom_client
        self.clock = clock or (lambda: datetime.now(UTC))
        # posting runs in a thread of its own (an upload takes minutes); tests run it inline
        self._background = run_in_background or _thread
        self._posting: set[str] = set()
        self._posting_lock = threading.Lock()
        # one step at a time (the loop's minute and a webhook wake-up can meet)
        self._step_lock = threading.Lock()

    def zoom(self):
        return self._zoom if self._zoom is not None else zoom.get_client()

    # ------------------------------------------------------------- the step
    def step(self) -> None:
        settings = get_settings()
        if not settings.autopilot_enabled:
            return
        with self._step_lock:
            now = self.clock()
            with self.store.editing() as state:
                # AUTOPILOT_START_AFTER always wins; without it the start is the
                # day it was switched on, again after every time it was off
                if settings.autopilot_start_after.strip() or state.start_after is None or state.switched_off:
                    state.start_after = iso(_first_day(now))
                state.switched_off = False
            if self.store.read().paused:
                return
            self._follow_jobs(now)
            if deploying():
                return  # an update is about to restart the app: start nothing
            self._drop_unwatched(now)
            self._start_posting(now)
            if self.hooks.worker_busy():
                return  # no Zoom checks and no new job while one runs
            if self._check_due(now):
                self._check_zoom(now)
            self._refresh_schedule(now)
            self._start_next(now)

    def note_switched_off(self) -> None:
        """The app started with the autopilot off: when it is switched on
        again, it starts from that day, not from where it stopped."""
        if not self.store.read().switched_off:
            with self.store.editing() as state:
                state.switched_off = True

    def _drop_unwatched(self, now: datetime) -> None:
        """A meeting of an account no longer ticked "autopilot" (or removed
        from the list) is not started; what is already under way finishes."""
        watched = set(emails.autopilot_emails())
        with self.store.editing() as state:
            for m in state.meetings.values():
                if (m.state in ("waiting_recording", "waiting_transcript", "ready")
                        and m.host_email and m.host_email not in watched):
                    m.state, m.error, m.updated_at = "skipped", "Its Zoom account is no longer on autopilot.", iso(now)

    def nudge(self, uuid: str) -> None:
        """Zoom's webhook says this meeting's recording (or transcript) is
        ready: look at it in the next check, which is due at once."""
        with self.store.editing() as state:
            if uuid not in state.nudges:
                state.nudges.append(uuid)

    # ------------------------------------------------------------- 1. jobs
    def _follow_jobs(self, now: datetime) -> None:
        processing = [m for m in self.store.read().meetings.values() if m.state == "processing"]
        jobs = {m.uuid: self.hooks.get_job(m.job_id) if m.job_id else None for m in processing}
        with self.store.editing() as state:
            for uuid, job in jobs.items():
                m = state.meetings.get(uuid)
                if m is None or m.state != "processing":
                    continue
                if job is None:
                    self._failed(m, now, "Its job was removed before it finished.")
                elif job.status == JobStatus.DONE:
                    m.state, m.post_attempts, m.next_try_at, m.error = "posting", 0, None, None
                elif job.status == JobStatus.SKIPPED_DESYNC:
                    m.state, m.error = "needs_attention", job.error or "The recording's sound and picture are out of sync."
                elif job.status in (JobStatus.FAILED, JobStatus.CANCELLED):
                    self._failed(m, now, job.error or f"The job ended: {job.status.value}.")
                elif job.status == JobStatus.DECIDED:
                    # stopped at the AI's picks for someone to review: no result to post
                    self._failed(m, now, "Its job stopped at the AI's picks (Render now was never pressed).")
                m.updated_at = iso(now)

    def _failed(self, m: Meeting, now: datetime, error: str) -> None:
        """Process it again after a wait, or (out of retries) wait for a person."""
        m.error = error[:500]
        m.job_id, m.by_hand = None, False
        retries = get_settings().autopilot_max_retries
        if m.attempts <= retries:
            wait = RETRY_WAITS[min(max(m.attempts - 1, 0), len(RETRY_WAITS) - 1)]
            m.state, m.next_try_at = "ready", iso(now + wait)
        else:
            m.state = "needs_attention"

    # ------------------------------------------------------------- 2. posting
    def _start_posting(self, now: datetime) -> None:
        if deploying():  # looked at again here: a step can take a while
            return
        for m in self.store.read().meetings.values():
            if m.state == "posting" and _due(m.next_try_at, now):
                self._post(m.uuid, m.job_id)

    def _post(self, uuid: str, job_id: str | None) -> None:
        with self._posting_lock:
            if uuid in self._posting:
                return
            self._posting.add(uuid)
        handed_over = False
        try:
            job = self.hooks.get_job(job_id) if job_id else None
            if job is None:
                self._finish(uuid, None, None)
                return
            with publish_run.PUBLISH_LOCK:
                if job.youtube_busy:
                    return  # a person is publishing it right now; look again next step
                plan = publish_run.plan_everything(job, get_settings().autopilot_privacy, by="autopilot")
                if plan is None:
                    self._finish(uuid, job, None)
                    return
                publish_run.prepare(job, plan)
                self.hooks.update_job(job)
            handed_over = True
            self._background(lambda: self._post_and_finish(uuid, job, plan))
        except Exception:  # one meeting must never stop the autopilot
            logger.exception("autopilot: posting meeting %s could not start", uuid)
        finally:
            if not handed_over:
                with self._posting_lock:
                    self._posting.discard(uuid)

    def _post_and_finish(self, uuid: str, job: JobRecord, plan) -> None:
        try:
            stopped = publish_run.publish_job(job, self.hooks.update_job, plan)
            self._finish(uuid, job, stopped)
        except Exception:
            logger.exception("autopilot: posting meeting %s failed", uuid)
        finally:
            with self._posting_lock:
                self._posting.discard(uuid)

    def _finish(self, uuid: str, job: JobRecord | None, stopped: str | None) -> None:
        """After a posting round: posted, waiting for a person, or try again."""
        now = self.clock()
        if job is None:
            source_state = get_ledger().meeting_state(f"zoom:{uuid}")
            verdict = "done" if source_state == "posted" else "missing"
        else:
            verdict = publish_run.posting_verdict(job)
        with self.store.editing() as state:
            m = state.meetings.get(uuid)
            if m is None:
                return
            m.updated_at = iso(now)
            if verdict == "done":
                m.state, m.error, m.posted_at, m.next_try_at = "posted", None, iso(now), None
            elif verdict == "check":
                m.state = "needs_attention"
                m.error = "A video's upload was cut off and needs a check on YouTube (see the posted list)."
            elif verdict == "missing":
                m.state = "needs_attention"
                m.error = "Some videos can't be posted: their files are no longer on the server."
            else:
                m.post_attempts += 1
                rounds, wait = (ACCOUNT_ROUNDS, ACCOUNT_RETRY) if stopped else (POST_ROUNDS, POST_RETRY)
                m.error = stopped or (_job_upload_error(job) if job else None) or "Some videos did not go up."
                if m.post_attempts >= rounds:
                    m.state = "needs_attention"
                else:
                    m.next_try_at = iso(now + wait)

    # ------------------------------------------------------------- 3. checks
    def _check_due(self, now: datetime) -> bool:
        state = self.store.read()
        if state.nudges:
            return True
        last = parse(state.last_check_at)
        if last is None or now - last >= timedelta(minutes=get_settings().autopilot_check_minutes):
            return True
        if any(not s.checked and parse(s.end_at) + AFTER_SCHEDULED_END <= now for s in state.schedule):
            return True
        return any(m.state == "waiting_transcript" and _due(m.deadline_at, now) for m in state.meetings.values())

    def _check_zoom(self, now: datetime) -> None:
        state = self.store.read()
        start_after = parse(state.start_after) or _first_day(now)
        watched = emails.autopilot_emails()
        client = self.zoom()
        errors: list[str] = []

        found: dict[str, tuple[str, dict]] = {}
        # from a day before the last check that listed every account (so a
        # pause or a Zoom outage of any length misses nothing), never before
        # the start; Zoom's from/to are UTC days
        last_ok = parse(state.last_ok_check_at)
        first = max(start_after, (last_ok - LOOKBACK) if last_ok else start_after).astimezone(UTC).date()
        listed_all = True
        for email in watched:
            try:
                for row in client.list_recordings(email, first, now.astimezone(UTC).date()):
                    if row.get("uuid"):
                        found.setdefault(row["uuid"], (email, row))
            except Exception as exc:  # noqa: BLE001 — one account's trouble must not stop the others
                errors.append(f"{email}: {exc}")
                listed_all = False
        with self.store.editing() as state:
            for uuid, (email, row) in found.items():
                start = parse(row.get("start_time"))
                if uuid in state.meetings or start is None or start < start_after:
                    continue
                state.meetings[uuid] = Meeting(
                    uuid=uuid, host_email=email, topic=(row.get("topic") or "").strip(),
                    start_time=row.get("start_time"), first_seen_at=iso(now), updated_at=iso(now),
                )
            to_look = [m.uuid for m in state.meetings.values()
                       if m.state in ("waiting_recording", "waiting_transcript")]
            to_look += [u for u in [*state.nudges, *state.nudges_later] if u not in to_look]
            nudged = set(state.nudges) | set(state.nudges_later)

        looked_at: set[str] = set()
        for uuid in to_look:
            try:
                meeting = client.get_meeting(uuid)
            except PipelineError as exc:
                if exc.code == "zoom_not_found":
                    self._gone(uuid, now)
                    looked_at.add(uuid)
                else:
                    errors.append(f"{uuid}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{uuid}: {exc}")
                continue
            looked_at.add(uuid)
            with self.store.editing() as state:
                self._evaluate(state, uuid, meeting, now, watched, start_after)

        with self.store.editing() as state:
            # a webhook's meeting Zoom couldn't be asked about is looked at again
            # in the next regular check (not at once: no loop on a broken call)
            failed = [u for u in [*state.nudges, *state.nudges_later] if u in nudged and u not in looked_at]
            state.nudges = [u for u in state.nudges if u not in nudged]
            state.nudges_later = list(dict.fromkeys(failed))[-MAX_NUDGES_LATER:]
            state.last_check_at = iso(now)
            if listed_all:
                state.last_ok_check_at = iso(now)
            state.last_check_error = "; ".join(errors)[:500] or None
            for scheduled in state.schedule:
                if parse(scheduled.end_at) + AFTER_SCHEDULED_END <= now:
                    scheduled.checked = True

    def _gone(self, uuid: str, now: datetime) -> None:
        with self.store.editing() as state:
            m = state.meetings.get(uuid)
            if m is not None and m.state in ("waiting_recording", "waiting_transcript", "ready"):
                m.state, m.error, m.updated_at = "skipped", "The recording was deleted in Zoom.", iso(now)

    def _evaluate(self, state, uuid: str, meeting: dict, now: datetime, watched: list[str],
                  start_after: datetime) -> None:
        settings = get_settings()
        m = state.meetings.get(uuid)
        if m is None:  # known only from the webhook: is it one of ours?
            host = (meeting.get("host_email") or "").strip().lower()
            start = parse(meeting.get("start_time"))
            if host not in watched or start is None or start < start_after:
                return
            m = state.meetings[uuid] = Meeting(
                uuid=uuid, host_email=host, topic=(meeting.get("topic") or "").strip(),
                start_time=meeting.get("start_time"), first_seen_at=iso(now),
            )
        if m.state not in ("waiting_recording", "waiting_transcript"):
            return
        m.updated_at = iso(now)
        m.topic = (meeting.get("topic") or m.topic).strip()
        choice = zoom.choose_files(meeting)
        end = _recording_end(meeting, choice)
        m.recording_end = iso(end) if end else m.recording_end
        if not choice.parts or not choice.recording_ready:
            if end is not None and now - end > RECORDING_GIVE_UP:
                m.state = "needs_attention"
                m.error = ("The recording has no video file." if not choice.parts
                           else "Zoom never finished making the video.")
            else:
                m.state = "waiting_recording"
            return
        deadline = (end or now) + timedelta(hours=settings.autopilot_transcript_wait_hours)
        m.deadline_at = iso(deadline)
        if choice.has_transcript:
            m.state, m.transcript, m.next_try_at = "ready", "zoom", None
        elif now >= deadline:
            m.state, m.transcript, m.next_try_at = "ready", "whisperx", None
        else:
            m.state = "waiting_transcript"

    # ------------------------------------------------------------- schedule
    def _refresh_schedule(self, now: datetime) -> None:
        state = self.store.read()
        last = parse(state.schedule_checked_at)
        if last is not None and now - last < SCHEDULE_EVERY:
            return
        client = self.zoom()
        entries: list[ScheduledMeeting] = []
        errors: list[str] = []
        for email in emails.autopilot_emails():
            try:
                upcoming = client.list_upcoming_meetings(email)
            except PipelineError as exc:
                if exc.code == "zoom_auth":
                    errors.append(f"the Zoom app needs the {zoom.SCHEDULE_SCOPE} permission to read schedules")
                    break
                errors.append(f"{email}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{email}: {exc}")
                continue
            for meeting in upcoming:
                start = parse(meeting.get("start_time"))
                if start is None:
                    continue  # a recurring meeting with no fixed time
                end = start + timedelta(minutes=float(meeting.get("duration") or 60))
                if now - timedelta(hours=1) < end < now + timedelta(days=7):
                    entries.append(ScheduledMeeting(email=email, topic=(meeting.get("topic") or "").strip(),
                                                    end_at=iso(end)))
        with self.store.editing() as state:
            checked = {(s.email, s.end_at) for s in state.schedule if s.checked}
            for entry in entries:
                entry.checked = (entry.email, entry.end_at) in checked
            state.schedule = sorted(entries, key=lambda s: s.end_at)
            state.schedule_checked_at = iso(now)
            state.schedule_error = "; ".join(errors)[:300] or None

    # ------------------------------------------------------------- 4. start
    def _start_next(self, now: datetime) -> None:
        ready = sorted((m for m in self.store.read().meetings.values() if m.state == "ready" and _due(m.next_try_at, now)),
                       key=lambda m: m.start_time or "")
        if not ready:
            return
        m = ready[0]
        if _nothing_left_to_post(m.uuid):
            with self.store.editing() as state:
                current = state.meetings[m.uuid]
                if current.state == "ready":
                    current.state, current.posted_at, current.error = "posted", iso(now), None
            return
        job = self.hooks.find_job(m.uuid)
        if job is not None:  # someone already processes it: post that job's result
            with self.store.editing() as state:
                current = state.meetings[m.uuid]
                if current.state != "ready":
                    return  # a person skipped it meanwhile
                current.state, current.job_id, current.by_hand = "processing", job.job_id, True
                current.updated_at = iso(now)
            return
        if self.hooks.worker_busy() or deploying():
            return
        try:
            job_id = self.hooks.submit(m.uuid, m.transcript == "whisperx")
        except PipelineError as exc:
            if exc.code == "zoom_not_found":
                self._gone(m.uuid, now)
                return
            with self.store.editing() as state:
                current = state.meetings[m.uuid]
                current.error, current.next_try_at = str(exc)[:500], iso(now + SUBMIT_TRY_AGAIN)
            return
        if job_id is None:
            return  # busy after all: next step
        with self.store.editing() as state:
            current = state.meetings[m.uuid]
            if current.state != "ready":
                # a person skipped it while the job was being started: the job
                # runs, but a skipped meeting is never posted
                logger.warning("autopilot: %r was skipped while job %s started; nothing will be posted",
                               m.topic, job_id)
                return
            current.state, current.job_id, current.by_hand = "processing", job_id, False
            current.attempts += 1
            current.error, current.next_try_at, current.updated_at = None, None, iso(now)
        logger.info("autopilot: started job %s for %r (%s transcript)", job_id, m.topic, m.transcript)

    # ------------------------------------------------------------- a person's say
    def retry(self, uuid: str) -> Meeting:
        """Try a meeting again now: post what is left, or process it again."""
        with self.store.editing() as state:
            m = state.meetings.get(uuid)
            if m is None:
                raise KeyError(uuid)
            if m.state in ("processing", "posted") or uuid in self._posting:
                raise ValueError(f"This meeting is {m.state.replace('_', ' ')}; there is nothing to retry.")
            job = self.hooks.get_job(m.job_id) if m.job_id else None
            if job is not None and job.status == JobStatus.DONE and not job.files_deleted:
                m.state, m.post_attempts = "posting", 0
            elif m.recording_end is None or m.state == "skipped" or m.transcript is None:
                m.state = "waiting_recording"  # looked at again in the next check, which is due now
                if uuid not in state.nudges:
                    state.nudges.append(uuid)
            else:
                m.state, m.attempts, m.job_id = "ready", 0, None
                m.transcript = m.transcript or "zoom"
            m.next_try_at, m.error = None, None
            return m.model_copy()

    def skip(self, uuid: str) -> Meeting:
        with self.store.editing() as state:
            m = state.meetings.get(uuid)
            if m is None:
                raise KeyError(uuid)
            if m.state in ("processing", "posting", "posted") or uuid in self._posting:
                raise ValueError(f"This meeting is {m.state.replace('_', ' ')} and can't be skipped now.")
            m.state, m.error = "skipped", "Skipped by a person."
            return m.model_copy()

    def after_check(self, job_id: str, source: str = "") -> None:
        """A person settled an upload in the posted list: the meeting it held
        up (the one of that job, or of that meeting under another job) posts
        what is left (or finds it all posted)."""
        with self.store.editing() as state:
            for m in state.meetings.values():
                if (m.state == "needs_attention" and m.job_id
                        and (m.job_id == job_id or f"zoom:{m.uuid}" == source)):
                    m.state, m.post_attempts, m.next_try_at, m.error = "posting", 0, None, None

    def set_paused(self, paused: bool) -> None:
        with self.store.editing() as state:
            state.paused = paused


# ------------------------------------------------------------------ helpers


def _first_day(now: datetime) -> datetime:
    """The start of the autopilot: AUTOPILOT_START_AFTER, else the start of
    today in the team's time zone (the day it was switched on)."""
    settings = get_settings()
    try:
        zone = ZoneInfo(settings.local_timezone)
    except Exception:  # noqa: BLE001
        zone = UTC
    if settings.autopilot_start_after:
        try:
            day = date.fromisoformat(settings.autopilot_start_after.strip())
            return datetime.combine(day, time.min, zone)
        except ValueError:
            logger.error("AUTOPILOT_START_AFTER=%r is not a date like 2026-11-01; using today",
                         settings.autopilot_start_after)
    return datetime.combine(now.astimezone(zone).date(), time.min, zone)


def deploying() -> bool:
    """deploy/auto-update.sh is waiting to restart the app (it writes this
    file first and removes it when done; an old one is ignored)."""
    flag = files.state_dir() / DEPLOY_FLAG
    try:
        return datetime.now(UTC).timestamp() - flag.stat().st_mtime < DEPLOY_FLAG_MAX_AGE.total_seconds()
    except OSError:
        return False


def _nothing_left_to_post(uuid: str) -> bool:
    """The meeting's full video is on the channel and its shorts already
    come from some run: a new run could post nothing."""
    mine = get_ledger().for_source(f"zoom:{uuid}")
    full = any(u.kind == "full" and u.state == "published" for u in mine)
    shorts = any(u.kind == "short" and u.state in ("published", "unsure", "interrupted", "uploading") for u in mine)
    return full and shorts


def _recording_end(meeting: dict, choice: zoom.Choice) -> datetime | None:
    ends = [parse(p.video.get("recording_end")) for p in choice.parts]
    ends = [e for e in ends if e is not None]
    if ends:
        return max(ends)
    start = parse(meeting.get("start_time"))
    return start + timedelta(minutes=float(meeting.get("duration") or 0)) if start else None


def _job_upload_error(job: JobRecord) -> str | None:
    errors = [job.youtube_error] + [s.error for s in job.youtube_shorts.values()]
    return next((e for e in errors if e), None)


def _thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="autopilot-post", daemon=True).start()
