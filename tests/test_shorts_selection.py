"""Fewer, better Shorts: the re-rank's hook / stands_alone / short_start fields,
how select_shorts uses them, and the manifest rows that carry them."""

from core.models import Moment, SegmentJudgment, ShortClip, Word
from decide.gemini_client import _apply_rerank, parse_rerank_lines
from deliver import clip_text, shorts_manifest_rows
from edl.highlights import select_shorts
from publish import settings as publish_settings


def _j(i, length=9.0, text=None):
    start = i * (length + 1.0)
    return SegmentJudgment(index=i, start=start, end=start + length, text=text or f"s{i}.", decision="keep",
                           removal_category="none", highlight_score=0, highlight_category="none")


def _m(id_, first, last, score=8, cat="concept", sa=None, ss=None, js=None):
    return Moment(id=id_, start=js[first].start, end=js[last].end, first_index=first, last_index=last, score=score,
                  category=cat, short_worthy=True, reranked=True, stands_alone=sa,
                  short_start=js[ss].start if ss is not None else None)


# ------------------------------------------------------------------ parser

OLD = "0|7|arch|4|9|y|Redis store|Cuts latency."
NEW = OLD + "|Redis cut our latency|8|6"


def test_old_eight_field_line_parses_as_before():
    (it,) = parse_rerank_lines(OLD)
    assert (it["t"], it["k"]) == ("Redis store", "Cuts latency.")
    assert (it["hk"], it["sa"], it["ss"]) == (None, None, None)


def test_eleven_field_line_fills_the_new_fields():
    (it,) = parse_rerank_lines(NEW)
    assert (it["t"], it["k"], it["hk"], it["sa"], it["ss"]) == (
        "Redis store", "Cuts latency.", "Redis cut our latency", 8.0, 6)


def test_bad_new_fields_are_ignored_but_the_line_stays():
    # hook too long, stands_alone out of range
    (it,) = parse_rerank_lines(OLD + "|this hook has far too many words in it|11|6")
    assert it["id"] == 0 and it["k"] == "Cuts latency." and it["hk"] is None and it["sa"] is None
    # "-" for a not short-worthy moment; and text instead of numbers = treated as part of the old line
    (it,) = parse_rerank_lines("1|4|dec|4|9|n|Status|Plain update.|-|-|-")
    assert (it["hk"], it["sa"], it["ss"]) == (None, None, None) and it["k"] == "Plain update."
    (it,) = parse_rerank_lines(OLD + "|a|b|c")
    assert it["id"] == 0 and it["sa"] is None and it["ss"] is None


def test_stray_closing_bar_and_malformed_new_fields_keep_the_why_clean():
    # trailing "|" after short_start
    (it,) = parse_rerank_lines(OLD + "|Redis cut our latency|8|6|")
    assert (it["k"], it["hk"], it["sa"], it["ss"]) == ("Cuts latency.", "Redis cut our latency", 8.0, 6)
    # "8/10" and "404-406" are bad values: dropped one by one, never glued onto the why
    (it,) = parse_rerank_lines(OLD + "|Redis cut our latency|8/10|404-406|")
    assert it["k"] == "Cuts latency." and it["hk"] == "Redis cut our latency"
    assert it["sa"] is None and it["ss"] is None
    (it,) = parse_rerank_lines(OLD + "|Redis cut our latency|8|404-406")
    assert it["k"] == "Cuts latency." and it["sa"] == 8.0 and it["ss"] is None


def test_pipeline_warns_only_when_material_is_missing_not_when_the_cap_limits(tmp_path):
    from types import SimpleNamespace

    import pipeline

    js = [_j(i) for i in range(80)]
    moments = [_m(k, _far(k), _far(k) + 2, js=js) for k in range(6)]
    job = SimpleNamespace(warnings=[], selection_path=None)
    opts = SimpleNamespace(highlights_target_s=None, shorts_count=None)
    media = SimpleNamespace(fps=30, total_frames=None)
    publish_settings.save({"shorts_per_meeting": 2})
    _, _, shorts = pipeline._select(job, tmp_path, js, moments, [], media, 0, js[-1].end, opts,
                                    pipeline.get_settings())
    assert len(shorts) == 2
    assert not [w for w in job.warnings if "short-worthy" in w]


def test_short_start_outside_a_to_b_is_ignored():
    js = [_j(i) for i in range(12)]
    m = _m(0, 4, 9, js=js)
    (ok,) = parse_rerank_lines(NEW)
    assert _apply_rerank(m, ok, js, 2).short_start == js[6].start
    (out,) = parse_rerank_lines(OLD + "|Redis cut our latency|8|2")  # 2 is before a=4
    got = _apply_rerank(m, out, js, 2)
    assert got.short_start is None and got.stands_alone == 8.0


def test_hook_with_words_nobody_said_is_dropped():
    js = [_j(i, text=f"Redis cut our latency {i}.") for i in range(12)]
    m = _m(0, 4, 9, js=js)
    (it,) = parse_rerank_lines(NEW)
    assert _apply_rerank(m, it, js, 2).screen_hook == "Redis cut our latency"
    (it,) = parse_rerank_lines(OLD + "|Secret trick nobody knows|8|6")
    assert _apply_rerank(m, it, js, 2).screen_hook is None


def test_old_saved_moment_still_loads():
    m = Moment.model_validate({"id": 1, "start": 0, "end": 5, "first_index": 0, "last_index": 1, "score": 5})
    assert (m.screen_hook, m.stands_alone, m.short_start) == (None, None, None)


