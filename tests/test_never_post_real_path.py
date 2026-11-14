"""The never-post list on the real path: the app's own autopilot, Zoom client,
job queue, routes and files, with only the outside world faked (Zoom's servers
answer with Zoom's real JSON over a mock transport, cutting a video is skipped,
and YouTube is tests/fake_channel.py). What happened on a real server is what
these tests replay: a new deploy over a live state file, crashes mid-upload,
restarts, Zoom outages, damaged files and two people saving at once."""

import dataclasses
import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from api.jobs import job_store
from api.queue import reconcile_on_startup
from autopilot import emails, never_post
from autopilot.engine import OLD_GONE_ERROR
from autopilot.store import store as autopilot_store
from core.config import get_settings
from core.models import JobRecord, JobStatus
from ingest import zoom as zoom_module
from ledger import files, prefs, uploads
from ledger.uploads import get_ledger
from publish import settings
from tests.fake_channel import Crash, FakeChannel
from tests.test_autopilot import PM, T0, World, _meeting, _signed
from tests.test_publish import _finished_job

_current = settings.current  # the real one, kept before a test swaps it

BUDDY = "zoom.buddy@flolabsinnovations.com"
OPS, PROJECTS, LEGAL = 84116996599, 85138528372, 89716953476


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def zoom_recording(uuid, topic, meeting_id, hours_ago=3.0, host=PM, transcript=True, minutes=60):
    """A meeting as Zoom's API sends it (GET /users/{email}/recordings)."""
    start = datetime.now(UTC) - timedelta(hours=hours_ago)
    end = start + timedelta(minutes=minutes)

    def file(number, file_type, extension, recording_type):
        return {"id": f"file-{uuid}-{number}", "meeting_id": uuid, "recording_start": _stamp(start),
                "recording_end": _stamp(end), "file_type": file_type, "file_extension": extension,
                "file_size": 123456, "play_url": f"https://zoom.us/rec/play/{number}",
                "download_url": f"https://zoom.us/rec/download/{number}", "status": "completed",
                "recording_type": recording_type}

    files_ = [file(1, "MP4", "MP4", "shared_screen_with_speaker_view"), file(2, "M4A", "M4A", "audio_only"),
              file(3, "TIMELINE", "JSON", "timeline")]
    if transcript:
        files_.append(file(4, "TRANSCRIPT", "VTT", "audio_transcript"))
    # the fields real Zoom sends (checked against its real answers on 2026-11-05); host_email is
    # not one of them, the fake servers only use it to know whose list a meeting is in
    meeting = {"uuid": uuid, "id": meeting_id, "account_id": "aCcOuNt", "host_id": "hOsTiD", "host_email": host,
               "type": 8, "start_time": _stamp(start), "timezone": "Asia/Karachi", "duration": minutes,
               "total_size": 987654321, "recording_count": len(files_), "share_url": "https://zoom.us/rec/share/x",
               "password": "pw", "recording_play_passcode": "code", "recording_files": files_}
    if topic is not None:
        meeting["topic"] = topic
    return meeting


