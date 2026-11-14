"""The full video's look: loudness levelling, optional HD, optional topic line
over the highlights reel. Synthetic lavfi sources only."""

import array
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

import outputs
import slice.ffmpeg_wrapper as fw
from core.config import get_settings
from core.models import EditDecisionList, EDLRange
from core.timeline import rate_str
from publish import settings as pub_settings
from slice import pipeline
from slice.ffmpeg_wrapper import measure_lufs
from slice.fonts import find_font
from slice.plan import plan_video
from slice.profile import HD_TIMEOUT_FACTOR, probe_header, probe_media

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")

FPS = Fraction(30)
SAMPLES_PER_FRAME = 1600


def _ff(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def _meeting(path: Path, seconds: int = 6, audio: str = "sine=frequency=440:sample_rate=48000",
             volume_db: float = -3.0, wav: Path | None = None) -> None:
    """testsrc2 + a tone (about -24 LUFS), bt709-tagged like a recording."""
    audio_in = ["-i", str(wav)] if wav else ["-f", "lavfi", "-i", f"{audio}:duration={seconds}"]
    _ff("-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=30:duration={seconds}", *audio_in,
        "-af", f"volume={volume_db}dB", "-c:v", "libx264", "-preset", "ultrafast", "-g", "60",
        "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-color_range", "tv", "-c:a", "aac", "-ac", "2", str(path))


def _loud_intro(path: Path) -> None:
    """2 s, stereo, about -9 LUFS: well above the -14 target."""
    _ff("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=2", "-af", "volume=12dB",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2", str(path))


def _edl(ranges, total):
    return EditDecisionList(
        ranges=[EDLRange(start=float(Fraction(s) / FPS), end=float(Fraction(e) / FPS), start_frame=s, end_frame=e)
                for s, e in ranges],
        source_duration=float(Fraction(total) / FPS), fps=rate_str(FPS), fade_frames=0, total_frames=total)


def _render(tmp_path, name, *, src, intro=None, reel=None, title="Weekly planning review", **look):
    pub_settings.save(look)
    job_dir = get_settings().jobs_dir / name
    job_dir.mkdir(parents=True)
    total = probe_media(src).total_frames
    return outputs.render_outputs(outputs.RenderInputs(
        job_dir=job_dir, source=src, media=probe_media(src), edl=_edl([(0, total)], total), removed=[],
        reel_edl=_edl(reel, total) if reel else None, shorts=[], words=[], title=title, intro=intro),
        set_status=lambda s: None)


def _record_graphs(monkeypatch):
    """Record every encode_aac call's gains and the graph text it wrote."""
    calls = []
    real = pipeline.encode_aac

    def spy(chunks, dest, graph_path, content_s=0.0, gains=None):
        real(chunks, dest, graph_path, content_s, gains)
        calls.append({"gains": gains, "graph": graph_path.read_text(encoding="utf-8") if graph_path.exists() else ""})

    monkeypatch.setattr(pipeline, "encode_aac", spy)
    return calls


def _record_ffmpeg(monkeypatch):
    calls = []
    real = fw.run_checked

    def spy(cmd, timeout=None, **kw):
        calls.append((list(cmd), timeout))
        return real(cmd, timeout=timeout, **kw)

    monkeypatch.setattr(fw, "run_checked", spy)
    return calls


def _wav_part(path: Path, start: float, seconds: float, dest: Path) -> Path:
    _ff("-ss", f"{start}", "-t", f"{seconds}", "-i", str(path), "-vn", "-ac", "2", "-ar", "48000", str(dest))
    return dest


def _video_cmds(calls):
    return [(c, t) for c, t in calls if "-frames:v" in c]


# --------------------------------------------------------------- loudness


@needs_ffmpeg
def test_the_meeting_is_levelled_and_the_intro_keeps_its_own_level(tmp_path, monkeypatch):
    src, intro = tmp_path / "meeting.mp4", tmp_path / "intro.mp4"
    _meeting(src)
    _loud_intro(intro)
    intro_lufs = measure_lufs(intro)
    assert intro_lufs is not None and intro_lufs > -12
    graphs = _record_graphs(monkeypatch)
    out = _render(tmp_path, "lv", src=src, intro=intro, full_loudness=True, full_hd=False, full_hook_overlay=False)
    assert out.loudness_measured_lufs is not None and -27 < out.loudness_measured_lufs < -21
    assert out.loudness_gain_db == pytest.approx(-14.0 - out.loudness_measured_lufs, abs=0.01)
    assert out.full_hd is False
    # only the meeting got a gain; the intro (first chunk) is left alone
    assert graphs[0]["gains"] == [None, out.loudness_gain_db]
    assert out.intro_s == 2.0
    meeting = measure_lufs(_wav_part(out.final_path, out.intro_s + 0.3, 5.0, tmp_path / "m.wav"))
    intro_after = measure_lufs(_wav_part(out.final_path, 0.1, 1.8, tmp_path / "i.wav"))
    assert meeting == pytest.approx(-14.0, abs=1.0)
    assert intro_after == pytest.approx(intro_lufs, abs=0.5)
    # the sample count is exact: the render's own assert holds and the header agrees to one AAC frame
    header = probe_header(out.final_path)
    assert header.video_frames == out.final_frames == 60 + 180
    assert header.audio_duration == pytest.approx(out.final_frames * SAMPLES_PER_FRAME / 48000, abs=2048 / 48000)


@needs_ffmpeg
def test_an_impulse_lands_on_the_same_sample_with_and_without_levelling(tmp_path):
    n, at = 48000 * 6, 48000 * 3 + 123
    samples = array.array("h", [0] * n)
    samples[at] = 12000
    import wave

    wav = tmp_path / "click.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(samples.tobytes())
    src = tmp_path / "meeting.mp4"
    _meeting(src, wav=wav, volume_db=-12.0)
    peaks = {}
    for name, on in (("plain", False), ("levelled", True)):
        out = _render(tmp_path, name, src=src, full_loudness=on, full_hd=False, full_hook_overlay=False)
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out.final_path), "-vn", "-ac", "1", "-ar", "48000",
                              "-f", "s16le", "-"], check=True, capture_output=True).stdout
        pcm = array.array("h")
        pcm.frombytes(raw)
        peaks[name] = max(range(len(pcm)), key=lambda i: abs(pcm[i]))
    assert abs(peaks["plain"] - at) <= 2048  # AAC latency of the source's own encode
    assert abs(peaks["levelled"] - peaks["plain"]) <= 1


