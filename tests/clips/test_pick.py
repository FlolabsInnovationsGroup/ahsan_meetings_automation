from clips import pick
from clips.config import clips_settings
from clips.models import (
    Caption,
    ClipKind,
    Fetched,
    Platform,
    Source,
    SourceVideo,
    Window,
)

SRC = Source(id="s", name="Some Streamer", platform=Platform.YOUTUBE)
VID = SourceVideo(id="youtube:abc", source_id="s", platform=Platform.YOUTUBE, native_id="abc", title="Free roam chaos")


def fetched(duration=600.0, **kw):
    return Fetched(video_path="missing.mp4", duration_s=duration, **kw)


class FakeLLM:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, []

    def json(self, system, user, *, images=None, max_tokens=2000):
        self.calls.append((system, user, images, max_tokens))
        if self.error:
            raise self.error
        return self.answer


def m(start, end, **kw):
    return {"start_s": start, "end_s": end, "kind": "short", "summary": f"thing at {start}", "score": 0.7, **kw}


def test_no_llm_returns_top_candidates_neutral():
    wins = [Window(start_s=10, end_s=50, score=0.4, signals={"loud": 0.4}),
            Window(start_s=100, end_s=140, score=0.9, signals={"replayed": 0.9}),
            Window(start_s=300, end_s=340, score=0.6)]
    out = pick.pick_moments(VID, fetched(), SRC, max_shorts=2, max_long=0, candidates=wins)
    assert [x.start_s for x in out] == [100, 300]
    assert all(x.hook == "" and x.summary for x in out)
    assert out[0].kind is ClipKind.SHORT


def test_llm_answer_validated_and_repaired():
    answer = {"moments": [
        m(100, 130, hook="x" * 80, spoiler="true", has_music="no", score=5),
        m(102, 125, score=0.4),  # overlaps the first
        m(-20, 5, score=0.3),  # starts before the video, too short
        m(500, 900, score=0.6),  # runs past the end and is too long
        {"start_s": "bad", "end_s": 10},
        m(200, 210, score=0.1),
        "junk",
    ]}
    llm = FakeLLM(answer)
    src = SRC.model_copy(update={"avoid_spoilers": False})
    out = pick.pick_moments(VID, fetched(600), src, llm=llm, max_shorts=3, max_long=0,
                            candidates=[Window(start_s=90, end_s=130, score=0.5)])
    assert llm.calls
    starts = [x.start_s for x in out]
    assert out[0].start_s == 100 and out[0].score == 1.0 and out[0].spoiler is True and out[0].has_music is False
    assert len(out[0].hook) <= 40
    assert 102 not in starts
    for x in out:
        assert 0 <= x.start_s < x.end_s <= 600
        assert 15 <= x.end_s - x.start_s <= 58
    assert len(out) <= 3


def test_caps_per_kind():
    answer = {"moments": [m(i * 100, i * 100 + 30, score=i / 10, summary=w) for i, w in enumerate(['red car', 'blue boat', 'green plane', 'pink bike', 'black van'], 1)]
              + [{"start_s": 50, "end_s": 300, "kind": "long", "summary": "big", "score": 0.5}]}
    out = pick.pick_moments(VID, fetched(900), SRC, llm=FakeLLM(answer), max_shorts=2, max_long=1,
                            candidates=[Window(start_s=0, end_s=40)])
    shorts = [x for x in out if x.kind is ClipKind.SHORT]
    longs = [x for x in out if x.kind is ClipKind.LONG]
    assert len(shorts) == 2 and len(longs) == 1
    assert 180 <= longs[0].end_s - longs[0].start_s <= 600


def test_near_duplicates_dropped():
    answer = {"moments": [m(100, 130, score=0.9), m(103, 133, score=0.8), m(300, 330, score=0.5, summary="thing at 100")]}
    out = pick.pick_moments(VID, fetched(), SRC, llm=FakeLLM(answer), max_shorts=5, max_long=0,
                            candidates=[Window(start_s=0, end_s=40)])
    assert [x.start_s for x in out] == [100]  # 103 starts too close; the third repeats the summary


def test_llm_error_falls_back_to_candidates():
    out = pick.pick_moments(VID, fetched(), SRC, llm=FakeLLM(error=RuntimeError("down")), max_shorts=1, max_long=0,
                            candidates=[Window(start_s=20, end_s=60, score=0.5)])
    assert [x.start_s for x in out] == [20]


def test_empty_answer_falls_back():
    out = pick.pick_moments(VID, fetched(), SRC, llm=FakeLLM({"moments": []}), max_shorts=1, max_long=0,
                            candidates=[Window(start_s=20, end_s=60, score=0.5)])
    assert len(out) == 1


