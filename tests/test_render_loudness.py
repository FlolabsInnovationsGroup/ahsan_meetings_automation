"""Loudness measure, gain clamp, and the AAC encode with per-chunk gain.
Synthetic lavfi/wave sources only."""

import array
import re
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

import slice.ffmpeg_wrapper as fw
from slice.ffmpeg_wrapper import AudioPiece, encode_aac, gain_to_target, measure_lufs
from slice.profile import probe_header

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")


def _run(*args):
    return subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True)


def _sine(path: Path, seconds: float, volume_db: float = 0.0, freq: int = 1000):
    _run("-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={seconds}",
         "-af", f"volume={volume_db}dB", "-ac", "2", str(path))


def _peak_dbfs(path: Path) -> float:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
                       check=True, capture_output=True, text=True)
    return float(re.search(r"max_volume:\s*(-?[\d.]+) dB", r.stderr).group(1))


def test_gain_to_target_clamps_and_handles_none():
    assert gain_to_target(None, -14) is None
    assert gain_to_target(-29.0, -14.0) == 15.0  # a quiet gallery clip needs about +15 dB
    assert gain_to_target(-45.0, -14.0) == 20.0  # would be +31
    assert gain_to_target(-5.0, -14.0) == -9.0
    assert gain_to_target(2.0, -14.0) == -10.0  # would be -16
    assert gain_to_target(-20.0, -14.0) == pytest.approx(6.0)
    assert gain_to_target(-30.0, -14.0, max_boost=12.0) == 12.0


@needs_ffmpeg
def test_a_quiet_sine_is_levelled_to_target_with_a_safe_peak(tmp_path):
    flac = tmp_path / "quiet.flac"
    _sine(flac, 6.0, volume_db=-5.0)
    measured = measure_lufs(flac)
    assert measured is not None and -28 < measured < -24
    gain = gain_to_target(measured, -14.0)
    assert gain is not None and 0 < gain <= 20.0
    out = tmp_path / "levelled.m4a"
    encode_aac([flac], out, tmp_path / "g.txt", content_s=6.0, gains=[gain])
    assert measure_lufs(out) == pytest.approx(-14.0, abs=1.0)
    assert _peak_dbfs(out) <= -1.0


@needs_ffmpeg
def test_a_piece_is_measured_on_its_own(tmp_path):
    flac = tmp_path / "two.flac"
    _run("-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=4", "-af",
         "volume='if(lt(t,2),0.1,0.01778)':eval=frame", "-ac", "2", str(flac))
    loud = measure_lufs(flac, AudioPiece(0, 96000, False, False))
    soft = measure_lufs(flac, AudioPiece(96000, 96000, False, False))
    assert loud is not None and soft is not None and loud - soft == pytest.approx(15.0, abs=1.5)


@needs_ffmpeg
def test_silence_and_unreadable_files_measure_none(tmp_path):
    silent = tmp_path / "silent.flac"
    _run("-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "3", str(silent))
    assert measure_lufs(silent) is None
    assert measure_lufs(tmp_path / "missing.flac") is None


@needs_ffmpeg
def test_an_impulse_keeps_its_sample_position_through_volume_and_limiter(tmp_path):
    n, at = 48000, 24000
    path = tmp_path / "impulse.wav"
    samples = array.array("h", [0] * n)
    samples[at] = 3000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(samples.tobytes())
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-af",
         f"volume=6.00dB,alimiter=limit={fw.PEAK_LIMIT}:level=0:latency=1,apad,atrim=end_sample={n}",
         "-f", "s16le", "-"], check=True, capture_output=True).stdout
    out = array.array("h")
    out.frombytes(raw)
    assert len(out) == n
    assert max(range(n), key=lambda i: abs(out[i])) == at


def test_encode_aac_without_gains_builds_todays_commands(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(fw, "run_checked", lambda cmd, timeout=None: seen.append(list(cmd)))
    monkeypatch.setattr(fw, "ffmpeg_bin", lambda: "ffmpeg")
    monkeypatch.setattr(fw, "filter_graph_args", lambda p: ["-/filter_complex", p.name])
    graph = tmp_path / "g.txt"
    chunks = [Path("a.flac"), Path("b.flac")]
    for gains in (None, [None, None]):
        encode_aac(chunks, Path("o.m4a"), graph, 10.0, gains=gains)
        assert graph.read_text(encoding="utf-8") == "[0:a][1:a]concat=n=2:v=0:a=1[aout]"
        assert seen[-1] == ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", "a.flac",
                            "-i", "b.flac", "-/filter_complex", "g.txt", "-map", "[aout]", "-c:a", "aac",
                            "-b:a", "128k", "-ar", "48000", "-ac", "2", "-vn", "o.m4a"]
    encode_aac(chunks[:1], Path("o.m4a"), graph, 10.0, gains=[None])
    assert seen[-1][6:] == ["-i", "a.flac", "-map", "0:a:0", "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
                            "-ac", "2", "-vn", "o.m4a"]


def test_encode_aac_with_a_gain_levels_only_that_chunk_then_limits(monkeypatch, tmp_path):
    monkeypatch.setattr(fw, "run_checked", lambda cmd, timeout=None: None)
    graph = tmp_path / "g.txt"
    encode_aac([Path("a.flac"), Path("b.flac")], Path("o.m4a"), graph, 10.0, gains=[None, 6.0])
    text = graph.read_text(encoding="utf-8")
    assert "[1:a]volume=6.00dB[g1]" in text and "[0:a][g1]concat=n=2:v=0:a=1[aj]" in text
    assert text.endswith("[aj]alimiter=limit=0.8414:level=0:latency=1[aout]")


@needs_ffmpeg
def test_encode_aac_with_a_gain_keeps_the_sample_count_exact(tmp_path):
    a, b = tmp_path / "a.flac", tmp_path / "b.flac"
    _sine(a, 2.0, -20.0, 440)
    _sine(b, 3.0, -30.0, 660)
    plain, levelled = tmp_path / "plain.m4a", tmp_path / "levelled.m4a"
    encode_aac([a, b], plain, tmp_path / "g1.txt", content_s=5.0)
    encode_aac([a, b], levelled, tmp_path / "g2.txt", content_s=5.0, gains=[None, 6.0])
    hp, hl = probe_header(plain), probe_header(levelled)
    assert hl.audio_duration_ts == hp.audio_duration_ts
    assert hl.audio_duration == pytest.approx(5.0, abs=2048 / 48000)
