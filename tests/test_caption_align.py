"""Caption words follow forced-alignment times; pauses shift them; the
proportional spread is the fallback. The aligner is mocked."""

import pytest

from core.models import Word
from slice import align
from slice.captions import caption_cues
from slice.pipeline import remap_cues


def _w(text, start, end):
    return Word(word=text, start=start, end=end, speaker="A")


def test_cues_use_the_aligned_word_times():
    item = _w("alpha beta gamma delta.", 10.0, 14.0)
    times = {id(item): [(10.0, 10.3), (10.4, 10.9), (12.0, 12.4), (12.5, 12.9)]}
    cues = caption_cues([item], 10.0, 14.0, max_words=2, word_times=times, bridge_s=0)
    assert [c.text for c in cues] == ["alpha beta", "gamma delta."]
    assert (cues[0].start, cues[0].end) == (0.0, 0.9)
    assert (cues[1].start, cues[1].end) == (2.0, 2.9)
    spread = caption_cues([item], 10.0, 14.0, max_words=2, bridge_s=0)
    assert spread[1].start != cues[1].start  # the character-weight spread differs


def test_a_wrong_word_count_falls_back_to_the_spread():
    item = _w("alpha beta.", 0.0, 2.0)
    assert caption_cues([item], 0.0, 2.0, word_times={id(item): [(0.0, 1.0)]}) == caption_cues([item], 0.0, 2.0)


def test_trimmed_pauses_shift_the_aligned_cues():
    item = _w("alpha beta gamma delta.", 10.0, 20.0)
    times = {id(item): [(10.0, 10.5), (10.6, 11.0), (16.0, 16.5), (16.6, 17.0)]}
    cues = caption_cues([item], 10.0, 20.0, max_words=2, word_times=times, bridge_s=0)
    # source 11.2 - 15.8 is a removed pause: 4.6 s gone
    out = remap_cues(cues, [(0.0, 1.2), (5.8, 10.0)])
    assert [c.text for c in out] == ["alpha beta", "gamma delta."]
    assert out[0].start == pytest.approx(0.0) and out[1].start == pytest.approx(6.0 - 4.6)


def test_align_clip_maps_times_to_the_source_and_fills_untimed_words(monkeypatch, tmp_path):
    item = _w("one 2 three.", 30.0, 33.0)
    monkeypatch.setattr(align, "_load_audio", lambda *a: None)
    monkeypatch.setattr(align, "_align_backend", lambda segs, audio, lang: [
        {"words": [{"word": "one", "start": 0.1, "end": 0.5}, {"word": "2"}, {"word": "three.", "start": 1.5, "end": 2.0}]}])
    got = align.align_clip(tmp_path / "a.flac", [item], 30.0, 34.0)
    (times,) = got.values()
    assert times[0] == pytest.approx((30.1, 30.5)) and times[2] == pytest.approx((31.5, 32.0))
    assert 30.5 <= times[1][0] <= times[1][1] <= 31.5


def test_align_clip_falls_back_when_the_aligner_raises_or_is_empty(monkeypatch, tmp_path):
    item = _w("one two.", 0.0, 2.0)
    monkeypatch.setattr(align, "_load_audio", lambda *a: None)

    def boom(*a):
        raise ImportError("no whisperx")

    monkeypatch.setattr(align, "_align_backend", boom)
    assert align.align_clip(tmp_path / "a.flac", [item], 0.0, 3.0) == {}
    monkeypatch.setattr(align, "_align_backend", lambda *a: [{"words": []}])
    assert align.align_clip(tmp_path / "a.flac", [item], 0.0, 3.0) == {}
    # the cues then come from the spread, as before
    assert caption_cues([item], 0.0, 3.0, word_times={}) == caption_cues([item], 0.0, 3.0)


def test_a_sentence_crossing_the_clip_edge_is_not_aligned(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(align, "_load_audio", lambda *a: called.append(1))
    inside, edge = _w("a b.", 5.0, 6.0), _w("c d.", 9.0, 12.0)
    monkeypatch.setattr(align, "_align_backend", lambda segs, audio, lang: [
        {"words": [{"word": "a", "start": 0.0, "end": 0.4}, {"word": "b.", "start": 0.5, "end": 0.9}]}])
    got = align.align_clip(tmp_path / "a.flac", [inside, edge], 4.0, 10.0)
    assert list(got) == [id(inside)]
