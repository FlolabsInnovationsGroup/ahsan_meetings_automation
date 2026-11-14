"""A real week on the real path, with the DEFAULT publishing settings.

Five working days of team meetings go through the app's own autopilot, Zoom
client, job queue, release plan, describe and ledger code; only the outside
world is faked (Zoom's servers as in tests/test_never_post_real_path.py, the
cut is a stub that leaves a realistic finished job, and YouTube is
tests/fake_channel.py). Time is a clock the test moves an hour at a time, so
the release plan books slots as it would over a real week.

What must hold at the end of the week, with release plan on, 12 h review,
2.5 h gap, 4 a day, 2 Shorts a day, 10:00-22:00 team time, Shorts 24 h after
their full video, 2 Shorts per meeting and topic-first titles:
- nothing of a never-post meeting reaches the channel;
- every public upload went up private with a publishAt inside the rules;
- each Short goes public after its full video and links to it;
- one notifying upload a team day at most, Shorts never notify;
- titles and descriptions are within YouTube's rules, chapters first at 0:00;
- no meeting is posted twice (two accounts recording the same meeting);
- the cost meter counts every upload.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import httpx
import pytest

import api.main as api_main
import costs
import costs.db
import costs.record
import ingest.zoom
import ledger.files
import publish.run
import publish.schedule
from api.jobs import job_store
from autopilot import emails, never_post
from autopilot.store import store as autopilot_store
from core.config import get_settings
from core.timeline import fmt_clock
from ingest import zoom as zoom_module
from ledger.uploads import get_ledger
from publish import series
from publish import settings as pub_settings
from tests.fake_channel import FakeChannel
from tests.test_never_post_real_path import (
    BUDDY,
    LEGAL,
    OPS,
    PM,
    PROJECTS,
    Server,
    ZoomServers,
    zoom_recording,
)
from tests.test_publish import _finished_job

TEAM = ZoneInfo("Asia/Karachi")
MONDAY = date(2026, 11, 16)
CHAPTER = re.compile(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s")
ANY_TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?!\d)")


# ------------------------------------------------------------------ the clock


class _Clock:
    at = datetime.combine(MONDAY, datetime.min.time(), TEAM).replace(hour=8).astimezone(UTC)


class _FakeDatetime(datetime):
    """datetime, except that now() is the test's clock."""

    @classmethod
    def now(cls, tz=None):
        moment = _Clock.at
        return moment.astimezone(tz) if tz is not None else moment.astimezone(UTC).replace(tzinfo=None)


class TimedChannel(FakeChannel):
    """The fake channel, remembering when (on the test's clock) each video went up."""

    def upload_video(self, *args, **kwargs):
        video_id = super().upload_video(*args, **kwargs)
        self.videos[video_id].setdefault("uploaded_at", _Clock.at)
        return video_id


# ------------------------------------------------------------------ the week


@dataclasses.dataclass(frozen=True)
class Meeting:
    uuid: str
    topic: str
    zoom_id: int
    day: int  # 0 = Monday
    start: str  # team time, "HH:MM"
    minutes: int
    host: str = PM
    public: bool = True
    # the two Shorts the cut picks: (title, hook, what is said in the clip)
    shorts: tuple[tuple[str, str, str], ...] = ()

    @property
    def start_utc(self) -> datetime:
        hour, minute = map(int, self.start.split(":"))
        day = MONDAY + timedelta(days=self.day)
        return datetime.combine(day, datetime.min.time(), TEAM).replace(hour=hour, minute=minute).astimezone(UTC)

    @property
    def team_day(self) -> str:
        return self.start_utc.astimezone(TEAM).date().isoformat()


def _shorts(*rows):
    return tuple(rows)


COPY_TOPIC = "Multi-modal Perception Team meeting"
COPY_SHORTS = _shorts(
    ("Fusing radar and camera for night driving", "At 9:30 the camera alone missed half the cones.",
     "fusing radar and camera for night driving works because radar sees the cones the camera misses"),
    ("Why the depth model hallucinates glass", "Glass doors fool every depth model we tried.",
     "the depth model hallucinates glass doors as open space so we mask them"),
)

