from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest

from clips import chunks, fetch
from clips.config import clips_settings
from clips.models import Platform, SourceVideo

URL = "https://www.youtube.com/watch?v=abc"


@pytest.fixture
def downloads_on(monkeypatch):
    monkeypatch.setenv("CLIPS_YOUTUBE_DOWNLOADS", "true")
    clips_settings.cache_clear()
    yield
    monkeypatch.delenv("CLIPS_YOUTUBE_DOWNLOADS", raising=False)
    clips_settings.cache_clear()


def _video(duration=3600.0, **kw):
    return SourceVideo(id="s:youtube:abc", source_id="s", platform=Platform.YOUTUBE, native_id="abc",
                       url=URL, duration_s=duration, **kw)


def _plan(duration=3600.0, **kw):
    args = {"heatmap": [], "loudness": [], "chapters": [], "count": 12, "length_s": 150.0}
    args.update(kw)
    return chunks.plan_ranges(duration, **args)


# --- is_chunked

def test_is_chunked():
    assert chunks.is_chunked(_video(25 * 60))
    assert not chunks.is_chunked(_video(20 * 60))
    assert not chunks.is_chunked(_video(25 * 60, path="/x/a.mp4"))
    folder = SourceVideo(id="f:a", source_id="f", platform=Platform.FOLDER, native_id="a", duration_s=9999)
    assert not chunks.is_chunked(folder)


# --- plan_ranges

def test_no_signal_is_evenly_spaced_inside_the_video():
    out = _plan(3600, count=4)
    assert len(out) == 4
    starts = [s for s, _e, _x in out]
    gaps = {round(b - a) for a, b in pairwise(starts)}
    assert gaps == {900}
    assert all(0 <= s and e <= 3600 and e - s == 150 and x == 0.0 for s, e, x in out)


def test_short_video_gives_one_range_and_bad_input_gives_none():
    assert _plan(100) == [(0.0, 100.0, 0.0)]
    assert _plan(0) == [] and _plan(3600, count=0) == [] and _plan(3600, length_s=0) == []


def test_heatmap_peaks_win_with_lead_in_and_no_overlap():
    heat = [(i * 60.0, i * 60.0 + 60, 0.1) for i in range(60)]
    heat[10] = (600.0, 660.0, 1.0)
    heat[11] = (660.0, 720.0, 0.95)  # right next to the best: must not make an overlapping chunk
    heat[40] = (2400.0, 2460.0, 0.8)
    out = _plan(3600, heatmap=heat, count=3)
    assert out[0][2] == 1.0 and out[0][0] == pytest.approx(630 - 20)  # peak centre minus the lead-in
    assert len(out) == 3
    ordered = sorted(out)
    assert all(a[1] <= b[0] for a, b in pairwise(ordered))
    assert any(abs(s - 2410) < 1 for s, _e, _x in out)


def test_loudness_alone_and_combined_with_heatmap():
    loud = [(float(t), 0.1) for t in range(3600)]
    loud[1000] = (1000.0, 0.9)
    loud[2500] = (2500.0, 0.7)
    out = _plan(3600, loudness=loud, count=2)
    assert [round(s) for s, _e, _x in out] == [980, 2480]
    heat = [(2400.0, 2600.0, 1.0)]
    both = _plan(3600, loudness=loud, heatmap=heat, count=1)
    assert 2400 - 150 <= both[0][0] <= 2600  # the heatmap agreeing with loudness lifts the later peak


def test_never_more_than_count_and_fewer_when_signal_is_thin():
    loud = [(float(t), 0.5 if t % 100 == 0 else 0.0) for t in range(3600)]
    assert len(_plan(3600, loudness=loud, count=5)) == 5
    assert len(_plan(3600, loudness=[(10.0, 0.9)], count=5)) == 1


def test_peaks_are_spread_not_clumped():
    # Many strong peaks packed into the first ten minutes, a weaker one far away.
    loud = [(float(t), 0.0) for t in range(3600)]
    for t in range(0, 600, 10):
        loud[t] = (float(t), 0.9)
    loud[3000] = (3000.0, 0.4)
    out = _plan(3600, loudness=loud, count=3)
    assert len(out) == 3 and any(s > 2000 for s, _e, _x in out)


def test_peak_at_the_edges_stays_inside_the_video():
    loud = [(0.0, 1.0), (3599.0, 0.9)]
    out = _plan(3600, loudness=loud, count=2)
    assert all(s >= 0 and e <= 3600 for s, e, _x in out)


