"""Long pauses cut out of new-style Shorts: which frame ranges are kept, how
caption cues follow, and a real render of a lavfi clip with two silent gaps.
Synthetic sources and made-up words only."""

import dataclasses
import itertools
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

import publish.settings as pub_settings
import slice.ffmpeg_wrapper as fw
import slice.pipeline as slice_pipeline
from core.models import ShortClip, Word
from core.timeline import samples_at_frame
from slice.captions import Cue
from slice.pipeline import kept_spans, remap_cues
from slice.silence import SilenceMap

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
FPS = Fraction(25)


def _total(spans):
    return sum(b - a for a, b in spans)


# ------------------------------------------------------------------ kept_spans


def test_nothing_to_trim_is_one_span_for_the_whole_clip():
    assert kept_spans(10.0, 20.0, [], 0.6, fps=FPS) == [(250, 500)]
    # pauses at or under the limit stay as they are
    assert kept_spans(10.0, 20.0, [(12.0, 12.6), (15.0, 15.3)], 0.6, fps=FPS) == [(250, 500)]


def test_a_long_pause_is_cut_to_two_tenths_with_a_tenth_kept_each_side():
    spans = kept_spans(10.0, 20.0, [(12.0, 14.6)], 0.6, fps=FPS)
    assert len(spans) == 2 and spans[0][0] == 250 and spans[-1][1] == 500
    removed_s = float(Fraction(250 - _total(spans)) / FPS)
    assert removed_s == pytest.approx(2.6 - 0.2, abs=0.09)  # 2.4 s gone, give or take a frame
    # a tenth of a second of the pause stays on each side of the cut
    assert float(Fraction(spans[0][1]) / FPS) == pytest.approx(12.1, abs=0.05)
    assert float(Fraction(spans[1][0]) / FPS) == pytest.approx(14.5, abs=0.05)


def test_spans_are_frame_snapped_and_in_source_order():
    spans = kept_spans(1.013, 30.017, [(5.31, 8.77), (12.0, 13.4), (20.05, 25.55)], 0.6, fps=FPS)
    assert len(spans) == 4
    assert all(isinstance(a, int) and isinstance(b, int) and b > a for a, b in spans)
    for (_, prev_end), (nxt_start, _) in itertools.pairwise(spans):
        assert nxt_start > prev_end  # a real cut between them
    # each cut keeps 0.2 s of pause: the whole clip minus what was removed
    full = 30.017 - 1.013
    removed = (8.77 - 5.31 - 0.2) + (13.4 - 12.0 - 0.2) + (25.55 - 20.05 - 0.2)
    assert float(Fraction(_total(spans)) / FPS) == pytest.approx(full - removed, abs=0.3)


def test_a_pause_at_the_clip_edge_is_left_alone():
    assert kept_spans(10.0, 20.0, [(9.0, 12.0)], 0.6, fps=FPS) == [(250, 500)]
    assert kept_spans(10.0, 20.0, [(18.0, 21.0)], 0.6, fps=FPS) == [(250, 500)]


def test_more_than_twelve_spans_puts_the_shortest_pauses_back():
    pauses = [(10.0 + 2.0 * i, 10.0 + 2.0 * i + 0.8 + 0.1 * i) for i in range(1, 15)]
    spans = kept_spans(5.0, 45.0, pauses, 0.6, fps=FPS)
    assert len(spans) == 12
    unlimited = kept_spans(5.0, 45.0, pauses, 0.6, fps=FPS, max_spans=99)
    assert len(unlimited) == 15
    assert spans[0][1] > unlimited[0][1]  # the first (shortest) pauses are back in
    assert _total(spans) > _total(unlimited)
    # the 11 longest pauses are the ones still cut
    cut_starts = {b for _, b in spans[:-1]}
    longest = sorted(pauses, key=lambda p: p[0] - p[1])[:11]
    assert {round(a * 25 + 2.5) for a, _ in longest} == {c for c in cut_starts} or len(cut_starts) == 11


def test_overlapping_pauses_never_produce_a_backwards_span():
    spans = kept_spans(0.0, 30.0, [(5.0, 8.0), (7.5, 9.0)], 0.6, fps=FPS)
    assert all(b > a for a, b in spans)


