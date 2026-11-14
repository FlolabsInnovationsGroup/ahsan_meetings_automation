"""The autopilot holds odd meetings for a person instead of posting them."""

from datetime import timedelta

import pytest

import api.main as api_main
from autopilot import never_post
from ledger import uploads
from ledger.uploads import get_ledger
from publish import describe, settings
from tests.test_autopilot import T0, World, _meeting


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


def _cut(world: World, uuid="m1", minutes=None, topic="Weekly Sync"):
    """A meeting whose job finished with a final video of `minutes` (None = unknown)."""
    world.zoom.meetings[uuid] = _meeting(uuid, T0 - timedelta(hours=2), topic=topic)
    world.step()
    job = world.finish(world.meeting(uuid).job_id, uuid)
    job.title = topic
    if minutes is not None:
        job.artifacts["final.mp4"].duration_s = minutes * 60
    api_main.job_store.update(job)
    return job


def test_a_too_long_video_is_held(world):
    _cut(world, minutes=297)
    world.step(minutes=1)
    m = world.meeting("m1")
    assert m.state == "needs_attention" and world.channel.videos == {}
    assert m.error.startswith("Held for a person: the final video is 4 h 57 min long (over 120 min)")
    assert "Post it after all" in m.error


def test_a_too_short_video_is_held(world):
    _cut(world, minutes=1.5)
    world.step(minutes=1)
    m = world.meeting("m1")
    assert m.state == "needs_attention" and "under 3 min" in m.error and world.channel.videos == {}


def test_a_normal_length_posts(world):
    _cut(world, minutes=45)
    world.step(minutes=1)
    assert world.meeting("m1").state == "posted"


def test_unknown_length_skips_the_length_rules(world):
    _cut(world, minutes=None)
    world.step(minutes=1)
    assert world.meeting("m1").state == "posted"


def test_a_limit_of_zero_turns_the_rule_off(world):
    settings.save({"hold_over_minutes": 0})
    _cut(world, minutes=400)
    world.step(minutes=1)
    assert world.meeting("m1").state == "posted"


def test_a_title_already_on_the_channel_is_held(world):
    # with "topic_first" describe.full_title adds the day itself to dodge a posted title, so the
    # hold only fires for the meeting-and-date style, where the same meeting gets the same title
    settings.save({"title_style": "meeting_date"})
    job = _cut(world, minutes=45)
    other = job.model_copy(update={"zoom_meeting_uuid": "elsewhere", "job_id": "old-job"})
    item = uploads.full_item(other)
    get_ledger().begin(item, title=describe.full_title(job).upper(), privacy="public", by="person")
    get_ledger().published(item.key, "abcdefghijk", by="person")
    world.step(minutes=1)
    m = world.meeting("m1")
    assert m.state == "needs_attention" and "same title is already on the channel" in m.error
    assert len(world.channel.videos) == 0


def test_post_it_after_all_skips_the_hold_once(world):
    _cut(world, minutes=297)
    world.step(minutes=1)
    assert world.meeting("m1").state == "needs_attention"
    world.engine.retry("m1")
    assert world.meeting("m1").hold_cleared
    world.step(minutes=1)
    assert world.meeting("m1").state == "posted" and not world.meeting("m1").hold_cleared


def test_a_go_ahead_survives_a_round_that_goes_only_partly(world, monkeypatch):
    import publish.youtube as yt

    _cut(world, minutes=297)
    world.step(minutes=1)
    world.engine.retry("m1")
    real = yt.upload_video

    def quota(*args, **kwargs):
        raise RuntimeError("quotaExceeded")

    monkeypatch.setattr(yt, "upload_video", quota)
    world.step(minutes=1)
    m = world.meeting("m1")
    assert m.state == "posting" and m.hold_cleared  # not held again, waiting for the next round
    monkeypatch.setattr(yt, "upload_video", real)
    world.step(minutes=61)
    assert world.meeting("m1").state == "posted" and not world.meeting("m1").hold_cleared


def test_a_retry_that_reprocesses_does_not_skip_the_holds(world):
    _cut(world, minutes=297)
    world.step(minutes=1)
    with world.engine.store.editing() as state:
        state.meetings["m1"].job_id = None  # the job is gone: the retry cuts the meeting again
    world.engine.retry("m1")
    assert not world.meeting("m1").hold_cleared


def test_the_same_meeting_recorded_twice_is_held_with_the_default_title_style(world):
    assert settings.current().title_style == "topic_first"
    job = _cut(world, minutes=45)
    other = job.model_copy(update={"zoom_meeting_uuid": "elsewhere", "job_id": "old-job"})
    item = uploads.full_item(other)
    get_ledger().begin(item, title="A different title | FloLabs X", privacy="public", by="person")
    get_ledger().published(item.key, "abcdefghijk", by="person")
    world.step(minutes=1)
    m = world.meeting("m1")
    assert m.state == "needs_attention" and "same title is already on the channel" in m.error


def test_the_never_post_list_still_wins_after_a_retry(world):
    _cut(world, minutes=297)
    world.step(minutes=1)
    never_post.set_entries(["weekly sync"])
    with pytest.raises(ValueError, match="never-post|can't be posted"):
        world.engine.retry("m1")
    # even with the flag set (state written by an older retry), the list is looked at first
    with world.engine.store.editing() as state:
        state.meetings["m1"].state, state.meetings["m1"].hold_cleared = "posting", True
    world.step(minutes=1)
    assert world.meeting("m1").state == "skipped" and world.channel.videos == {}


def test_an_old_state_file_loads_without_the_flag():
    from autopilot.store import Meeting

    assert Meeting(uuid="x").hold_cleared is False


def test_a_repeat_is_held_in_the_topic_first_style_too(world):
    # full_title dodges a posted title by adding the day; the hold must still see the repeat
    settings.save({"title_style": "topic_first"})
    job = _cut(world, minutes=45)
    other = job.model_copy(update={"zoom_meeting_uuid": "elsewhere", "job_id": "old-job"})
    item = uploads.full_item(other)
    plain = min(describe.title_variants(job))
    get_ledger().begin(item, title=plain, privacy="public", by="person")
    get_ledger().published(item.key, "abcdefghijk", by="person")
    world.step(minutes=1)
    assert world.meeting("m1").state == "needs_attention"


def test_a_copy_still_uploading_on_another_account_holds_the_second_copy(world):
    # uploads run later, as real threads do: the first copy is not on the channel yet
    handed = []
    world.engine._background = handed.append
    _cut(world, "m1", minutes=45)
    world.step(minutes=1)
    assert len(handed) == 1 and world.meeting("m1").state == "posting"
    world.zoom.meetings["m2"] = _meeting("m2", T0 - timedelta(hours=2), topic="Weekly Sync")
    world.step(minutes=30)
    assert world.meeting("m2") is not None, world.engine.store.read()
    job = world.finish(world.meeting("m2").job_id, "m2")
    job.title = "Weekly Sync"
    job.artifacts["final.mp4"].duration_s = 45 * 60
    api_main.job_store.update(job)
    world.step(minutes=1)
    m2 = world.meeting("m2")
    assert m2.state == "needs_attention" and "same title is already on the channel" in m2.error
    assert len(handed) == 1
