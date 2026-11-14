"""The new Shorts filter graph (slide/speaker crops, multi-span), the HD
profiles and the render_short command. Graph-text tests need no ffmpeg; the
renders use synthetic lavfi sources only."""

import re
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import pytest

import slice.ffmpeg_wrapper as fw
from slice.ffmpeg_wrapper import (
    AudioPiece,
    build_short_graph,
    build_short_graph_new,
    render_short,
)
from slice.plan import InputSpan
from slice.profile import SHORTS_HD_CODEC_ARGS, shorts_video_args, video_args

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
FPS = Fraction(25)


@dataclass
class Lay:
    kind: str
    panel: tuple | None = None
    change_box: tuple | None = None
    focus_x: int | None = None
    stamp: tuple | None = None


@dataclass
class Look:
    screen_zoom: str = "whole_slide"
    speaker_layout: str = "full"
    denoise: bool = False


def _graph(layout, look=None, **kw):
    args = {"spans_frames": [75], "fps": FPS, "width": 1080, "height": 1920, "pre": 480, "samples": 144000,
                "layout": layout, "look": look or Look(), "subtitles": "", "gain_db": None}
    args.update(kw)
    return build_short_graph_new(**args)


def _crops(graph):
    return [tuple(int(v) for v in m) for m in re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", graph)]


# ------------------------------------------------------------ graph text


def test_zoom_timestamp_inside_the_shown_picture_is_blurred_before_the_crop():
    g = _graph(Lay("gallery", (0, 30, 1920, 960), stamp=(1668, 976, 232, 32)), src_size=(1920, 1020))
    assert "split=2[s0][s1]" in g
    assert "[s1]crop=232:32:1668:976,boxblur=" in g
    assert "[s0][sb]overlay=1668:976[hs]" in g
    # the blur is on the source frame, before the picture is cropped, scaled and laid over the blurred copy
    assert g.index("overlay=1668:976[hs]") < g.index("[hs]null,split=2[b0][f0]") < g.index("crop=1920:960:0:30")


def test_a_timestamp_outside_the_crop_needs_no_blur():
    g = _graph(Lay("gallery", (0, 146, 1920, 726), stamp=(1668, 976, 232, 32)), src_size=(1920, 1020))
    assert "[s1]" not in g and "[hs]" not in g
    g = _graph(Lay("gallery", (0, 30, 1920, 960)), src_size=(1920, 1020))
    assert "[s1]" not in g


def test_the_timestamp_is_blurred_for_full_speaker_and_fit_too():
    for lay in (Lay("speaker", focus_x=900, stamp=(1668, 976, 232, 32)), Lay("fit", stamp=(1668, 976, 232, 32))):
        g = _graph(lay, src_size=(1920, 1020))
        assert "[s1]crop=232:32:1668:976,boxblur=" in g and "[hs]null," in g

def test_screen_crops_the_panel_and_centres_it_in_the_band():
    g = _graph(Lay("screen", (460, 224, 1352, 772)))
    assert "crop=1352:772:460:224,scale=1080:-2:flags=lanczos" in g
    assert "split=2[b0][f0]" in g and "eq=brightness=-0.3:saturation=0.7" in g
    assert "overlay=x='(W-w)/2':y='480+max(0,trunc((650-h)/4)*2)'" in g
    assert g.rstrip().endswith("[aout]") and "format=yuv420p[vout]" in g


def test_screen_zoom_is_960_wide_centred_on_the_change_box_and_inside_the_panel():
    layout = Lay("screen", (460, 224, 1352, 772), change_box=(1000, 600, 200, 100))
    g = _graph(layout, Look(screen_zoom="zoom"))
    [(w, h, x, y)] = _crops(g)
    assert w == 960 and h == 548 and x % 2 == 0 and y % 2 == 0
    assert abs((x + w / 2) - 1100) <= 2 and (y + h / 2) == pytest.approx(650, abs=2)
    # a change box at the far edge cannot push the crop out of the panel
    g = _graph(Lay("screen", (460, 224, 1352, 772), change_box=(1800, 900, 10, 10)), Look(screen_zoom="zoom"))
    [(w, h, x, y)] = _crops(g)
    assert x + w <= 460 + 1352 and y + h <= 224 + 772 and x >= 460 and y >= 224
    # nothing to centre on: the whole slide
    assert _crops(_graph(Lay("screen", (460, 224, 1352, 772)), Look(screen_zoom="zoom"))) == [(1352, 772, 460, 224)]


def test_crops_are_even_and_clamped_inside_the_source():
    g = _graph(Lay("screen", (461, 225, 1351, 771)))
    assert all(v % 2 == 0 for v in _crops(g)[0])
    g = _graph(Lay("gallery", (1800, 900, 500, 400)), src_size=(1920, 1080))
    w, h, x, y = _crops(g)[0]
    assert x + w <= 1920 and y + h <= 1080 and all(v % 2 == 0 for v in (x, y, w, h))


def test_gallery_uses_the_content_box_in_the_same_band():
    g = _graph(Lay("gallery", (0, 90, 1920, 900)))
    assert "crop=1920:900:0:90,scale=1080:-2:flags=lanczos" in g and "y='480+max(0" in g


def test_speaker_full_has_no_background_chain():
    g = _graph(Lay("speaker", focus_x=900))
    assert "boxblur" not in g and "split" not in g and "overlay" not in g
    assert "crop=w='trunc(ih*9/16/2)*2':h='trunc(ih/2)*2':x='trunc(min(max(0,900-ow/2),iw-ow)/2)*2':y=0" in g
    assert "scale=1080:1920:flags=lanczos" in g and "format=yuv420p[vout]" in g
    assert "iw/2" in _graph(Lay("speaker"))  # no focus: the middle


def test_speaker_panel_is_a_4_5_crop_at_y_285_over_the_blur():
    g = _graph(Lay("speaker", focus_x=700), Look(speaker_layout="panel"))
    assert "ih*4/5" in g and "scale=1080:1350:flags=lanczos" in g and "boxblur" in g
    assert "overlay=x=0:y=285" in g


def test_fit_is_todays_fit_chain():
    g = _graph(Lay("fit"))
    assert "[f0]scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[fg]" in g
    assert "overlay=x=(W-w)/2:y=(H-h)/2" in g
    assert "lanczos" not in g
    # a screen layout with no panel falls back to fit instead of failing
    assert "force_original_aspect_ratio=decrease" in _graph(Lay("screen", None))


def test_other_output_sizes_scale_the_layout():
    g = _graph(Lay("screen", (460, 224, 1352, 772)), width=216, height=384)
    assert "scale=216:-2:flags=lanczos" in g and "y='96+max(0,trunc((130-h)/4)*2)'" in g
    g = _graph(Lay("speaker", focus_x=700), Look(speaker_layout="panel"), width=216, height=384)
    assert "scale=216:270:flags=lanczos" in g and "overlay=x=0:y=57" in g


def test_subtitles_come_after_the_overlay_before_the_format():
    g = _graph(Lay("screen", (0, 0, 1920, 1080)), subtitles="subtitles=filename=x.ass")
    assert g.index("overlay=") < g.index("subtitles=") < g.index("format=yuv420p[vout]")
    g = _graph(Lay("speaker"), subtitles="subtitles=filename=x.ass")
    assert g.index("lanczos") < g.index("subtitles=") < g.index("format=yuv420p[vout]")


def test_audio_chain_is_todays_when_nothing_is_added():
    g = _graph(Lay("fit"))
    assert g.endswith("[1:a]apad,atrim=start_sample=480:end_sample=144480,asetpts=PTS-STARTPTS,"
                      "afade=t=in:ss=0:ns=4800,afade=t=out:ss=139200:ns=4800[aout]")


def test_gain_adds_a_limiter_with_latency_and_trims_again_after_it():
    g = _graph(Lay("fit"), gain_db=6.5)
    tail = g.split("asetpts=PTS-STARTPTS")[-1]
    assert tail.index("volume=6.50dB") < tail.index("alimiter=limit=0.8414:level=0:latency=1")
    assert tail.index("alimiter") < tail.index("apad,atrim=end_sample=144000") and tail.endswith("[aout]")


def test_denoise_comes_first_in_the_audio_chain():
    g = _graph(Lay("fit"), Look(denoise=True))
    assert "[1:a]afftdn=nr=10:nf=-40,apad,atrim=start_sample=1680:end_sample=" in g


def test_three_spans_are_cut_together_with_a_concat():
    g = _graph(Lay("screen", (0, 0, 1920, 1080)), spans_frames=[30, 20, 25])
    assert "concat=n=3:v=1:a=0" in g
    for k, n in enumerate((30, 20, 25)):
        assert f"[{k}:v]fps=25/1:start_time=0:round=down,tpad=stop_mode=clone:stop=3,trim=end_frame={n}" in g
    assert "[3:a]apad" in g  # the audio comes after the video inputs
    g = _graph(Lay("speaker", focus_x=800), spans_frames=[30, 45])
    assert "concat=n=2:v=1:a=0,crop=" in g


def test_several_audio_pieces_are_joined_with_micro_fades_then_levelled():
    pieces = [AudioPiece(48000, 48000, False, True), AudioPiece(240000, 96000, True, False)]
    g = _graph(Lay("fit"), spans_frames=[25, 50], samples=144000, pre=[480, 480], n_audio=2,
               audio_pieces=pieces, gain_db=3.0, look=Look(denoise=True))
    assert "[2:a]apad" in g and "[3:a]apad" in g and "concat=n=2:v=0:a=1" in g
    assert "[araw]afftdn=nr=10:nf=-40,atrim=start_sample=1200,asetpts=PTS-STARTPTS,apad,atrim=end_sample=144000,volume=3.00dB,alimiter=" in g and g.endswith("atrim=end_sample=144000[aout]")
    with pytest.raises(ValueError):
        _graph(Lay("fit"), n_audio=2)


# ------------------------------------------------------------ classic stays byte-identical

CLASSIC_GRAPH = (
    "[0:v]fps=25/1:start_time=0:round=down,tpad=stop_mode=clone:stop=3,trim=end_frame=750,setpts=PTS-STARTPTS,"
    "split=2[b0][f0];\n"
    "[b0]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
    "boxblur=luma_radius=8:luma_power=2:chroma_radius=4:chroma_power=2,scale=1080:1920,setsar=1[bg];\n"
    "[f0]scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[fg];\n"
    "[bg][fg]overlay=(W-w)/2:(H-h)/2,subtitles=filename=x.ass,format=yuv420p[vout];\n"
    "[1:a]apad,atrim=start_sample=480:end_sample=1440480,asetpts=PTS-STARTPTS,"
    "afade=t=in:ss=0:ns=4800,afade=t=out:ss=1435200:ns=4800[aout]")

CLASSIC_CMD = [
    "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-ss", "3.990000", "-t", "30.120000",
    "-i", "src.mp4", "-ss", "0.990000", "-t", "30.020000", "-i", "a.flac", "-/filter_complex", "g.txt",
    "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-profile:v", "high",
    "-pix_fmt", "yuv420p", "-r", "25/1", "-fps_mode", "cfr", "-maxrate", "2500k", "-bufsize", "5000k",
    "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", "-frames:v", "750", "-movflags", "+faststart", "o.mp4"]


def _spy(monkeypatch):
    seen = {}
    monkeypatch.setattr(fw, "run_checked", lambda cmd, timeout=None: seen.update(cmd=list(cmd), timeout=timeout))
    monkeypatch.setattr(fw, "ffmpeg_bin", lambda: "ffmpeg")
    monkeypatch.setattr(fw, "filter_graph_args", lambda p: ["-/filter_complex", p.name])
    monkeypatch.setattr(fw, "ffmpeg_timeout", lambda content_s: content_s)  # the factor, visibly
    return seen


def test_classic_graph_text_is_unchanged():
    g = build_short_graph(frames=750, fps=FPS, width=1080, height=1920, pre=480, samples=1_440_000,
                          subtitles="subtitles=filename=x.ass")
    assert g == CLASSIC_GRAPH


def test_classic_render_command_is_unchanged(monkeypatch, tmp_path):
    seen = _spy(monkeypatch)
    render_short(Path("src.mp4"), Path("a.flac"), InputSpan(100, 850), AudioPiece(48000, 1_440_000, True, True),
                 CLASSIC_GRAPH, tmp_path / "g.txt", Path("o.mp4"), FPS)
    assert seen["cmd"] == CLASSIC_CMD and seen["timeout"] == pytest.approx(4.0 * 30.0)


def test_hd_render_uses_the_hd_args_and_a_6x_timeout(monkeypatch, tmp_path):
    seen = _spy(monkeypatch)
    render_short(Path("src.mp4"), Path("a.flac"), InputSpan(100, 850), AudioPiece(48000, 1_440_000, True, True),
                 CLASSIC_GRAPH, tmp_path / "g.txt", Path("o.mp4"), FPS, hd=True)
    cmd = seen["cmd"]
    assert cmd[cmd.index("-crf") + 1] == "20" and cmd[cmd.index("-maxrate") + 1] == "8M"
    assert cmd[cmd.index("-preset") + 1] == "medium" and seen["timeout"] == pytest.approx(6.0 * 30.0)


def test_render_short_takes_lists_video_inputs_first_then_audio(monkeypatch, tmp_path):
    seen = _spy(monkeypatch)
    spans = [InputSpan(100, 150), InputSpan(400, 450)]
    pieces = [AudioPiece(48000, 96000, False, True), AudioPiece(480000, 96000, True, False)]
    render_short(Path("src.mp4"), Path("a.flac"), spans, pieces, "g", tmp_path / "g.txt", Path("o.mp4"), FPS)
    cmd = seen["cmd"]
    assert [cmd[i + 1] for i, c in enumerate(cmd) if c == "-i"] == ["src.mp4", "src.mp4", "a.flac", "a.flac"]
    assert cmd[cmd.index("-frames:v") + 1] == "100"


def test_shorts_profile_args():
    hd = shorts_video_args(FPS, True)
    assert hd[hd.index("-crf") + 1] == "20" and hd[hd.index("-maxrate") + 1] == "8M"
    assert hd[hd.index("-bufsize") + 1] == "16M" and hd[hd.index("-preset") + 1] == "medium"
    assert hd[hd.index("-r") + 1] == "25/1" and "cfr" in hd and SHORTS_HD_CODEC_ARGS[0:2] == ["-c:v", "libx264"]
    assert shorts_video_args(FPS, False) == video_args(FPS)
    assert fw.SHORT_HD_TIMEOUT_FACTOR == 6.0 and fw.SHORT_TIMEOUT_FACTOR == 4.0


def test_full_video_hd_profile():
    from slice.profile import HD_TIMEOUT_FACTOR

    hd = video_args(FPS, hd=True)
    assert hd[hd.index("-preset") + 1] == "medium" and hd[hd.index("-crf") + 1] == "20"
    assert hd[hd.index("-maxrate") + 1] == "6M" and hd[hd.index("-bufsize") + 1] == "12M"
    assert video_args(FPS, hd=False) == video_args(FPS) and HD_TIMEOUT_FACTOR == 2.2


# ------------------------------------------------------------ real renders (lavfi sources)

def _run(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True)


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    d = tmp_path_factory.mktemp("p1")
    src, flac = d / "src.mp4", d / "a.flac"
    _run("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=6",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src))
    _run("-i", str(src), "-vn", "-ar", "48000", "-ac", "2", str(flac))
    return d, src, flac


