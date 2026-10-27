"""The autopilot end to end, with a fake Zoom account, a fake YouTube channel
(tests/fake_channel.py) and a clock the tests move: it waits for Zoom's
transcript, falls back to WhisperX after the wait, starts one job at a time,
never checks Zoom during a job, posts everything once, retries failures, and
picks up where it stopped after a restart."""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from functools import partial

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from api.queue import reconcile_on_startup
from autopilot import emails, webhook
from autopilot.engine import Engine, Hooks, iso
from autopilot.store import Store
from core.config import get_settings
from core.errors import PipelineError
from core.models import JobRecord, JobStatus
from ingest import zoom
from ledger import uploads
from ledger.uploads import get_ledger
from tests.fake_channel import Crash, FakeChannel
from tests.test_publish import _finished_job

PM = "pm@flomad.com"
T0 = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)  # 15:00 in Karachi


def _meeting(uuid, start, minutes=60, transcript=True, video_ready=True, host=PM, topic="Weekly Sync"):
    end = start + timedelta(minutes=minutes)
    files = [{"file_type": "MP4", "recording_type": "active_speaker",
              "status": "completed" if video_ready else "processing",
              "download_url": "https://zoom.us/rec/v" if video_ready else "",
              "recording_start": iso(start), "recording_end": iso(end)}]
    if transcript:
        files.append({"file_type": "TRANSCRIPT", "recording_type": "audio_transcript", "status": "completed",
                      "download_url": "https://zoom.us/rec/t", "recording_start": iso(start),
                      "recording_end": iso(end)})
    return {"uuid": uuid, "id": 123, "topic": topic, "start_time": iso(start), "duration": minutes,
            "host_email": host, "recording_files": files}


class FakeZoom:
    def __init__(self):
        self.meetings = {}
        self.upcoming = {}
        self.schedule_error = None
        self.calls = []

    def list_recordings(self, email, from_date, to_date):
        self.calls.append(("list", email))
        rows = []
        for m in self.meetings.values():
            day = datetime.fromisoformat(m["start_time"]).date()
            if m["host_email"] == email and from_date <= day <= to_date:
                rows.append(zoom.summarize(m))
        return rows

    def get_meeting(self, uuid):
        self.calls.append(("get", uuid))
        if uuid not in self.meetings:
            raise PipelineError("Zoom: recording not found", code="zoom_not_found")
        return self.meetings[uuid]

    def list_upcoming_meetings(self, email):
        self.calls.append(("schedule", email))
        if self.schedule_error is not None:
            raise self.schedule_error
        return self.upcoming.get(email, [])


class World:
    """The autopilot with everything around it faked."""

    def __init__(self, monkeypatch):
        monkeypatch.setenv("AUTOPILOT_ENABLED", "true")
        get_settings.cache_clear()
        self.now = T0
        self.zoom = FakeZoom()
        self.channel = FakeChannel().install(monkeypatch)
        self.busy = False
        self.submitted = []
        self.manual = {}
        self.engine = self.restart()

    def restart(self) -> Engine:
        """A new process: the autopilot's file and the ledger are read again,
        and the job records are reconciled as at every startup."""
        uploads._registry.clear()
        reconcile_on_startup(api_main.job_store)
        self.engine = Engine(
            Hooks(worker_busy=lambda: self.busy, submit=self.submit, find_job=lambda uuid: self.manual.get(uuid),
                  get_job=api_main.job_store.get, update_job=api_main.job_store.update),
            store=Store(), zoom_client=self.zoom, clock=lambda: self.now, run_in_background=lambda fn: fn(),
        )
        return self.engine

    def submit(self, uuid, allow_asr):
        if self.busy:
            return None
        job_id = f"job{len(self.submitted) + 1}"
        self.submitted.append((uuid, allow_asr))
        api_main.job_store.create(JobRecord(job_id=job_id, status=JobStatus.QUEUED, zoom_meeting_uuid=uuid))
        return job_id

    def finish(self, job_id, uuid, **kwargs) -> JobRecord:
        job = _finished_job(job_id, **kwargs)
        job.zoom_meeting_uuid = uuid
        api_main.job_store.update(job)
        return job

    def fail(self, job_id, error="the AI service failed"):
        job = api_main.job_store.get(job_id)
        job.status, job.error = JobStatus.FAILED, error
        api_main.job_store.update(job)

    def step(self, minutes=0):
        self.now += timedelta(minutes=minutes)
        self.engine.step()

    def meeting(self, uuid):
        return self.engine.store.read().meetings.get(uuid)


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


