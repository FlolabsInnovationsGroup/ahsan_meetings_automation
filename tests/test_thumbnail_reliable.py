"""Every full meeting and topic video gets our thumbnail, reliably: retries
instead of final skips, the wait for processing, the block re-check, the
fallbacks when a picture cannot be built, the dedupe on video id, the quota
order and the daily sweep. Synthetic data only; no network."""

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from autopilot import never_post
from ledger import files
from ledger.uploads import Item, get_ledger
from publish import extras, optional, quota, thumbnail, youtube
from publish import settings as publishing_settings
from publish import youtube_extra as ye
from tests.test_thumbnail import FakeHttpError


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(extras, "_builders", {})
    monkeypatch.setattr(extras, "_runners", {})
    monkeypatch.setattr(extras, "_loaded", True)
    monkeypatch.setattr(youtube, "granted_scopes", lambda: set(youtube.SCOPES))
    monkeypatch.setattr(youtube, "youtube_status", lambda: {"connected": True})
    monkeypatch.setattr(quota, "_limits", lambda: (10000, 2000))
    monkeypatch.setattr(ye, "processing_status", lambda vid: {"processingStatus": "succeeded"})


_REAL_CURRENT = publishing_settings.current


def _cfg(**changes):
    return dataclasses.replace(_REAL_CURRENT(), thumbnail_ai=False, **changes)


def _task(tmp_path, video_kind="full", attempts=0):
    jpg = tmp_path / "t.jpg"
    jpg.write_bytes(b"\xff\xd8x")
    return extras.Task(kind="thumbnail", video_id="vid1", video_kind=video_kind, file=str(jpg), attempts=attempts)


def _posted(n, title="Moving the session store to Redis | FloLabs Space", meeting="Space Weekly", kind="full"):
    item = Item(f"job:{n}|{kind}", f"job:{n}", kind, f"job{n}", meeting, topic=meeting)
    get_ledger().begin(item, title=title, privacy="public", by="test")
    return item, get_ledger().published(item.key, f"vid{n:08d}", by="test")


def _raiser(exc):
    def fn(*a, **k):
        raise exc

    return fn


# ---------------------------------------------------- 2 and 4: retries, not final skips


@pytest.mark.parametrize("exc", [FakeHttpError(403, "forbidden"), FakeHttpError(404), FakeHttpError(400, "badRequest"),
                                 ValueError("library glitch"), FakeHttpError(418)])
def test_a_thumbnail_error_is_retried_with_back_off_not_skipped(exc):
    out = optional.call("thumbnails", 50, _raiser(exc), retry_unknown=True)
    assert out.status == optional.RETRY and out.retry_at is not None


def test_a_404_or_400_is_final_after_the_early_attempts():
    for exc in (FakeHttpError(404), FakeHttpError(400)):
        assert optional.call("thumbnails", 50, _raiser(exc), attempt=0, retry_unknown=True).status == optional.RETRY
        late = optional.call("thumbnails", 50, _raiser(exc), attempt=optional.UNKNOWN_RETRIES, retry_unknown=True)
        assert late.status == optional.SKIPPED


def test_other_features_keep_the_final_skip_for_a_404():
    assert optional.call("playlists", 50, _raiser(FakeHttpError(404))).status == optional.SKIPPED


def test_an_error_with_no_answer_that_is_not_the_network_is_retried_then_given_up():
    out = optional.call("captions", 1, _raiser(RuntimeError("token read glitch")))
    assert out.status == optional.RETRY and out.retry_at is not None
    task = extras.Task(kind="captions", created_at=extras._iso(datetime.now(UTC) - timedelta(days=30)))
    extras._add(task, None, SimpleNamespace(key="k"), SimpleNamespace(video_id="v"), "full", None)
    saved = extras.all_tasks()[0]
    extras._apply(saved, out, datetime.now(UTC))
    assert extras._get(saved.id).state == extras.GIVEN_UP  # after the retry window


def test_a_403_still_blocks_thumbnails_for_a_day_but_is_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(ye, "set_thumbnail", _raiser(FakeHttpError(403, "forbidden")))
    out = thumbnail.run(_task(tmp_path))
    assert out.status == optional.RETRY
    assert extras._blocked_until("thumbnail", datetime.now(UTC)) is not None


