import pytest

from clips import guard
from clips.config import MARKER, clips_settings
from clips.guard import GuardError
from clips.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "db" / "clips.sqlite")


def set_env(monkeypatch, **values):
    for key, value in values.items():
        monkeypatch.setenv("CLIPS_" + key.upper(), value)
    clips_settings.cache_clear()


# ---- folder

def test_init_storage_makes_folder_and_marker(tmp_path):
    folder = tmp_path / "fresh" / "clips"
    guard.init_storage(folder)
    assert (folder / MARKER).exists()


def test_init_storage_refuses_company_state(tmp_path):
    folder = tmp_path / "company"
    (folder / "state").mkdir(parents=True)
    (folder / "state" / "autopilot.json").write_text("{}")
    with pytest.raises(GuardError, match="company"):
        guard.init_storage(folder)
    assert not (folder / MARKER).exists()


def test_init_storage_refuses_company_jobs(tmp_path):
    folder = tmp_path / "company"
    (folder / "jobs" / "abc").mkdir(parents=True)
    (folder / "jobs" / "abc" / "job.json").write_text("{}")
    with pytest.raises(GuardError, match="job.json"):
        guard.init_storage(folder)


def test_init_storage_accepts_an_existing_clips_folder(clips_storage):
    guard.init_storage(clips_storage)


def test_assert_isolated_passes_in_the_test_setup():
    guard.assert_isolated()


def test_assert_isolated_needs_the_marker(clips_storage):
    (clips_storage / MARKER).unlink()
    with pytest.raises(GuardError, match="not marked"):
        guard.assert_isolated()


def test_assert_isolated_needs_matching_app_storage(tmp_path, monkeypatch):
    from core.config import get_settings

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "company-storage"))
    get_settings.cache_clear()
    with pytest.raises(GuardError, match="STORAGE_DIR"):
        guard.assert_isolated()


# ---- kill switch

def test_kill_switch_roundtrip():
    assert not guard.kill_switch_on()
    guard.set_kill_switch(True, "strike seen")
    assert guard.kill_switch_on()
    assert guard.kill_reason() == "strike seen"
    guard.set_kill_switch(False)
    assert not guard.kill_switch_on()
    guard.set_kill_switch(False)  # off twice is fine


# ---- channel

def test_check_channel_accepts_the_configured_one(monkeypatch):
    set_env(monkeypatch, channel_id="UCclips")
    guard.check_channel("UCclips")


@pytest.mark.parametrize("given", ["", "UCother"])
def test_check_channel_refuses_empty_and_other(monkeypatch, given):
    set_env(monkeypatch, channel_id="UCclips")
    with pytest.raises(GuardError):
        guard.check_channel(given)


def test_check_channel_refuses_when_not_configured():
    with pytest.raises(GuardError, match="CLIPS_CHANNEL_ID"):
        guard.check_channel("UCclips")


def test_check_channel_refuses_forbidden_even_if_configured(monkeypatch):
    set_env(monkeypatch, channel_id="UCcompany", forbidden_channel_ids="UCcompany, UCother")
    with pytest.raises(GuardError, match="never-post-to"):
        guard.check_channel("UCcompany")


# ---- breaker

def test_failures_in_a_row_open_the_breaker(store):
    for _ in range(2):
        guard.note_upload_result(store, False, "boom")
    assert not guard.breaker_open(store)
    guard.note_upload_result(store, False, "boom")
    assert guard.breaker_open(store)
    assert "boom" in guard.breaker_reason(store)


def test_success_resets_the_failure_count(store):
    guard.note_upload_result(store, False)
    guard.note_upload_result(store, False)
    guard.note_upload_result(store, True)
    guard.note_upload_result(store, False)
    assert not guard.breaker_open(store)


def test_strike_opens_at_once_and_success_does_not_close_it(store):
    guard.note_strike(store, "claim on clip abc")
    assert guard.breaker_open(store)
    guard.note_upload_result(store, True)
    assert guard.breaker_open(store)


def test_reset_closes_the_breaker(store):
    guard.note_strike(store, "x")
    guard.reset_breaker(store)
    assert not guard.breaker_open(store)
    guard.note_upload_result(store, False)
    assert not guard.breaker_open(store)


def test_breaker_limit_follows_settings(store, monkeypatch):
    set_env(monkeypatch, max_upload_failures="1")
    guard.note_upload_result(store, False, "once")
    assert guard.breaker_open(store)


def test_breaker_writes_an_event(store):
    guard.note_strike(store, "claim")
    assert any(e["kind"] == "breaker" for e in store.list_events())


def test_kill_switch_on_leaves_no_error_when_there_is_nothing_to_pull_back():
    guard.set_kill_switch(True, "strike")
    assert guard.kill_switch_on()


def _waiting_clip(store):
    from datetime import UTC, datetime, timedelta

    from clips.models import Clip, ClipStatus, Moment

    return store.save_clip(
        Clip(id="aaa", source_id="s", video_id="v", moment=Moment(start_s=1, end_s=20), status=ClipStatus.UPLOADED,
             youtube_id="vid1", publish_at=datetime.now(UTC) + timedelta(hours=3))
    )


def test_pull_back_off_the_clips_folder_never_reads_the_token(store, tmp_path, monkeypatch):
    """The company's STORAGE_DIR: the YouTube token there must not be read or refreshed."""
    from clips import uploader
    from core.config import get_settings
    from publish import youtube

    _waiting_clip(store)
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "company"))
    get_settings.cache_clear()

    def boom(*a, **k):
        raise AssertionError("the token was touched")

    monkeypatch.setattr(youtube, "_load_credentials", boom)
    with pytest.raises(GuardError):
        uploader.pull_back_pending(store)
    out = guard.pull_back_now(store)
    assert out["pulled_back"] == [] and "company" in out["skipped"]
    assert any(e["kind"] == "kill" and "NOT pulled back" in e["message"] for e in store.list_events())


def test_set_kill_switch_reports_what_was_pulled_back(monkeypatch):
    from clips import uploader

    db = clips_settings().db_path
    db.parent.mkdir(parents=True, exist_ok=True)
    clip = _waiting_clip(Store(db))
    monkeypatch.setattr(uploader, "pull_back_pending", lambda st, service=None: ([], [clip.id]))
    report = guard.set_kill_switch(True, "strike")
    assert report["failed"] == [clip.id] and guard.kill_switch_on()
