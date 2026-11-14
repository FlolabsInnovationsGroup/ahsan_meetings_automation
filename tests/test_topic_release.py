"""Topic cuts in the publish run: the front door (the cut goes public first and tells
subscribers, the full video follows quietly but is uploaded first), the release plan,
the never-post checks, and the verdicts. A fake YouTube; synthetic data only."""

from datetime import timedelta

import pytest

import api.main as api_main
from autopilot import never_post
from core.config import get_settings
from ledger import uploads
from ledger.uploads import get_ledger
from publish import describe, schedule, series
from publish import run as publish_run
from publish import settings as pub_settings
from publish import youtube as yt
from tests.fake_channel import FakeChannel
from tests.test_publish import _connect_fake_channel, _reread
from tests.test_topic_packaging import job_with_topics, topic_row

TEAM = "IoT Team Meeting"


@pytest.fixture(autouse=True)
def queued(monkeypatch):
    """The follow-ups (thumbnail, playlist, captions) are tested in their own files; here they
    are only recorded, so no worker thread outlives a test."""
    calls = []
    monkeypatch.setattr(publish_run, "_queue_extras",
                        lambda job, item, posted, kind, index, cfg: calls.append((item.kind, kind, index)))
    return calls


@pytest.fixture
def channel(monkeypatch):
    _connect_fake_channel()
    return FakeChannel().install(monkeypatch)


def _cut_job(rows=None, tier="public", job_id="pub-1", **kwargs):
    job = job_with_topics(rows, job_id=job_id, **kwargs)
    job.title = TEAM
    api_main.job_store.update(job)
    if tier:
        series.set_row(series.series_key(TEAM), tier=tier, label="IoT")
    return job


def _run(job, privacy="public", shorts=(), topics=((1, None, None),), title="Full video", **kwargs):
    plan = publish_run.build_plan(job, title=title, shorts=list(shorts), topics=list(topics), privacy=privacy, **kwargs)
    publish_run.prepare(job, plan)
    return plan, publish_run.publish_job(job, api_main.job_store.update, plan)


def _by_title(channel, start):
    return next(v for v in channel.uploaded if v["title"].startswith(start))


def _full_and_cut(channel):
    return _by_title(channel, "Full video"), _by_title(channel, "Moving the session store")


# ------------------------------------------------------------------ the front door


def test_the_cut_is_the_front_door_the_full_video_is_uploaded_first_and_follows_quietly(channel):
    job = _cut_job()
    _, stopped = _run(job)
    assert stopped is None
    assert [v["title"] for v in channel.uploaded] == [
        "Full video", "Moving the session store to Redis | FloLabs IoT"]  # the full video went up first
    full, cut = _full_and_cut(channel)
    assert full["privacy"] == cut["privacy"] == "private" and full["publish_at"] and cut["publish_at"]
    assert cut["publish_at"] < full["publish_at"]  # but the cut goes public first
    assert full["publish_at"] - cut["publish_at"] >= timedelta(hours=2)
    assert cut["notify"] is True and full["notify"] is False
    full_id = next(i for i, v in channel.videos.items() if v is full)
    assert f"Full meeting: https://youtu.be/{full_id}?t=100" in cut["description"]  # 60 s intro + 40 s in
    after = _reread(job.job_id)
    assert after.youtube_status == "published" and after.youtube_topics[1].status == "published"
    assert after.youtube_topics[1].publish_at
    assert {u.kind for u in get_ledger().posted()} == {"full", "topic"}
    cut_slot = schedule.get(uploads.topic_item(job, 1).key)
    full_slot = schedule.get(uploads.full_item(job).key)
    assert cut_slot.kind == "topic" and cut_slot.notify and not full_slot.notify


def test_a_posted_cut_queues_its_thumbnail_and_playlist_as_a_topic(channel, queued):
    job = _cut_job()
    _run(job)
    assert ("topic", "topic", 1) in queued and ("full", "full", None) in queued