WEEK = [
    # ---- Monday
    Meeting("/sp//mon==", "Space Team meeting", 81929595102, 0, "11:00", 60, shorts=_shorts(
        ("How the rover finds its dock in the dark", "At 9:30 the dock lights went out and it still parked.",
         "the rover finds its dock in the dark with the infrared beacon"),
        ("Why we dropped the second lidar", "One lidar was enough once the map was good.",
         "we dropped the second lidar because the map was good enough with one"),
    )),
    Meeting("ops+mon==", "Operations team meeting", OPS, 0, "12:30", 45, public=False),
    Meeting("design+mon==", "Design Team", 87096814480, 0, "15:00", 50, shorts=_shorts(
        ("Cache hit rate > 90% after the icon sprite fix", "Latency went from 900 ms to <200 ms.",
         "cache hit rate went above ninety percent after the icon sprite fix"),
        ("The onboarding flow in three screens", "We cut onboarding from seven screens to three.",
         "the onboarding flow now fits in three screens"),
    )),
    # ---- Tuesday
    Meeting("brain/tue==", "FloBrain Weekly Backend Meeting", 89952328054, 1, "11:00", 70, shorts=_shorts(
        ("Moving the session store to Redis", "Sessions were the slowest query we had.",
         "moving the session store to redis made sessions the fastest query"),
        ("Retries that do not melt the queue", "Exponential backoff with jitter, and why both matter.",
         "retries with backoff and jitter do not melt the queue"),
    )),
    Meeting("legal+tue==", "Ethics & Legal Team meeting", LEGAL, 1, "14:00", 40, public=False),
    Meeting("one1/tue==", "Ayesha / Bilal 1:1", 84506002536, 1, "16:00", 30, public=False),
    # ---- Wednesday
    Meeting("iot/wed==", "IoT Team Meeting", 86012345678, 2, "11:00", 55, shorts=_shorts(
        ("Flashing ESP32 boards over the air", "OTA updates on 40 boards at once, 10:15 sharp.",
         "flashing esp32 boards over the air on forty boards at once"),
        ("Why the sensors drop off the mesh", "A cheap antenna, not the firmware.",
         "the sensors drop off the mesh because of a cheap antenna"),
    )),
    Meeting("catchup/wed==", "Weekly catch-up", PROJECTS, 2, "13:00", 35, public=False),
    Meeting("gtm/wed==", "GTM & Influencers Team meeting", 87437018817, 2, "17:00", 40, shorts=_shorts(
        ("How a single creator video brought in more qualified signups than our entire paid campaign did",
         "One honest review beat a month of ads.",
         "a single creator video brought more qualified signups than the entire paid campaign"),
        ("Picking creators who actually build things", "Followers matter less than whether they ship.",
         "picking creators who actually build things matters more than followers"),
    )),
    # ---- Thursday
    Meeting("hum/thu==", "Humanoid Team Meeting", 87633045920, 3, "11:00", 60, shorts=_shorts(
        ("Teaching the hand to grip an egg", "Force feedback at 1 kHz, finally.",
         "teaching the hand to grip an egg needed force feedback"),
        ("Walking on gravel without falling", "The gait planner now reads the ground.",
         "walking on gravel without falling because the gait planner reads the ground"),
    )),
    # the same meeting recorded by two Zoom accounts: only one copy may go up
    Meeting("mmp+pm==", COPY_TOPIC, 88123456789, 3, "15:00", 65, shorts=COPY_SHORTS),
    Meeting("mmp+buddy==", COPY_TOPIC, 88123456789, 3, "15:02", 63, host=BUDDY, shorts=COPY_SHORTS),
    # ---- Friday
    Meeting("/sp//fri==", "Space Team meeting", 81929595102, 4, "11:00", 60, shorts=_shorts(
        ("Charging the rover from a solar mat", "The mat charges it in four hours of sun.",
         "charging the rover from a solar mat takes four hours of sun"),
        ("How the rover finds its dock in the dark", "The beacon now works through dust too.",
         "the rover finds its dock in the dark even through dust"),
    )),
    Meeting("design+fri==", "Design Team", 87096814480, 4, "14:00", 45, shorts=_shorts(
        ("Dark mode without washed-out charts", "Every chart color checked against both themes.",
         "dark mode charts without washed out colors"),
        ("A type scale that works on phones", "Five sizes, not eleven.",
         "a type scale that works on phones with five sizes"),
    )),
]
BY_UUID = {m.uuid: m for m in WEEK}
PUBLIC = [m for m in WEEK if m.public]
NEVER = [m for m in WEEK if not m.public]
COPIES = ("mmp+pm==", "mmp+buddy==")