LAYOUTS = {
    "screen": (Lay("screen", (100, 40, 400, 240)), Look()),
    "screen_zoom": (Lay("screen", (0, 0, 640, 360), change_box=(500, 200, 60, 40)), Look(screen_zoom="zoom")),
    "gallery": (Lay("gallery", (0, 30, 640, 300)), Look()),
    "speaker_full": (Lay("speaker", focus_x=450), Look()),
    "speaker_panel": (Lay("speaker", focus_x=200), Look(speaker_layout="panel")),
    "fit": (Lay("fit"), Look()),
    "gallery_stamp": (Lay("gallery", (0, 30, 640, 330), stamp=(520, 330, 100, 20)), Look()),
    "speaker_stamp": (Lay("speaker", focus_x=450, stamp=(520, 330, 100, 20)), Look()),
}


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
@pytest.mark.parametrize("name", sorted(LAYOUTS))
@pytest.mark.parametrize("hd", [False, True])
def test_every_kind_renders_exact_frames_and_audio(synthetic, name, hd):
    from slice import pipeline
    from slice.profile import assert_frames

    d, src, flac = synthetic
    layout, look = LAYOUTS[name]
    piece = AudioPiece(24000, 144000, True, True)
    _, pre = fw.audio_input_args(flac, piece)
    graph = build_short_graph_new(spans_frames=[75], fps=FPS, width=216, height=384, pre=pre, samples=144000,
                                  layout=layout, look=look, subtitles="", gain_db=3.0, src_size=(640, 360))
    out = d / f"{name}_{hd}.mp4"
    render_short(src, flac, InputSpan(25, 100), piece, graph, d / "g.txt", out, FPS, hd=hd)
    header = assert_frames(out, 75, name)
    assert (header.width, header.height) == (216, 384)
    assert abs(header.audio_duration - 3.0) <= pipeline.AAC_TOLERANCE_S


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_three_spans_render_to_the_sum_of_their_frames(synthetic):
    from slice import pipeline
    from slice.profile import assert_frames

    d, src, flac = synthetic
    spans = [InputSpan(0, 20), InputSpan(30, 55), InputSpan(60, 75)]
    pieces = [AudioPiece(0, 38400, False, True), AudioPiece(57600, 48000, True, True),
              AudioPiece(115200, 28800, True, False)]
    pres = [fw.audio_input_args(flac, p)[1] for p in pieces]
    total = sum(p.samples for p in pieces)
    graph = build_short_graph_new(spans_frames=[20, 25, 15], fps=FPS, width=216, height=384, pre=pres,
                                  samples=total, layout=Lay("screen", (100, 40, 400, 240)), look=Look(),
                                  subtitles="", gain_db=None, n_audio=3, audio_pieces=pieces)
    out = d / "three.mp4"
    render_short(src, flac, spans, pieces, graph, d / "g3.txt", out, FPS)
    header = assert_frames(out, 60, "three spans")
    assert abs(header.audio_duration - total / 48000) <= pipeline.AAC_TOLERANCE_S