def _count_lists(world) -> int:
    """How often the autopilot listed an account's recordings (one per check)."""
    return sum(1 for call in world.zoom.calls if call[0] == "list")


# ------------------------------------------------------------------ finding meetings


def test_off_means_nothing_at_all(monkeypatch):
    w = World(monkeypatch)
    monkeypatch.setenv("AUTOPILOT_ENABLED", "false")
    get_settings.cache_clear()
    w.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2))
    w.step()
    assert w.zoom.calls == [] and w.engine.store.read().meetings == {}


def test_a_meeting_with_zoom_transcript_is_processed_at_once(world):
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2))
    world.step()
    assert world.submitted == [("m1", False)]
    assert world.meeting("m1").state == "processing" and world.meeting("m1").transcript == "zoom"


def test_older_recordings_are_never_posted_when_it_is_switched_on(world):
    # the day it is switched on starts at midnight in Karachi (19:00 UTC the day before)
    world.zoom.meetings["old"] = _meeting("old", datetime(2026, 11, 1, 18, 0, tzinfo=UTC))
    world.zoom.meetings["today"] = _meeting("today", datetime(2026, 11, 1, 20, 0, tzinfo=UTC))
    world.step()
    assert set(world.engine.store.read().meetings) == {"today"}


def test_the_start_date_setting_wins(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_START_AFTER", "2026-10-30")
    w = World(monkeypatch)
    w.zoom.meetings["m1"] = _meeting("m1", datetime(2026, 10, 31, 9, 0, tzinfo=UTC))
    w.step()
    assert "m1" in w.engine.store.read().meetings


def test_only_watched_accounts_are_followed(world):
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2), host="zoom.buddy@flolabsinnovations.com")
    world.step()
    assert world.engine.store.read().meetings == {}
    emails.set_autopilot("zoom.buddy@flolabsinnovations.com", True)
    world.step(minutes=30)
    assert "m1" in world.engine.store.read().meetings


def test_it_waits_ten_hours_for_zoom_then_transcribes_itself(world):
    start = T0 - timedelta(hours=2)  # ended an hour ago
    world.zoom.meetings["m1"] = _meeting("m1", start, transcript=False)
    world.step()
    m = world.meeting("m1")
    assert m.state == "waiting_transcript" and world.submitted == []
    assert m.deadline_at == iso(start + timedelta(hours=11))
    world.step(minutes=8 * 60)  # 9 h after the end: still waiting
    assert world.meeting("m1").state == "waiting_transcript" and world.submitted == []
    world.step(minutes=61)  # past the 10 h
    assert world.submitted == [("m1", True)]
    assert world.meeting("m1").transcript == "whisperx"


def test_zoom_transcript_arriving_during_the_wait_is_used(world):
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2), transcript=False)
    world.step()
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2), transcript=True)
    world.step(minutes=30)
    assert world.submitted == [("m1", False)]


def test_a_video_zoom_is_still_making_is_waited_for(world):
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2), video_ready=False)
    world.step()
    assert world.meeting("m1").state == "waiting_recording"
    world.step(minutes=49 * 60)
    assert world.meeting("m1").state == "needs_attention"


def test_a_recording_deleted_in_zoom_is_skipped(world):
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2), transcript=False)
    world.step()
    del world.zoom.meetings["m1"]
    world.step(minutes=30)
    assert world.meeting("m1").state == "skipped"


def test_no_zoom_check_and_no_new_job_while_a_job_runs(world):
    world.busy = True
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2))
    world.step()
    assert world.zoom.calls == [] and world.submitted == []
    world.busy = False
    world.step(minutes=1)
    assert world.submitted == [("m1", False)]


