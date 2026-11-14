import shutil
import subprocess
from pathlib import Path

import pytest

from clips import signals
from clips.models import ClipKind, Fetched

EBU = """\
[Parsed_ebur128_0 @ 0000] t: 0.0999792  TARGET:-23 LUFS    M:-120.7 S:-120.7     I: -70.0 LUFS     LRA:   0.0 LU
[Parsed_ebur128_0 @ 0000] t: 0.5  TARGET:-23 LUFS    M: -30.0 S: -40.0     I: -70.0 LUFS     LRA:   0.0 LU
[Parsed_ebur128_0 @ 0000] t: 1.2  TARGET:-23 LUFS    M: -10.0 S: -20.0     I: -70.0 LUFS     LRA:   0.0 LU
[Parsed_ebur128_0 @ 0000] t: 2.2  TARGET:-23 LUFS    M:-inf S: -20.0     I: -70.0 LUFS     LRA:   0.0 LU
other noise
"""

SHOW = """\
[Parsed_showinfo_1 @ 0x1] n:   0 pts:  1000 pts_time:12.5 duration: 1 fmt:yuv420p
[Parsed_showinfo_1 @ 0x1] n:   1 pts:  2000 pts_time:3.25 duration: 1
frame=  1 pts_time:99
"""


def test_parse_ebur128_keeps_loudest_per_step():
    pts = signals.parse_ebur128(EBU, step_s=1.0)
    assert [t for t, _ in pts] == [0.0, 1.0, 2.0]
    assert pts[0][1] == 0.6  # -30 LUFS
    assert pts[1][1] == 1.0
    assert pts[2][1] == 0.0  # -inf is silence


def test_parse_scene_times_sorted_and_showinfo_only():
    assert signals.parse_scene_times(SHOW) == [3.25, 12.5]


def test_loudness_and_scenes_use_ffmpeg_output(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(signals, "_run_ffmpeg_stderr", lambda args: seen.append(args) or (EBU if "-af" in args else SHOW))
    assert len(signals.loudness_curve(tmp_path / "a.mp4")) == 3
    assert signals.scene_changes(tmp_path / "a.mp4", threshold=0.5) == [3.25, 12.5]
    assert "select='gt(scene,0.5)',showinfo" in seen[1]


def _fetched(duration=600.0, **kw):
    return Fetched(video_path="x.mp4", duration_s=duration, **kw)


def test_no_signals_gives_even_windows():
    wins = signals.candidate_windows(_fetched(), kind=ClipKind.SHORT, max_candidates=5)
    assert len(wins) == 5
    assert wins[0].start_s == 0.0
    assert all(w.signals == {"even": 0.1} for w in wins)
    assert all(w.end_s <= 600.0 for w in wins)


def test_video_shorter_than_the_minimum_gives_no_window():
    assert signals.candidate_windows(_fetched(10.0), kind=ClipKind.SHORT) == []


def test_short_video_gives_no_long_window():
    assert signals.candidate_windows(_fetched(120.0), kind=ClipKind.LONG) == []
    assert signals.candidate_windows(_fetched(120.0), kind=ClipKind.SHORT)


def test_loudness_uses_a_log_level_that_prints_frames(monkeypatch):
    seen = {}
    monkeypatch.setattr(signals, "_run_ffmpeg_stderr", lambda args: seen.setdefault("a", args) and "")
    signals.loudness_curve(Path("x.mp4"))
    assert "ebur128=framelog=info" in seen["a"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_loudness_curve_real_ffmpeg(tmp_path):
    wav = tmp_path / "tone.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", str(wav)], check=True)
    assert signals.loudness_curve(wav)


def test_heatmap_peak_starts_before_peak():
    heat = [(i * 10.0, i * 10.0 + 10, 0.2) for i in range(60)]
    heat[30] = (300.0, 310.0, 0.9)
    wins = signals.candidate_windows(_fetched(heatmap=heat), kind=ClipKind.SHORT)
    assert wins[0].signals == {"replayed": 0.9}
    assert wins[0].start_s < 305.0 < wins[0].end_s
    assert 5.0 <= 305.0 - wins[0].start_s <= 6.0
    assert 15.0 <= wins[0].end_s - wins[0].start_s <= 58.0


def test_combined_signals_score_higher_and_merge():
    heat = [(i * 10.0, i * 10.0 + 10, 0.1) for i in range(60)]
    heat[30] = (300.0, 310.0, 0.8)
    loud = [(float(t), 0.2) for t in range(600)]
    loud[308] = (308.0, 1.0)
    wins = signals.candidate_windows(_fetched(heatmap=heat), kind=ClipKind.SHORT, loudness=loud)
    assert len(wins) == 1
    assert set(wins[0].signals) == {"replayed", "loud"}
    assert wins[0].score > 0.8


def test_scene_burst_and_limit_and_ordering():
    scenes = [100.0, 102.0, 104.0, 106.0, 400.0, 401.0, 402.0]
    wins = signals.candidate_windows(_fetched(), kind=ClipKind.SHORT, scenes=scenes, max_candidates=1)
    assert len(wins) == 1
    assert wins[0].signals["cuts"] == 0.5
    both = signals.candidate_windows(_fetched(), kind=ClipKind.SHORT, scenes=scenes)
    assert [w.score for w in both] == sorted((w.score for w in both), reverse=True)


def test_windows_stay_inside_video_and_long_length():
    scenes = [595.0, 597.0, 598.0]
    wins = signals.candidate_windows(_fetched(), kind=ClipKind.SHORT, scenes=scenes)
    assert wins[0].end_s <= 600.0
    long_wins = signals.candidate_windows(_fetched(1800.0), kind=ClipKind.LONG)
    assert all(180 <= w.end_s - w.start_s <= 600 for w in long_wins)


def test_zero_duration_returns_nothing():
    assert signals.candidate_windows(Fetched(video_path="x"), kind=ClipKind.SHORT) == []