def test_defaults_come_from_settings(monkeypatch):
    monkeypatch.setenv("CLIPS_SHORTS_PER_VIDEO", "1")
    monkeypatch.setenv("CLIPS_LONG_PER_VIDEO", "0")
    clips_settings.cache_clear()
    wins = [Window(start_s=0, end_s=40, score=0.5), Window(start_s=200, end_s=240, score=0.4)]
    assert len(pick.pick_moments(VID, fetched(), SRC, candidates=wins)) == 1


def test_zero_duration_or_zero_wanted():
    assert pick.pick_moments(VID, Fetched(video_path="x"), SRC) == []
    assert pick.pick_moments(VID, fetched(), SRC, max_shorts=0, max_long=0) == []


def test_candidates_computed_when_not_given(monkeypatch):
    from clips import signals

    called = []
    monkeypatch.setattr(signals, "candidate_windows",
                        lambda f, *, kind, **kw: called.append(kind) or [Window(start_s=0, end_s=40, score=0.5)])
    out = pick.pick_moments(VID, fetched(), SRC, max_shorts=1, max_long=0)
    assert called == [ClipKind.SHORT] and len(out) == 1


def test_gather_uses_signals_when_file_exists(monkeypatch, tmp_path):
    from clips import signals

    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    monkeypatch.setattr(signals, "loudness_curve", lambda p: [(0.0, 0.1)])
    monkeypatch.setattr(signals, "scene_changes", lambda p: (_ for _ in ()).throw(RuntimeError("no ffmpeg")))
    seen = {}
    monkeypatch.setattr(signals, "candidate_windows",
                        lambda fe, *, kind, loudness=None, scenes=None, **kw: seen.update(l=loudness, s=scenes) or [])
    pick._gather_candidates(Fetched(video_path=str(f), duration_s=100), 1, 0)
    assert seen == {"l": [(0.0, 0.1)], "s": None}


def test_prompt_has_captions_and_signals():
    f = fetched(captions=[Caption(start_s=12, end_s=14, text="oh no the helicopter"), Caption(start_s=400, end_s=401, text="later")])
    text = pick.build_prompt(VID, f, SRC, [Window(start_s=10, end_s=50, score=0.8, signals={"loud": 0.8})],
                             {ClipKind.SHORT: 3, ClipKind.LONG: 1})
    assert "Free roam chaos" in text and "loud 0.80" in text and "helicopter" in text and "later" not in text


def test_vision_off_sends_no_images_and_on_sends_frames(monkeypatch, tmp_path):
    wins = [Window(start_s=0, end_s=40, score=0.5)]
    llm = FakeLLM({"moments": [m(10, 40)]})
    pick.pick_moments(VID, fetched(), SRC, llm=llm, max_shorts=1, max_long=0, candidates=wins)
    assert llm.calls[0][2] is None

    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    monkeypatch.setenv("CLIPS_VISION", "true")
    clips_settings.cache_clear()
    monkeypatch.setattr(pick, "grab_frame", lambda path, at, out: b"jpeg")
    llm = FakeLLM({"moments": [m(10, 40)]})
    pick.pick_moments(VID, Fetched(video_path=str(f), duration_s=600), SRC, llm=llm, max_shorts=1, max_long=0, candidates=wins)
    assert llm.calls[0][2] == [b"jpeg", b"jpeg"]


def test_frame_failure_means_no_images(monkeypatch, tmp_path):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    monkeypatch.setattr(pick, "grab_frame", lambda *a: (_ for _ in ()).throw(RuntimeError("ffmpeg")))
    assert pick.grab_candidate_frames(Fetched(video_path=str(f)), [Window(start_s=0, end_s=40)]) == []


def test_made_up_time_is_rejected_not_slid():
    answer = {"moments": [m(5000, 5040, hook="Huge flip")]}
    out = pick.pick_moments(VID, fetched(600), SRC, llm=FakeLLM(answer), max_shorts=1, max_long=0,
                            candidates=[Window(start_s=0, end_s=40)])
    assert all(x.hook != "Huge flip" and x.start_s < 5 for x in out)
    assert pick.validate_moments(answer["moments"], 600, {ClipKind.SHORT: 3, ClipKind.LONG: 0}) == []


def test_whole_short_video_is_never_a_long_clip():
    raw = [{"start_s": 0, "end_s": 120, "kind": "long", "summary": "all of it", "score": 0.9}]
    assert pick.validate_moments(raw, 120, {ClipKind.SHORT: 0, ClipKind.LONG: 1}) == []


def test_spoilers_dropped_before_the_cap():
    raw = [m(100, 130, score=0.9, spoiler=True, summary="reveal one"),
           m(200, 230, score=0.8, spoiler=True, summary="reveal two"),
           m(300, 330, score=0.5, summary="clean chase")]
    out = pick.validate_moments(raw, 600, {ClipKind.SHORT: 1, ClipKind.LONG: 0}, source=SRC)
    assert [x.summary for x in out] == ["clean chase"]
