"""The team gate in the autopilot and the publish plan: a new team waits for a
person, never-tier teams are never posted, archive teams go up unlisted with
the full video only, and the never-post list always runs first."""

from datetime import timedelta

import pytest

import api.main as api_main
from autopilot import never_post
from core.models import JobRecord, JobStatus
from publish import run as publish_run
from publish import series, settings
from tests.test_autopilot import T0, World, _meeting

TOPIC = "Gadget Review"
KEY = series.series_key(TOPIC)


@pytest.fixture
def world(monkeypatch):
    w = World(monkeypatch)
    settings.save({"series_mode": "allow_list"})  # the default these tests are about
    return w


def _arrive(world: World, uuid="m1", topic=TOPIC):
    world.zoom.meetings[uuid] = _meeting(uuid, T0 - timedelta(hours=2), topic=topic)
    world.step()
    return world.meeting(uuid)


def _finish(world: World, uuid="m1", topic=TOPIC):
    job = world.finish(world.meeting(uuid).job_id, uuid)
    job.title = topic
    api_main.job_store.update(job)
    return job


def _gets(world: World):
    return [c for c in world.zoom.calls if c[0] == "get"]


def test_an_unknown_team_is_held_at_first_sight(world):
    m = _arrive(world)
    assert m.state == "needs_attention" and m.series_hold and m.series_key == KEY
    assert m.error == f'Held for a person: "{TOPIC}" is a new team. Post this series from now on, or Skip.'
    world.step(minutes=5)
    assert world.submitted == [] and world.channel.videos == {}
    assert series.lookup(TOPIC).seen == 1  # counted once, not once a check


def test_allow_series_releases_it_and_it_posts(world):
    _arrive(world)
    released = world.engine.allow_series(KEY)
    assert [m.uuid for m in released] == ["m1"]
    assert series.lookup(TOPIC).tier == "public"
    m = world.meeting("m1")
    assert m.state == "waiting_recording" and not m.series_hold and "m1" in world.engine.store.read().nudges
    world.step(minutes=1)
    assert world.meeting("m1").state == "processing" and len(world.submitted) == 1
    _finish(world)
    world.step(minutes=1)
    assert world.meeting("m1").state == "posted" and world.channel.videos


def test_allow_series_never_brings_back_a_skipped_meeting(world):
    _arrive(world)
    world.engine.skip("m1")
    assert not world.meeting("m1").series_hold
    assert world.engine.allow_series(KEY) == []
    assert world.meeting("m1").state == "skipped"


def test_the_never_tier_skips_without_asking_zoom(world):
    series.set_row(KEY, tier="never")
    m = _arrive(world)
    assert m.state == "skipped" and not m.series_hold
    label = series.lookup(TOPIC).label
    assert m.error == f'Never posted: the team "{label}" is set to never in the Teams list.'
    assert _gets(world) == [] and world.submitted == []


def test_the_never_tier_stops_a_meeting_zoom_already_knows(world):
    # a meeting first seen by the webhook path (get_meeting) is stopped by the gate itself
    _arrive(world)
    world.engine.never_series(KEY)
    assert world.meeting("m1").state == "skipped"
    world.step(minutes=5)
    assert world.submitted == []


def test_never_post_beats_a_public_team(world):
    series.set_row(KEY, tier="public")
    never_post.set_entries([KEY])
    m = _arrive(world)
    assert m.state == "skipped" and m.error.startswith("Never posted: ")
    assert "team" not in m.error  # the never-post list answered, not the Teams list
    assert world.submitted == []


def test_retry_refuses_on_a_series_hold(world):
    _arrive(world)
    with pytest.raises(ValueError, match="Decide the team first"):
        world.engine.retry("m1")
    assert world.meeting("m1").state == "needs_attention" and world.meeting("m1").series_hold


def test_post_it_after_all_does_not_bypass_a_series_hold(world):
    _arrive(world)
    world.meeting("m1")  # error text starts with "Held for a person", like a length hold
    with pytest.raises(ValueError):
        world.engine.retry("m1")
    world.step(minutes=5)
    assert world.meeting("m1").series_hold and not world.meeting("m1").hold_cleared
    assert world.submitted == [] and world.channel.videos == {}


def test_a_name_with_no_team_in_it_is_held_too(world):
    m = _arrive(world, topic="Weekly Team Sync")
    assert m.state == "needs_attention" and m.series_hold and not m.series_key
    assert m.error == "Held for a person: this meeting's name has no team in it. Post it after all, or Skip."
    world.step(minutes=5)
    assert world.submitted == [] and world.channel.videos == {}