def _recording(m: Meeting) -> dict:
    """Zoom's JSON for the meeting, at its own time on the test's clock."""
    rec = zoom_recording(m.uuid, m.topic, m.zoom_id, host=m.host, minutes=m.minutes)
    start, end = m.start_utc, m.start_utc + timedelta(minutes=m.minutes)
    rec["start_time"] = _stamp(start)
    for f in rec["recording_files"]:
        f["recording_start"], f["recording_end"] = _stamp(start), _stamp(end)
    return rec


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cut_the_week(job, update):
    """The cut, as deliver() leaves it: a final video, two Shorts with their
    words, chapters, highlights and the AI usage, for the meeting the job is of."""
    m = BY_UUID[job.zoom_meeting_uuid]
    done = _finished_job(job.job_id)
    done.title, done.zoom_meeting, done.zoom_meeting_uuid = job.title, job.zoom_meeting, job.zoom_meeting_uuid
    done.zoom_options_hash, done.options = job.zoom_options_hash, job.options
    done.meeting_date = m.team_day
    job_dir = get_settings().jobs_dir / job.job_id
    # the owner's "2 Shorts per meeting": the cut makes as many as that
    count = pub_settings.current().shorts_per_meeting
    for k in range(count + 1, 4):
        done.artifacts.pop(f"shorts/short_{k:02d}.mp4", None)
        (job_dir / "shorts" / f"short_{k:02d}.mp4").unlink(missing_ok=True)
    clips = [(110.0, 150.0), (30.0, 70.0)]
    rows = [{"file": f"shorts/short_{k + 1:02d}.mp4", "source_start": s, "source_end": e, "title": title,
             "hook": hook, "text": text}
            for k, ((title, hook, text), (s, e)) in enumerate(zip(m.shorts[:count], clips, strict=False))]
    highlights = [{"final_at_s": 3.0, "source_start": 110.0, "source_end": 150.0, "title": m.shorts[0][0]}]
    (job_dir / "manifest.json").write_text(json.dumps({"job_id": job.job_id, "shorts": rows,
                                                       "highlights": highlights}), encoding="utf-8")
    # chapter titles come out of decide/chapters.py without < or >
    entries = [{"t": 0, "title": "Highlights"}, {"t": 95, "title": " ".join(re.sub(r"[<>]", " ", m.shorts[0][0]).split())},
               {"t": 610, "title": m.shorts[1][0]}, {"t": 1500, "title": "Questions and next steps"}]
    (job_dir / "chapters.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")
    (job_dir / "chapters.txt").write_text("\n".join(f"{fmt_clock(e['t'])} {e['title']}" for e in entries) + "\n",
                                          encoding="utf-8")
    done.artifacts["final.mp4"].duration_s = (m.minutes - 8) * 60.0
    done.llm_usage = {"calls": 4, "prompt_tokens": 60000, "output_tokens": 5000}
    done.stage_timings = {"rendering": 900.0, "deciding": 120.0}
    job_store.update(done)


# ------------------------------------------------------------------ the server