def test_checks_come_every_thirty_minutes(world):
    world.step()
    lists = partial(_count_lists, world)
    assert lists() == 1
    world.step(minutes=29)
    assert lists() == 1
    world.step(minutes=1)
    assert lists() == 2


def test_a_webhook_call_makes_the_check_happen_now(world):
    world.step()
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=1))
    world.engine.nudge("m1")
    world.step(minutes=1)
    assert world.submitted == [("m1", False)]


def test_a_webhook_about_another_account_is_ignored(world):
    world.step()
    world.zoom.meetings["x"] = _meeting("x", T0 - timedelta(hours=1), host="someone@else.com")
    world.engine.nudge("x")
    world.step(minutes=1)
    assert "x" not in world.engine.store.read().meetings and world.engine.store.read().nudges == []


def test_zoom_schedule_makes_a_check_right_after_a_meeting_ends(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_CHECK_MINUTES", "600")  # only the schedule asks for a check here
    world = World(monkeypatch)
    world.zoom.upcoming[PM] = [{"topic": "Standup", "start_time": iso(T0 + timedelta(minutes=20)), "duration": 30}]
    world.step()
    lists = partial(_count_lists, world)
    assert lists() == 1
    world.step(minutes=64)  # the meeting ended 14 minutes ago
    assert lists() == 1
    world.step(minutes=1)  # 15 minutes after its end
    assert lists() == 2
    world.step(minutes=1)
    assert lists() == 2  # used up


def test_a_missing_schedule_permission_is_shown_not_fatal(world):
    world.zoom.schedule_error = PipelineError("Zoom refused", code="zoom_auth")
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2))
    world.step()
    state = world.engine.store.read()
    assert zoom.SCHEDULE_SCOPE in state.schedule_error and world.submitted == [("m1", False)]


def test_paused_does_nothing(world):
    world.engine.set_paused(True)
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2))
    world.step()
    assert world.zoom.calls == [] and world.submitted == []


# ------------------------------------------------------------------ jobs and posting


def _process(world, uuid="m1"):
    world.zoom.meetings[uuid] = _meeting(uuid, T0 - timedelta(hours=2))
    world.step()
    job_id = world.meeting(uuid).job_id
    world.finish(job_id, uuid)
    return job_id


def test_a_finished_job_is_posted_shorts_first_then_the_full_video(world):
    job_id = _process(world)
    world.step(minutes=1)
    assert world.meeting("m1").state == "posted"
    titles = [v["title"] for v in world.channel.videos.values()]
    assert len(titles) == 4  # the three shorts, then the full video
    assert titles[-1].startswith("Weekly Sync")  # the full video last
    assert all(v["privacy"] == "public" for v in world.channel.videos.values())
    job = api_main.job_store.get(job_id)
    assert job.files_deleted  # everything is up, so the files went
    assert get_ledger().meeting_state("zoom:m1") == "posted"


def test_nothing_is_posted_twice_whatever_happens_next(world):
    _process(world)
    world.step(minutes=1)
    before = dict(world.channel.videos)
    for minutes in (1, 30, 600):
        world.step(minutes=minutes)
    world.restart()
    world.step(minutes=1)
    assert world.channel.videos == before


def test_a_crash_mid_posting_resumes_without_a_second_video(world):
    job_id = _process(world)
    world.channel.crash_at = "after_last_byte"
    with pytest.raises(Crash):
        world.step(minutes=1)
    world.restart()
    world.step(minutes=1)
    assert world.meeting("m1").state == "posted"
    # the short that went up just before the crash was found through its link, not sent again
    sessions = {v["session"] for v in world.channel.videos.values()}
    assert len(world.channel.videos) == len(sessions) == 4
    assert world.channel.calls.count("status") == 1
    assert api_main.job_store.get(job_id).files_deleted