def test_the_cut_was_booked_before_the_full_video(channel):
    job = _cut_job()
    _run(job)
    cut_slot = schedule.get(uploads.topic_item(job, 1).key)
    full_slot = schedule.get(uploads.full_item(job).key)
    assert cut_slot.booked_at <= full_slot.booked_at and cut_slot.at < full_slot.at


def test_a_description_a_person_typed_for_the_cut_is_never_rewritten(channel):
    job = _cut_job()
    _run(job, topics=[(1, "My own title", "My own words.")])
    cut = _by_title(channel, "My own title")
    assert cut["description"] == "My own words."


def test_the_shorts_come_after_the_front_door_and_link_to_the_cut_only_when_inside_it(channel):
    job = _cut_job()
    _run(job, shorts=[(1, None, None), (2, None, None)])
    full, cut = _full_and_cut(channel)
    assert len(channel.uploaded) == 4
    inside = _by_title(channel, "Why we moved to Postgres")  # clip 110-150, inside the cut (100-200)
    outside = _by_title(channel, "The rollout plan")  # clip 30-70, not inside
    for short in (inside, outside):
        assert short["publish_at"] - cut["publish_at"] >= timedelta(hours=24)
        assert short["publish_at"] - full["publish_at"] >= timedelta(hours=24)
        assert short["notify"] is False
    cut_id = next(i for i, v in channel.videos.items() if v is cut)
    full_id = next(i for i, v in channel.videos.items() if v is full)
    assert f"https://youtu.be/{cut_id}?t=34" in inside["description"] and full_id not in inside["description"]
    assert f"https://youtu.be/{full_id}?t=90" in outside["description"] and cut_id not in outside["description"]


def test_shorts_link_to_the_full_video_when_the_owner_says_so(channel):
    pub_settings.save({"shorts_link_target": "full"})
    job = _cut_job()
    _run(job, shorts=[(1, None, None)])
    full, _ = _full_and_cut(channel)
    full_id = next(i for i, v in channel.videos.items() if v is full)
    assert f"https://youtu.be/{full_id}?t=110" in _by_title(channel, "Why we moved to Postgres")["description"]


def test_with_no_slot_for_the_cut_it_is_skipped_and_the_full_video_books_normally(monkeypatch, channel):
    real = schedule.book

    def no_cut_slot(key, kind, *args, **kwargs):
        return None if kind == "topic" else real(key, kind, *args, **kwargs)

    monkeypatch.setattr(schedule, "book", no_cut_slot)
    job = _cut_job(with_shorts=False)
    _, stopped = _run(job)
    after = _reread(job.job_id)
    assert stopped is None and len(channel.videos) == 1
    full = channel.uploaded[0]
    assert full["notify"] is True  # the full video keeps the day's notification
    assert after.youtube_status == "published"
    assert after.youtube_topics[1].status == "skipped"
    assert "no free release slot" in after.youtube_topics[1].error
    assert publish_run.posting_verdict(after) == "done"  # left for a person, never retried
    assert not publish_run.everything_published(after)  # its file stays
    plan = publish_run.plan_everything(after, "public", "autopilot")
    assert plan is None or plan.topics == ()


def test_a_failed_full_upload_gives_the_cuts_booking_back_and_the_next_round_tries_again(monkeypatch):
    _connect_fake_channel()
    channel = FakeChannel(crash_at="before_link", raise_with=RuntimeError).install(monkeypatch)
    job = _cut_job()
    _, stopped = _run(job)
    after = _reread(job.job_id)
    assert stopped is None and channel.videos == {}  # the cut never went up pointing at nothing
    assert after.youtube_status == "failed"
    assert after.youtube_topics[1].status == "failed" and "full video" in after.youtube_topics[1].error
    assert schedule.upcoming() == []  # both bookings are back
    assert publish_run.posting_verdict(after) == "left"
    job = _reread(job.job_id)
    _, stopped = _run(job)  # the next round
    after = _reread(job.job_id)
    assert len(channel.videos) == 2 and after.youtube_topics[1].status == "published"
    assert len(schedule.upcoming()) == 2