@pytest.fixture
def week(monkeypatch):
    for name, value in (("ZOOM_ACCOUNT_ID", "acct"), ("ZOOM_CLIENT_ID", "cid"), ("ZOOM_CLIENT_SECRET", "secret"),
                        ("ZOOM_DEFAULT_EMAIL", PM), ("AUTOPILOT_CHECK_MINUTES", "0"),
                        ("AUTOPILOT_START_AFTER", (MONDAY - timedelta(days=1)).isoformat()),
                        ("LOCAL_TIMEZONE", "Asia/Karachi"), ("ZOOM_WEBHOOK_SECRET_TOKEN", "whsec")):
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    monkeypatch.setattr(_Clock, "at", _Clock.at)
    for module in (publish.schedule, publish.run, ledger.files, ingest.zoom, costs.db, costs.record):
        monkeypatch.setattr(module, "datetime", _FakeDatetime)
    zoom = ZoomServers()
    client = zoom_module.ZoomClient(transport=httpx.MockTransport(zoom), sleep=lambda seconds: None)
    monkeypatch.setattr(zoom_module, "get_client", lambda: client)
    monkeypatch.setattr(api_main, "run_zoom_pipeline", _cut_the_week)
    channel = TimedChannel().install(monkeypatch)
    autopilot_store._state = None
    running = Server(zoom, channel)
    monkeypatch.setattr(running.engine, "clock", lambda: _Clock.at)
    running.engine.set_enabled(True)
    emails.set_autopilot(BUDDY, True)
    # the owner has these teams on the Teams list already (a new team would wait for a person)
    for m in WEEK:
        if never_post.match(m.topic, m.zoom_id) is None:
            series.set_row(series.series_key(m.topic), tier="public")
    yield running
    assert api_main.job_queue.wait_idle(15), "a test left a job running"


def _live_the_week(server) -> None:
    """Hour by hour from Monday 08:00 to Saturday 08:00, team time. A meeting's
    recording shows up in Zoom 20 minutes after it ends."""
    assert pub_settings.current() == pub_settings.DEFAULTS
    shown: set[str] = set()
    end = _Clock.at + timedelta(days=5)
    while _Clock.at <= end:
        for m in WEEK:
            if m.uuid not in shown and m.start_utc + timedelta(minutes=m.minutes + 20) <= _Clock.at:
                server.zoom.add(_recording(m))
                shown.add(m.uuid)
        server.settle()
        _Clock.at += timedelta(hours=1)
    assert shown == set(BY_UUID)


# ------------------------------------------------------------------ what went up


@dataclasses.dataclass
class Video:
    video_id: str
    kind: str
    source: str  # zoom:<uuid>
    meeting: str
    meeting_date: str | None
    title: str
    description: str
    privacy: str
    publish_at: datetime | None
    notify: bool
    uploaded_at: datetime
    url: str

    @property
    def uuid(self) -> str:
        return self.source.removeprefix("zoom:")


def _videos(server) -> list[Video]:
    by_id = {u.video_id: u for u in get_ledger().posted()}
    found = []
    for video_id, v in server.channel.videos.items():
        u = by_id[video_id]
        found.append(Video(video_id, u.kind, u.source, u.meeting, u.meeting_date, v["title"], v["description"],
                           v["privacy"], v["publish_at"], v["notify"], v["uploaded_at"], u.url or ""))
    return found



def _check_every_public_meeting_is_posted_once_and_nothing_else(server, videos):
    meetings = autopilot_store.read().meetings
    for m in PUBLIC:
        if m.uuid in COPIES:
            continue
        assert meetings[m.uuid].state == "posted", (m.topic, meetings[m.uuid].error)
    # the two copies: one posted, the other held for a person
    states = sorted(meetings[u].state for u in COPIES)
    assert states == ["needs_attention", "posted"], [(u, meetings[u].state, meetings[u].error) for u in COPIES]
    held = next(u for u in COPIES if meetings[u].state == "needs_attention")
    assert meetings[held].error.startswith("Held for a person") and held not in {v.uuid for v in videos}
    fulls = Counter(v.uuid for v in videos if v.kind == "full")
    assert set(fulls) == {m.uuid for m in PUBLIC} - {held}, fulls
    assert all(n == 1 for n in fulls.values()), fulls
    # one meeting name on one day is one full video, whichever account recorded it
    per_meeting = Counter((v.meeting.casefold(), v.meeting_date) for v in videos if v.kind == "full")
    assert all(n == 1 for n in per_meeting.values()), per_meeting
    # the owner's "2 Shorts per meeting"
    shorts = Counter(v.uuid for v in videos if v.kind == "short")
    assert all(n <= 2 for n in shorts.values()), shorts


