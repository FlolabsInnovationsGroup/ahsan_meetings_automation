"""Streams and long videos in the runner: stretches (chunks) in, Shorts out, with fakes only."""

from __future__ import annotations

from pathlib import Path

import pytest

from clips.config import clips_settings
from clips.models import (
    Chunk,
    ClipKind,
    ClipStatus,
    Fetched,
    Moment,
    Platform,
    RightsBasis,
    Source,
    SourceVideo,
    VideoStatus,
)
from clips.runner import Runner
from clips.store import Store

from .test_runner import Fakes

URL = "https://www.youtube.com/watch?v=abc123"


class StreamFakes(Fakes):
    """The runner's fakes plus a long video that is chunked into three stretches."""

    def __init__(self):
        super().__init__()
        self.videos = []
        self.chunked = True
        self.chunk_offsets = [600.0, 3000.0, 5000.0]
        self.by_chunk: dict[str, list[Moment]] = {}
        self.pick_fail: set[str] = set()
        self.chunk_calls = 0
        self.fetched_whole = 0

    def chunks(self):
        return [
            Chunk(offset_s=off, strength=0.9 - i * 0.1, fetched=Fetched(video_path=f"/media/c{i}.mp4", duration_s=150))
            for i, off in enumerate(self.chunk_offsets)
        ]

    def stages(self):
        st = super().stages()
        whole = st.fetch

        def fetch(video, dest, **kw):
            self.fetched_whole += 1
            return whole(video, dest, **kw)

        def fetch_chunks(video, dest, *, max_height=1080):
            self.chunk_calls += 1
            return self.chunks()

        def pick(video, fetched, source, **kw):
            if fetched.video_path in self.pick_fail:
                raise RuntimeError("model broke on this stretch")
            return list(self.by_chunk.get(fetched.video_path, self.moments))

        st.fetch = fetch
        st.is_chunked = lambda video: self.chunked
        st.fetch_chunks = fetch_chunks
        st.pick_moments = pick
        st.credit_line = lambda source, video=None: f"Clip from Demo - {video.url}" if video else "Clip from Demo"
        return st


def short(start, score, reason="", **kw):
    return Moment(start_s=start, end_s=start + 30, kind=ClipKind.SHORT, score=score, reason=reason, **kw)


@pytest.fixture(autouse=True)
def _downloads_on(_clips_env, monkeypatch):
    monkeypatch.setenv("CLIPS_YOUTUBE_DOWNLOADS", "true")
    clips_settings.cache_clear()


@pytest.fixture
def fakes():
    return StreamFakes()


@pytest.fixture
def store(clips_storage):
    s = Store(clips_storage / "clips.sqlite")
    s.save_source(
        Source(id="s1", name="Demo", platform=Platform.YOUTUBE, rights=RightsBasis.OWN,
               evidence_url="https://e.example", enabled=True)
    )
    s.save_video(SourceVideo(id="youtube:abc123", source_id="s1", platform=Platform.YOUTUBE,
                             native_id="abc123", url=URL, duration_s=7200, was_live=True))
    return s


def runner(store, fakes, *, llm=None):
    return Runner(store, settings=clips_settings(), stages=fakes.stages(), llm=llm)


def drafts(store):
    return store.list_clips(status=ClipStatus.DRAFT)


def test_chunked_video_makes_clips_from_several_chunks_with_offsets(store, fakes):
    fakes.by_chunk = {"/media/c0.mp4": [short(10, 0.9)], "/media/c1.mp4": [short(20, 0.8)], "/media/c2.mp4": [short(0, 0.7)]}
    assert runner(store, fakes).process() == 3
    assert fakes.chunk_calls == 1 and fakes.fetched_whole == 0
    assert sorted(c.source_offset_s for c in drafts(store)) == [610.0, 3020.0, 5000.0]
    assert store.get_video("youtube:abc123").status is VideoStatus.DONE


def test_description_link_carries_the_time(store, fakes):
    fakes.by_chunk = {"/media/c0.mp4": [short(25, 0.9)], "/media/c1.mp4": [], "/media/c2.mp4": []}
    runner(store, fakes).process()
    (clip,) = drafts(store)
    assert f"{URL}&t=625" in clip.credit
    assert clip.source_offset_s == 625.0


def test_bar_and_cap_hold_with_a_language_model(store, fakes, monkeypatch):
    monkeypatch.setenv("CLIPS_STREAM_SHORTS_MAX", "2")
    monkeypatch.setenv("CLIPS_SHORTS_PER_VIDEO", "5")
    clips_settings.cache_clear()
    fakes.by_chunk = {
        "/media/c0.mp4": [short(0, 0.95), short(40, 0.3)],
        "/media/c1.mp4": [short(0, 0.85), short(40, 0.59)],
        "/media/c2.mp4": [short(0, 0.7)],
    }
    assert runner(store, fakes, llm=object()).process() == 2
    scores = sorted(c.moment.score for c in drafts(store))
    assert scores == [0.85, 0.95]  # the best two; nothing under 0.6


def test_no_language_model_keeps_the_old_behaviour(store, fakes):
    fakes.by_chunk = {"/media/c0.mp4": [short(0, 0.2)], "/media/c1.mp4": [short(0, 0.1)], "/media/c2.mp4": []}
    assert runner(store, fakes).process() == 2  # low scores are fine without a model


def test_signal_fallback_moments_skip_the_bar_even_with_a_model(store, fakes):
    fakes.by_chunk = {
        "/media/c0.mp4": [short(0, 0.2, reason="picked from signals: loud")], "/media/c1.mp4": [], "/media/c2.mp4": [],
    }
    assert runner(store, fakes, llm=object()).process() == 1


