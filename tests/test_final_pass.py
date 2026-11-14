"""Regression tests for the final review pass: a cut-off upload whose release
time no longer suits, the never-post words in what goes out, and the small
fixes (stale error on a posted row, cost rows that only grow, a truncated
channel scan, names in titles)."""

import json
from datetime import UTC, datetime, timedelta

import pytest

import api.main as api_main
from core.config import get_settings
from costs import db as costs_db
from ledger import uploads
from ledger.uploads import get_ledger
from publish import channel_check, describe, once, schedule
from publish import run as publish_run
from publish import settings as pub_settings
from tests.fake_channel import FakeChannel
from tests.test_channel_check import FakeService, _video
from tests.test_publish import _connect_fake_channel, _finished_job, _reread
from tests.test_release_plan import _run


def _full_key(job):
    return uploads.full_item(job).key


def _cut_off_full(monkeypatch, *, privacy="public", right_away=False, crash="mid_upload"):
    """A run whose full video's upload dies; returns (channel, job)."""
    _connect_fake_channel()
    channel = FakeChannel(crash_at=crash, raise_with=RuntimeError).install(monkeypatch)
    job = _finished_job()
    _run(job, privacy=privacy, shorts=[], right_away=right_away)
    assert get_ledger().get(_full_key(job)).state == "interrupted"
    return channel, _reread(job.job_id)


# ------------------------------------------------- a cut-off link whose time no longer suits


def test_a_cut_off_upload_whose_slot_is_inside_the_review_window_gets_a_new_link_and_slot(monkeypatch):
    channel, job = _cut_off_full(monkeypatch)
    key = _full_key(job)
    old = datetime.fromisoformat(get_ledger().get(key).publish_at)
    pub_settings.save({"review_hours": 72})  # as if days had passed: the old slot no longer leaves the review time
    _run(job, shorts=[])
    full = channel.uploaded[-1]
    assert channel.calls.count("new_link") == 2  # the old link was dropped, not finished
    assert full["publish_at"] >= datetime.now(UTC) + timedelta(hours=71)
    held = get_ledger().get(key)
    assert held.state == "published" and held.publish_at == schedule.rfc3339(full["publish_at"])
    assert schedule.get(key).at == full["publish_at"] != old
    assert _reread(job.job_id).youtube_status == "published"


def test_a_cut_off_upload_that_had_finished_keeps_its_time_and_says_so(monkeypatch):
    channel, job = _cut_off_full(monkeypatch, crash="after_last_byte")
    key = _full_key(job)
    old = schedule.get(key).at
    pub_settings.save({"review_hours": 72})
    _run(job, shorts=[])
    assert len(channel.videos) == 1 and channel.calls.count("new_link") == 1  # no second video
    assert schedule.get(key).at == old  # the schedule says what YouTube holds
    after = _reread(job.job_id)
    assert after.youtube_status == "published" and "Check this video in YouTube Studio" in after.youtube_error


def test_a_cut_off_scheduled_upload_published_private_starts_a_new_link(monkeypatch):
    channel, job = _cut_off_full(monkeypatch)
    _run(job, privacy="private", shorts=[])
    video = channel.uploaded[-1]
    assert video["privacy"] == "private" and video["publish_at"] is None  # the old publishAt is not carried over
    assert channel.calls.count("new_link") == 2
    assert schedule.upcoming() == []


def test_a_cut_off_right_away_upload_joins_the_release_plan_with_a_new_link(monkeypatch):
    channel, job = _cut_off_full(monkeypatch, right_away=True)
    assert get_ledger().get(_full_key(job)).publish_at is None
    _run(job, shorts=[])
    video = channel.uploaded[-1]
    assert video["privacy"] == "private" and video["publish_at"] is not None
    assert schedule.get(_full_key(job)).at == video["publish_at"]
    assert [b.key for b in schedule.upcoming()] == [_full_key(job)]


