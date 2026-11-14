"""The release plan end to end: a public upload goes up private with its own
time to go public, full video first, Shorts after (publish/run.py), and the
never-post words added with it (autopilot/never_post.py)."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from autopilot import never_post
from ledger import files, prefs, uploads
from ledger.uploads import get_ledger
from publish import describe, media, once, schedule
from publish import run as publish_run
from publish import settings as pub_settings
from publish import youtube as yt
from tests.fake_channel import FakeChannel
from tests.test_publish import _connect_fake_channel, _finished_job, _reread

SHORTS = [(1, None, None), (2, None, None)]


@pytest.fixture
def channel(monkeypatch):
    _connect_fake_channel()
    return FakeChannel().install(monkeypatch)


@pytest.fixture
def client():
    return TestClient(api_main.app)


def _run(job, privacy="public", shorts=SHORTS, **kwargs):
    plan = publish_run.build_plan(job, title="Full video", shorts=shorts, privacy=privacy, **kwargs)
    publish_run.prepare(job, plan)
    return plan, publish_run.publish_job(job, api_main.job_store.update, plan)


def _at(video) -> datetime:
    return video["publish_at"]


# ------------------------------------------------------------------ the order


def test_a_public_upload_goes_up_private_full_video_first_then_the_shorts(channel):
    job = _finished_job()
    _, stopped = _run(job)
    assert stopped is None
    videos = channel.uploaded
    assert videos[0]["title"] == "Full video"  # the full video first
    assert len(videos) == 3
    assert all(v["privacy"] == "private" and v["publish_at"] for v in videos)
    full, short1, short2 = videos
    # each Short goes public at least a day after the full video, and the slots are apart
    assert _at(short1) - _at(full) >= timedelta(hours=24)
    assert len({_at(v) for v in videos}) == 3
    # only the full video tells subscribers
    assert (full["notify"], short1["notify"], short2["notify"]) == (True, False, False)
    # the owner's category and language and the meeting's day go with every video
    assert {(v["category_id"], v["language"], v["recording_date"]) for v in videos} == {("28", "en", "2026-10-05")}
    # review time: nothing goes public sooner than review_hours from now
    assert _at(full) >= datetime.now(UTC) + timedelta(hours=11, minutes=59)


def test_the_shorts_link_to_the_full_video_and_the_full_video_lists_no_shorts(channel):
    job = _finished_job()
    _run(job)
    after = _reread(job.job_id)
    full_url = after.youtube_url
    assert full_url and after.youtube_status == "published"
    full, short1, short2 = channel.uploaded
    assert describe.SHORTS_HEADING not in full["description"]
    assert after.youtube_listed_shorts == []
    assert full_url in short1["description"] and full_url in short2["description"]


def test_a_description_a_person_typed_for_a_short_is_never_rewritten(channel):
    job = _finished_job()
    plan = publish_run.build_plan(job, title="Full video", privacy="public",
                                  shorts=[(1, None, "My own words"), (2, None, None)])
    assert [s.suggested for s in plan.shorts] == [False, True]
    publish_run.prepare(job, plan)
    publish_run.publish_job(job, api_main.job_store.update, plan)
    _, short1, short2 = channel.uploaded
    assert short1["description"] == "My own words"
    assert _reread(job.job_id).youtube_url in short2["description"]


def test_every_item_keeps_its_slot_and_the_rows_say_when_it_goes_public(channel):
    job = _finished_job()
    _run(job)
    after = _reread(job.job_id)
    booked = {b.key: b for b in schedule.upcoming()}
    assert len(booked) == 3
    for index, state in after.youtube_shorts.items():
        item = uploads.short_item(job, index)
        assert state.status == "published" and state.publish_at
        assert booked[item.key].at == datetime.fromisoformat(state.publish_at)
        assert get_ledger().get(item.key).publish_at == state.publish_at
    posted = get_ledger().get(uploads.full_item(job).key)
    assert posted.publish_at and posted.notify is True


@pytest.mark.parametrize("settings, privacy, right_away", [
    ({}, "private", False),
    ({}, "unlisted", False),
    ({}, "public", True),
    ({"scheduled": False}, "public", False),
])
def test_everything_else_behaves_as_before(channel, settings, privacy, right_away):
    pub_settings.save(settings)
    job = _finished_job()
    _run(job, privacy=privacy, right_away=right_away)
    titles = [v["title"] for v in channel.uploaded]
    assert titles[-1] == "Full video"  # the shorts first, the full video last
    assert all(v["privacy"] == privacy and v["publish_at"] is None for v in channel.uploaded)
    assert describe.SHORTS_HEADING in channel.uploaded[-1]["description"]
    assert schedule.upcoming() == []
    # the owner's fields still go with it
    assert {(v["category_id"], v["language"], v["recording_date"]) for v in channel.uploaded} == {
        ("28", "en", "2026-10-05")}


# ------------------------------------------------------------------ no slot


def test_a_short_with_no_slot_is_skipped_and_counts_as_done(channel):
    pub_settings.save({"shorts_per_day": 0})
    job = _finished_job()
    every_short = [(1, None, None), (2, None, None), (3, None, None)]
    _, stopped = _run(job, shorts=every_short, delete_after=False)
    after = _reread(job.job_id)
    assert stopped is None and len(channel.videos) == 1  # only the full video went up
    assert after.youtube_status == "published"
    for state in after.youtube_shorts.values():
        assert state.status == "skipped"
        assert state.error == ("Not posted: no free release slot in the next 7 days. "
                               "Publish it by hand if you want it.")
    assert publish_run.posting_verdict(after) == "done"  # the autopilot is finished with it
    assert not publish_run.everything_published(after)  # the files stay for a person
    assert publish_run.plan_everything(after, "public", "autopilot") is None  # never retried
    assert after.youtube_busy is False


def test_an_autopilot_run_with_a_skipped_short_keeps_the_files(channel):
    pub_settings.save({"shorts_per_day": 0})
    job = _finished_job()
    _run(job, shorts=[(1, None, None)], delete_after=True)
    after = _reread(job.job_id)
    assert after.youtube_shorts[1].status == "skipped"
    assert after.files_deleted is False  # a person can still publish it by hand


def test_a_skipped_short_can_still_be_published_by_hand(channel):
    pub_settings.save({"shorts_per_day": 0})
    job = _finished_job()
    _run(job, shorts=[(1, None, None)], delete_after=False)
    assert _reread(job.job_id).youtube_shorts[1].status == "skipped"
    job = _reread(job.job_id)
    _, stopped = _run_by_hand(job)
    after = _reread(job.job_id)
    assert stopped is None and after.youtube_shorts[1].status == "published"
    assert after.youtube_shorts[1].error is None
    assert channel.uploaded[-1]["privacy"] == "private"


def _run_by_hand(job):
    plan = publish_run.build_plan(job, shorts=[(1, None, None)], privacy="private")
    publish_run.prepare(job, plan)
    return plan, publish_run.publish_job(job, api_main.job_store.update, plan)


def test_the_full_video_with_no_slot_fails_with_a_plain_reason_and_its_shorts_wait(channel):
    pub_settings.save({"review_hours": 30, "max_days_ahead": 1})
    job = _finished_job()
    _, stopped = _run(job)
    after = _reread(job.job_id)
    assert stopped is None and channel.videos == {}
    assert after.youtube_status == "failed"
    assert after.youtube_error == ("Not uploaded: no free release slot in the next 1 day. "
                                   "Try again later or publish it right away.")
    assert {s.status for s in after.youtube_shorts.values()} == {"failed"}
    assert publish_run.posting_verdict(after) == "left"  # the autopilot tries again later
    assert schedule.upcoming() == []


# ------------------------------------------------------------------ failures


def test_a_failed_upload_gives_its_slot_back(monkeypatch):
    _connect_fake_channel()
    channel = FakeChannel(crash_at="before_link", raise_with=RuntimeError).install(monkeypatch)
    job = _finished_job()
    _, stopped = _run(job)
    after = _reread(job.job_id)
    assert stopped is None and after.youtube_status == "failed"
    assert schedule.get(uploads.full_item(job).key) is None and schedule.upcoming() == []
    assert {s.status for s in after.youtube_shorts.values()} == {"failed"}  # they wait for the full video
    # a second try books a fresh slot and everything goes up
    job = _reread(job.job_id)
    _run(job)
    assert len(channel.videos) == 3 and len(schedule.upcoming()) == 3


def test_a_cut_off_upload_keeps_its_slot_and_finishes_in_it(monkeypatch):
    _connect_fake_channel()
    channel = FakeChannel(crash_at="mid_upload", raise_with=RuntimeError).install(monkeypatch)
    job = _finished_job()
    _run(job)
    key = uploads.full_item(job).key
    kept = schedule.get(key)
    assert kept is not None  # YouTube holds an upload link for it: the slot stays
    assert get_ledger().get(key).state == "interrupted"
    job = _reread(job.job_id)
    _run(job)  # carries on through the same link
    assert len(channel.videos) == 3
    after = _reread(job.job_id)
    posted = get_ledger().get(key)
    assert posted.state == "published" and posted.publish_at == schedule.rfc3339(kept.at)
    assert schedule.get(key) == kept
    assert after.youtube_status == "published"


# ------------------------------------------------------------------ never post


def test_the_never_post_list_still_stops_every_upload_in_the_new_order(channel):
    job = _finished_job()
    plan = publish_run.build_plan(job, title="Full video", privacy="public", shorts=SHORTS)
    publish_run.prepare(job, plan)
    never_post.set_entries(["weekly"])  # someone adds it while the job waits
    stopped = publish_run.publish_job(job, api_main.job_store.update, plan)
    after = _reread(job.job_id)
    assert channel.videos == {} and channel.calls == []
    assert stopped and "never-post list" in stopped
    assert after.youtube_status == "failed" and "Not uploaded:" in after.youtube_error
    assert all(s.status == "failed" and "never-post list" in s.error for s in after.youtube_shorts.values())
    assert schedule.upcoming() == []  # no slot was taken for what never went up


def test_the_list_is_looked_at_again_between_the_full_video_and_its_shorts(monkeypatch, channel):
    real = channel.upload_video

    def then_listed(*args, **kwargs):
        done = real(*args, **kwargs)
        never_post.set_entries(["weekly"])
        return done

    monkeypatch.setattr(yt, "upload_video", then_listed)
    job = _finished_job()
    _, stopped = _run(job)
    after = _reread(job.job_id)
    assert stopped and "never-post list" in stopped
    assert len(channel.videos) == 1 and after.youtube_status == "published"  # the full video was already up
    assert all(s.status == "failed" and "never-post list" in s.error for s in after.youtube_shorts.values())
    assert len(schedule.upcoming()) == 1


# ------------------------------------------------------------------ the readback


def test_a_readback_that_differs_is_reported_and_the_upload_still_counts(channel):
    channel.read_override = {"privacyStatus": "public", "publishAt": None}
    job = _finished_job(with_shorts=False)
    plan = publish_run.build_plan(job, title="Full video", privacy="public")
    publish_run.prepare(job, plan)
    publish_run.publish_job(job, api_main.job_store.update, plan)
    after = _reread(job.job_id)
    assert after.youtube_status == "published" and len(channel.videos) == 1
    assert "Check this video in YouTube Studio" in after.youtube_error
    assert "shows it as public" in after.youtube_error and "private" in after.youtube_error


def test_upload_once_returns_the_mismatch_as_a_warning(channel):
    channel.read_override = {"privacyStatus": "private", "publishAt": "2030-01-01T10:00:00Z"}
    job = _finished_job(with_shorts=False)
    item = uploads.full_item(job)
    asked = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
    posted = once.upload_once(item, lambda: media.open_media(job, media.final_member(job)), title="t",
                              description="d", privacy="public", by="person", publish_at=asked)
    assert posted.state == "published" and "Check this video in YouTube Studio" in posted.warning
    assert get_ledger().get(item.key).state == "published"


def test_a_matching_readback_says_nothing(channel):
    job = _finished_job()
    _run(job)
    assert len(channel.reads) == 3  # asked once per new video
    assert _reread(job.job_id).youtube_error is None


def test_a_readback_that_cannot_be_read_never_fails_the_upload(channel):
    channel.read_error = RuntimeError("quota")
    job = _finished_job(with_shorts=False)
    plan = publish_run.build_plan(job, title="Full video", privacy="public")
    publish_run.prepare(job, plan)
    publish_run.publish_job(job, api_main.job_store.update, plan)
    after = _reread(job.job_id)
    assert after.youtube_status == "published" and after.youtube_error is None


# ------------------------------------------------------------------ the API


def test_publish_request_goes_on_the_release_plan_unless_right_away(client, channel):
    job = _finished_job(with_shorts=False)
    res = client.post(f"/jobs/{job.job_id}/publish", json={"title": "Full video", "privacy": "public"})
    assert res.status_code == 200
    assert channel.uploaded[0]["privacy"] == "private" and channel.uploaded[0]["publish_at"]
    other = _finished_job("pub-2", with_shorts=False)
    res = client.post(f"/jobs/{other.job_id}/publish",
                      json={"title": "Another", "privacy": "public", "right_away": True})
    assert res.status_code == 200
    assert channel.uploaded[1]["privacy"] == "public" and channel.uploaded[1]["publish_at"] is None


def test_the_release_plan_route_lists_what_is_coming(client, channel):
    assert client.get("/publishing/schedule").json() == {"upcoming": []}
    job = _finished_job()
    _run(job)
    rows = client.get("/publishing/schedule").json()["upcoming"]
    assert [r["kind"] for r in rows] == ["full", "short", "short"]  # soonest first
    assert rows[0]["notify"] is True and rows[1]["notify"] is False
    assert rows[0]["meeting"] == "Weekly Sync"
    assert rows[0]["at"].endswith("+00:00") and rows[0]["at_team"]  # "Nov 15, 2:30 PM"


def test_the_posted_list_carries_the_time_it_goes_public(client, channel):
    job = _finished_job()
    _run(job)
    body = client.get("/posted").json()
    group = body["meetings"][0]
    assert group["full"]["publish_at"] and all(s["publish_at"] for s in group["shorts"])
    assert body["timezone"]


def test_the_publish_options_tell_the_page_about_the_plan(client, channel):
    job = _finished_job()
    body = client.get(f"/jobs/{job.job_id}/publish").json()
    assert body["release"]["scheduled"] is True and body["release"]["review_hours"] == 12
    _run(job)
    again = client.get(f"/jobs/{job.job_id}/publish").json()
    assert again["full"]["publish_at"] and all(s["publish_at"] for s in again["shorts"] if s["status"])


# ------------------------------------------------------------------ the posted list file


def test_a_posted_list_written_before_the_plan_still_loads():
    line = {"posted_at": "2026-10-01T10:00:00+00:00", "meeting": "Old", "kind": "full", "title": "Old",
            "url": "https://youtu.be/abcdefghijk", "video_id": "abcdefghijk", "key": "zoom:old|full",
            "source": "zoom:old", "job_id": "j"}
    files.append_jsonl(files.state_dir() / uploads.POSTED, line)
    ledger = uploads.Ledger(files.state_dir())
    (only,) = ledger.posted()
    assert only.publish_at is None and only.notify is None


# ------------------------------------------------------------------ never-post words


@pytest.mark.parametrize("title", [
    "Internal planning", "Confidential: Q4", "Private call", "NDA review", "Candidate interview",
    "Interviews round 2", "Investor update", "Investors Q&A", "Partner sync", "Partners meeting",
])
def test_the_new_words_stop_these_titles(title):
    assert never_post.match(title) is not None


@pytest.mark.parametrize("title", ["Internet of Things Meeting", "Partnership ideas", "Deprivate"])
def test_whole_words_only(title):
    assert never_post.match(title) is None


def _saved() -> dict:
    return json.loads((files.state_dir() / prefs.FILE).read_text(encoding="utf-8"))


def test_a_list_saved_earlier_gets_the_new_words_once():
    files.write_json(files.state_dir() / prefs.FILE, {"never_post": ["Board meeting", "partner"], "other": 1})
    got = never_post.entries()
    # the owner's own entries stay first and untouched; only the missing words are added
    assert got[:2] == ["Board meeting", "partner"]
    assert got[2:] == [w for w in never_post.NEW_DEFAULTS_V2 if w != "partner"]
    saved = _saved()
    assert saved[never_post.DEFAULTS_V2_FLAG] is True and saved["other"] == 1 and saved["never_post"] == got


def test_the_new_words_are_not_added_a_second_time_over_a_choice_to_remove_them():
    files.write_json(files.state_dir() / prefs.FILE, {"never_post": ["board"]})
    never_post.entries()
    never_post.set_entries(["board", "investor"])  # the owner takes the rest out
    never_post._defaults_checked.clear()  # as after a restart
    assert never_post.entries() == ["board", "investor"]
    assert never_post.entries() == ["board", "investor"]


def test_a_list_saved_from_the_page_is_never_topped_up_later():
    never_post.set_entries(["board"])  # first thing that happens: the owner saves
    never_post._defaults_checked.clear()
    assert never_post.entries() == ["board"]
    assert _saved()[never_post.DEFAULTS_V2_FLAG] is True


def test_with_nothing_saved_the_built_in_list_has_the_new_words():
    assert never_post.entries() == list(never_post.DEFAULT_ENTRIES)
    assert set(never_post.NEW_DEFAULTS_V2) <= set(never_post.entries())
    assert "partners" in never_post.entries()


def test_every_release_plan_row_carries_the_meeting_name(client, channel, monkeypatch):
    _run(_finished_job())
    stored = schedule.upcoming()
    assert stored and all(b.meeting == "Weekly Sync" for b in stored)
    # no ledger fallback: the stored field alone has to carry the name
    monkeypatch.setattr(api_main, "_meeting_name_for", lambda *a, **k: "")
    rows = client.get("/publishing/schedule").json()["upcoming"]
    assert rows and all(r["meeting"] == "Weekly Sync" for r in rows)