def _check_nothing_of_a_never_post_meeting_reaches_the_channel(server, videos):
    meetings = autopilot_store.read().meetings
    never = {m.uuid for m in NEVER}
    assert never.isdisjoint(v.uuid for v in videos)
    for m in NEVER:
        assert meetings[m.uuid].state == "skipped" and meetings[m.uuid].error.startswith("Never posted: "), m.topic
        assert server.jobs_of(m.uuid) == [], f"{m.topic}: a job was started"
        assert m.uuid not in server.zoom.asked_about, f"{m.topic}: Zoom was asked for its files"
    for v in videos:
        for m in NEVER:
            assert m.topic.casefold() not in (v.title + "\n" + v.description).casefold()


def _check_every_public_upload_waits_its_review_inside_the_window(server, videos):
    cfg = pub_settings.current()
    assert videos
    for v in videos:
        assert v.privacy == "private" and v.publish_at is not None, v.title
        assert v.publish_at >= v.uploaded_at + timedelta(hours=cfg.review_hours), v.title
        local = v.publish_at.astimezone(TEAM)
        assert cfg.window_start_hour * 60 <= local.hour * 60 + local.minute < cfg.window_end_hour * 60, (v.title, local)
        assert v.publish_at <= v.uploaded_at + timedelta(days=cfg.max_days_ahead)


def _check_release_slots_are_spread_out(server, videos):
    cfg = pub_settings.current()
    times = sorted(v.publish_at for v in videos)
    for a, b in pairwise(times):
        assert b - a >= timedelta(hours=cfg.min_gap_hours), (a, b)
    per_day, shorts_per_day, notifying = Counter(), Counter(), Counter()
    for v in videos:
        day = v.publish_at.astimezone(TEAM).date()
        per_day[day] += 1
        shorts_per_day[day] += v.kind == "short"
        notifying[day] += bool(v.notify)
        if v.kind == "short":
            assert v.notify is False, v.title
    assert max(per_day.values()) <= cfg.max_per_day, per_day
    assert max(shorts_per_day.values()) <= cfg.shorts_per_day, shorts_per_day
    assert max(notifying.values()) <= 1, notifying


def _check_each_short_follows_its_full_video_and_links_to_it(server, videos):
    cfg = pub_settings.current()
    fulls = {v.uuid: v for v in videos if v.kind == "full"}
    shorts = [v for v in videos if v.kind == "short"]
    assert shorts
    for s in shorts:
        full = fulls[s.uuid]
        assert s.publish_at - full.publish_at >= timedelta(hours=cfg.shorts_after_full_hours), s.title
        assert full.url and full.url in s.description, (s.title, s.description)


def _check_words_are_within_youtubes_rules(server, videos):
    for v in videos:
        assert 0 < len(v.title) <= 100 and "<" not in v.title and ">" not in v.title, v.title
        assert len(v.description.encode("utf-8")) <= 5000 and "<" not in v.description and ">" not in v.description
        if v.kind != "full":
            continue
        # topic first: the moment, then the team ("... | FloLabs Space")
        assert " | FloLabs" in v.title and not v.title.startswith(v.meeting), v.title
        lines = v.description.splitlines()
        first = next(i for i, line in enumerate(lines) if CHAPTER.match(line))
        assert re.match(r"^0?0:00\s", lines[first]), lines[first]
        assert not any(ANY_TIME.search(line) for line in lines[:first]), lines[:first]