def test_a_failed_job_is_tried_again_three_times_then_waits_for_a_person(world):
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2))
    world.step()
    waits = [30, 120, 360]
    for wait in waits:
        world.fail(world.meeting("m1").job_id)
        world.step(minutes=1)
        assert world.meeting("m1").state == "ready"
        world.step(minutes=wait - 1)
        assert world.meeting("m1").state == "ready"  # not yet
        world.step(minutes=1)
        assert world.meeting("m1").state == "processing"
    world.fail(world.meeting("m1").job_id)
    world.step(minutes=1)
    m = world.meeting("m1")
    assert m.state == "needs_attention" and "AI service" in m.error and len(world.submitted) == 4
    # a person presses Retry
    world.engine.retry("m1")
    world.step(minutes=1)
    assert world.meeting("m1").state == "processing" and len(world.submitted) == 5


def test_a_job_someone_started_by_hand_is_posted_not_processed_again(world):
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2))
    job = world.finish("manual1", "m1")
    world.manual["m1"] = job
    world.step()
    assert world.submitted == []
    assert world.meeting("m1").by_hand and world.meeting("m1").job_id == "manual1"
    world.step(minutes=1)
    assert world.meeting("m1").state == "posted" and len(world.channel.videos) == 4


def test_what_a_person_already_posted_is_not_posted_again(world):
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2))
    job = world.finish("manual1", "m1")
    world.manual["m1"] = job
    client = TestClient(api_main.app)
    from tests.test_publish import _connect_fake_channel
    _connect_fake_channel()
    res = client.post("/jobs/manual1/publish", json={"title": "By hand", "privacy": "private", "delete_after": False})
    assert res.status_code == 200
    world.step()
    world.step(minutes=1)
    assert world.meeting("m1").state == "posted"
    assert [v["title"] for v in world.channel.videos.values()].count("By hand") == 1
    assert len(world.channel.videos) == 4  # the full video by hand, the three shorts by the autopilot


def test_a_quota_stop_tries_posting_again_an_hour_later(world):
    _process(world)

    def quota(*args, **kwargs):
        raise RuntimeError("quotaExceeded")

    real = world.channel.upload_video
    import publish.youtube as yt
    yt.upload_video = quota
    try:
        world.step(minutes=1)
        m = world.meeting("m1")
        assert m.state == "posting" and m.post_attempts == 1 and "limit" in m.error
        yt.upload_video = real
        world.step(minutes=30)
        assert world.meeting("m1").state == "posting"
        world.step(minutes=31)
        assert world.meeting("m1").state == "posted"
    finally:
        yt.upload_video = real


def test_an_unsure_upload_waits_for_a_person_then_finishes(world):
    _process(world)
    world.channel.crash_at = "mid_upload"
    with pytest.raises(Crash):
        world.step(minutes=1)
    world.restart()
    world.channel.forget_links()
    world.step(minutes=1)
    m = world.meeting("m1")
    assert m.state == "needs_attention" and "check" in m.error
    [check] = get_ledger().needing_check()
    get_ledger().resolve(check.key, on_youtube=False)
    world.engine.retry("m1")
    world.step(minutes=1)
    assert world.meeting("m1").state == "posted"
    assert len(world.channel.videos) == 4


def test_skip_and_retry_rules(world):
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2), transcript=False)
    world.step()
    world.engine.skip("m1")
    world.step(minutes=30)
    assert world.meeting("m1").state == "skipped" and world.submitted == []
    with pytest.raises(KeyError):
        world.engine.skip("nope")
    world.engine.retry("m1")
    world.step(minutes=1)
    assert world.meeting("m1").state == "waiting_transcript"


def test_a_restart_carries_on_where_it_stopped(world):
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2), transcript=False)
    world.step()
    world.restart()
    world.step(minutes=10 * 60)
    assert world.submitted == [("m1", True)]


# ------------------------------------------------------------------ the email list


def test_pm_is_the_default_and_watched_and_can_not_be_removed():
    rows = emails.list_emails()
    assert rows[0] == {"email": PM, "autopilot": True, "default": True}
    assert {"email": "zoom.buddy@flolabsinnovations.com", "autopilot": False, "default": False} in rows
    with pytest.raises(ValueError):
        emails.remove_email(PM)


