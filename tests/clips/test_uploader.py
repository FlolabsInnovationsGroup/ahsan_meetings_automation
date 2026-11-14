import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from clips import guard, uploader
from clips.config import clips_settings
from clips.guard import GuardError
from clips.models import (
    Clip,
    ClipKind,
    ClipStatus,
    Copy,
    Moment,
    Platform,
    RightsBasis,
    RightsVerdict,
    Source,
)
from clips.store import Store
from tests.fake_channel import FakeChannel

CHANNEL = "UCclips"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "db" / "clips.sqlite")


@pytest.fixture
def env(monkeypatch):
    """A configured clips channel that is connected, rights that pass, a fake YouTube."""
    monkeypatch.setenv("CLIPS_CHANNEL_ID", CHANNEL)
    clips_settings.cache_clear()
    state = SimpleNamespace(rights=RightsVerdict(ok=True), channel=(CHANNEL, "Clips"))
    monkeypatch.setitem(sys.modules, "clips.rights", SimpleNamespace(check_clip=lambda c, s: state.rights))
    monkeypatch.setattr(uploader, "connected_channel", lambda service=None: state.channel)
    state.yt = FakeChannel().install(monkeypatch)
    return state


def make_clip(store, tmp_path, **kw) -> Clip:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0" * 1000)
    fields = {
        "id": "abc123def456", "source_id": "s", "video_id": "v", "kind": ClipKind.SHORT,
        "moment": Moment(start_s=1, end_s=20), "file": str(video),
        "text": Copy(title="Wild chase", description="A chase.", hashtags=["#gta6"]),
        "credit": "Footage: Someone", "rights": RightsBasis.OWN, "status": ClipStatus.SCHEDULED,
        "publish_at": datetime.now(UTC) + timedelta(hours=5),
    }
    fields.update(kw)
    return store.save_clip(Clip(**fields))


def source() -> Source:
    return Source(id="s", name="S", platform=Platform.FOLDER, rights=RightsBasis.OWN)


def test_uploads_private_with_publish_time(env, store, tmp_path):
    clip = make_clip(store, tmp_path)
    done = uploader.upload_clip(clip, source(), store)
    assert done.youtube_id and done.privacy == "private"
    sent = env.yt.uploaded[0]
    assert sent["privacy"] == "private"
    assert sent["publish_at"] == clip.publish_at
    assert sent["title"] == "Wild chase"
    assert "Footage: Someone" in sent["description"] and "#gta6" in sent["description"]
    assert sent["category_id"] == "20"
    assert store.get_flag("upload_failures", "0") == "0"


def test_the_ledger_stays_in_the_clips_folder(env, store, tmp_path, clips_storage):
    clip = make_clip(store, tmp_path)
    uploader.upload_clip(clip, source(), store)
    assert (clips_storage / "state" / "uploads.jsonl").exists()


def test_kill_switch_refuses_before_anything_is_sent(env, store, tmp_path):
    guard.set_kill_switch(True, "stop")
    with pytest.raises(GuardError, match="kill switch"):
        uploader.upload_clip(make_clip(store, tmp_path), source(), store)
    assert env.yt.calls == []


def test_not_isolated_refuses(env, store, tmp_path, clips_storage):
    (clips_storage / ".clips-storage").unlink()
    with pytest.raises(GuardError):
        uploader.upload_clip(make_clip(store, tmp_path), source(), store)
    assert env.yt.calls == []


def test_open_breaker_refuses(env, store, tmp_path):
    guard.note_strike(store, "claim")
    with pytest.raises(GuardError, match="breaker"):
        uploader.upload_clip(make_clip(store, tmp_path), source(), store)
    assert env.yt.calls == []


def test_failed_rights_refuse(env, store, tmp_path):
    env.rights = RightsVerdict(ok=False, reasons=["no basis"])
    with pytest.raises(GuardError, match="no basis"):
        uploader.upload_clip(make_clip(store, tmp_path), source(), store)
    assert env.yt.calls == []


def test_guard_order_kill_switch_before_rights(env, store, tmp_path):
    env.rights = RightsVerdict(ok=False, reasons=["no basis"])
    guard.set_kill_switch(True, "stop")
    with pytest.raises(GuardError, match="kill switch"):
        uploader.upload_clip(make_clip(store, tmp_path), source(), store)


@pytest.mark.parametrize("channel", [None, ("UCcompany", "Company"), ("", "")])
def test_wrong_or_missing_channel_never_receives_an_upload(env, store, tmp_path, channel):
    env.channel = channel
    with pytest.raises(GuardError):
        uploader.upload_clip(make_clip(store, tmp_path), source(), store)
    assert env.yt.calls == []