def test_a_slot_still_in_time_is_kept_for_the_resume(monkeypatch):
    channel, job = _cut_off_full(monkeypatch)
    kept = schedule.get(_full_key(job))
    _run(job, shorts=[])
    assert channel.calls.count("new_link") == 1 and channel.uploaded[-1]["publish_at"] == kept.at


def test_booking_does_not_move_a_slot_when_the_ledger_will_refuse_the_upload():
    key, source = "zoom:u|full", "zoom:u"
    get_ledger().begin(uploads.Item(key, source, "full", "job-a", "Weekly Sync"), title="t", privacy="public",
                       by="test", publish_at="2026-11-20T10:00:00Z")
    first = schedule.book(key, "full", source)
    pub_settings.save({"review_hours": 72})  # the booking is now inside the window: book() would move it
    other = uploads.Item(key, source, "full", "job-b", "Weekly Sync")  # a re-process of the same meeting
    slot = publish_run._slot_for(other, None, pub_settings.current())
    assert slot.at == first.at and schedule.get(key).at == first.at


# ------------------------------------------------- never-post words in what goes out


def test_a_manual_publish_with_a_never_post_word_in_the_title_is_refused():
    _connect_fake_channel()
    job = _finished_job()
    with pytest.raises(publish_run.PublishRefused) as info:
        publish_run.build_plan(job, title="Keeping investor demo keys safe", privacy="private")
    assert info.value.status_code == 400 and '"investor"' in str(info.value)
    with pytest.raises(publish_run.PublishRefused) as info:
        publish_run.build_plan(job, title="Fine title", description="A confidential pricing talk", privacy="private")
    assert '"confidential"' in str(info.value)


def test_the_apps_own_team_line_and_the_owners_links_are_not_looked_at():
    pub_settings.save({"links_text": "Our private beta: https://flolabs.example/join"})
    assert publish_run.listed_words("Moving to Redis", f"Notes.\n{describe.ORG_LINE}\nOur private beta: https://flolabs.example/join") is None
    assert publish_run.listed_words("Moving to Redis", "Notes about the private beta") == "private"


def test_the_autopilot_holds_a_meeting_whose_suggested_words_have_a_listed_word(monkeypatch):
    job = _finished_job()
    assert publish_run.suggested_words_listed(job) is None
    monkeypatch.setattr(describe, "short_title", lambda info, job: "Keeping demo keys private")
    why = publish_run.suggested_words_listed(job)
    assert why is not None and '"private"' in why and "never-post list" in why


def test_the_last_check_before_an_upload_also_looks_at_the_zoom_topic_and_id(monkeypatch):
    _connect_fake_channel()
    FakeChannel().install(monkeypatch)
    job = _finished_job()
    job.zoom_meeting = {"topic": "Operations team meeting", "meeting_id": "84116996599"}
    item = uploads.full_item(job)
    assert item.topic == "Operations team meeting" and item.meeting_id == "84116996599"
    with pytest.raises(once.NeverPost):
        once.upload_once(item, lambda: None, title="t", description="d", privacy="private", by="test")
    assert get_ledger().get(item.key) is None  # nothing was claimed


# ------------------------------------------------- the small fixes


def test_a_row_the_ledger_knows_is_posted_does_not_show_the_jobs_old_failure():
    job = _finished_job()
    job.youtube_status, job.youtube_error = "failed", publish_run.INTERRUPTED
    get_ledger().record_existing(uploads.full_item(job), title="Full", url="https://youtu.be/abcdefghijk")
    full = publish_run.publish_options(job)["full"]
    assert full["status"] == "published" and full["error"] is None


def test_a_short_booked_after_the_full_video_went_public_still_waits_after_it():
    cfg = pub_settings.current()
    job = _finished_job()
    item = uploads.full_item(job)
    get_ledger().begin(item, title="t", privacy="public", by="test", publish_at="2026-11-01T10:00:00Z")
    get_ledger().published(item.key, "abcdefghijk")
    assert publish_run._after_full(item, cfg) == datetime(2026, 11, 1, 10, tzinfo=UTC) + timedelta(
        hours=cfg.shorts_after_full_hours)