@needs_ffmpeg
def test_loudness_off_builds_todays_audio_graph(tmp_path, monkeypatch):
    src, intro = tmp_path / "meeting.mp4", tmp_path / "intro.mp4"
    _meeting(src)
    _loud_intro(intro)
    graphs = _record_graphs(monkeypatch)
    out = _render(tmp_path, "off", src=src, intro=intro, full_loudness=False, full_hd=False,
                  full_hook_overlay=False)
    assert out.loudness_measured_lufs is None and out.loudness_gain_db is None
    assert graphs[0]["gains"] == [None, None]
    assert graphs[0]["graph"] == "[0:a][1:a]concat=n=2:v=0:a=1[aout]"


@needs_ffmpeg
def test_a_silent_meeting_measures_none_and_gets_no_gain(tmp_path, monkeypatch):
    src = tmp_path / "meeting.mp4"
    _meeting(src, audio="anullsrc=r=48000:cl=stereo")
    graphs = _record_graphs(monkeypatch)
    out = _render(tmp_path, "silent", src=src, full_loudness=True, full_hd=False, full_hook_overlay=False)
    assert out.loudness_measured_lufs is None and out.loudness_gain_db is None
    assert graphs[0]["gains"] == [None]
    assert "volume=" not in graphs[0]["graph"] and "alimiter" not in graphs[0]["graph"]
    assert any("sound level not adjusted" in w for w in out.warnings)


# --------------------------------------------------------------------- HD