def _check_the_cost_meter_counts_every_cut_once(server, videos):
    end = max(date.today(), MONDAY + timedelta(days=30))
    report = costs.report(400, today=end)
    assert report["totals"]["ai_usd"] > 0 and report["totals"]["total_usd"] == report["totals"]["ai_usd"]
    assert not {"uploads", "compute_hours"} & set(report) and "fixed_usd" not in report["totals"]
    # every job that was cut is counted once (the held copy's job too: it was cut)
    cut = {j.job_id for j in map(job_store.get, job_store.list_ids()) if j is not None}
    assert report["meetings"]["count"] == len(cut) == len(PUBLIC)


def _check_every_short_either_went_up_or_waits_for_a_person(server, videos):
    """A Short with no free slot within max_days_ahead is left for a person; it
    never goes up outside the rules."""
    by_meeting = defaultdict(list)
    for v in videos:
        by_meeting[v.uuid].append(v.kind)
    for m in PUBLIC:
        if m.uuid in COPIES and m.uuid not in by_meeting:
            continue
        jobs = [j for j in server.jobs_of(m.uuid) if j.youtube_status == "published"]
        assert len(jobs) == 1, m.topic
        for index, state in jobs[0].youtube_shorts.items():
            assert state.status in ("published", "skipped"), (m.topic, index, state.status, state.error)


def _timetable(videos) -> str:
    """The week's release plan, in team time, for a failure message."""
    rows = sorted(videos, key=lambda v: v.publish_at or v.uploaded_at)
    return "\n".join(
        f"{(v.publish_at.astimezone(TEAM).strftime('%a %d %H:%M') if v.publish_at else 'now'):>12}  "
        f"{v.kind:5} notify={v.notify!s:5} up {v.uploaded_at.astimezone(TEAM):%a %H:%M}  {v.title}"
        for v in rows)


def test_a_real_week_with_the_default_publishing_settings(week):
    _live_the_week(week)
    videos = _videos(week)
    print(_timetable(videos))
    for check in (
        _check_every_public_meeting_is_posted_once_and_nothing_else,
        _check_nothing_of_a_never_post_meeting_reaches_the_channel,
        _check_every_public_upload_waits_its_review_inside_the_window,
        _check_release_slots_are_spread_out,
        _check_each_short_follows_its_full_video_and_links_to_it,
        _check_words_are_within_youtubes_rules,
        _check_every_short_either_went_up_or_waits_for_a_person,
        _check_the_cost_meter_counts_every_cut_once,
    ):
        check(week, videos)


def test_a_short_retried_after_its_full_went_public_still_waits_a_day(week, monkeypatch):
    """A late meeting (21:00 team time) whose Short hits YouTube's daily upload
    limit right after its full video went up. The limit resets at midnight
    Pacific (13:00 team time), after the full video has gone public."""
    import sys

    import publish.youtube as yt

    late = dataclasses.replace(BY_UUID["design+mon=="], start="21:00", minutes=60)
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "WEEK", [late])
    monkeypatch.setattr(module, "BY_UUID", {late.uuid: late})
    pacific, real, used = ZoneInfo("America/Los_Angeles"), week.channel.upload_video, Counter()

    def one_a_day(*args, **kwargs):
        day = _Clock.at.astimezone(pacific).date()
        if kwargs.get("session_uri") is None:
            if used[day] >= 1:
                raise RuntimeError("uploadLimitExceeded: the user has exceeded the number of videos they may upload")
            used[day] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(yt, "upload_video", one_a_day)
    _live_the_week(week)
    videos = _videos(week)
    full = next(v for v in videos if v.kind == "full")
    shorts = [v for v in videos if v.kind == "short"]
    assert shorts, _timetable(videos)
    hours = pub_settings.current().shorts_after_full_hours
    for s in shorts:
        assert s.publish_at - full.publish_at >= timedelta(hours=hours), _timetable(videos)