def test_post_it_after_all_releases_a_held_meeting_with_no_team(world):
    _arrive(world, topic="Weekly Team Sync")
    world.engine.retry("m1")  # no team to allow: the person's say is for this meeting
    m = world.meeting("m1")
    assert not m.series_hold and m.series_cleared and m.state == "waiting_recording"
    world.step(minutes=1)
    assert world.meeting("m1").state == "processing" and not world.meeting("m1").series_hold
    assert len(world.submitted) == 1
    _finish(world, topic="Weekly Team Sync")
    world.step(minutes=1)
    assert world.meeting("m1").state == "posted" and world.channel.videos


def test_skip_a_meeting_with_no_team(world):
    _arrive(world, topic="Standup")
    world.engine.skip("m1")
    m = world.meeting("m1")
    assert m.state == "skipped" and not m.series_hold and not m.series_cleared


def test_a_team_decided_by_hand_lets_retry_through(world):
    _arrive(world)
    series.set_row(KEY, tier="public")
    world.engine.retry("m1")
    assert not world.meeting("m1").series_hold and world.meeting("m1").state == "waiting_recording"


def test_archive_posts_the_full_video_unlisted_with_no_shorts(world):
    series.set_row(KEY, tier="archive")
    _arrive(world)
    world.step(minutes=1)
    _finish(world)
    world.step(minutes=1)
    assert world.meeting("m1").state == "posted"
    videos = list(world.channel.videos.values())
    assert len(videos) == 1
    assert "unlisted" in repr(videos[0]).lower()


def test_archive_set_to_skip_is_not_posted(world):
    settings.save({"series_archive_privacy": "skip"})
    series.set_row(KEY, tier="archive")
    m = _arrive(world)
    assert m.state == "skipped" and m.error == f'Not posted: "{series.lookup(TOPIC).label}" is an archive team.'


def test_allow_all_changes_nothing(world):
    settings.save({"series_mode": "allow_all"})
    m = _arrive(world)
    assert m.state in ("ready", "processing") and not m.series_hold
    world.step(minutes=1)
    _finish(world)
    world.step(minutes=1)
    assert world.meeting("m1").state == "posted"


def test_a_tier_changed_after_processing_is_honoured(world):
    series.set_row(KEY, tier="public")
    _arrive(world)
    world.step(minutes=1)
    _finish(world)
    series.set_row(KEY, tier="never")  # between processing and posting
    world.step(minutes=1)
    m = world.meeting("m1")
    assert m.state == "skipped" and "set to never" in m.error and world.channel.videos == {}


def test_a_team_deleted_after_processing_is_held_not_posted(world):
    series.set_row(KEY, tier="public")
    _arrive(world)
    world.step(minutes=1)
    _finish(world)
    series.delete_row(KEY)
    world.step(minutes=1)
    m = world.meeting("m1")
    assert m.state == "needs_attention" and m.series_hold and world.channel.videos == {}
    world.engine.allow_series(KEY)  # the job is done: it goes straight to posting
    assert world.meeting("m1").state == "posting"
    world.step(minutes=1)
    assert world.meeting("m1").state == "posted"


def test_skip_clears_the_series_hold(world):
    _arrive(world)
    m = world.engine.skip("m1")
    assert m.state == "skipped" and not m.series_hold


def test_old_files_load_without_the_new_fields():
    from autopilot.store import Meeting

    m = Meeting.model_validate({"uuid": "x", "topic": "Anything"})
    assert m.series_key is None and m.series_hold is False


def _job(topic: str) -> JobRecord:
    return JobRecord(job_id="j1", status=JobStatus.DONE, title=topic, zoom_meeting={"topic": topic, "meeting_id": "5551"})


def test_build_plan_refuses_a_never_tier_team():
    series.set_row(KEY, tier="never")
    with pytest.raises(publish_run.PublishRefused) as refused:
        publish_run.build_plan(_job(TOPIC), title="T")
    assert refused.value.status_code == 403 and refused.value.args[0] == "This team is set to never in the Teams list."


def test_build_plan_answers_the_never_post_list_first():
    series.set_row(KEY, tier="never")
    never_post.set_entries([KEY])
    with pytest.raises(publish_run.PublishRefused) as refused:
        publish_run.build_plan(_job(TOPIC), title="T")
    assert refused.value.status_code == 403 and "never-post list" in refused.value.args[0]


def test_publish_options_names_the_team():
    series.set_row(KEY, tier="archive")
    info = publish_run.publish_options(_job(TOPIC))["series"]
    assert info == {"key": KEY, "label": series.lookup(TOPIC).label, "tier": "archive"}
    other = publish_run.publish_options(_job("Totally New Thing"))["series"]
    assert other["tier"] == "unknown" and other["key"] == series.series_key("Totally New Thing")
