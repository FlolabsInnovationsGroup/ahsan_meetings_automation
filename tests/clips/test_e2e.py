"""The whole clips flow with the real modules; only the outside world is faked.

Faked: ffprobe/ffmpeg (they write small files), the language model, and YouTube
(tests/fake_channel.py records what was sent)."""

import os
import subprocess
from pathlib import Path

import pytest

from clips import guard, render, signals, sources, uploader
from clips.config import clips_settings
from clips.models import ClipStatus, Fetched, Platform, RightsBasis, VideoStatus
from clips.runner import Runner
from clips.sources import RightsError
from clips.store import Store
from tests.fake_channel import FakeChannel

CHANNEL = "UCclips"


class FakeLLM:
    """Answers every question with a title; the picker falls back to its own windows."""

    def json(self, system, user, *, images=None, max_tokens=2000):
        return {"title": "Wild chase through the city", "description_first_line": "A chase.", "tags": ["gta 6"]}


@pytest.fixture
def world(tmp_path, monkeypatch):
    import core.proc

    monkeypatch.setenv("CLIPS_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("CLIPS_LONG_PER_VIDEO", "0")
    monkeypatch.setenv("CLIPS_SHORTS_PER_VIDEO", "2")
    clips_settings.cache_clear()

    def fake_run(cmd, timeout=None, **kw):
        Path(cmd[-1]).write_bytes(b"fake media")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(core.proc, "run_checked", fake_run)
    monkeypatch.setattr(signals, "_run_ffmpeg_stderr", lambda args: "")
    monkeypatch.setattr(render, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(render, "_probe_size", lambda p: (1920, 1080))
    monkeypatch.setattr(render, "_fonts_dir", lambda: None)
    monkeypatch.setattr(
        "clips.fetch.probe",
        lambda p: Fetched(video_path=str(p), width=1920, height=1080, fps=30, duration_s=300, has_audio=True),
    )
    monkeypatch.setattr(uploader, "connected_channel", lambda service=None: (CHANNEL, "Clips"))
    youtube = FakeChannel().install(monkeypatch)

    folder = tmp_path / "footage"
    folder.mkdir()
    video = folder / "run1.mp4"
    video.write_bytes(b"fake video")
    old = video.stat().st_mtime - 3600  # the folder watcher skips files still being written
    os.utime(video, (old, old))

    store = Store()
    runner = Runner(store, llm=FakeLLM())
    return type("World", (), {"store": store, "runner": runner, "yt": youtube, "folder": folder})


def add_own_source(world, **kw):
    return sources.add_source(
        world.store, name="My footage", platform=Platform.FOLDER, rights=RightsBasis.OWN,
        folder=str(world.folder), enabled=True, **kw,
    )


def drive_to_scheduled(world):
    assert world.runner.watch() == 1
    assert world.runner.process(limit=5) >= 1
    drafts = world.store.list_clips(status=ClipStatus.DRAFT)
    assert drafts
    for clip in drafts:
        world.runner.approve(clip.id)
    assert world.runner.schedule() == len(drafts)
    return drafts


def test_whole_flow_uploads_private_with_a_publish_time(world):
    source = add_own_source(world)
    drive_to_scheduled(world)
    video = world.store.list_videos(source_id=source.id)[0]
    assert video.status is VideoStatus.DONE

    assert world.runner.upload_due() >= 1
    sent = world.yt.uploaded[0]
    assert sent["privacy"] == "private"
    assert sent["publish_at"] is not None
    assert sent["title"] == "Wild chase through the city"
    assert "A chase." in sent["description"] and "#gta6" in sent["description"]
    assert sent["category_id"] == "20"
    uploaded = world.store.list_clips(status=ClipStatus.UPLOADED)
    assert uploaded and all(c.youtube_id for c in uploaded)
    assert (clips_settings().storage_dir / "state" / "uploads.jsonl").exists()


def test_a_second_upload_of_the_same_clip_makes_no_second_video(world):
    add_own_source(world)
    drive_to_scheduled(world)
    world.runner.upload_due()
    made = len(world.yt.uploaded)
    clip = world.store.list_clips(status=ClipStatus.UPLOADED)[0]
    again = uploader.upload_clip(clip, world.store.get_source(clip.source_id), world.store)
    assert again.youtube_id == clip.youtube_id
    assert len(world.yt.uploaded) == made


def test_a_source_without_rights_is_never_enabled_clipped_or_posted(world):
    with pytest.raises(RightsError):
        sources.add_source(
            world.store, name="Nobody", platform=Platform.FOLDER, rights=RightsBasis.NONE,
            folder=str(world.folder), enabled=True,
        )
    listed = sources.add_source(world.store, name="Nobody", platform=Platform.FOLDER, folder=str(world.folder))
    with pytest.raises(RightsError):
        sources.enable_source(world.store, listed.id)
    # even when someone writes the enabled flag straight into the database
    world.store.save_source(listed.model_copy(update={"enabled": True}))
    assert world.runner.watch() == 0
    assert world.runner.process() == 0
    assert world.store.list_clips() == []
    assert world.yt.uploaded == []


def test_a_clip_whose_source_lost_its_rights_is_held_and_not_uploaded(world):
    source = add_own_source(world)
    drive_to_scheduled(world)
    world.store.save_source(source.model_copy(update={"rights": RightsBasis.NONE}))
    world.runner.upload_due()
    assert world.yt.uploaded == []
    clip = world.store.list_clips()[0]
    assert clip.status is ClipStatus.HELD


@pytest.mark.parametrize("channel", [("UCcompany", "Company"), ("", ""), None])
def test_a_wrong_channel_receives_nothing(world, monkeypatch, channel):
    add_own_source(world)
    drive_to_scheduled(world)
    monkeypatch.setattr(uploader, "connected_channel", lambda service=None: channel)
    world.runner.upload_due()
    assert world.yt.uploaded == []
    assert not world.store.list_clips(status=ClipStatus.UPLOADED)


def test_a_forbidden_channel_receives_nothing_even_if_configured(world, monkeypatch):
    monkeypatch.setenv("CLIPS_FORBIDDEN_CHANNEL_IDS", CHANNEL)
    clips_settings.cache_clear()
    add_own_source(world)
    drive_to_scheduled(world)
    world.runner.upload_due()
    assert world.yt.uploaded == []


def test_the_kill_switch_stops_everything(world):
    add_own_source(world)
    drive_to_scheduled(world)
    guard.set_kill_switch(True, "test")
    assert world.runner.tick() == {"killed": True}
    assert world.runner.upload_due() == 0
    assert world.yt.uploaded == []
    clip = world.store.list_clips(status=ClipStatus.SCHEDULED)[0]
    with pytest.raises(guard.GuardError):
        uploader.upload_clip(clip, world.store.get_source(clip.source_id), world.store)


def test_a_permission_source_gets_its_credit_in_the_description(world):
    sources.add_source(
        world.store, name="Some Creator", platform=Platform.FOLDER, rights=RightsBasis.WRITTEN_PERMISSION,
        folder=str(world.folder), enabled=True, evidence_note="Email from the creator, kept in the shared drive",
        evidence_date="2026-01-05",
    )
    drive_to_scheduled(world)
    world.runner.upload_due()
    assert "Clip from Some Creator" in world.yt.uploaded[0]["description"]


# ------------------------------------------------------------------ the company's token is never touched


@pytest.fixture
def company_folder(tmp_path, monkeypatch):
    """A process whose STORAGE_DIR is NOT the clips folder, with a company token in it."""
    from core.config import get_settings

    folder = tmp_path / "company-storage"
    folder.mkdir()
    token = folder / "youtube_token.json"
    token.write_text('{"refresh_token": "company"}', encoding="utf-8")
    monkeypatch.setenv("STORAGE_DIR", str(folder))
    get_settings.cache_clear()
    yield folder
    get_settings.cache_clear()


def _token_untouched(folder):
    """The token is byte for byte as it was, and nothing else was written (the app's own settings
    make empty jobs, transcripts and videos folders; those are not clips' doing)."""
    extra = {p.name for p in folder.iterdir()} - {"jobs", "transcripts", "videos", "youtube_token.json"}
    token = (folder / "youtube_token.json").read_text(encoding="utf-8")
    return not extra and token == '{"refresh_token": "company"}'


def test_status_connect_and_callback_leave_the_company_token_alone(company_folder, monkeypatch):
    from fastapi.testclient import TestClient

    from clips import api
    from publish import youtube

    def boom(*a, **k):
        raise AssertionError("the sign-in token was used outside the clips folder")

    for name in ("_load_credentials", "start_connect", "finish_connect", "disconnect"):
        monkeypatch.setattr(youtube, name, boom)
    monkeypatch.setattr(api, "_store", Store())
    monkeypatch.setattr(api, "_runner", None)
    client = TestClient(api.app)

    status = client.get("/api/status").json()
    assert status["isolated"] is False and status["channel"] is None
    assert client.get("/youtube/connect", follow_redirects=False).status_code == 503
    api._pending_states.add("s")
    assert client.get("/youtube/callback?state=s&code=x").status_code == 503
    assert _token_untouched(company_folder)


def test_cli_status_does_not_read_the_company_token(company_folder, monkeypatch, capsys):
    from clips import __main__ as cli
    from publish import youtube

    def boom(*a, **k):
        raise AssertionError("the sign-in token was used outside the clips folder")

    monkeypatch.setattr(youtube, "_load_credentials", boom)
    assert cli.main(["status"]) == 0
    assert '"isolated": false' in capsys.readouterr().out
    assert _token_untouched(company_folder)