def test_chapters_alone_give_weak_hints_and_even_spacing_covers_tiny_counts():
    out = _plan(3600, chapters=[(0.0, 900.0, "a"), (900.0, 1800.0, "b")], count=2)
    assert len(out) == 2 and all(x == 0.3 for _s, _e, x in out)
    assert len(_plan(200, count=12)) == 1  # only one stretch fits


def test_zero_signal_values_fall_back_to_even_spacing():
    out = _plan(3600, heatmap=[(0.0, 60.0, 0.0)], loudness=[(5.0, 0.0)], count=3)
    assert len(out) == 3 and all(x == 0.0 for _s, _e, x in out)


# --- fetch_chunks

def _fake_world(monkeypatch, tmp_path, *, meta=None, fail=(), loud=None, scout_error=None):
    seen = {"ranges": None, "audio": []}
    meta = meta if meta is not None else {"duration": 3600, "heatmap": [
        {"start_time": 600, "end_time": 660, "value": 1.0}, {"start_time": 2400, "end_time": 2460, "value": 0.8}]}
    monkeypatch.setattr(chunks, "_ytdlp_metadata", lambda url: meta)

    def scout(url, dest, stem):
        if scout_error:
            raise fetch.FetchError("scout failed")
        audio = dest / f"{stem}.m4a"
        audio.write_bytes(b"a")
        seen["audio"].append(audio)
        return audio

    def download(url, dest, stems, ranges, fmt):
        seen["ranges"], seen["fmt"], seen["stems"] = ranges, fmt, stems
        out = []
        for i, stem in enumerate(stems):
            if i in fail:
                out.append(None)
                continue
            path = dest / f"{stem}.mp4"
            path.write_bytes(b"v")
            out.append(path)
        return out

    monkeypatch.setattr(chunks, "_ytdlp_scout_audio", scout)
    monkeypatch.setattr(chunks, "_ytdlp_download_ranges", download)
    monkeypatch.setattr("clips.signals.loudness_curve", lambda p, step_s=1.0: loud or [])
    monkeypatch.setattr("slice.profile.probe_media", lambda p: SimpleNamespace(
        width=1920, height=1080, fps=30, duration=150.0, has_audio=True))
    return seen


def test_fetch_chunks_downloads_exactly_the_planned_ranges(tmp_path, monkeypatch, downloads_on):
    seen = _fake_world(monkeypatch, tmp_path)
    out = chunks.fetch_chunks(_video(), tmp_path / "media", max_height=720)
    planned = chunks.plan_ranges(
        3600.0,
        heatmap=[(600.0, 660.0, 1.0), (2400.0, 2460.0, 0.8)],
        loudness=[], chapters=[], count=12, length_s=150.0,
    )
    assert seen["ranges"] == [(s, e) for s, e, _x in planned]
    assert "height<=720" in seen["fmt"]
    assert [c.offset_s for c in out] == [s for s, _e, _x in planned]
    assert out[0].strength >= out[-1].strength
    assert all(Path(c.fetched.video_path).name.startswith("abc_c") for c in out)
    # The heatmap bin (600-660) is clipped to the chunk (610-760) and made relative to it.
    assert out[0].fetched.heatmap and out[0].fetched.heatmap[0][:2] == (0.0, 50.0)


def test_scout_audio_is_used_and_deleted(tmp_path, monkeypatch, downloads_on):
    loud = [(float(t), 0.1) for t in range(3600)]
    loud[1800] = (1800.0, 1.0)
    seen = _fake_world(monkeypatch, tmp_path, meta={"duration": 3600}, loud=loud)
    out = chunks.fetch_chunks(_video(), tmp_path / "media")
    assert seen["audio"] and not any(p.exists() for p in seen["audio"])
    assert not list((tmp_path / "media").glob("*_scout.*"))
    assert out[0].offset_s == pytest.approx(1780)


def test_scout_audio_deleted_when_measuring_fails(tmp_path, monkeypatch, downloads_on):
    seen = _fake_world(monkeypatch, tmp_path, meta={"duration": 3600})

    def boom(p, step_s=1.0):
        raise RuntimeError("ffmpeg died")

    monkeypatch.setattr("clips.signals.loudness_curve", boom)
    out = chunks.fetch_chunks(_video(), tmp_path / "media")
    assert not any(p.exists() for p in seen["audio"])
    assert out  # fell back to even spacing