def test_a_failed_chunk_is_survived(store, fakes):
    fakes.pick_fail = {"/media/c1.mp4"}
    fakes.by_chunk = {"/media/c0.mp4": [short(0, 0.9)], "/media/c2.mp4": [short(0, 0.8)]}
    assert runner(store, fakes).process() == 2
    assert store.get_video("youtube:abc123").status is VideoStatus.DONE
    assert any("stretch" in e["message"] and "failed" in e["message"] for e in store.list_events(limit=50))


def test_a_failed_render_keeps_the_others(store, fakes):
    fakes.by_chunk = {"/media/c0.mp4": [short(7, 0.9)], "/media/c1.mp4": [short(8, 0.8)], "/media/c2.mp4": []}
    fakes.render_fail_start = 7
    assert runner(store, fakes).process() == 1


def test_overlapping_stretches_do_not_make_the_same_moment_twice(store, fakes):
    fakes.chunk_offsets = [1000.0, 1010.0, 5000.0]
    fakes.by_chunk = {"/media/c0.mp4": [short(20, 0.9)], "/media/c1.mp4": [short(10, 0.8)], "/media/c2.mp4": []}
    assert runner(store, fakes).process() == 1


def test_nothing_clears_the_bar_finishes_done_not_failed(store, fakes):
    fakes.by_chunk = {"/media/c0.mp4": [short(0, 0.3)], "/media/c1.mp4": [short(0, 0.4)], "/media/c2.mp4": []}
    assert runner(store, fakes, llm=object()).process() == 0
    assert store.get_video("youtube:abc123").status is VideoStatus.DONE
    assert any("good enough" in e["message"] for e in store.list_events(limit=50))


def test_not_chunked_video_still_uses_the_plain_fetch(store, fakes):
    fakes.chunked = False
    fakes.moments = [short(1, 0.9), short(40, 0.8)]
    assert runner(store, fakes).process() == 2
    assert fakes.fetched_whole == 1 and fakes.chunk_calls == 0
    assert all(c.source_offset_s == 0 for c in drafts(store))


def test_bar_also_applies_to_a_video_that_is_not_chunked(store, fakes):
    fakes.chunked = False
    fakes.moments = [short(1, 0.9), short(40, 0.5)]
    assert runner(store, fakes, llm=object()).process() == 1


def test_downloads_off_never_reaches_fetch_chunks(store, fakes, monkeypatch):
    monkeypatch.setenv("CLIPS_YOUTUBE_DOWNLOADS", "false")
    clips_settings.cache_clear()
    runner(store, fakes).process()
    assert fakes.chunk_calls == 0


def test_bad_rights_never_reach_fetch_chunks(store, fakes):
    fakes.ok = False
    runner(store, fakes).process()
    assert fakes.chunk_calls == 0
    assert store.get_video("youtube:abc123").status is VideoStatus.SKIPPED


def test_a_video_whose_own_check_fails_never_reaches_fetch_chunks(store, fakes):
    fakes.fresh_ok = False
    runner(store, fakes).process()
    assert fakes.chunk_calls == 0


def test_env_example_lists_the_new_settings():
    text = (Path(__file__).parents[2] / "deploy" / "clips" / "env.example").read_text(encoding="utf-8")
    for name in ("INCLUDE_STREAMS", "CHUNK_MINUTES_OVER", "CHUNKS_PER_VIDEO", "CHUNK_SECONDS",
                 "SCOUT_AUDIO", "MAX_STREAM_HOURS", "STREAM_SHORTS_MAX", "MIN_MOMENT_SCORE"):
        assert f"CLIPS_{name}=" in text


def test_chunked_video_never_gets_a_path_saved(store, fakes):
    fakes.by_chunk = {"/media/c0.mp4": [short(10, 0.9)], "/media/c1.mp4": [], "/media/c2.mp4": []}
    runner(store, fakes).process()
    assert store.get_video("youtube:abc123").path == ""


def test_an_interrupted_chunked_video_is_replanned_not_read_as_one_chunk(store, fakes):
    r = runner(store, fakes)
    clips_settings().media_dir.mkdir(parents=True, exist_ok=True)
    v = store.get_video("youtube:abc123")
    v.status, v.path = VideoStatus.PICKED, str(clips_settings().media_dir / "abc123_c00_600.mp4")
    store.save_video(v)
    r.process(limit=0)
    after = store.get_video("youtube:abc123")
    assert after.status is VideoStatus.NEW and after.path == ""


def test_a_real_local_path_is_kept_on_recovery(store, fakes, tmp_path):
    r = runner(store, fakes)
    v = store.get_video("youtube:abc123")
    v.status, v.path = VideoStatus.PICKED, str(tmp_path / "abc123_c00_600.mp4")  # not in the media folder
    store.save_video(v)
    r.process(limit=0)
    assert store.get_video("youtube:abc123").path == v.path


def test_a_chunk_at_offset_zero_still_gets_its_time(store, fakes):
    fakes.chunk_offsets = [0.0]
    fakes.by_chunk = {"/media/c0.mp4": [short(95, 0.9)]}
    runner(store, fakes).process()
    (clip,) = drafts(store)
    assert clip.source_offset_s == 95.0 and f"{URL}&t=95" in clip.credit


def test_a_video_that_is_not_ready_stays_new(store, fakes):
    from clips.fetch import NotReady

    st = fakes.stages()

    def not_ready(video, dest, **kw):
        raise NotReady("video is post_live")

    st.fetch_chunks = not_ready
    r = Runner(store, settings=clips_settings(), stages=st)
    assert r.process() == 0
    assert store.get_video("youtube:abc123").status is VideoStatus.NEW
