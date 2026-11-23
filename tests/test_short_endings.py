"""A Short ends on a finished sentence: the 60 s cap, extension, the tail pad,
long Shorts and trim_to_speech never cut the end early."""

from core.models import Moment, SegmentJudgment
from edl.highlights import select_shorts
from slice.silence import trim_to_speech


def _j(i, start, length, text, speaker="A"):
    return SegmentJudgment(index=i, start=start, end=start + length, text=text, speaker=speaker, decision="keep")


def _moment(js, first, last, score=8, sa=None, peak=None):
    return Moment(id=0, start=js[first].start, end=js[last].end, first_index=first, last_index=last, score=score,
                  category="concept", short_worthy=True, reranked=True, stands_alone=sa, peak_index=peak)


def _run(js, m, **kw):
    kw.setdefault("min_s", 20)
    kw.setdefault("max_s", 60)
    return select_shorts([m], js, count=1, **kw)


def _lines(texts, length=10.0, gap=1.0):
    return [_j(i, i * (length + gap), length, t) for i, t in enumerate(texts)]


def test_a_cap_that_would_cut_mid_sentence_ends_on_the_last_full_sentence():
    # 0 1 2 3 4 5 6: line 5 ends mid-sentence; the 60 s cap cuts after line 4 or 5
    js = _lines(["One.", "Two.", "Three.", "Four.", "Five and", "six.", "Seven."])
    (s,) = _run(js, _moment(js, 0, 6, peak=0), tail_pad_s=0)
    assert s.end == js[3].end  # "Four." (line 4 would end mid-sentence; line 5 is past the cap)


def test_extension_forward_to_a_sentence_end_when_it_still_fits():
    js = _lines(["One.", "Two.", "Three and", "four.", "Five."])
    m = _moment(js, 0, 2)  # the moment itself ends on "Three and"
    (s,) = _run(js, m, tail_pad_s=0)
    assert s.end == js[3].end


def test_dropped_when_no_sentence_end_fits():
    js = _lines(["a", "b", "c", "d", "e", "f", "g"])  # nothing ends a sentence
    assert _run(js, _moment(js, 0, 6)) == []


def test_extension_stops_at_a_speaker_change():
    js = _lines(["One.", "Two.", "Three and", "four."])
    js[3] = _j(3, js[3].start, 10, "four.", speaker="B")
    (s,) = _run(js, _moment(js, 0, 2), tail_pad_s=0)
    assert s.end == js[1].end  # walks back instead (still >= 20 s)


def test_tail_pad_is_added_and_clamped_to_the_next_segment():
    js = _lines(["One.", "Two.", "Three."], gap=1.0)
    (s,) = _run(js, _moment(js, 0, 1), tail_pad_s=0.5)
    assert s.end == js[1].end + 0.5
    js2 = _lines(["One.", "Two.", "Three."], gap=0.2)
    (s,) = _run(js2, _moment(js2, 0, 1), tail_pad_s=0.5)
    assert s.end == js2[2].start  # not into the next line
    (s,) = _run(js2, _moment(js2, 0, 1), tail_pad_s=0.0)
    assert s.end == js2[1].end


def test_a_moment_too_long_for_60s_is_trimmed_unless_it_is_high_quality():
    js = _lines([f"Line {i}." for i in range(10)])  # 10 x 10 s, 1 s gaps: ~109 s
    m = _moment(js, 0, 9, score=7, sa=9)
    (s,) = _run(js, m, long_max_s=170, tail_pad_s=0)
    assert s.length_type == "standard" and s.end - s.start <= 60
    good = _moment(js, 0, 9, score=9, sa=9)
    (s,) = _run(js, good, long_max_s=170, tail_pad_s=0)
    assert s.length_type == "long" and s.end == js[9].end
    # long Shorts off: the same moment is a standard one
    (s,) = _run(js, good, tail_pad_s=0)
    assert s.length_type == "standard"
    # the long cap still ends on a sentence
    (s,) = _run(js, good, long_max_s=100, tail_pad_s=0)
    assert s.length_type == "long" and s.end - s.start <= 100 and s.end in {j.end for j in js}


def test_trim_to_speech_never_pulls_the_end_before_min_end():
    silences = [(55.0, 70.0)]
    assert trim_to_speech(10.0, 62.0, silences, 0.3, 0.2)[1] == 55.3
    assert trim_to_speech(10.0, 62.0, silences, 0.3, 0.2, min_end=62.0)[1] == 62.0
    assert trim_to_speech(10.0, 62.0, silences, 0.3, 0.2, min_end=58.0)[1] == 58.0