def test_a_never_post_word_added_mid_run_stops_the_cut(monkeypatch, channel):
    real = channel.upload_video

    def then_listed(*args, **kwargs):
        done = real(*args, **kwargs)
        never_post.set_entries(["iot"])
        return done

    monkeypatch.setattr(yt, "upload_video", then_listed)
    job = _cut_job()
    _, stopped = _run(job, shorts=[(1, None, None)])
    after = _reread(job.job_id)
    assert stopped and "never-post list" in stopped
    assert len(channel.videos) == 1 and after.youtube_status == "published"  # only the full video was up
    assert after.youtube_topics[1].status == "failed" and "never-post list" in after.youtube_topics[1].error
    assert len(schedule.upcoming()) == 1  # the cut's booking was given back


def test_the_never_post_list_stops_everything_before_the_first_upload(channel):
    job = _cut_job()
    plan = publish_run.build_plan(job, title="Full video", topics=[(1, None, None)], privacy="public")
    publish_run.prepare(job, plan)
    never_post.set_entries(["iot"])
    stopped = publish_run.publish_job(job, api_main.job_store.update, plan)
    after = _reread(job.job_id)
    assert stopped and channel.videos == {} and channel.calls == []
    assert after.youtube_topics[1].status == "failed" and "never-post list" in after.youtube_topics[1].error
    assert schedule.upcoming() == []


def test_build_plan_refuses_a_never_post_meeting_for_topics_with_403():
    job = _cut_job()
    never_post.set_entries(["iot"])
    with pytest.raises(publish_run.PublishRefused) as refused:
        publish_run.build_plan(job, topics=[(1, None, None)])
    assert refused.value.status_code == 403


def test_build_plan_refuses_a_never_team_and_checks_the_cut(channel):
    job = _cut_job(tier="never")
    with pytest.raises(publish_run.PublishRefused) as refused:
        publish_run.build_plan(job, topics=[(1, None, None)])
    assert refused.value.status_code == 403
    job = _cut_job(tier="public")
    with pytest.raises(publish_run.PublishRefused) as refused:
        publish_run.build_plan(job, topics=[(7, None, None)])
    assert refused.value.status_code == 404
    with pytest.raises(publish_run.PublishRefused) as refused:
        publish_run.build_plan(job, topics=[(1, "A title with <angle>", None)])
    assert refused.value.status_code == 400
    never_post.set_entries(["investor"])
    with pytest.raises(publish_run.PublishRefused) as refused:
        publish_run.build_plan(job, topics=[(1, "A fine title", "words about the investor")])
    assert refused.value.status_code == 400 and "never-post" in str(refused.value)


def test_build_plan_says_410_when_the_cut_file_is_gone(channel):
    job = _cut_job()
    (get_settings().jobs_dir / job.job_id / "topic" / "topic_01.mp4").unlink()
    with pytest.raises(publish_run.PublishRefused) as refused:
        publish_run.build_plan(job, topics=[(1, None, None)])
    assert refused.value.status_code == 410