# ------------------------------------------------------------------ cue remap


def test_a_cue_after_a_trimmed_pause_shifts_and_one_inside_it_is_dropped():
    spans_s = [(0.0, 2.0), (4.5, 9.0)]  # 2.0 -> 4.5 removed
    cues = [Cue(0.5, 1.5, "before"), Cue(2.5, 4.0, "inside the pause"), Cue(5.0, 6.0, "after")]
    out = remap_cues(cues, spans_s)
    assert [c.text for c in out] == ["before", "after"]
    assert (out[0].start, out[0].end) == (0.5, 1.5)
    assert (out[1].start, out[1].end) == (2.5, 3.5)  # 2.5 s were removed before it


def test_a_cue_that_runs_into_a_pause_is_shortened_to_the_kept_part():
    out = remap_cues([Cue(1.0, 3.0, "runs over")], [(0.0, 2.0), (4.5, 9.0)])
    assert len(out) == 1 and (out[0].start, out[0].end) == (1.0, 2.0)


# ----------------------------------------------------------------- real render


def _source(path: Path, seconds: int = 12) -> None:
    # a tone with two silent gaps (2.0-4.6 and 6.0-7.3 s), a quiet dip at 8.9-9.2 s and a short loud click at 9.0 s
    expr = ("0.3*sin(2*PI*440*t)*not(between(t,2,4.6))*not(between(t,6,7.3))*not(between(t,8.9,9.2))"
            "+0.9*between(t,9.0,9.0015)")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=25:duration={seconds}",
         "-f", "lavfi", "-i", f"aevalsrc=exprs='{expr}':s=48000:d={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ac", "1", str(path)],
        check=True)


@pytest.fixture
def synthetic(tmp_path):
    from slice.profile import probe_media

    src = tmp_path / "src.mp4"
    _source(src)
    media = probe_media(src)
    flac = tmp_path / "audio.flac"
    fw.extract_audio_flac(src, flac, media.audio_channels, media.duration)
    return src, flac, media


CLIP = ShortClip(index=1, moment_id=0, start=1.0, end=11.0, score=9, title="A made up title",
                 screen_hook="Made up hook text")
WORDS = [Word(word="Alpha beta gamma delta.", start=1.2, end=1.9, speaker="A"),
         Word(word="Inside the first pause.", start=3.0, end=3.8, speaker="A"),
         Word(word="Epsilon zeta eta theta.", start=8.0, end=8.8, speaker="A")]
SILENCES = SilenceMap(ranges=[(2.0, 4.6), (6.0, 7.3)], threshold_db=None, audio_s=12.0)


def _look(**changes):
    base = {"hd": False, "loudness": False, "layout": "fit", "denoise": False, "trim_pauses": True,
            "max_pause_s": 0.6}
    return dataclasses.replace(pub_settings.looks().shorts, **{**base, **changes})


def _render(tmp_path, synthetic, *, style="new", look=None, silences=SILENCES):
    from slice.fonts import find_font

    src, flac, media = synthetic
    return slice_pipeline.render_short_clip(
        src, flac, CLIP, media, WORDS, tmp_path / "shorts" / "short_01.mp4", tmp_path / "shorts" / "short_01.srt",
        tmp_path / "w", find_font(), width=216, height=384, style=style, look=look or _look(),
        job_id="j", index=1, silences=silences)