class ZoomServers:
    """Zoom's API, by its own rules: a UUID that starts with "/" or holds "//"
    must arrive double-encoded, or Zoom answers 3301."""

    def __init__(self):
        self.recordings: dict[str, dict] = {}
        self.not_in_the_list: set[str] = set()
        self.asked_about: list[str] = []
        self.down = False

    def add(self, *recordings):
        for recording in recordings:
            self.recordings[recording["uuid"]] = recording

    @staticmethod
    def _as_sent(recording: dict) -> dict:
        return {key: value for key, value in recording.items() if key != "host_email"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode().split("?")[0]
        if request.url.host == "zoom.us" and path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        if self.down:
            return httpx.Response(503, json={"code": 503, "message": "Service unavailable"})
        found = re.fullmatch(r"/v2/meetings/(.+)/recordings", path)
        if found:
            once = unquote(found[1])
            uuid = unquote(once) if "%" in once else once
            self.asked_about.append(uuid)
            needs_double = uuid.startswith("/") or "//" in uuid
            if uuid not in self.recordings or (needs_double and "%" not in once):
                return httpx.Response(404, json={"code": 3301, "message": "This recording does not exist."})
            return httpx.Response(200, json=self._as_sent(self.recordings[uuid]))
        listing = re.fullmatch(r"/v2/users/([^/]+)/recordings", path)
        if listing:
            email = unquote(listing[1])
            rows = [self._as_sent(r) for r in self.recordings.values()
                    if r["host_email"] == email and r["uuid"] not in self.not_in_the_list]
            return httpx.Response(200, json={"from": request.url.params.get("from"), "to": request.url.params.get("to"),
                                             "page_size": 300, "total_records": len(rows), "next_page_token": "",
                                             "meetings": rows})
        if re.fullmatch(r"/v2/users/[^/]+/meetings", path):
            return httpx.Response(200, json={"page_size": 300, "next_page_token": "", "meetings": []})
        return httpx.Response(404, json={"code": 1001, "message": f"no such route {path}"})


def _cut_the_video(job, update):
    """Stands in for the hours of real cutting: a finished job with real files."""
    done = _finished_job(job.job_id)
    done.title, done.zoom_meeting, done.zoom_meeting_uuid = job.title, job.zoom_meeting, job.zoom_meeting_uuid
    done.zoom_options_hash, done.options = job.zoom_options_hash, job.options
    job_store.update(done)


class Server:
    def __init__(self, zoom, channel):
        self.zoom, self.channel = zoom, channel
        self.engine = api_main.autopilot_engine
        self.client = TestClient(api_main.app)

    def settle(self):
        """The autopilot's minute-by-minute steps, until nothing changes any more."""
        before = None
        for _ in range(80):
            self.engine.step()
            assert api_main.job_queue.wait_idle(15), "a job never ended"
            for _ in range(500):
                if not self.engine._posting:
                    break
                time.sleep(0.01)
            now = {u: (m.state, m.job_id) for u, m in autopilot_store.read().meetings.items()}
            if now == before:
                return
            before = now
        pytest.fail(f"the autopilot never settled: {before}")

    def state(self, uuid):
        return autopilot_store.read().meetings[uuid]

    def titles(self):
        return [v["title"] for v in self.channel.videos.values()]

    def on_channel(self, topic):
        return sum(1 for title in self.titles() if title.startswith(topic))

    def jobs_of(self, uuid):
        return [j for j in map(job_store.get, job_store.list_ids()) if j and j.zoom_meeting_uuid == uuid]

    def restart(self):
        """A new process, as after a deploy: everything is read from disk again."""
        autopilot_store._state = None
        uploads._registry.clear()
        reconcile_on_startup(job_store)


@pytest.fixture
def server(monkeypatch):
    for name, value in (("ZOOM_ACCOUNT_ID", "acct"), ("ZOOM_CLIENT_ID", "cid"), ("ZOOM_CLIENT_SECRET", "secret"),
                        ("ZOOM_DEFAULT_EMAIL", PM), ("AUTOPILOT_CHECK_MINUTES", "0"),
                        ("AUTOPILOT_START_AFTER", (datetime.now(UTC) - timedelta(days=30)).date().isoformat()),
                        ("ZOOM_WEBHOOK_SECRET_TOKEN", "whsec")):
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    # titles must start with the meeting name and videos go up public at once here; pinned without touching the saved prefs
    monkeypatch.setattr(settings, "current", lambda: dataclasses.replace(_current(), title_style="meeting_date", scheduled=False))
    zoom = ZoomServers()
    client = zoom_module.ZoomClient(transport=httpx.MockTransport(zoom), sleep=lambda seconds: None)
    monkeypatch.setattr(zoom_module, "get_client", lambda: client)
    monkeypatch.setattr(api_main, "run_zoom_pipeline", _cut_the_video)
    channel = FakeChannel().install(monkeypatch)
    autopilot_store._state = None
    running = Server(zoom, channel)
    running.engine.set_enabled(True)
    yield running
    assert api_main.job_queue.wait_idle(15), "a test left a job running"


# ------------------------------------------------------------------ a real week


def test_a_real_week_of_meetings_on_the_real_path(server):
    private = {
        "/ops+1//Mon==": ("Operations team meeting", OPS, 90),
        "ops2/Wed+x==": ("Operations team meeting", OPS, 60),
        "//ops3==": ("Operations team meeting", OPS, 30),
        "/pm1==": ("Project Management Team meeting", PROJECTS, 80),
        "pm2==": ("Project Management Team meeting", PROJECTS, 20),
        "legal1==": ("Ethics & Legal Team meeting", LEGAL, 70),
        "hr1==": ("Someone and HR Team", 82348993968, 50),
        "one1==": ("Someone: 1 to 1 meeting", 84506002536, 40),
    }
    team = {
        "/space//1==": ("Space Team meeting", 81929595102, 85),
        "design+1==": ("Design Team", 87096814480, 65),
        "brain/1==": ("FloBrain Weekly Backend Meeting", 89952328054, 45),
        "gtm1==": ("GTM & Influencers Team meeting", 87437018817, 25),
    }
    for uuid, (topic, meeting_id, hours_ago) in {**private, **team}.items():
        server.zoom.add(zoom_recording(uuid, topic, meeting_id, hours_ago=hours_ago))
    server.settle()

    for uuid, (topic, *_rest) in team.items():
        assert server.state(uuid).state == "posted", topic
        assert server.on_channel(topic) == 1
    assert len(server.titles()) == 4 * len(team)  # three shorts and the full video of each
    assert all(video["privacy"] == "public" for video in server.channel.videos.values())
    for uuid, (topic, *_rest) in private.items():
        m = server.state(uuid)
        assert m.state == "skipped" and m.error.startswith("Never posted: "), topic
        assert server.on_channel(topic) == 0
        assert server.jobs_of(uuid) == [], f"{topic}: a job was started"
        assert uuid not in server.zoom.asked_about, f"{topic}: Zoom was asked for its files"
        assert get_ledger().meeting_state(f"zoom:{uuid}") != "posted"


def test_nothing_changes_however_many_times_it_looks_again(server):
    server.zoom.add(zoom_recording("ops==", "Operations team meeting", OPS),
                    zoom_recording("team==", "Space Team meeting", 81929595102))
    server.settle()
    before = sorted(server.titles())
    for _ in range(5):
        server.settle()
    assert sorted(server.titles()) == before and server.state("ops==").state == "skipped"


def test_a_session_recorded_later_is_stopped_too(server):
    server.zoom.add(zoom_recording("ops1==", "Operations team meeting", OPS, hours_ago=30))
    server.settle()
    server.zoom.add(zoom_recording("ops2==", "Operations team meeting", OPS, hours_ago=1))
    server.settle()
    assert server.state("ops2==").state == "skipped" and server.titles() == []


def test_a_new_zoom_meeting_someone_titles_their_own_way_is_stopped(server):
    server.zoom.add(zoom_recording("a==", "PM Team Meeting", 11111111111), zoom_recording("b==", "Ops sync", 22222222222),
                    zoom_recording("c==", "1on1 Someone", 33333333333), zoom_recording("d==", "Human Resources", 44444444444))
    server.settle()
    assert all(server.state(u).state == "skipped" for u in ("a==", "b==", "c==", "d==")) and server.titles() == []


def test_a_renamed_session_of_a_repeating_meeting_is_stopped_by_its_id(server):
    server.zoom.add(zoom_recording("r==", "Monday catch-up", OPS))
    server.settle()
    assert server.state("r==").state == "skipped" and "84116996599" in server.state("r==").error
    assert server.titles() == []


def test_a_recording_with_no_title_is_judged_by_its_id(server):
    server.zoom.add(zoom_recording("none==", None, PROJECTS), zoom_recording("other==", None, 81929595102))
    server.settle()
    assert server.state("none==").state == "skipped" and server.jobs_of("none==") == []
    assert server.state("other==").state == "posted"


def test_a_transcript_still_being_made_does_not_change_anything(server):
    server.zoom.add(zoom_recording("ops==", "Operations team meeting", OPS, hours_ago=2, transcript=False))
    server.settle()
    assert server.state("ops==").state == "skipped" and server.jobs_of("ops==") == []


def test_a_second_account_on_autopilot_keeps_its_hr_meetings_off(server):
    emails.set_autopilot(BUDDY, True)
    server.zoom.add(zoom_recording("hr==", "Someone: FloLabs HR Meeting", 83859306677, host=BUDDY),
                    zoom_recording("op==", "Operations: Team Chat - A & B", 85035852933, host=BUDDY))
    server.settle()
    assert server.state("hr==").state == "skipped" and server.state("op==").state == "skipped"
    assert server.titles() == []


def test_what_the_list_can_not_know_a_meeting_named_only_after_people(server):
    """This is why that account stays unticked: the title says nothing of what it is."""
    emails.set_autopilot(BUDDY, True)
    server.zoom.add(zoom_recording("x==", "Someone and Someone Else", 85790752587, host=BUDDY))
    server.settle()
    assert server.state("x==").state == "posted"


# ------------------------------------------------------------------ Zoom's webhook


def test_zoom_webhook_about_a_listed_meeting_on_the_real_route(server):
    """Real route, real signature. Zoom's answer about one meeting names no host,
    so a meeting known only from the webhook isn't followed until the host's list
    shows it; by then it is stopped like any other."""
    server.zoom.add(zoom_recording("/hook//ops==", "Operations team meeting", OPS))
    server.zoom.not_in_the_list.add("/hook//ops==")
    server.engine.step()
    for event in ("recording.completed", "recording.transcript_completed"):
        raw, headers = _signed({"event": event, "payload": {"object": {"uuid": "/hook//ops==", "id": OPS}}})
        assert server.client.post("/zoom/webhook", content=raw, headers=headers).status_code == 200
        server.settle()
    assert "/hook//ops==" not in autopilot_store.read().meetings and server.jobs_of("/hook//ops==") == []
    server.zoom.not_in_the_list.clear()
    raw, headers = _signed({"event": "recording.completed", "payload": {"object": {"uuid": "/hook//ops==", "id": OPS}}})
    assert server.client.post("/zoom/webhook", content=raw, headers=headers).status_code == 200
    server.settle()
    assert server.state("/hook//ops==").state == "skipped" and server.titles() == []
    assert server.jobs_of("/hook//ops==") == []


def test_zoom_webhook_about_a_team_meeting_still_posts_it(server):
    server.zoom.add(zoom_recording("/hook//team==", "Space Team meeting", 81929595102))
    server.engine.step()
    raw, headers = _signed({"event": "recording.completed", "payload": {"object": {"uuid": "/hook//team==", "id": 1}}})
    assert server.client.post("/zoom/webhook", content=raw, headers=headers).status_code == 200
    server.settle()
    assert server.state("/hook//team==").state == "posted" and server.on_channel("Space Team meeting") == 1


def test_zoom_webhook_about_a_waiting_meeting_renamed_onto_the_list_stops_it(server):
    """Its first look found no transcript; Zoom's webhook brings the transcript and a new title."""
    server.zoom.add(zoom_recording("w==", "Monday catch-up", 77777777777, transcript=False, hours_ago=2))
    server.settle()
    assert server.state("w==").state == "waiting_transcript"
    server.zoom.add(zoom_recording("w==", "Operations team meeting", 77777777777, hours_ago=2))
    raw, headers = _signed({"event": "recording.transcript_completed", "payload": {"object": {"uuid": "w==", "id": 1}}})
    assert server.client.post("/zoom/webhook", content=raw, headers=headers).status_code == 200
    server.settle()
    assert server.state("w==").state == "skipped" and server.jobs_of("w==") == [] and server.titles() == []


# ------------------------------------------------------------------ the deploy itself


def _row(uuid, topic, state, **extra):
    """A meeting as the version on the server now wrote it: no meeting_id yet."""
    start = datetime.now(UTC) - timedelta(hours=5)
    return {"uuid": uuid, "host_email": PM, "topic": topic, "start_time": _stamp(start),
            "recording_end": _stamp(start + timedelta(hours=1)), "state": state, "transcript": "zoom",
            "deadline_at": _stamp(start + timedelta(hours=11)), "job_id": None, "by_hand": False, "attempts": 1,
            "post_attempts": 0, "next_try_at": None, "error": None, "not_found_since": None,
            "first_seen_at": _stamp(start), "updated_at": _stamp(start), "posted_at": None, **extra}


def _deploy_over(server, rows, **state):
    files.write_json(files.state_dir() / "autopilot.json", {
        "enabled": True, "paused": False, "start_after": _stamp(datetime.now(UTC) - timedelta(days=30)),
        "meetings": {row["uuid"]: row for row in rows}, **state})
    server.restart()


def test_deploying_over_the_servers_live_file_stops_every_listed_meeting_at_every_stage(server):
    job_store.create(JobRecord(job_id="ops-cutting", status=JobStatus.DECIDING, zoom_meeting_uuid="ops-processing"))
    finished = _finished_job("ops-done")
    finished.zoom_meeting_uuid = "ops-posting"
    job_store.update(finished)
    _deploy_over(server, [
        _row("ops-waiting", "Operations team meeting", "waiting_recording"),
        _row("ops-transcript", "Operations team meeting", "waiting_transcript"),
        _row("pm-ready", "Project Management Team meeting", "ready"),
        _row("pm-retry", "Project Management Team meeting", "ready", next_try_at=_stamp(datetime.now(UTC))),
        _row("ops-processing", "Operations team meeting", "processing", job_id="ops-cutting"),
        _row("ops-posting", "Operations team meeting", "posting", job_id="ops-done"),
        _row("legal-attention", "Ethics & Legal Team meeting", "needs_attention", error="Some videos did not go up."),
        _row("ops-old-gone", "Operations team meeting", "skipped", error=OLD_GONE_ERROR),
        _row("pm-old-gone", "Project Management Team meeting", "skipped", error=OLD_GONE_ERROR),
    ])
    server.settle()
    for uuid in ("ops-waiting", "ops-transcript", "pm-ready", "pm-retry", "ops-processing", "ops-posting",
                 "legal-attention", "ops-old-gone", "pm-old-gone"):
        m = server.state(uuid)
        assert m.state == "skipped" and m.error.startswith("Never posted: "), uuid
    assert server.titles() == []
    assert not any(r for r in server.jobs_of("pm-ready"))


def test_the_deploy_leaves_everything_else_alone(server):
    server.zoom.add(zoom_recording("team-ready", "Space Team meeting", 81929595102),
                    zoom_recording("team-old-gone", "Design Team", 87096814480),
                    zoom_recording("team-person", "Humanoid Team Meeting", 87633045920))
    _deploy_over(server, [
        _row("team-ready", "Space Team meeting", "ready"),
        _row("team-old-gone", "Design Team", "skipped", error=OLD_GONE_ERROR),
        _row("team-person", "Humanoid Team Meeting", "skipped", error="Skipped by a person."),
        _row("ops-posted", "Operations team meeting", "posted", posted_at=_stamp(datetime.now(UTC))),
    ])
    server.settle()
    assert server.state("team-ready").state == "posted" and server.on_channel("Space Team meeting") == 1
    assert server.state("team-old-gone").state == "posted" and server.on_channel("Design Team") == 1
    assert server.state("team-person").state == "skipped" and server.state("team-person").error == "Skipped by a person."
    # what is already on YouTube is a person's to take down: never marked as anything else
    assert server.state("ops-posted").state == "posted"


def test_the_list_holds_from_the_first_step_even_if_the_autopilot_is_paused_at_the_deploy(server):
    _deploy_over(server, [_row("ops-ready", "Operations team meeting", "ready")], paused=True)
    server.engine.step()
    assert server.state("ops-ready").state == "skipped"
    server.client.post("/autopilot/resume")
    server.settle()
    assert server.titles() == [] and server.jobs_of("ops-ready") == []


def test_an_old_record_without_an_id_is_caught_as_soon_as_zoom_gives_it(server):
    server.zoom.add(zoom_recording("renamed", "Monday catch-up", OPS))
    _deploy_over(server, [_row("renamed", "Monday catch-up", "ready")])
    server.settle()
    assert server.state("renamed").state == "skipped" and server.state("renamed").meeting_id == str(OPS)
    assert server.jobs_of("renamed") == [] and server.titles() == []


def test_an_old_waiting_record_learns_its_id_from_zooms_answer_when_the_list_no_longer_shows_it(server):
    """Too old for the list's look-back, so only Zoom's answer about it gives the ID away."""
    server.zoom.add(zoom_recording("old-renamed", "Monday catch-up", OPS, transcript=False))
    server.zoom.not_in_the_list.add("old-renamed")
    _deploy_over(server, [_row("old-renamed", "Monday catch-up", "waiting_transcript")])
    server.settle()
    m = server.state("old-renamed")
    assert m.state == "skipped" and m.meeting_id == str(OPS) and server.jobs_of("old-renamed") == []


def test_no_check_due_the_last_look_still_stops_a_renamed_meeting(server, monkeypatch):
    """No look at Zoom is due, so only the job learns its real ID: it is cut, never posted."""
    monkeypatch.setenv("AUTOPILOT_CHECK_MINUTES", "600")
    get_settings.cache_clear()
    server.zoom.add(zoom_recording("renamed", "Monday catch-up", OPS))
    _deploy_over(server, [_row("renamed", "Monday catch-up", "ready")], last_check_at=_stamp(datetime.now(UTC)),
                 schedule_checked_at=_stamp(datetime.now(UTC)))
    server.settle()
    m = server.state("renamed")
    assert m.state == "skipped" and "84116996599" in m.error and server.titles() == []
    assert len(server.jobs_of("renamed")) == 1  # the job existed; nothing of it went up


# ------------------------------------------------------------------ crashes mid-upload


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


def _cut(world, uuid, topic, meeting_id=OPS):
    meeting = _meeting(uuid, T0 - timedelta(hours=2), topic=topic)
    meeting["id"] = meeting_id
    world.zoom.meetings[uuid] = meeting
    world.step()
    job = world.finish(world.meeting(uuid).job_id, uuid)
    job.title, job.zoom_meeting = topic, {"topic": topic, "meeting_id": meeting_id}
    job_store.update(job)
    return job


@pytest.mark.parametrize("point", ["before_link", "after_link", "mid_upload", "after_last_byte"])
def test_an_upload_cut_off_by_a_crash_is_never_finished_once_it_is_listed(world, point):
    """Uploading under the old version when the server restarted for the deploy."""
    never_post.set_entries([])
    _cut(world, "m1", "Operations team meeting")
    world.channel.crash_at = point
    with pytest.raises(Crash):
        world.step(minutes=1)
    videos_at_the_crash = len(world.channel.videos)
    never_post.set_entries(list(never_post.DEFAULT_ENTRIES))
    world.restart()
    for minutes in (1, 30, 60, 600):
        world.step(minutes=minutes)
    assert world.meeting("m1").state == "skipped" and len(world.channel.videos) == videos_at_the_crash


def test_an_upload_running_when_it_is_listed_ends_that_round_and_no_more_follow(world):
    never_post.set_entries([])
    _cut(world, "m1", "Operations team meeting")
    held = []
    world.engine._background = held.append
    world.step(minutes=1)
    assert world.meeting("m1").state == "posting" and len(held) == 1  # the upload thread is running
    never_post.set_entries(list(never_post.DEFAULT_ENTRIES))
    world.step(minutes=1)
    assert world.meeting("m1").state == "posting"  # not called back, and not marked as stopped when it isn't
    world.channel.crash_at, world.channel.raise_with = "mid_upload", RuntimeError  # that round fails
    held[0]()
    videos = len(world.channel.videos)
    assert videos < 4
    for minutes in (1, 31, 61):
        world.step(minutes=minutes)
    assert world.meeting("m1").state == "skipped" and len(world.channel.videos) == videos and len(held) == 1


def test_a_person_settling_a_cut_off_upload_does_not_restart_a_listed_meeting(world):
    never_post.set_entries([])
    job = _cut(world, "m1", "Operations team meeting")
    never_post.set_entries(list(never_post.DEFAULT_ENTRIES))
    with world.engine.store.editing() as state:
        state.meetings["m1"].state, state.meetings["m1"].job_id = "needs_attention", job.job_id
    world.engine.after_check(job.job_id)  # "It's on YouTube" / "Not there, upload again" in the side panel
    assert world.meeting("m1").state == "posting"
    world.step(minutes=1)
    assert world.meeting("m1").state == "skipped" and world.channel.videos == {}


def test_a_failed_job_of_a_listed_meeting_is_not_tried_again(world):
    never_post.set_entries([])
    meeting = _meeting("m1", T0 - timedelta(hours=2), topic="Operations team meeting")
    world.zoom.meetings["m1"] = meeting
    world.step()
    never_post.set_entries(list(never_post.DEFAULT_ENTRIES))
    world.fail(world.meeting("m1").job_id)
    for minutes in (1, 31, 121, 361):
        world.step(minutes=minutes)
    assert world.meeting("m1").state == "skipped" and len(world.submitted) == 1


# ------------------------------------------------------------------ Zoom misbehaving


def test_zoom_being_down_never_lets_a_listed_meeting_through(server):
    server.zoom.add(zoom_recording("team", "Space Team meeting", 81929595102))
    _deploy_over(server, [_row("ops-ready", "Operations team meeting", "ready")])
    server.zoom.down = True
    server.settle()
    assert server.state("ops-ready").state == "skipped" and server.titles() == []
    server.zoom.down = False
    server.settle()
    assert server.state("team").state == "posted" and server.on_channel("Operations") == 0


def test_zoom_saying_not_found_about_a_listed_meeting_changes_nothing(server):
    _deploy_over(server, [_row("ops-gone", "Operations team meeting", "skipped", error=OLD_GONE_ERROR)])
    for _ in range(3):
        server.settle()
    m = server.state("ops-gone")
    assert m.state == "skipped" and m.error.startswith("Never posted: ")


# ------------------------------------------------------------------ a person at the page


def test_post_it_after_all_is_refused_on_the_real_route(server):
    server.zoom.add(zoom_recording("ops==", "Operations team meeting", OPS))
    server.settle()
    refused = server.client.post("/autopilot/meetings/retry", json={"uuid": "ops=="})
    assert refused.status_code == 409 and "Take it off the list first" in refused.json()["detail"]
    server.settle()
    assert server.titles() == [] and server.state("ops==").state == "skipped"


def test_pressing_process_on_a_listed_meeting_makes_a_job_but_the_autopilot_never_posts_it(server):
    server.zoom.add(zoom_recording("ops==", "Operations team meeting", OPS))
    made = server.client.post("/jobs/zoom", json={"meeting_uuid": "ops=="})
    assert made.status_code in (200, 202)
    server.settle()
    assert job_store.get(made.json()["job_id"]).status == JobStatus.DONE
    assert server.state("ops==").state == "skipped" and server.titles() == []


def test_a_change_on_the_page_applies_and_taking_one_off_the_list_lets_it_be_posted(server):
    server.zoom.add(zoom_recording("space==", "Space Team meeting", 81929595102, hours_ago=2),
                    zoom_recording("ops==", "Operations team meeting", OPS, hours_ago=3))
    entries = server.client.get("/autopilot").json()["never_post"]
    assert server.client.post("/autopilot/never-post", json={"entries": [*entries, "space team"]}).status_code == 200
    server.settle()
    assert server.state("space==").state == "skipped" and server.titles() == []
    assert server.client.post("/autopilot/never-post", json={"entries": entries}).status_code == 200
    server.settle()
    assert server.state("space==").state == "skipped"  # never comes back by itself
    assert server.client.post("/autopilot/meetings/retry", json={"uuid": "space=="}).status_code == 200
    server.settle()
    assert server.state("space==").state == "posted" and server.on_channel("Space Team meeting") == 1
    assert server.on_channel("Operations") == 0


def test_emptying_the_list_on_the_page_is_a_choice_that_survives_a_restart(server):
    assert server.client.post("/autopilot/never-post", json={"entries": []}).json()["never_post"] == []
    server.restart()
    assert server.client.get("/autopilot").json()["never_post"] == []


def test_the_list_needs_the_api_key_like_every_other_setting(server, monkeypatch):
    monkeypatch.setenv("HC_API_TOKEN", "api-key")
    get_settings.cache_clear()
    assert server.client.post("/autopilot/never-post", json={"entries": []}).status_code == 401
    assert server.client.get("/autopilot").status_code == 401
    assert never_post.entries() == list(never_post.DEFAULT_ENTRIES)
    saved = server.client.post("/autopilot/never-post", json={"entries": ["board"]}, headers={"X-API-Key": "api-key"})
    assert saved.status_code == 200 and never_post.entries() == ["board"]


@pytest.mark.parametrize("body, status", [
    ({"entries": "operations"}, 422),
    ({"entries": [1, 2]}, 422),
    ({}, 422),
    ({"entries": ["x"] * 501}, 422),
    ({"entries": ["&&"]}, 400),
    ({"entries": [".*"]}, 400),
    ({"entries": ["y" * 101]}, 400),
])
def test_bad_input_from_the_page_changes_nothing(server, body, status):
    assert server.client.post("/autopilot/never-post", json=body).status_code == status
    assert never_post.entries() == list(never_post.DEFAULT_ENTRIES)


def test_what_a_browser_on_windows_sends_is_cleaned(server):
    saved = server.client.post("/autopilot/never-post", json={
        "entries": ["  Board   meeting\r", "\tboard meeting", "<img src=x onerror=alert(1)>", "c++ guild"]})
    assert saved.json()["never_post"] == ["Board meeting", "<img src=x onerror=alert(1)>", "c++ guild"]
    assert never_post.match("The Board-Meeting") is not None


# ------------------------------------------------------------------ the files


def test_the_list_survives_a_restart(server):
    never_post.set_entries(["board"])
    server.restart()
    assert never_post.entries() == ["board"] and server.client.get("/autopilot").json()["never_post"] == ["board"]


def test_a_damaged_settings_file_falls_back_to_the_backup_then_the_built_in_list(server):
    never_post.set_entries(["board"])
    main = files.state_dir() / prefs.FILE
    main.write_text('{"never_post": ["boa', encoding="utf-8")  # a half-written file
    assert never_post.entries() == ["board"]
    (files.backup_dir() / prefs.FILE).unlink()
    assert never_post.entries() == list(never_post.DEFAULT_ENTRIES)
    main.unlink()
    assert never_post.entries() == list(never_post.DEFAULT_ENTRIES)
    assert never_post.match("Operations team meeting") is not None


def test_people_saving_at_the_same_moment_lose_nothing(server):
    errors, seen = [], []

    def saves(fn, *args):
        try:
            for _ in range(40):
                fn(*args)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def reads():
        try:
            for _ in range(200):
                seen.append(tuple(never_post.entries()))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    workers = [threading.Thread(target=saves, args=(never_post.set_entries, ["board"])),
               threading.Thread(target=saves, args=(prefs.set_retention_hours, 12.0)),
               threading.Thread(target=saves, args=(never_post.set_entries, ["board"])),
               threading.Thread(target=reads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert errors == []
    assert prefs.retention_hours() == 12.0 and never_post.entries() == ["board"]
    # a reader sees the built-in list or the new one, never an empty or half-written one
    assert set(seen) <= {tuple(never_post.DEFAULT_ENTRIES), ("board",)}
    saved = json.loads((files.state_dir() / prefs.FILE).read_text(encoding="utf-8"))
    saved.pop(never_post.DEFAULTS_V2_FLAG, None)  # the one-time note that the 2026-11-14 words were dealt with
    assert saved == {"job_retention_hours": 12.0, "never_post": ["board"]}


def test_two_saves_at_the_very_same_moment_keep_both(server, monkeypatch):
    """Each save rewrites the whole file: without the lock, the slower one wipes out the other."""
    read = prefs._read

    def slow_read():
        data = read()
        time.sleep(0.05)  # both have read the file before either writes
        return data

    def save(start, fn, *args):
        start.wait()
        fn(*args)

    monkeypatch.setattr(prefs, "_read", slow_read)
    for round_number in range(3):
        files.write_json(files.state_dir() / prefs.FILE, {})
        start = threading.Barrier(2)
        workers = [threading.Thread(target=save, args=(start, never_post.set_entries, [f"board{round_number}"])),
                   threading.Thread(target=save, args=(start, prefs.set_retention_hours, 12.0))]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        saved = json.loads((files.state_dir() / prefs.FILE).read_text(encoding="utf-8"))
        saved.pop(never_post.DEFAULTS_V2_FLAG, None)  # the one-time note that the 2026-11-14 words were dealt with
        assert saved == {"never_post": [f"board{round_number}"], "job_retention_hours": 12.0}


def test_a_long_list_and_a_long_queue_stay_quick(server):
    never_post.set_entries([f"word{i}" for i in range(490)] + ["operations"])
    _deploy_over(server, [_row(f"m{i}", f"Team {i} meeting" if i % 2 else f"Operations {i}", "waiting_recording")
                          for i in range(300)])
    started = time.perf_counter()
    with autopilot_store.editing() as state:
        server.engine._drop_never_post(state, datetime.now(UTC))
    assert time.perf_counter() - started < 3.0
    states = {m.uuid: m.state for m in autopilot_store.read().meetings.values()}
    assert sum(1 for s in states.values() if s == "skipped") == 150