def test_forbidden_channel_refused_even_when_configured(env, store, tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPS_FORBIDDEN_CHANNEL_IDS", CHANNEL)
    clips_settings.cache_clear()
    with pytest.raises(GuardError, match="never-post-to"):
        uploader.upload_clip(make_clip(store, tmp_path), source(), store)
    assert env.yt.calls == []


def test_missing_file_or_text_is_refused(env, store, tmp_path):
    clip = make_clip(store, tmp_path, file=str(tmp_path / "nope.mp4"))
    with pytest.raises(GuardError, match="missing"):
        uploader.upload_clip(clip, source(), store)
    with pytest.raises(GuardError, match="title"):
        uploader.upload_clip(make_clip(store, tmp_path, text=None), source(), store)
    assert env.yt.calls == []


def test_dry_run_checks_and_sends_nothing(env, store, tmp_path):
    done = uploader.upload_clip(make_clip(store, tmp_path), source(), store, dry_run=True)
    assert done.youtube_id == "" and env.yt.calls == []


def test_dry_run_still_runs_the_guards(env, store, tmp_path):
    env.channel = ("UCcompany", "Company")
    with pytest.raises(GuardError):
        uploader.upload_clip(make_clip(store, tmp_path), source(), store, dry_run=True)


def test_a_clip_with_a_youtube_id_is_not_sent_again(env, store, tmp_path):
    clip = make_clip(store, tmp_path, youtube_id="vid12345678")
    done = uploader.upload_clip(clip, source(), store)
    assert done.youtube_id == "vid12345678" and env.yt.calls == []


def test_retry_after_a_cut_off_upload_makes_one_video(env, store, tmp_path):
    clip = make_clip(store, tmp_path)
    env.yt.crash_at = "after_last_byte"
    env.yt.raise_with = RuntimeError
    with pytest.raises(uploader.UploadError):
        uploader.upload_clip(clip, source(), store)
    assert store.get_clip(clip.id).status is ClipStatus.SCHEDULED
    done = uploader.upload_clip(store.get_clip(clip.id), source(), store)
    assert len(env.yt.videos) == 1 and done.youtube_id in env.yt.videos


def test_second_call_after_success_does_not_make_a_second_video(env, store, tmp_path):
    clip = make_clip(store, tmp_path)
    first = uploader.upload_clip(clip, source(), store)
    store.recover_stale_uploads(older_than_hours=0)  # the caller died before saving UPLOADED
    again = uploader.upload_clip(store.get_clip(clip.id), source(), store)
    assert again.youtube_id == first.youtube_id and len(env.yt.videos) == 1


def test_failed_uploads_open_the_breaker(env, store, tmp_path):
    env.yt.raise_with = RuntimeError
    clip = make_clip(store, tmp_path)
    for _ in range(3):
        env.yt.crash_at = "before_link"
        with pytest.raises(uploader.UploadError):
            uploader.upload_clip(clip, source(), store)
    assert guard.breaker_open(store)
    with pytest.raises(GuardError, match="breaker"):
        uploader.upload_clip(clip, source(), store)


def test_quota_error_is_translated(env, store, tmp_path, monkeypatch):
    from publish import youtube

    def boom(*args, **kwargs):
        raise RuntimeError("uploadLimitExceeded")

    monkeypatch.setattr(youtube, "upload_video", boom)
    with pytest.raises(uploader.UploadError, match="upload limit"):
        uploader.upload_clip(make_clip(store, tmp_path), source(), store)


def test_past_publish_time_is_refused_never_posted_at_once(env, store, tmp_path):
    clip = make_clip(store, tmp_path, publish_at=datetime.now(UTC) - timedelta(minutes=5))
    with pytest.raises(uploader.NotScheduledError, match="past"):
        uploader.upload_clip(clip, source(), store)
    assert env.yt.calls == []
    assert store.get_clip(clip.id).status is ClipStatus.SCHEDULED


def test_a_slot_seconds_away_is_refused_too(env, store, tmp_path):
    clip = make_clip(store, tmp_path, publish_at=datetime.now(UTC) + timedelta(seconds=20))
    with pytest.raises(uploader.NotScheduledError):
        uploader.upload_clip(clip, source(), store)
    assert env.yt.calls == []


def test_a_slot_under_fifteen_minutes_away_is_refused_and_not_a_guard_error(env, store, tmp_path):
    clip = make_clip(store, tmp_path, publish_at=datetime.now(UTC) + timedelta(minutes=14))
    with pytest.raises(uploader.NotScheduledError) as info:
        uploader.upload_clip(clip, source(), store)
    assert not isinstance(info.value, GuardError)
    assert env.yt.calls == []
    assert store.get_clip(clip.id).status is ClipStatus.SCHEDULED


def test_a_slot_just_over_fifteen_minutes_away_goes_up_private_with_publish_at(env, store, tmp_path):
    clip = make_clip(store, tmp_path, publish_at=datetime.now(UTC) + timedelta(minutes=16))
    done = uploader.upload_clip(clip, source(), store)
    assert done.privacy == "private"
    assert env.yt.uploaded[0]["privacy"] == "private"
    assert env.yt.uploaded[0]["publish_at"] == clip.publish_at


def test_a_clip_with_no_publish_time_is_refused_even_in_a_dry_run(env, store, tmp_path):
    clip = make_clip(store, tmp_path, publish_at=None)
    for dry in (False, True):
        with pytest.raises(uploader.NotScheduledError, match="no publish time"):
            uploader.upload_clip(clip, source(), store, dry_run=dry)
    assert env.yt.calls == [] and env.yt.uploaded == []


def test_dry_run_sends_nothing_and_a_late_clip_is_refused_in_it(env, store, tmp_path):
    ok = make_clip(store, tmp_path, publish_at=datetime.now(UTC) + timedelta(hours=2))
    result = uploader.upload_clip(ok, source(), store, dry_run=True)
    assert result.privacy == "private" and result.youtube_id == ""
    assert env.yt.uploaded == []
    late = make_clip(store, tmp_path, id="late00000001", publish_at=datetime.now(UTC) + timedelta(minutes=5))
    with pytest.raises(uploader.NotScheduledError):
        uploader.upload_clip(late, source(), store, dry_run=True)


def test_a_clip_taken_by_another_process_is_not_uploaded(env, store, tmp_path):
    clip = make_clip(store, tmp_path)
    assert store.claim_upload(clip.id)  # the other process got there first
    with pytest.raises(uploader.ClipBusy):
        uploader.upload_clip(clip, source(), store)
    assert env.yt.calls == []
    assert store.get_clip(clip.id).status is ClipStatus.UPLOADING
    assert store.get_flag("upload_failures", "0") == "0"  # not a failure


def test_a_second_process_with_a_stale_ledger_sees_the_first_upload(env, store, tmp_path):
    """Process A's upload response is lost; process B (older ledger in memory) retries: one video."""
    from ledger import uploads

    clip = make_clip(store, tmp_path)
    uploads.get_ledger()  # B loaded its ledger before A wrote anything
    env.yt.crash_at = "after_last_byte"
    env.yt.raise_with = RuntimeError
    with pytest.raises(uploader.UploadError):
        uploader.upload_clip(clip, source(), store)
    uploads._registry.clear()  # B is a different process: its ledger predates A's journal lines
    stale = uploads.get_ledger()
    stale._uploads.clear()
    uploads._registry[next(iter(uploads._registry))] = stale
    done = uploader.upload_clip(store.get_clip(clip.id), source(), store)
    assert len(env.yt.videos) == 1 and done.youtube_id in env.yt.videos


def test_sign_in_in_progress_holds_uploads(env, store, tmp_path, clips_storage):
    (clips_storage / uploader.CONNECT_FLAG).write_text(str(__import__("time").time()), encoding="utf-8")
    with pytest.raises(GuardError, match="sign-in"):
        uploader.upload_clip(make_clip(store, tmp_path), source(), store)
    assert env.yt.calls == []


def test_finish_auth_never_writes_a_forbidden_token(monkeypatch, tmp_path):
    """The token of a wrong channel is checked in memory and never reaches the token file."""
    from publish import youtube

    monkeypatch.setenv("CLIPS_CHANNEL_ID", CHANNEL)
    clips_settings.cache_clear()
    token_file = youtube._token_path()
    token_file.parent.mkdir(parents=True, exist_ok=True)

    def fake_finish(state, code):
        youtube._write_secret(token_file, "{}")
        return "/"

    monkeypatch.setattr(youtube, "finish_connect", fake_finish)
    monkeypatch.setattr(uploader, "_channel_of_token", lambda text: ("UCcompany", "Company"))
    with pytest.raises(GuardError):
        uploader.finish_auth("code")
    assert not token_file.exists()
    assert not (clips_settings().storage_dir / uploader.CONNECT_FLAG).exists()


def test_finish_auth_writes_the_token_for_the_clips_channel(monkeypatch):
    from publish import youtube

    monkeypatch.setenv("CLIPS_CHANNEL_ID", CHANNEL)
    clips_settings.cache_clear()
    token_file = youtube._token_path()
    token_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(youtube, "finish_connect", lambda s, c: youtube._write_secret(token_file, "{}") or "/")
    monkeypatch.setattr(uploader, "_channel_of_token", lambda text: (CHANNEL, "Clips"))
    assert uploader.finish_auth("code") == (CHANNEL, "Clips")
    assert token_file.read_text(encoding="utf-8") == "{}"


def test_kill_switch_pulls_back_uploaded_clips_that_are_not_public_yet(env, store, tmp_path, monkeypatch):
    soon = make_clip(store, tmp_path, id="aaa", status=ClipStatus.UPLOADED, youtube_id="vid1",
                     publish_at=datetime.now(UTC) + timedelta(hours=3))
    gone = make_clip(store, tmp_path, id="bbb", status=ClipStatus.UPLOADED, youtube_id="vid2",
                     publish_at=datetime.now(UTC) - timedelta(hours=3))  # already public
    assert [c.id for c in guard.pending_releases(store)] == [soon.id]
    updates = []

    class Svc:
        def videos(self):
            return self

        def update(self, part, body):
            updates.append(body)
            return self

        def execute(self):
            return {}

    done, failed = uploader.pull_back_pending(store, Svc())
    assert done == [soon.id] and failed == []
    assert updates[0]["status"]["privacyStatus"] == "private" and "publishAt" not in updates[0]["status"]
    assert store.get_clip(soon.id).status is ClipStatus.HELD
    assert store.get_clip(gone.id).status is ClipStatus.UPLOADED


def test_pull_back_failure_is_reported_not_hidden(env, store, tmp_path):
    clip = make_clip(store, tmp_path, status=ClipStatus.UPLOADED, youtube_id="vid1",
                     publish_at=datetime.now(UTC) + timedelta(hours=3))

    class Svc:
        def videos(self):
            return self

        def update(self, **kw):
            raise RuntimeError("quotaExceeded")

    done, failed = uploader.pull_back_pending(store, Svc())
    assert failed == [clip.id] and not done
    assert "Could not pull back" in store.get_clip(clip.id).error


def test_long_clip_uses_the_watch_link(env, store, tmp_path):
    clip = make_clip(store, tmp_path, kind=ClipKind.LONG)
    done = uploader.upload_clip(clip, source(), store)
    assert done.url.startswith("https://youtu.be/")


def test_title_is_cleaned_and_cut():
    assert uploader._title("A<b>" + "x" * 200).startswith("Ab")
    assert len(uploader._title("x" * 200)) == 100


def test_connected_channel_reads_the_signed_in_channel():
    class Svc:
        def channels(self):
            return self

        def list(self, **kw):
            assert kw["mine"] is True
            return self

        def execute(self):
            return {"items": [{"id": "UC1", "snippet": {"title": "Clips"}}]}

    assert uploader.connected_channel(Svc()) == ("UC1", "Clips")


def test_connected_channel_is_none_without_a_token():
    assert uploader.connected_channel() is None


def test_finish_auth_refuses_and_disconnects_a_forbidden_channel(monkeypatch):
    from publish import youtube

    monkeypatch.setenv("CLIPS_CHANNEL_ID", CHANNEL)
    clips_settings.cache_clear()
    calls = []
    monkeypatch.setattr(youtube, "finish_connect", lambda state, code: calls.append("finish") or "/")
    monkeypatch.setattr(youtube, "disconnect", lambda: calls.append("disconnect"))
    monkeypatch.setattr(uploader, "connected_channel", lambda service=None: ("UCcompany", "Company"))
    with pytest.raises(GuardError):
        uploader.finish_auth("code")
    assert calls == ["finish", "disconnect"]


def test_finish_auth_returns_the_configured_channel(monkeypatch):
    from publish import youtube

    monkeypatch.setenv("CLIPS_CHANNEL_ID", CHANNEL)
    clips_settings.cache_clear()
    monkeypatch.setattr(youtube, "finish_connect", lambda state, code: "/")
    monkeypatch.setattr(uploader, "connected_channel", lambda service=None: (CHANNEL, "Clips"))
    assert uploader.finish_auth("code") == (CHANNEL, "Clips")