# ------------------------------------------------------------- select_shorts


def _far(k):  # moments far apart, so none overlap
    return 10 * k


def test_count_follows_the_publishing_setting():
    js = [_j(i) for i in range(80)]
    moments = [_m(k, _far(k), _far(k) + 2, js=js) for k in range(5)]
    publish_settings.save({"shorts_per_meeting": 2})
    assert len(select_shorts(moments, js, count=4)) == 2
    publish_settings.save({"shorts_per_meeting": 5})
    assert len(select_shorts(moments, js, count=4)) == 4  # SHORTS_COUNT still caps
    publish_settings.save({"shorts_per_meeting": 0})
    assert select_shorts(moments, js, count=4) == []


def test_stands_alone_moments_come_first():
    publish_settings.save({"shorts_per_meeting": 3})
    js = [_j(i) for i in range(80)]
    low = _m(0, 0, 2, score=10, sa=5, js=js)
    high = _m(1, 10, 12, score=6, sa=9, js=js)
    unknown = _m(2, 20, 22, score=9, js=js)
    assert [s.moment_id for s in select_shorts([low, high, unknown], js, count=3)] == [1, 0, 2]
    # without stands_alone anywhere: today's order, best score first
    plain = [m.model_copy(update={"stands_alone": None}) for m in (low, high, unknown)]
    assert [s.moment_id for s in select_shorts(plain, js, count=3)] == [0, 2, 1]


def test_a_funny_moment_needs_stands_alone_of_8():
    js = [_j(i) for i in range(40)]
    assert select_shorts([_m(0, 0, 2, cat="funny", sa=7, js=js)], js, count=2) == []
    assert len(select_shorts([_m(0, 0, 2, cat="funny", sa=8, js=js)], js, count=2)) == 1
    assert len(select_shorts([_m(0, 0, 2, cat="funny", sa=None, js=js)], js, count=2)) == 1  # as before
    assert len(select_shorts([_m(0, 0, 2, cat="concept", sa=7, js=js)], js, count=2)) == 1


def test_short_start_trims_the_setup_but_keeps_the_minimum_length():
    js = [_j(i) for i in range(40)]  # 9 s lines, 10 s apart
    m = _m(0, 0, 5, js=js, ss=2)  # 0..59 s; start on line 2 -> 20..59 = 39 s
    (s,) = select_shorts([m], js, count=1, min_s=20, max_s=60)
    assert s.start == js[2].start and s.end == js[5].end
    late = _m(0, 0, 5, js=js, ss=5)  # would leave 9 s: under the minimum, keep the old start
    (s,) = select_shorts([late], js, count=1, min_s=20, max_s=60)
    assert s.start == js[0].start
    outside = _m(0, 0, 5, js=js).model_copy(update={"short_start": js[20].start})
    (s,) = select_shorts([outside], js, count=1, min_s=20, max_s=60)
    assert s.start == js[0].start and s.end == js[5].end


def test_the_clip_carries_the_new_fields():
    js = [_j(i) for i in range(40)]
    m = _m(0, 0, 5, sa=9, js=js).model_copy(update={"screen_hook": "Redis cut latency"})
    (s,) = select_shorts([m], js, count=1)
    assert (s.screen_hook, s.stands_alone) == ("Redis cut latency", 9)


# ------------------------------------------------------------------ manifest


def test_manifest_rows_carry_hook_score_and_text():
    words = [Word(word=w, start=i * 1.0, end=i * 1.0 + 0.9, speaker="A")
             for i, w in enumerate(["one", "two", "three", "four", "five", "six"])]
    clips = [ShortClip(index=1, moment_id=0, start=1.0, end=4.0, score=8, screen_hook="two three", stands_alone=9),
             ShortClip(index=2, moment_id=1, start=4.0, end=6.0, score=7)]
    rows = shorts_manifest_rows([("shorts/short_01.mp4", 1.0, 4.0, "T1", "H1"),
                                 ("shorts/short_02.mp4", 4.0, 6.0, "T2", "H2")], clips, words)
    assert rows[0]["screen_hook"] == "two three" and rows[0]["stands_alone"] == 9
    assert rows[0]["text"] == "two three four" and rows[0]["title"] == "T1" and rows[0]["hook"] == "H1"
    assert rows[1]["screen_hook"] is None and rows[1]["stands_alone"] is None
    assert len(clip_text([Word(word="x" * 3000, start=0, end=1, speaker="A")], 0, 1)) == 2000


# ------------------------------------------------- re-rank parsing (review fix)


def test_rerank_short_or_broken_tail_never_leaks_pipes():
    from decide.gemini_client import _clean_title, parse_rerank_lines
    lines = [
        "12|8|arch|402|409|y|Moving the store|Cuts latency.|Redis cut our latency|8",
        "1|4|dec|4|9|n|Status|Why text.|-|3|",
        "1|4|dec|4|9|n|Status||-|-|-",
    ]
    for line, why in zip(lines, ["Cuts latency.", "Why text.", ""]):
        (item,) = parse_rerank_lines(line)
        assert item["k"] == why and "|" not in item["k"]
        assert item["hk"] is None and item["sa"] is None and item["ss"] is None
    assert _clean_title("a|b", 50) == "a b"
    (ok,) = parse_rerank_lines("1|4|dec|4|9|n|Status|Why.|Hook|8|5|")
    assert (ok["k"], ok["hk"], ok["sa"], ok["ss"]) == ("Why.", "Hook", 8.0, 5)