def test_emails_are_added_kept_on_the_server_and_removed():
    client = TestClient(api_main.app)
    res = client.post("/zoom/emails", json={"email": " New.Person@Example.com "})
    assert res.status_code == 200
    assert {"email": "new.person@example.com", "autopilot": False, "default": False} in res.json()["emails"]
    assert client.post("/zoom/emails", json={"email": "not an email"}).status_code == 400
    assert client.post("/zoom/emails/autopilot", json={"email": "new.person@example.com", "enabled": True}).json()
    assert "new.person@example.com" in emails.autopilot_emails()
    # survives a restart: it's in the file
    assert "new.person@example.com" in json.dumps(json.loads((get_settings().storage_dir / "state" /
                                                              "emails.json").read_text()))
    assert client.post("/zoom/emails/remove", json={"email": "new.person@example.com"}).status_code == 200
    assert client.post("/zoom/emails/remove", json={"email": PM}).status_code == 400
    assert client.post("/zoom/emails/remove", json={"email": "ghost@example.com"}).status_code == 404


# ------------------------------------------------------------------ Zoom's webhook


def _signed(body: dict, secret="whsec", ts=None):
    raw = json.dumps(body).encode()
    ts = str(ts if ts is not None else int(datetime.now(UTC).timestamp() * 1000))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{raw.decode()}".encode(), hashlib.sha256).hexdigest()
    return raw, {"x-zm-signature": sig, "x-zm-request-timestamp": ts, "content-type": "application/json"}


@pytest.fixture
def webhook_secret(monkeypatch):
    monkeypatch.setenv("ZOOM_WEBHOOK_SECRET_TOKEN", "whsec")
    monkeypatch.setenv("HC_API_TOKEN", "api-key")  # the webhook needs no key
    get_settings.cache_clear()


def test_the_webhook_answers_zoom_url_validation(webhook_secret):
    raw, headers = _signed({"event": "endpoint.url_validation", "payload": {"plainToken": "abc"}})
    res = TestClient(api_main.app).post("/zoom/webhook", content=raw, headers=headers)
    assert res.status_code == 200
    assert res.json() == {"plainToken": "abc",
                          "encryptedToken": hmac.new(b"whsec", b"abc", hashlib.sha256).hexdigest()}


def test_a_signed_recording_event_wakes_the_autopilot(webhook_secret):
    raw, headers = _signed({"event": "recording.completed", "payload": {"object": {"uuid": "u1=="}}})
    res = TestClient(api_main.app).post("/zoom/webhook", content=raw, headers=headers)
    assert res.status_code == 200
    assert api_main.autopilot_engine.store.read().nudges == ["u1=="]


def test_the_webhook_refuses_anything_not_signed_by_zoom(webhook_secret):
    client = TestClient(api_main.app)
    raw, headers = _signed({"event": "recording.completed", "payload": {"object": {"uuid": "u1"}}}, secret="wrong")
    assert client.post("/zoom/webhook", content=raw, headers=headers).status_code == 401
    raw, headers = _signed({"event": "recording.completed"}, ts=1000)  # replayed from long ago
    assert client.post("/zoom/webhook", content=raw, headers=headers).status_code == 401
    assert client.post("/zoom/webhook", content=b"{}").status_code == 401
    assert api_main.autopilot_engine.store.read().nudges == []


def test_the_webhook_is_closed_until_its_secret_is_set():
    raw, headers = _signed({"event": "recording.completed"})
    assert TestClient(api_main.app).post("/zoom/webhook", content=raw, headers=headers).status_code == 503


def test_webhook_signature_follows_zoom_rules():
    import os
    os.environ["ZOOM_WEBHOOK_SECRET_TOKEN"] = "s"
    get_settings.cache_clear()
    try:
        body = b'{"event":"x"}'
        sig = "v0=" + hmac.new(b"s", b"v0:1700000000000:" + body, hashlib.sha256).hexdigest()
        assert webhook.verify(body, sig, "1700000000000", now=1700000000.0) == {"event": "x"}
        with pytest.raises(webhook.WebhookRefused):
            webhook.verify(body, sig, "1700000000000", now=1700000000.0 + 301)
    finally:
        os.environ.pop("ZOOM_WEBHOOK_SECRET_TOKEN")
        get_settings.cache_clear()