@needs_ffmpeg
def test_full_hd_uses_one_profile_on_every_track_and_scales_the_timeouts(tmp_path, monkeypatch):
    src, intro = tmp_path / "meeting.mp4", tmp_path / "intro.mp4"
    _meeting(src)
    _loud_intro(intro)
    font = find_font()
    if font is None:
        pytest.skip("no font for the title card")
    runs = {}
    for name, hd in (("sd", False), ("hd", True)):
        calls = _record_ffmpeg(monkeypatch)
        out = _render(tmp_path, name, src=src, intro=intro, reel=[(0, 60), (90, 120)], full_loudness=False,
                      full_hd=hd, full_hook_overlay=False)
        runs[name] = (out, _video_cmds(calls))
    out_sd, sd = runs["sd"]
    out_hd, hd_cmds = runs["hd"]
    assert out_hd.full_hd is True and out_sd.full_hd is False
    # cleaned, highlights, card and intro: four video commands at least, all in the same profile
    assert len(hd_cmds) == len(sd) >= 4
    for cmd, _ in hd_cmds:
        assert cmd[cmd.index("-crf") + 1] == "20" and cmd[cmd.index("-preset") + 1] == "medium"
    for cmd, _ in sd:
        assert cmd[cmd.index("-crf") + 1] == "24" and cmd[cmd.index("-preset") + 1] == "veryfast"
    for (c_sd, t_sd), (c_hd, t_hd) in zip(sd, hd_cmds, strict=True):
        assert t_hd == pytest.approx(t_sd * HD_TIMEOUT_FACTOR)
        assert [a for a in c_sd if a not in c_hd] and c_sd[:4] == c_hd[:4]
    # the joined file passed assemble's own concat check and keeps every frame
    assert out_hd.final_frames == out_sd.final_frames
    assert probe_header(out_hd.final_path).video_frames == out_hd.final_frames


@needs_ffmpeg
def test_full_hd_off_commands_are_the_standard_profile(tmp_path, monkeypatch):
    from slice.profile import video_args

    src = tmp_path / "meeting.mp4"
    _meeting(src)
    calls = _record_ffmpeg(monkeypatch)
    _render(tmp_path, "std", src=src, full_loudness=False, full_hd=False, full_hook_overlay=False)
    cmds = _video_cmds(calls)
    assert cmds
    std = video_args(FPS)
    for cmd, _ in cmds:
        assert cmd[-len(std) - 3:-3] == std  # ... video_args, -frames:v N, dest


# ------------------------------------------------------------ hook overlay


def test_the_hook_overlay_is_on_the_first_reel_range_only(tmp_path):
    bold = Path("C:/fonts/bold.ttf")
    media = type("M", (), {"even_width": 1280, "even_height": 720})()
    overlays = outputs._hook_overlay("Quarterly planning for the robotics platform", media, tmp_path, bold)
    assert list(overlays) == [0]
    assert overlays[0].endswith(":enable=lt(t\\,3)") and overlays[0].startswith("drawtext=")
    assert "y=h*0.12" in overlays[0] and "fontsize=54" in overlays[0]
    assert (tmp_path / "hook_title.txt").read_text(encoding="utf-8").startswith("Quarterly planning")
    parts = plan_video([(0, 60), (90, 120), (150, 180)], 0, 8, 10_000)
    graph = fw.build_video_part_graph(parts[0], FPS, 1280, 720, overlays)
    chains = [line for line in graph.split(";\n") if line.startswith("[")]
    with_text = [c for c in chains if "drawtext=" in c]
    assert len(chains) == 4 and len(with_text) == 1 and with_text[0].startswith("[0:v]")
    assert "enable=lt(t\\,3)" in with_text[0]


def test_an_empty_title_gives_no_overlay(tmp_path):
    media = type("M", (), {"even_width": 1280, "even_height": 720})()
    assert outputs._hook_overlay("   ", media, tmp_path, Path("b.ttf")) is None


@needs_ffmpeg
def test_render_outputs_passes_the_overlay_to_the_highlights_only(tmp_path, monkeypatch):
    if find_font(bold=True) is None:
        pytest.skip("no font")
    src = tmp_path / "meeting.mp4"
    _meeting(src)
    seen = []
    real = outputs.render_track

    def spy(*args, **kw):
        kind = args[5] if len(args) > 5 else kw.get("kind")
        overlays = args[7] if len(args) > 7 else kw.get("overlays")
        seen.append((kind, overlays))
        return real(*args, **kw)

    monkeypatch.setattr(outputs, "render_track", spy)
    reel = [(0, 60), (90, 120)]
    _render(tmp_path, "hook", src=src, reel=reel, full_loudness=False, full_hd=False, full_hook_overlay=True)
    by_kind = dict(seen)
    assert by_kind["cleaned"] is None
    assert list(by_kind["highlights"]) == [0] and "enable=lt(t\\,3)" in by_kind["highlights"][0]
    seen.clear()
    _render(tmp_path, "nohook", src=src, reel=reel, full_loudness=False, full_hd=False, full_hook_overlay=False)
    assert dict(seen)["highlights"] is None
    seen.clear()
    out = _render(tmp_path, "noreel", src=src, reel=None, full_loudness=False, full_hd=False,
                  full_hook_overlay=True)
    assert [k for k, _ in seen] == ["cleaned"] and seen[0][1] is None and out.highlights is None