@pytest.mark.parametrize("kind", ["full", "topic"])
def test_full_and_topic_wait_for_processing_like_a_short(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(ye, "processing_status", lambda vid: {"processingStatus": "processing"})
    monkeypatch.setattr(ye, "set_thumbnail", lambda *a: pytest.fail("set before processing finished"))
    now = datetime.now(UTC)
    first = thumbnail.run(_task(tmp_path, kind, attempts=0))
    second = thumbnail.run(_task(tmp_path, kind, attempts=1))
    assert first.status == second.status == optional.RETRY
    assert timedelta(minutes=29) < first.retry_at - now < timedelta(minutes=31)
    assert timedelta(hours=1, minutes=59) < second.retry_at - now < timedelta(hours=2, minutes=1)
    monkeypatch.setattr(ye, "processing_status", lambda vid: {"processingStatus": "succeeded"})
    monkeypatch.setattr(ye, "set_thumbnail", lambda *a: {})
    assert thumbnail.run(_task(tmp_path, kind)).status == optional.DONE


def test_a_video_not_listed_yet_is_looked_for_again_before_it_is_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(ye, "processing_status", lambda vid: None)
    monkeypatch.setattr(ye, "set_thumbnail", lambda *a: pytest.fail("set on a missing video"))
    for attempts in range(thumbnail.GONE_RETRIES):
        assert thumbnail.run(_task(tmp_path, "short", attempts)).status == optional.RETRY
    gone = thumbnail.run(_task(tmp_path, "short", thumbnail.GONE_RETRIES))
    assert gone.status == optional.SKIPPED and "gone" in gone.reason


def test_a_failed_processing_ends_it(tmp_path, monkeypatch):
    monkeypatch.setattr(ye, "processing_status", lambda vid: {"processingStatus": "failed"})
    assert thumbnail.run(_task(tmp_path, "full")).status == optional.SKIPPED


# ---------------------------------------------------------------- 3: the block re-check


def _queue_two_thumbnails():
    for n in (1, 2):
        item, upload = _posted(n)
        extras._add(extras.Task(kind="thumbnail"), None, item, upload, "full", None)


def test_one_refusal_holds_back_the_rest_of_the_run_instead_of_skipping_them():
    _queue_two_thumbnails()
    calls = []

    def runner(task):
        calls.append(task.video_id)
        extras.block_kind("thumbnail", 24)  # what a 403 does
        return optional.Outcome(optional.RETRY, reason="refused", retry_at=datetime.now(UTC) + timedelta(days=1))

    extras.register("thumbnail", lambda *a, **k: None, runner)
    extras.run_due()
    assert len(calls) == 1
    states = {t.video_id: t for t in extras.all_tasks()}
    assert all(t.state == extras.WAITING for t in states.values())
    held = next(t for t in states.values() if t.video_id not in calls)
    assert extras._parse(held.not_before) > datetime.now(UTC) + timedelta(hours=23)


# ------------------------------------------------------ 5 and 6: fallbacks when building


def test_a_crash_building_from_the_frame_falls_back_to_the_title_card(tmp_path, monkeypatch):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not a picture")
    monkeypatch.setattr(thumbnail, "_base_row", lambda *a: (bad, {}))
    item, upload = _posted(1)
    job = SimpleNamespace(job_id="job1", id="job1", title="Space Weekly")
    task = thumbnail.build(job, item, upload, video_kind="full", index=None, cfg=_cfg(thumbnail_mode="custom"))
    assert task.state == extras.WAITING and Path(task.file).is_file()
    assert task.data["card"] is True and task.data["design"] == "topic"


def test_when_even_the_card_fails_a_visible_failed_task_is_saved_for_the_sweep(monkeypatch):
    monkeypatch.setattr(thumbnail, "compose", _raiser(RuntimeError("pillow")))
    item, upload = _posted(1)
    job = SimpleNamespace(job_id="job1", id="job1", title="Space Weekly")
    task = thumbnail.build(job, item, upload, video_kind="full", index=None, cfg=_cfg(thumbnail_mode="custom"))
    assert task.state == extras.SKIPPED and task.reason.startswith(thumbnail.FAILED_TEXT)
    extras._add(task, job, item, upload, "full", None)
    assert extras.all_tasks()[0].state == extras.SKIPPED  # saved, visible on the page


def test_the_neutral_card_is_the_team_the_series_name_and_the_brand(tmp_path):
    out = tmp_path / "n.jpg"
    data = thumbnail._neutral_card("Space", "3A5BA0", out, thumbnail.SIZE_FULL)
    assert data["design"] == "neutral" and data["label"] == "Space" and out.is_file()


def test_a_never_post_series_name_is_not_drawn_on_the_neutral_card(tmp_path):
    never_post.set_entries(["secret"])
    data = thumbnail._neutral_card("Secret", "3A5BA0", tmp_path / "n.jpg", thumbnail.SIZE_FULL)
    assert data["label"] == thumbnail.BRAND


# ------------------------------------------------------------- 7: dedupe on the video id


def test_a_re_upload_with_a_new_video_id_gets_its_own_task_and_the_same_video_only_one():
    item, upload = _posted(1)
    extras._add(extras.Task(kind="thumbnail"), None, item, upload, "full", None)
    extras._add(extras.Task(kind="thumbnail"), None, item, upload, "full", None)
    assert len(extras.all_tasks()) == 1
    again = SimpleNamespace(video_id="otherVideo01")  # the same item uploaded again
    extras._add(extras.Task(kind="thumbnail"), None, item, again, "full", None)
    assert sorted(t.video_id for t in extras.all_tasks()) == sorted([upload.video_id, "otherVideo01"])


# ------------------------------------------------------------------ 8: the quota order


def test_thumbnails_run_before_caption_tracks_and_playlists():
    order = []
    item, upload = _posted(1)
    for kind in ("captions", "playlist", "thumbnail"):
        extras._add(extras.Task(kind=kind), None, item, upload, "full", None)
        extras.register(kind, lambda *a, **k: None, lambda task: order.append(task.kind) or optional.Outcome(optional.DONE))
    extras.run_due()
    assert order == ["thumbnail", "playlist", "captions"]


# --------------------------------------------------------------- 9: the 2 MB limit


def _noise(size=(1280, 720)):
    from PIL import Image

    return Image.frombytes("RGB", size, __import__("os").urandom(size[0] * size[1] * 3))


def test_a_picture_too_big_even_at_low_quality_is_shrunk_until_it_fits(tmp_path, monkeypatch):
    monkeypatch.setattr(thumbnail, "MAX_BYTES", 400_000)
    out = thumbnail._save_jpeg(_noise(), tmp_path / "a.jpg")
    assert out.stat().st_size <= 400_000


def test_a_picture_that_can_never_fit_is_refused_not_saved_silently(tmp_path, monkeypatch):
    monkeypatch.setattr(thumbnail, "MAX_BYTES", 50)
    with pytest.raises(ValueError):
        thumbnail._save_jpeg(_noise((400, 300)), tmp_path / "b.jpg")


# ----------------------------------------------------------------- the daily mechanism


def test_a_daily_pass_runs_once_a_day():
    runs = []
    extras.register("thumbnail", lambda *a, **k: None, lambda t: optional.Outcome(optional.DONE))
    extras.register_daily("thumbnail", "t_daily", lambda: runs.append(1))
    now = datetime.now(UTC)
    extras._daily_passes(now)
    extras._daily_passes(now + timedelta(hours=23))
    assert runs == [1]
    extras._daily_passes(now + timedelta(hours=25))
    assert runs == [1, 1]


def test_a_daily_pass_that_returns_false_is_tried_again_next_run():
    runs = []
    extras.register("thumbnail", lambda *a, **k: None, lambda t: optional.Outcome(optional.DONE))
    extras.register_daily("thumbnail", "t_daily2", lambda: runs.append(1) or False)
    extras._daily_passes(datetime.now(UTC))
    extras._daily_passes(datetime.now(UTC))
    assert runs == [1, 1]


def test_the_sweep_is_registered_as_a_daily_pass():
    import importlib

    importlib.reload(thumbnail)  # registers into the test's own (empty) lists
    assert any(name == "thumbnail_sweep_missing" for _k, name, _f in extras._daily)


# ---------------------------------------------------------------------- the sweep


@pytest.fixture
def custom(monkeypatch):
    cfg = _cfg(thumbnail_mode="custom")
    monkeypatch.setattr(publishing_settings, "current", lambda: cfg)
    return cfg


def _thumb_tasks(video_id):
    return [t for t in extras.all_tasks() if t.kind == "thumbnail" and t.video_id == video_id]


def test_the_sweep_queues_a_picture_for_a_posted_video_that_never_had_one(custom):
    _item, upload = _posted(1)
    assert thumbnail.sweep_missing() is True
    [task] = _thumb_tasks(upload.video_id)
    assert task.state == extras.WAITING and Path(task.file).is_file() and task.video_kind == "full"
    assert task.data["arm"] == "custom"
    thumbnail.sweep_missing()  # a second pass adds nothing
    assert len(_thumb_tasks(upload.video_id)) == 1


def test_the_sweep_leaves_done_and_waiting_ones_alone_and_skips_shorts(custom):
    _i1, done = _posted(1)
    extras._add(extras.Task(kind="thumbnail", state=extras.DONE), None, _i1, done, "full", None)
    _i2, short = _posted(2, kind="short")
    thumbnail.sweep_missing()
    assert [t.state for t in _thumb_tasks(done.video_id)] == [extras.DONE]
    assert _thumb_tasks(short.video_id) == []


def test_the_sweep_rebuilds_a_given_up_task_but_not_one_that_ended_for_good(custom):
    i1, retryable = _posted(1)
    extras._add(extras.Task(kind="thumbnail", state=extras.GIVEN_UP, reason="YouTube is still processing this video",
                            data={"arm": "custom", "label": ""}), None, i1, retryable, "full", None)
    i2, gone = _posted(2)
    extras._add(extras.Task(kind="thumbnail", state=extras.SKIPPED, reason="The video or playlist is gone"),
                None, i2, gone, "full", None)
    thumbnail.sweep_missing()
    [again] = _thumb_tasks(retryable.video_id)
    assert again.state == extras.WAITING and Path(again.file).is_file() and again.attempts == 0
    [stays] = _thumb_tasks(gone.video_id)
    assert stays.state == extras.SKIPPED


def test_the_old_no_picture_skip_is_rebuilt_as_the_neutral_card(custom):
    item, upload = _posted(1, title="日本語のタイトル | FloLabs Space")
    extras._add(extras.Task(kind="thumbnail", state=extras.SKIPPED, reason=(
        "No usable picture from the recording and no safe words for a card, so YouTube keeps its own picture")),
        None, item, upload, "full", None)
    thumbnail.sweep_missing()
    [task] = _thumb_tasks(upload.video_id)
    assert task.state == extras.WAITING and task.data["design"] == "neutral"


def test_the_sweep_never_touches_a_never_post_meeting(custom):
    _item, upload = _posted(1, meeting="Operations Weekly")
    never_post.set_entries(["operations weekly"])
    thumbnail.sweep_missing()
    assert _thumb_tasks(upload.video_id) == []


def test_the_sweep_skips_the_auto_arm_under_test_and_everything_when_off(monkeypatch):
    _item, upload = _posted(1)
    files.append_jsonl(thumbnail._experiments_path(), {"video_id": upload.video_id, "feature": "thumbnail",
                                                       "arm": "auto"})
    monkeypatch.setattr(publishing_settings, "current", lambda: _cfg(thumbnail_mode="test"))
    thumbnail.sweep_missing()
    assert _thumb_tasks(upload.video_id) == []
    monkeypatch.setattr(publishing_settings, "current", lambda: _cfg(thumbnail_mode="off"))
    thumbnail.sweep_missing()
    assert _thumb_tasks(upload.video_id) == []


def test_the_sweep_caps_the_pictures_per_day(custom, monkeypatch):
    monkeypatch.setattr(thumbnail, "SWEEP_PER_DAY", 2)
    for n in range(1, 5):
        _posted(n)
    thumbnail.sweep_missing()
    assert len([t for t in extras.all_tasks() if t.kind == "thumbnail"]) == 2


def test_an_unsure_upload_a_person_settled_gets_its_thumbnail_queued(custom):
    item = Item("job:9|full", "job:9", "full", "job9", "Space Weekly", topic="Space Weekly")
    get_ledger().begin(item, title="Redis moves | FloLabs Space", privacy="public", by="test")
    get_ledger().unsure(item.key, "no answer")
    get_ledger().end(item.key)
    upload = get_ledger().resolve(item.key, on_youtube=True, url="https://www.youtube.com/watch?v=abcdefghijk")
    assert thumbnail.queue_for_posted(upload) is True
    assert len(_thumb_tasks("abcdefghijk")) == 1


def test_a_task_in_the_json_file_survives_the_daily_state(custom):
    _posted(1)
    extras.register("thumbnail", lambda *a, **k: None, lambda t: optional.Outcome(optional.DONE))
    extras.register_daily("thumbnail", "keep", lambda: True)
    extras._daily_passes(datetime.now(UTC))
    state = json.loads(extras._path().read_text(encoding="utf-8"))
    assert "keep" in state["daily"]