# ------------------------------------------------------------------ what the page shows


def test_the_process_button_knows_what_is_posted(world, monkeypatch):
    _process(world)
    world.step(minutes=1)

    class Client:
        def list_recordings(self, host, start, end):
            return [{"uuid": "m1", "topic": "Weekly Sync", "status": "ready"},
                    {"uuid": "m2", "topic": "Other", "status": "ready"}]

    monkeypatch.setattr(zoom, "get_client", lambda: Client())
    monkeypatch.setattr(api_main.autopilot_store, "_state", None)
    api_main.autopilot_store._ensure()
    rows = TestClient(api_main.app).get("/zoom/recordings?host=pm@flomad.com").json()["meetings"]
    assert [(r["uuid"], r["posted"]) for r in rows] == [("m1", "posted"), ("m2", None)]


def test_the_posted_list_groups_videos_by_meeting(world):
    _process(world)
    world.step(minutes=1)
    body = TestClient(api_main.app).get("/posted").json()
    [group] = body["meetings"]
    assert group["meeting"] == "Weekly Sync" and group["full"]["url"].startswith("https://youtu.be/")
    assert [s["short"] for s in group["shorts"]] == [1, 2, 3] and body["needs_check"] == []


def test_the_autopilot_panel_lists_what_needs_a_person_first(world):
    world.zoom.meetings["a"] = _meeting("a", T0 - timedelta(hours=3), transcript=False)
    world.zoom.meetings["b"] = _meeting("b", T0 - timedelta(hours=2), video_ready=False)
    world.step()
    world.engine.skip("b")
    api_main.autopilot_store._state = None  # the panel reads the file the engine wrote
    body = TestClient(api_main.app).get("/autopilot").json()
    assert body["enabled"] and body["watching"] == [PM] and body["privacy"] == "public"
    assert [(m["uuid"], m["label"]) for m in body["meetings"]] == [
        ("a", "waiting for Zoom's transcript"), ("b", "skipped")]


def test_process_shows_the_autopilots_job_instead_of_cutting_twice(world, monkeypatch):
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2))
    world.step()
    job_id = world.meeting("m1").job_id
    monkeypatch.setattr(zoom, "get_client", lambda: world.zoom)
    api_main.autopilot_store._state = None
    res = TestClient(api_main.app).post("/jobs/zoom", json={"meeting_uuid": "m1",
                                                            "options": {"cut_silence": False}})
    assert res.status_code == 200 and res.json()["job_id"] == job_id and res.json()["deduplicated"] is True


# ------------------------------------------------------------------ the job it starts


def test_the_autopilots_job_may_transcribe_and_gets_the_longer_wall_clock(monkeypatch):
    fake = FakeZoom()
    fake.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=12), transcript=False)
    monkeypatch.setattr(zoom, "get_client", lambda: fake)
    queued = []
    monkeypatch.setattr(api_main.job_queue, "submit",
                        lambda job_id, fn, max_depth=None, max_wall_s=None: queued.append((job_id, max_wall_s)))
    job_id = api_main._autopilot_submit("m1", True)
    job = api_main.job_store.get(job_id)
    assert job.options.allow_asr and job.zoom_meeting_uuid == "m1" and job.title == "Weekly Sync"
    assert queued == [(job_id, 21600.0)]
    # the dedupe key is the same as a plain job's: allow_asr is not part of it
    assert job.zoom_options_hash == api_main._options_hash(api_main.JobOptions())
    # a second call finds that job instead of starting another
    assert api_main._autopilot_submit("m1", True) == job_id and len(queued) == 1