def test_scout_download_failure_falls_back(tmp_path, monkeypatch, downloads_on):
    _fake_world(monkeypatch, tmp_path, meta={"duration": 3600}, scout_error=True)
    assert chunks.fetch_chunks(_video(), tmp_path / "media")


def test_scout_can_be_switched_off(tmp_path, monkeypatch, downloads_on):
    seen = _fake_world(monkeypatch, tmp_path, meta={"duration": 3600})
    monkeypatch.setattr(chunks, "_setting", lambda name: False if name == "scout_audio" else getattr(clips_settings(), name))
    chunks.fetch_chunks(_video(), tmp_path / "media")
    assert seen["audio"] == []


def test_a_failed_chunk_is_skipped_and_the_rest_come_back(tmp_path, monkeypatch, downloads_on):
    _fake_world(monkeypatch, tmp_path, fail={0}, meta={"duration": 3600})
    out = chunks.fetch_chunks(_video(), tmp_path / "media")
    assert len(out) == 11  # twelve planned, one lost


def test_unreadable_chunk_is_skipped(tmp_path, monkeypatch, downloads_on):
    _fake_world(monkeypatch, tmp_path)
    real = fetch.probe
    calls = []

    def flaky(path):
        calls.append(path)
        if len(calls) == 1:
            raise fetch.FetchError("bad file")
        return real(path)

    monkeypatch.setattr(fetch, "probe", flaky)
    assert len(chunks.fetch_chunks(_video(), tmp_path / "media")) == len(calls) - 1


def test_nothing_fetched_is_a_plain_error(tmp_path, monkeypatch, downloads_on):
    _fake_world(monkeypatch, tmp_path, fail=set(range(20)), meta={"duration": 3600})
    with pytest.raises(fetch.FetchError, match="could be downloaded"):
        chunks.fetch_chunks(_video(), tmp_path / "media")


def test_downloads_off_refuses_before_any_call(tmp_path, monkeypatch):
    clips_settings.cache_clear()
    called = []
    monkeypatch.setattr(chunks, "_ytdlp_metadata", lambda url: called.append(1) or {})
    with pytest.raises(fetch.FetchError, match="CLIPS_YOUTUBE_DOWNLOADS"):
        chunks.fetch_chunks(_video(), tmp_path / "media")
    assert called == []


def test_a_video_over_the_hour_limit_is_refused(tmp_path, monkeypatch, downloads_on):
    seen = _fake_world(monkeypatch, tmp_path, meta={"duration": 13 * 3600})
    with pytest.raises(fetch.FetchError, match="12 hour limit"):
        chunks.fetch_chunks(_video(13 * 3600), tmp_path / "media")
    assert seen["ranges"] is None and seen["audio"] == []


def test_unknown_length_is_refused(tmp_path, monkeypatch, downloads_on):
    _fake_world(monkeypatch, tmp_path, meta={})
    with pytest.raises(fetch.FetchError, match="how long"):
        chunks.fetch_chunks(_video(0.0), tmp_path / "media")


def test_chunk_files_are_removed_by_cleanup(tmp_path, monkeypatch, downloads_on):
    import os
    import time

    _fake_world(monkeypatch, tmp_path)
    out = chunks.fetch_chunks(_video(), tmp_path / "media")
    old = time.time() - 10 * 3600
    for c in out:
        os.utime(c.fetched.video_path, (old, old))
    assert fetch.cleanup(tmp_path / "media", 5) == len(out)


@pytest.mark.parametrize("state", ["is_live", "is_upcoming", "post_live"])
def test_a_stream_that_is_not_finished_is_not_fetched(tmp_path, monkeypatch, downloads_on, state):
    seen = _fake_world(monkeypatch, tmp_path, meta={"duration": 3600, "live_status": state})
    with pytest.raises(fetch.NotReady):
        chunks.fetch_chunks(_video(), tmp_path / "media")
    assert seen["ranges"] is None and not seen["audio"]


def test_the_scout_asks_for_audio_only(tmp_path, monkeypatch):
    import yt_dlp

    opts = {}

    class Fake:
        def __init__(self, o):
            opts.update(o)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            return {}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Fake)
    chunks._ytdlp_scout_audio(URL, tmp_path, "abc_scout")
    assert opts["format"] == "worstaudio"
