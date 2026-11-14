import json
import shutil
import subprocess
from pathlib import Path

import pytest

from clips import render
from clips.models import Caption, ClipKind, Layout, Moment, Platform, Source

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
MOMENT = Moment(start_s=2.0, end_s=7.5, kind=ClipKind.SHORT, hook="Big jump", layout=Layout.AUTO)


def test_filter_path_escapes_windows_drive_and_quotes():
    out = render.filter_text_path(Path("C:/Users/it's me/a b/text.ass"))
    assert "C\\\\:" in out  # the colon is escaped at both levels
    assert "\\\\\\'" in out  # the apostrophe too


def test_subtitles_filter_with_fonts_dir():
    out = render.subtitles_filter(Path("/tmp/t.ass"), Path("/fonts"))
    assert out.startswith("subtitles=filename=")
    assert ":fontsdir=" in out


def test_short_cmd_shape():
    cmd = render.build_short_cmd("ffmpeg", Path("in.mp4"), MOMENT, "GRAPH", Path("out.mp4"), fps=30)
    assert cmd[0] == "ffmpeg"
    assert cmd[cmd.index("-ss") + 1] == "2.000"
    assert cmd[cmd.index("-t") + 1] == "5.500"
    assert cmd[cmd.index("-filter_complex") + 1] == "GRAPH"
    assert cmd[cmd.index("-r") + 1] == "30"
    assert "libx264" in cmd and "aac" in cmd
    assert "loudnorm=I=-14:TP=-1.5:LRA=11" in cmd
    assert cmd[cmd.index("-movflags") + 1] == "+faststart"
    assert cmd[-1] == "out.mp4"
    assert "0:a:0?" in cmd  # a source with no sound still renders
    assert cmd.index("-ss") < cmd.index("-i")


def test_long_cmd_never_upscales_and_burns_credit():
    cmd = render.build_long_cmd("ffmpeg", Path("in.mp4"), MOMENT, Path("/tmp/c.ass"), None, Path("o.mp4"))
    vf = cmd[cmd.index("-vf") + 1]
    assert "min(1920,iw)" in vf and "min(1080,ih)" in vf and "subtitles=" in vf
    assert "-filter_complex" not in cmd


def test_thumb_cmd():
    cmd = render.build_thumb_cmd("ffmpeg", Path("v.mp4"), 1.5, Path("t.jpg"))
    assert "-frames:v" in cmd and cmd[-1] == "t.jpg"


def _fake_run(monkeypatch, calls):
    import core.proc

    def fake(cmd, timeout=None, **kw):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"x")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(core.proc, "run_checked", fake)
    monkeypatch.setattr(render, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(render, "_probe_size", lambda p: (1920, 1080))
    monkeypatch.setattr(render, "_fonts_dir", lambda: None)


def test_render_short_runs_one_encode_and_a_thumbnail(tmp_path, monkeypatch):
    calls = []
    _fake_run(monkeypatch, calls)
    src = Source(id="s", name="S", platform=Platform.FOLDER)
    caps = [Caption(start_s=2, end_s=4, text="hello there")]
    r = render.render_short(tmp_path / "in.mp4", MOMENT, src, tmp_path / "out", clip_id="abc123abc123",
                            captions=caps, credit="Gameplay: S")
    assert len(calls) == 2
    graph = calls[0][calls[0].index("-filter_complex") + 1]
    assert "[v]subtitles=" in graph and graph.endswith("[vout]")
    assert r.file.endswith("abc123abc123.mp4") and r.thumb.endswith("abc123abc123.jpg")
    assert (r.width, r.height) == (1080, 1920)
    assert r.layout is Layout.SQUARE and r.duration_s == 5.5


def test_render_short_uses_stack_for_facecam_source(tmp_path, monkeypatch):
    calls = []
    _fake_run(monkeypatch, calls)
    src = Source(id="s", name="S", platform=Platform.FOLDER, facecam_box=(0.7, 0.05, 0.25, 0.25))
    r = render.render_short(tmp_path / "in.mp4", MOMENT, src, tmp_path, clip_id="c")
    assert r.layout is Layout.FACECAM_STACK
    assert "vstack" in calls[0][calls[0].index("-filter_complex") + 1]


def test_render_long(tmp_path, monkeypatch):
    calls = []
    _fake_run(monkeypatch, calls)
    r = render.render_long(tmp_path / "in.mp4", MOMENT, None, tmp_path, clip_id="c", credit="x")
    assert (r.width, r.height) == (1920, 1080)
    assert "-vf" in calls[0]


def test_missing_ffmpeg_gives_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "ffmpeg_available", lambda: False)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        render.render_short(tmp_path / "in.mp4", MOMENT, None, tmp_path, clip_id="c")


def test_failed_thumbnail_is_not_fatal(tmp_path, monkeypatch):
    import core.proc

    def fake(cmd, timeout=None, **kw):
        if cmd[-1].endswith(".jpg"):
            raise core.proc.MediaCommandError("boom", cmd)
        Path(cmd[-1]).write_bytes(b"x")

    monkeypatch.setattr(core.proc, "run_checked", fake)
    monkeypatch.setattr(render, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(render, "_probe_size", lambda p: (1920, 1080))
    monkeypatch.setattr(render, "_fonts_dir", lambda: None)
    r = render.render_short(tmp_path / "in.mp4", MOMENT, None, tmp_path, clip_id="c")
    assert r.thumb == ""


@pytest.fixture
def test_video(tmp_path):
    out = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=6",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(out)], check=True)
    return out


def _probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_real_short(tmp_path, test_video):
    m = Moment(start_s=1.0, end_s=4.0, hook="Test hook")
    r = render.render_short(test_video, m, None, tmp_path / "o", clip_id="real1",
                            captions=[Caption(start_s=1, end_s=3, text="some words here")], credit="Credit")
    info = _probe(r.file)
    vid = info["streams"][0]
    assert (vid["width"], vid["height"]) == (1080, 1920)
    assert abs(float(info["format"]["duration"]) - 3.0) < 0.5
    assert Path(r.thumb).exists()


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_real_long(tmp_path, test_video):
    m = Moment(start_s=0.0, end_s=3.0, kind=ClipKind.LONG)
    r = render.render_long(test_video, m, None, tmp_path / "o", clip_id="real2", credit="Credit")
    vid = _probe(r.file)["streams"][0]
    assert (vid["width"], vid["height"]) == (1280, 720)