def test_a_job_that_transcribes_itself_gets_its_own_wall_clock(monkeypatch):
    from api.jobs import JobStore
    from api.queue import JobQueue

    monkeypatch.setenv("JOB_MAX_WALL_S", "1")
    get_settings.cache_clear()
    queue = JobQueue(JobStore())
    seen = {}

    def run(name):
        queue._running_started -= 5  # it has been running for 5 s
        try:
            queue.check(name)
            seen[name] = "ok"
        except Exception as exc:  # noqa: BLE001
            seen[name] = type(exc).__name__

    queue.submit("asr", lambda: run("asr"), max_wall_s=100)
    assert queue.wait_idle(5)
    queue.submit("plain", lambda: run("plain"))
    assert queue.wait_idle(5)
    assert seen == {"asr": "ok", "plain": "JobTimedOut"}



# ------------------------------------------------------------------ review round 1


def _raise(exc):
    def fail(*args, **kwargs):
        raise exc
    return fail


def test_a_decide_only_job_is_not_adopted_and_never_strands_a_meeting(world):
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2))
    api_main.job_store.create(JobRecord(job_id="picks", status=JobStatus.DECIDED, zoom_meeting_uuid="m1"))
    assert api_main._live_zoom_job("m1") is None
    # and if one is followed anyway, it is let go and the meeting goes on
    world.step()
    job = api_main.job_store.get(world.meeting("m1").job_id)
    job.status = JobStatus.DECIDED
    api_main.job_store.update(job)
    world.step(minutes=1)
    assert world.meeting("m1").state == "ready" and "Render now" in world.meeting("m1").error


def test_the_start_date_setting_always_wins_and_switching_on_again_starts_today(monkeypatch):
    w = World(monkeypatch)
    w.step()
    monkeypatch.setenv("AUTOPILOT_START_AFTER", "2026-11-05")
    get_settings.cache_clear()
    w.zoom.meetings["early"] = _meeting("early", datetime(2026, 11, 3, 9, 0, tzinfo=UTC))
    w.now = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
    w.step()
    assert "early" not in w.engine.store.read().meetings
    # off, then on again two weeks later: nothing recorded while it was off
    monkeypatch.setenv("AUTOPILOT_START_AFTER", "")
    get_settings.cache_clear()
    w.engine.note_switched_off()
    w.zoom.meetings["while_off"] = _meeting("while_off", datetime(2026, 11, 18, 9, 0, tzinfo=UTC))
    w.now = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
    w.step()
    assert "while_off" not in w.engine.store.read().meetings
    assert w.engine.store.read().start_after == "2026-11-19T19:00:00+00:00"  # 20 Nov, Karachi


def test_unticking_an_account_stops_its_waiting_meetings(world):
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2), transcript=False)
    world.step()
    emails.set_autopilot(PM, False)
    world.step(minutes=11 * 60)
    assert world.submitted == [] and world.meeting("m1").state == "skipped"
    assert "no longer on autopilot" in world.meeting("m1").error


def test_process_by_hand_is_not_handed_the_autopilots_failed_job(world, monkeypatch):
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2))
    world.step()
    world.fail(world.meeting("m1").job_id)  # the autopilot is paused, so it stays "processing"
    monkeypatch.setattr(api_main.autopilot_store, "_state", None)
    assert api_main._autopilot_job("m1") is None


def test_a_long_outage_misses_no_meeting(world):
    world.step()
    world.zoom.meetings["m1"] = _meeting("m1", T0 + timedelta(hours=1))
    real = world.zoom.list_recordings
    world.zoom.list_recordings = _raise(PipelineError("down", code="zoom_unavailable"))
    for _ in range(10):  # Zoom refuses every listing for five days
        world.step(minutes=12 * 60)
    world.zoom.list_recordings = real
    world.step(minutes=30)
    assert world.submitted == [("m1", False)]


def test_a_webhook_meeting_zoom_could_not_answer_for_is_looked_at_later(world):
    world.step()
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(minutes=30))
    real = world.zoom.get_meeting
    world.zoom.get_meeting = _raise(PipelineError("down", code="zoom_unavailable"))
    world.zoom.list_recordings = lambda *a: []  # only the webhook knows it
    world.engine.nudge("m1")
    world.step(minutes=1)
    assert world.engine.store.read().nudges_later == ["m1"]
    world.zoom.get_meeting = real
    world.step(minutes=30)
    assert world.submitted == [("m1", False)] and world.engine.store.read().nudges_later == []