def test_default_full_video_commands_are_unchanged_and_hd_is_opt_in(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(fw, "run_checked", lambda cmd, timeout=None: seen.append((list(cmd), timeout)))
    monkeypatch.setattr(fw, "ffmpeg_bin", lambda: "ffmpeg")
    monkeypatch.setattr(fw, "filter_graph_args", lambda p: ["-/filter_complex", p.name])
    graph = tmp_path / "g.txt"
    fw.render_card_video(tmp_path / "a.mp4", "g", graph, FPS, 50)
    fw.render_card_video(tmp_path / "b.mp4", "g", graph, FPS, 50, hd=False)
    fw.render_card_video(tmp_path / "c.mp4", "g", graph, FPS, 50, hd=True)
    (std, t_std), (std2, t_std2), (hd, t_hd) = seen
    assert std[:-1] == std2[:-1] and t_std == t_std2
    assert std[std.index("-c:v"):-1][:8] == video_args(FPS)[:8] and "-crf" in hd
    assert hd[hd.index("-crf") + 1] == "20" and "-maxrate" in hd and hd[hd.index("-maxrate") + 1] == "6M"
    assert t_hd >= t_std


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg on PATH")
def test_denoise_keeps_an_impulse_in_sync(tmp_path):
    import array
    import wave
    n, at = 48000, 24000
    path = tmp_path / "impulse.wav"
    samples = array.array("h", [0] * (n + 4000))
    samples[at] = 3000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(samples.tobytes())
    chain = f"afftdn=nr=10:nf=-40,apad,atrim=start_sample={fw.AFFTDN_DELAY}:end_sample={fw.AFFTDN_DELAY + n}"
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-af", chain, "-f", "s16le", "-"],
                         check=True, capture_output=True).stdout
    out = array.array("h")
    out.frombytes(raw)
    assert len(out) == n
    assert abs(max(range(n), key=lambda i: abs(out[i])) - at) <= 2