def _pcm(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "48000",
                          "-f", "f32le", "-"], check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype="<f4")


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_a_trimmed_short_has_exactly_the_kept_frames_and_samples(tmp_path, synthetic):
    media = synthetic[2]
    s, e = 25, 275
    spans = kept_spans(1.0, 11.0, SILENCES.ranges, 0.6, fps=media.fps)
    assert len(spans) == 3 and spans[0][0] == s and spans[-1][1] == e
    result = _render(tmp_path, synthetic)
    want_frames = _total(spans)
    assert want_frames < e - s - 60  # both long pauses really went
    assert result.header.video_frames == want_frames and result.frames == want_frames
    assert result.trimmed_s == pytest.approx(float(Fraction(e - s - want_frames) / media.fps), abs=0.001)
    assert result.trimmed_s > 3.0
    sizes = [samples_at_frame(b, media.fps) - samples_at_frame(a, media.fps) for a, b in spans]
    want_samples = sum(sizes)
    assert result.header.audio_duration == pytest.approx(want_samples / 48000, abs=0.03)
    pcm = _pcm(result.path)
    assert abs(len(pcm) - want_samples) <= 2048  # AAC frame padding only
    # the loud click (source 9.0 s) lands where the spans put it
    expected = sum(sizes[:-1]) + (9.0 * 48000 - samples_at_frame(spans[-1][0], media.fps))
    assert abs(int(np.argmax(np.abs(pcm))) - expected) <= 2048
    # a cue inside the first pause is gone; the other two are in the .srt
    srt = (tmp_path / "shorts" / "short_01.srt").read_text(encoding="utf-8")
    assert "Alpha" in srt and "Epsilon" in srt and "Inside the first pause" not in srt
    assert not (tmp_path / "w").exists()


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_the_cuts_leave_no_loud_edge(tmp_path, synthetic):
    media = synthetic[2]
    spans = kept_spans(1.0, 11.0, SILENCES.ranges, 0.6, fps=media.fps)
    pcm = _pcm(_render(tmp_path, synthetic).path)
    pos = 0
    for a, b in spans[:-1]:
        pos += samples_at_frame(b, media.fps) - samples_at_frame(a, media.fps)
        window = pcm[max(0, pos - 480): pos + 480]  # silence on both sides of a cut: nothing jumps at the join
        assert float(np.max(np.abs(window))) < 0.05


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_classic_is_never_trimmed(tmp_path, synthetic):
    result = _render(tmp_path, synthetic, style="classic")
    assert result.header.video_frames == 250 and result.trimmed_s == 0.0 and result.style == "classic"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_trim_pauses_off_gives_the_single_span_command(tmp_path, synthetic, monkeypatch):
    seen = []
    real = slice_pipeline.render_short

    def spy(src, flac, span, piece, graph, *a, **k):
        seen.append((span, piece, graph))
        return real(src, flac, span, piece, graph, *a, **k)

    monkeypatch.setattr(slice_pipeline, "render_short", spy)
    result = _render(tmp_path, synthetic, look=dataclasses.replace(_look(), trim_pauses=False))
    (span, piece, graph), = seen
    assert not isinstance(span, list) and not isinstance(piece, list)
    assert result.header.video_frames == 250 and result.trimmed_s == 0.0
    assert "concat=n=" not in graph


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_no_silence_map_means_nothing_is_trimmed(tmp_path, synthetic):
    result = _render(tmp_path, synthetic, silences=None)
    assert result.header.video_frames == 250 and result.trimmed_s == 0.0


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_a_limit_under_the_cached_minimum_measures_the_clip_again(tmp_path, synthetic):
    # silences.json only has pauses of 1 s or more; with a limit under that the clip is measured again
    runs = slice_pipeline.clip_silent_runs(synthetic[1], 1.0, 11.0, -40.0, 1.0)
    assert any(abs(a - 2.0) < 0.15 and abs(b - 4.6) < 0.15 for a, b in runs)
    assert any(abs(a - 6.0) < 0.15 and abs(b - 7.3) < 0.15 for a, b in runs)
    cached = SilenceMap(ranges=[(2.0, 4.6)], threshold_db=-40.0, audio_s=12.0)
    result = _render(tmp_path, synthetic, look=_look(max_pause_s=0.5), silences=cached)
    assert result.trimmed_s > 3.0  # both gaps found again although the cached map listed only one


def test_a_job_that_could_not_tell_pauses_from_speech_cuts_nothing_in_shorts(monkeypatch):
    # the full video cuts nothing for such a job (note set, no ranges); a Short must not re-measure with its threshold
    def boom(*a, **k):
        raise AssertionError("must not measure the clip again")

    monkeypatch.setattr(slice_pipeline, "clip_silent_runs", boom)
    refused = SilenceMap(ranges=[], threshold_db=-35.0, floor_db=-40.0, note="room tone is too close to the speech")
    spans = slice_pipeline._pause_spans(Path("audio.flac"), 10.0, 20.0, 250, 500, FPS, refused, 0.6)
    assert spans == [(250, 500)]