def test_settling_a_check_lets_the_autopilot_finish_posting(world, monkeypatch):
    _process(world)
    world.channel.crash_at = "mid_upload"
    with pytest.raises(Crash):
        world.step(minutes=1)
    world.restart()
    world.channel.forget_links()
    world.step(minutes=1)
    assert world.meeting("m1").state == "needs_attention"
    monkeypatch.setattr(api_main, "autopilot_engine", world.engine)
    client = TestClient(api_main.app)
    [check] = client.get("/posted").json()["needs_check"]
    assert check["state"] == "unsure"
    assert client.post("/posted/resolve", json={"key": check["key"], "on_youtube": False}).status_code == 200
    world.step(minutes=1)
    assert world.meeting("m1").state == "posted" and len(world.channel.videos) == 4


def test_a_meeting_already_fully_posted_is_not_processed_again(world):
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2))
    job = world.finish("byhand", "m1")
    get_ledger().record_existing(uploads.full_item(job), title="x", url="https://youtu.be/dQw4w9WgXcQ")
    get_ledger().record_existing(uploads.short_item(job, 1), title="y", url="https://youtube.com/shorts/aaaaaaaaaaa")
    job.files_deleted = True
    api_main.job_store.update(job)
    world.step()
    assert world.submitted == [] and world.meeting("m1").state == "posted"


def test_retry_looks_at_zoom_again_for_a_meeting_that_never_got_ready(world):
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2), video_ready=False)
    world.step()
    world.step(minutes=49 * 60)
    assert world.meeting("m1").state == "needs_attention"
    world.engine.retry("m1")
    world.step(minutes=1)
    assert world.submitted == [] and world.meeting("m1").state == "needs_attention"  # still no video: asked again
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2))
    world.engine.retry("m1")
    world.step(minutes=1)
    assert world.submitted == [("m1", False)]


def test_nothing_new_starts_while_an_update_waits_to_restart_the_app(world):
    from ledger import files

    _process(world)
    flag = files.state_dir() / ".deploying"
    flag.write_text("now")
    world.step(minutes=31)
    assert world.channel.videos == {}
    flag.unlink()
    world.step(minutes=1)
    assert world.meeting("m1").state == "posted"


def test_a_skip_made_while_the_job_starts_is_kept(world):
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2))
    real_submit = world.submit

    def submit_and_skip(uuid, allow_asr):
        job_id = real_submit(uuid, allow_asr)
        world.engine.skip(uuid)  # a person presses Skip meanwhile
        return job_id

    world.engine.hooks.submit = submit_and_skip
    world.step()
    assert world.meeting("m1").state == "skipped"
    world.finish("job1", "m1")
    world.step(minutes=1)
    assert world.channel.videos == {}


def test_the_store_lock_is_released_when_its_file_can_not_be_read(monkeypatch):
    import threading

    store = Store()
    monkeypatch.setattr("autopilot.store.files.read_json", _raise(PermissionError("EACCES")))
    with pytest.raises(PermissionError), store.editing():
        pass
    got = []
    thread = threading.Thread(target=lambda: got.append(store._lock.acquire(timeout=2)))
    thread.start()
    thread.join(5)
    assert got == [True]


def test_the_public_webhook_never_buffers_a_huge_body(webhook_secret):
    def chunks():
        for _ in range(64):
            yield b"x" * 65536  # 4 MB, sent without a Content-Length

    res = TestClient(api_main.app).post("/zoom/webhook", content=chunks())
    assert res.status_code == 413


def test_settling_another_jobs_upload_frees_the_meeting_it_held_up(world):
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2))
    world.step()
    with world.engine.store.editing() as state:
        state.meetings["m1"].state = "needs_attention"
    world.engine.after_check("some-older-job", "zoom:m1")
    assert world.meeting("m1").state == "posting"