def test_a_cost_row_only_grows_when_an_older_snapshot_arrives_late():
    with costs_db.connect() as con:
        def put(calls, usd, grows_by="calls"):
            costs_db.upsert_event(con, kind="ai", key="ai:j1", at="2026-11-01T10:00:00+00:00", day="2026-11-01",
                                  calls=calls, usd=usd, grows_by=grows_by)
            return con.execute("SELECT calls, usd FROM events WHERE key='ai:j1'").fetchone()

        assert tuple(put(5, 0.5)) == (5, 0.5)
        assert tuple(put(3, 0.3)) == (5, 0.5)  # the older snapshot does not lower it
        assert tuple(put(7, 0.7)) == (7, 0.7)
        assert tuple(put(7, 0.7)) == (7, 0.7)  # recording again changes nothing


def test_the_channel_scan_says_when_it_stopped_at_its_page_cap(monkeypatch):
    videos = [_video(f"{i:011d}", f"Video {i}") for i in range(4)]
    assert channel_check.check_channel(FakeService(videos))["truncated"] is False
    monkeypatch.setattr(channel_check, "MAX_PAGES", 1)  # the service has a second page
    assert channel_check.check_channel(FakeService(videos))["truncated"] is True


def test_a_full_title_never_names_a_speaker():
    job = _finished_job()
    job_dir = get_settings().jobs_dir / job.job_id
    manifest = json.loads((job_dir / "manifest.json").read_text())
    manifest["shorts"][0]["title"] = "Ahsan explains the Postgres move"
    manifest["shorts"][1]["title"] = "The rollout plan"
    (job_dir / "manifest.json").write_text(json.dumps(manifest))
    assert describe._best_moment(job)[0] == "Ahsan explains the Postgres move"  # no speakers known: kept
    (job_dir / "segments.json").write_text(json.dumps([
        {"speaker": "Ahsan Yasin", "text": "hi"}, {"speaker": "SPEAKER_01", "text": "hello"}]))
    assert describe._best_moment(job)[0] == "The rollout plan"
    assert describe._names_someone("Ahsan explains it", describe._speaker_names(job))
    assert not describe._names_someone("Why ahsan-free tools help", set())


def test_the_publishing_defaults_fit_a_normal_week():
    assert pub_settings.capacity_warning(pub_settings.DEFAULTS) is None
    tight = pub_settings.Publishing(shorts_per_day=1)
    assert "some Shorts would be skipped" in pub_settings.capacity_warning(tight)
    assert pub_settings.capacity_warning(pub_settings.Publishing(scheduled=False, shorts_per_day=0)) is None


def test_the_settings_routes_carry_the_capacity_warning():
    from fastapi.testclient import TestClient

    client = TestClient(api_main.app)
    assert client.get("/publishing/settings").json()["warning"] is None
    body = client.post("/publishing/settings", json={"shorts_per_day": 1}).json()
    assert "some Shorts would be skipped" in body["warning"]


def test_a_video_found_on_the_channel_already_gives_back_the_slot_booked_for_this_attempt():
    job = _finished_job()
    item = uploads.full_item(job)
    slot = schedule.book(item.key, "full", item.source)
    get_ledger().record_existing(item, title="Full", url="https://youtu.be/abcdefghijk")  # another run posted it
    publish_run._settle_slot(item, slot, get_ledger().get(item.key))
    assert schedule.get(item.key) is None and schedule.upcoming() == []


def test_a_video_found_on_the_channel_with_its_own_time_keeps_that_time():
    job = _finished_job()
    item = uploads.full_item(job)
    slot = schedule.book(item.key, "full", item.source)
    theirs = slot.at + timedelta(days=2)
    get_ledger().begin(item, title="t", privacy="public", by="test", publish_at=schedule.rfc3339(theirs), notify=True)
    posted = get_ledger().published(item.key, "abcdefghijk")
    publish_run._settle_slot(item, slot, posted)
    assert schedule.get(item.key).at == theirs
