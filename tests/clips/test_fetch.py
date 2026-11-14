import os
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest

from clips import fetch
from clips.config import clips_settings
from clips.models import Platform, SourceVideo

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _info(**kw):
    base = {"width": 1920, "height": 1080, "fps": 30, "duration": 12.5, "has_audio": True}
    base.update(kw)
    return SimpleNamespace(**base)


def test_local_path_probed_in_place(tmp_path, monkeypatch):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    monkeypatch.setattr("slice.profile.probe_media", lambda p: _info())
    v = SourceVideo(id="folder:v.mp4", source_id="s", platform=Platform.FOLDER, native_id="v.mp4", path=str(f))
    out = fetch.fetch(v, tmp_path / "dest")
    assert out.video_path == str(f) and out.width == 1920 and out.duration_s == 12.5
    assert f.exists() and not (tmp_path / "dest").exists()


def test_missing_local_file(tmp_path):
    v = SourceVideo(id="folder:v", source_id="s", platform=Platform.FOLDER, native_id="v", path=str(tmp_path / "x.mp4"))
    with pytest.raises(fetch.FetchError, match="missing"):
        fetch.fetch(v, tmp_path)


def test_youtube_download_collects_extras(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPS_YOUTUBE_DOWNLOADS", "true")
    clips_settings.cache_clear()
    calls = {}

    def fake_dl(url, dest, stem, fmt):
        calls["fmt"] = fmt
        (dest / f"{stem}.mp4").write_bytes(b"x")
        (dest / f"{stem}.en.vtt").write_bytes(b"x")
        return {
            "heatmap": [{"start_time": 0, "end_time": 10, "value": 0.9}, {"bad": 1}],
            "chapters": [{"start_time": 0, "end_time": 60, "title": "Intro"}],
        }

    seg = SimpleNamespace(start=1.0, end=3.0, text=" hello there ")
    monkeypatch.setattr(fetch, "_ytdlp_download", fake_dl)
    monkeypatch.setattr("slice.profile.probe_media", lambda p: _info())
    monkeypatch.setattr("transcribe.native.fetch_youtube_transcript", lambda url: ([], [seg]))
    v = SourceVideo(id="youtube:abc", source_id="s", platform=Platform.YOUTUBE, native_id="abc",
                    url="https://www.youtube.com/watch?v=abc")
    out = fetch.fetch(v, tmp_path / "media", max_height=720)
    assert "height<=720" in calls["fmt"] and "avc1" in calls["fmt"]
    assert out.video_path.endswith("abc.mp4")
    assert out.heatmap == [(0.0, 10.0, 0.9)]
    assert out.chapters == [(0.0, 60.0, "Intro")]
    assert [(c.start_s, c.end_s, c.text) for c in out.captions] == [(1.0, 3.0, "hello there")]


def test_captions_failure_is_not_fatal(monkeypatch):
    def boom(url):
        raise RuntimeError("no")

    monkeypatch.setattr("transcribe.native.fetch_youtube_transcript", boom)
    assert fetch._captions("u") == []
    monkeypatch.setattr("transcribe.native.fetch_youtube_transcript", lambda url: None)
    assert fetch._captions("u") == []


def test_probe_failure_is_plain(tmp_path, monkeypatch):
    def boom(p):
        raise RuntimeError("bad")

    monkeypatch.setattr("slice.profile.probe_media", boom)
    with pytest.raises(fetch.FetchError, match="ffprobe"):
        fetch.probe(tmp_path / "a.mp4")


def test_download_error_hint():
    msg = fetch._explain("u", RuntimeError("Sign in to confirm you're not a bot"))
    assert "cookies" in msg.lower()
    assert "JavaScript" in fetch._explain("u", RuntimeError("no JavaScript runtime"))


def test_cleanup_only_old_files_inside(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    outside = tmp_path / "keep.mp4"
    outside.write_bytes(b"x")
    old, new = media / "old.mp4", media / "new.mp4"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    t = time.time() - 10 * 3600
    os.utime(old, (t, t))
    os.utime(outside, (t, t))
    assert fetch.cleanup(media, 5) == 1
    assert not old.exists() and new.exists() and outside.exists()
    assert fetch.cleanup(tmp_path / "missing", 5) == 0


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_probe_real_file(tmp_path):
    f = tmp_path / "t.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=1",
         "-f", "lavfi", "-i", "sine=duration=1", "-shortest", "-pix_fmt", "yuv420p", str(f)],
        check=True,
    )
    out = fetch.probe(f)
    assert (out.width, out.height) == (320, 240) and out.has_audio and out.fps == 25


def test_youtube_video_without_a_file_is_refused_while_downloads_are_off(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(fetch, "_ytdlp_download", lambda *a, **k: called.append(1) or {})
    v = SourceVideo(id="youtube:abc", source_id="s", platform=Platform.YOUTUBE, native_id="abc",
                    url="https://www.youtube.com/watch?v=abc")
    with pytest.raises(fetch.FetchError, match="CLIPS_YOUTUBE_DOWNLOADS"):
        fetch.fetch(v, tmp_path / "media")
    assert called == []


def test_a_local_file_is_fetched_while_downloads_are_off(tmp_path, monkeypatch):
    path = tmp_path / "a.mp4"
    path.write_bytes(b"x")
    monkeypatch.setattr("slice.profile.probe_media", lambda p: _info())
    v = SourceVideo(id="f:a", source_id="s", platform=Platform.YOUTUBE, native_id="a", path=str(path),
                    url="https://www.youtube.com/watch?v=a")
    assert fetch.fetch(v, tmp_path / "media").video_path == str(path)