def test_a_cut_off_cut_resumes_with_a_new_slot_when_its_old_one_is_stale(monkeypatch):
    _connect_fake_channel()
    channel = FakeChannel().install(monkeypatch)
    real = channel.upload_video
    calls = []

    def cut_off_the_second(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:  # the cut: its upload is cut off with half of the bytes in
            channel.crash_at, channel.raise_with = "mid_upload", RuntimeError
        return real(*args, **kwargs)

    monkeypatch.setattr(yt, "upload_video", cut_off_the_second)
    job = _cut_job()
    _run(job)
    key = uploads.topic_item(job, 1).key
    assert get_ledger().get(key).state == "interrupted"
    old = get_ledger().get(key).publish_at
    pub_settings.save({"review_hours": 72})  # when it carries on, the old time no longer leaves the review time
    job = _reread(job.job_id)
    _run(job, title=None)
    posted = get_ledger().get(key)
    assert posted.state == "published" and len(channel.videos) == 2  # one video for the cut, not two
    assert posted.publish_at != old
    assert posted.publish_at == schedule.rfc3339(schedule.get(key).at)  # what YouTube holds is what is booked


def test_without_the_front_door_the_cut_goes_after_the_full_video(channel):
    pub_settings.save({"topic_front_door": False})
    job = _cut_job()
    _run(job, shorts=[(1, None, None)])
    full, cut = _full_and_cut(channel)
    assert [v["title"] for v in channel.uploaded[:2]] == [full["title"], cut["title"]]
    assert full["notify"] is True and cut["publish_at"] > full["publish_at"]
    short = _by_title(channel, "Why we moved to Postgres")
    assert short["publish_at"] - full["publish_at"] >= timedelta(hours=24)


@pytest.mark.parametrize("privacy", ["private", "unlisted"])
def test_a_private_or_unlisted_cut_goes_up_after_the_full_video_with_the_same_privacy(channel, privacy):
    job = _cut_job()
    _run(job, privacy=privacy)
    assert [v["privacy"] for v in channel.uploaded] == [privacy, privacy]
    assert all(v["publish_at"] is None for v in channel.uploaded)
    assert channel.uploaded[0]["title"] == "Full video"


def test_a_public_right_away_cut_waits_for_the_full_video(channel):
    job = _cut_job()
    _run(job, right_away=True)
    assert [v["privacy"] for v in channel.uploaded] == ["public", "public"]
    assert channel.uploaded[0]["title"] == "Full video" and all(v["publish_at"] is None for v in channel.uploaded)


def test_a_cut_alone_goes_up_by_itself_without_a_full_video_link(channel):
    job = _cut_job()
    _run(job, title=None)
    (cut,) = channel.uploaded
    assert "Full meeting" not in cut["description"] and cut["notify"] is True


# ------------------------------------------------------------------ what the autopilot plans


def test_plan_everything_adds_the_cut_for_a_public_team():
    job = _cut_job()
    plan = publish_run.plan_everything(job, "public", "autopilot")
    assert [c.index for c in plan.topics] == [1] and plan.topics[0].title.endswith("| FloLabs IoT")
    assert plan.topics[0].span == (100.0, 200.0) and plan.topics[0].suggested


def test_plan_everything_gives_no_cut_for_an_archive_team_or_full_only_or_when_off():
    archive = _cut_job(tier="archive")
    plan = publish_run.plan_everything(archive, "unlisted", "autopilot", kinds={"full"})
    assert plan is not None and plan.topics == ()
    assert publish_run.plan_everything(archive, "unlisted", "autopilot").topics == ()
    public = _cut_job(tier="public", job_id="pub-2")
    assert publish_run.plan_everything(public, "public", "autopilot", kinds={"full"}).topics == ()
    pub_settings.save({"topic_cuts": False})
    assert publish_run.plan_everything(public, "public", "autopilot").topics == ()


def test_plan_everything_leaves_out_a_cut_that_is_posted_skipped_blocked_or_has_no_file(channel):
    job = _cut_job(with_shorts=False)
    plan = publish_run.plan_everything(job, "public", "autopilot")
    publish_run.prepare(job, plan)
    publish_run.publish_job(job, api_main.job_store.update, plan)
    after = _reread(job.job_id)
    assert publish_run.plan_everything(after, "public", "autopilot") is None  # everything is posted
    other = _cut_job(job_id="pub-3")  # another processing run of the same meeting: blocked
    other.zoom_meeting_uuid = None
    assert publish_run.plan_everything(other, "public", "autopilot") is not None


def test_suggested_words_listed_finds_a_never_post_word_in_the_cut(monkeypatch):
    monkeypatch.setattr(never_post, "entries", lambda: ["investor"])
    job = _cut_job([topic_row(hook="The investor wanted a cheaper store.")])
    why = publish_run.suggested_words_listed(job)
    assert why is not None and "topic cut 1" in why and "investor" in why
    assert publish_run.suggested_words_listed(_cut_job(job_id="pub-2")) is None


def test_suggested_words_ignore_cuts_for_an_archive_team(monkeypatch):
    monkeypatch.setattr(never_post, "entries", lambda: ["investor"])
    job = _cut_job([topic_row(hook="The investor wanted a cheaper store.")], tier="archive")
    assert publish_run.suggested_words_listed(job) is None  # an archive team's cut is never posted


# ------------------------------------------------------------------ verdicts and the panel


def test_posting_verdict_waits_for_the_cut(channel):
    job = _cut_job(with_shorts=False)
    assert publish_run.posting_verdict(job) == "left"
    _run(job)
    after = _reread(job.job_id)
    assert publish_run.posting_verdict(after) == "done" and publish_run.everything_published(after) is True
    assert after.files_deleted is True  # delete_after: everything, the cut included, is on the channel


def test_posting_verdict_missing_when_the_cut_file_is_gone_and_check_when_unsure(channel):
    job = _cut_job(with_shorts=False)
    _run(job, topics=(), privacy="private")  # the full video is on the channel; only the cut is left
    job = _reread(job.job_id)
    path = get_settings().jobs_dir / job.job_id / "topic" / "topic_01.mp4"
    path.unlink()
    assert publish_run.posting_verdict(job) == "missing"
    path.write_bytes(b"cut")
    item = uploads.topic_item(job, 1, 100.0, 200.0)
    get_ledger().begin(item, title="t", privacy="private", by="person")
    get_ledger().unsure(item.key, "gone")
    assert publish_run.posting_verdict(job) == "check"


def test_posting_verdict_ignores_cuts_for_an_archive_team_or_when_off(channel):
    archive = _cut_job(tier="archive")
    assert publish_run.posting_verdict(archive) == "left"  # the full video and Shorts, as before
    pub_settings.save({"topic_cuts": False})
    public = _cut_job(tier="public", job_id="pub-2")
    _run(public, topics=(), title="Full video", shorts=[(1, None, None), (2, None, None), (3, None, None)])
    assert publish_run.posting_verdict(_reread("pub-2")) == "done"


def test_publish_options_list_the_cuts_for_the_page():
    job = _cut_job()
    options = publish_run.publish_options(job)
    (row,) = options["topics"]
    assert row["index"] == 1 and row["available"] and row["ok"] and row["front_door"]
    assert row["title"] == "Moving the session store to Redis | FloLabs IoT"
    assert row["at"] == "01:40" and row["duration_s"] == 100
    assert options["topic_cuts"] == {"on": True, "front_door": True}
    # a cut with a never-post word starts unticked
    never_post.set_entries(["redis"])
    assert publish_run.publish_options(job)["topics"][0]["ok"] is False


def test_a_job_that_was_posted_before_topic_cuts_loads_and_publishes(channel):
    from tests.test_publish import _finished_job

    job = _finished_job("old-1")
    assert job.youtube_topics == {} and publish_run.publish_options(job)["topics"] == []
    plan = publish_run.build_plan(job, title="Full video", privacy="private")
    publish_run.prepare(job, plan)
    assert publish_run.publish_job(job, api_main.job_store.update, plan) is None
    assert describe.load_topics(job) == []


def test_the_words_the_page_sends_back_unchanged_still_get_the_links(channel):
    """The page always sends the text it shows; text the person did not change is
    still the suggested text, so the cut gets the full-meeting link and a Short
    inside the cut links to the cut."""
    job = _cut_job()
    cut_info = describe.load_topics(job)[0]
    short_info = next(s for s in describe.load_shorts(job) if s.index == 1)
    shown_cut = describe.topic_description(cut_info, job, None)
    shown_short = describe.short_description(short_info, job, None)
    _run(job, shorts=[(1, None, shown_short)], topics=[(1, None, shown_cut)])
    full, cut = _full_and_cut(channel)
    full_id = next(i for i, v in channel.videos.items() if v is full)
    cut_id = next(i for i, v in channel.videos.items() if v is cut)
    assert f"Full meeting: https://youtu.be/{full_id}?t=100" in cut["description"]
    inside = _by_title(channel, "Why we moved to Postgres")
    assert f"https://youtu.be/{cut_id}?t=34" in inside["description"]
