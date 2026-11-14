from types import SimpleNamespace

import pytest

from core.models import (
    Chapter,
    EditDecisionList,
    EDLRange,
    Moment,
    RenderManifest,
    RenderPiece,
)
from decide import topics
from edl.builder import slice_edl, validate_edl
from publish.describe import clip_position

FPS = 25


def manifest(d=0):
    # three pieces; with d > 0 each later one overlaps the previous by d frames
    pieces = [(0, 3000, 0), (4000, 8000, 3000 - d), (9000, 15000, 7000 - 2 * d)]
    return RenderManifest(
        kind="full", fps="25", width=1280, height=720, fade_frames=d,
        pieces=[RenderPiece(src_start_frame=a, src_end_frame=b, out_start_frame=o) for a, b, o in pieces],
    )


def cfg(**kw):
    base = {"topic_cuts": True, "topic_cuts_per_meeting": 1, "topic_min_minutes": 5.0, "topic_max_minutes": 20.0,
                "topic_min_score": 7.0, "topic_cold_open": True}
    base.update(kw)
    return SimpleNamespace(**base)


def moment(i, start, end, raw=8.0, cat="insight", **kw):
    return Moment(id=i, start=start, end=end, first_index=0, last_index=0, score=raw, raw_score=raw,
                  category=cat, title=f"Topic {i}", hook=f"Hook {i}", **kw)


def plain(n_min=60):
    """One piece (a 60-minute cleaned video) with chapters every 6 minutes."""
    total = n_min * 60 * FPS
    m = RenderManifest(kind="full", fps="25", width=1, height=1,
                       pieces=[RenderPiece(src_start_frame=0, src_end_frame=total, out_start_frame=0)])
    chapters = [Chapter(start=i * 360.0, title=f"Part {i}") for i in range(n_min // 6)]
    return m, chapters


# ----------------------------------------------------------- the mapping


@pytest.mark.parametrize("d", [0, 12])
def test_mapping_round_trips_against_clip_position(d):
    m = manifest(d=d)
    checked = 0
    for k, piece in enumerate(m.pieces):
        later_start = m.pieces[k + 1].out_start_frame if k + 1 < len(m.pieces) else None
        for off in (0, 7, 100, 500):
            out = piece.out_start_frame + off
            if later_start is not None and out >= later_start:
                continue  # inside the dissolve: that time belongs to the later piece
            src_frame = piece.src_start_frame + off
            assert topics.cleaned_to_source_frame(m, out / FPS) == src_frame
            pos = clip_position(m, src_frame / FPS, src_frame / FPS + 5, 0.0)
            assert pos == int(out / FPS + 1e-6)
            checked += 1
    assert checked >= 8


def test_dissolve_time_maps_to_the_later_piece():
    m = manifest(d=12)
    out = m.pieces[1].out_start_frame + 3  # inside the dissolve into piece 1
    assert topics.cleaned_to_source_frame(m, out / FPS) == m.pieces[1].src_start_frame + 3


def test_mapping_clamps_outside_the_pieces():
    m = manifest()
    assert topics.cleaned_to_source_frame(m, 99999.0) == m.pieces[-1].src_end_frame
    assert topics.cleaned_to_source_frame(m, 0.0) == 0


def test_source_to_cleaned_skips_removed_stretches():
    m = manifest()
    assert topics.source_to_cleaned_s(m, 3500 / FPS) == 3000 / FPS  # removed: the next kept start
    assert topics.source_to_cleaned_s(m, 3500 / FPS, end=True) == 3000 / FPS  # previous kept end
    assert topics.source_to_cleaned_s(m, 20000 / FPS) is None


# ------------------------------------------------------------ candidates


def spans(lengths):
    out, t = [], 0.0
    for i, n in enumerate(lengths):
        out.append(topics.Span(t, t + n, f"c{i}"))
        t += n
    return out


def test_candidates_are_runs_of_one_to_three_chapters_within_bounds():
    found = topics.candidates(spans([360] * 6), 300, 1200)
    assert {len(c.titles) for c in found} == {1, 2, 3}
    assert all(300 <= c.length <= 1200 for c in found)


def test_candidates_length_bounds():
    only = topics.candidates(spans([200, 200]), 300, 1200)
    assert [(c.length, len(c.titles)) for c in only] == [(400, 2)]  # the pair, not the singles
    assert topics.candidates(spans([2000]), 300, 1200) == []
    assert len(topics.candidates(spans([360, 360, 360, 360]), 300, 400)) == 4  # singles only


def test_chapter_spans_end_at_the_next_start_and_the_video_end():
    s = topics.chapter_spans([Chapter(start=0, title="a"), Chapter(start=100, title="b")], 250)
    assert [(x.start, x.end) for x in s] == [(0, 100), (100, 250)]


# ------------------------------------------------------------------ gates


def run(moments, chapters=None, config=None, **kw):
    m, default = plain()
    return topics.pick_cuts(m, default if chapters is None else chapters, moments, config or cfg(), **kw)


def test_picks_the_best_and_rules_out_one_over_the_length_cap():
    cuts = run([moment(0, 400, 1000, raw=8.0), moment(1, 1800, 2900, raw=9.0)])
    assert len(cuts) == 1 and cuts[0].best.id == 1
    cuts = run([moment(0, 400, 1000, raw=8.0), moment(1, 0, 3500, raw=9.0)])  # 58 min: over the cap
    assert [c.best.id for c in cuts] == [0]
    for c in cuts:
        assert 300 <= c.cleaned_end_s - c.cleaned_start_s <= 1200


def test_score_gate():
    assert run([moment(0, 400, 1000, raw=6.5)]) == []
    assert len(run([moment(0, 400, 1000, raw=7.0)])) == 1


def test_category_gate():
    assert run([moment(0, 400, 1000, cat="funny")]) == []
    assert len(run([moment(0, 400, 1000, cat="decision")])) == 1


def test_stands_alone_gate_and_unknown_passes():
    assert run([moment(0, 400, 1000, stands_alone=5.0)]) == []
    assert len(run([moment(0, 400, 1000, stands_alone=6.0)])) == 1
    assert len(run([moment(0, 400, 1000, stands_alone=None, screen_hook=None)])) == 1


def test_never_post_word_in_a_chapter_title_blocks_the_candidate():
    _, chapters = plain()
    blocked = [Chapter(start=c.start, title="Weekly operations review") for c in chapters]
    mo = [moment(0, 400, 1000)]
    assert run(mo, chapters=blocked, never_words=["operations"]) == []
    assert len(run(mo, chapters=blocked, never_words=["payroll"])) == 1


def test_moment_must_overlap_sixty_percent_of_its_own_length():
    # 150-550 s: 52% in the first chapter and 48% in the second, so only the pair holds it
    cuts = run([moment(0, 150, 550, raw=8.0)])
    assert [(c.cleaned_start_s, c.cleaned_end_s) for c in cuts] == [(0.0, 720.0)]
    # 100-500 s: 65% in the first chapter, so that chapter alone is enough
    cuts = run([moment(0, 100, 500, raw=8.0)])
    assert [(c.cleaned_start_s, c.cleaned_end_s) for c in cuts] == [(0.0, 360.0)]


def test_fallback_without_chapters_uses_single_moments_that_fit():
    mo = [moment(0, 100, 300, raw=9.0), moment(1, 1000, 1500, raw=8.0), moment(2, 2000, 3600, raw=9.5)]
    cuts = run(mo, chapters=[])
    assert [c.best.id for c in cuts] == [1]
    assert cuts[0].chapter_titles == []


def test_off_means_no_cuts():
    assert run([moment(0, 400, 1000)], config=cfg(topic_cuts=False)) == []


def test_two_cuts_never_overlap():
    mo = [moment(0, 400, 1000, raw=9.0), moment(1, 600, 1300, raw=8.5), moment(2, 2400, 3000, raw=8.0)]
    cuts = run(mo, config=cfg(topic_cuts_per_meeting=2))
    assert len(cuts) == 2
    a, b = sorted(cuts, key=lambda c: c.cleaned_start_s)
    assert a.cleaned_end_s <= b.cleaned_start_s
    assert [c.index for c in cuts] == [0, 1]


def test_fields_come_from_the_best_moment():
    c = run([moment(0, 400, 1000, screen_hook="Big claim")])[0]
    assert c.title == "Topic 0" and c.hook == "Big claim"
    assert c.src_start_frame == round(c.cleaned_start_s * FPS)
    assert c.src_end_frame == round(c.cleaned_end_s * FPS)
    assert 400 <= c.thumb_src_s <= 1000
    assert run([moment(0, 400, 1000)])[0].hook == "Hook 0"  # no screen_hook: the plain hook


# ------------------------------------------------------------- cold open


def segs():
    return [{"start": float(i * 4), "end": float(i * 4 + 3.5)} for i in range(400)]


def test_cold_open_is_inside_the_cut_and_15_to_40_seconds():
    mo = moment(0, 400, 1000, peak_index=120)
    c = run([mo], segments=segs())[0]
    a, b = c.open_span
    assert 15 <= b - a <= 40
    assert c.src_start_frame / FPS <= a < b <= c.src_end_frame / FPS
    assert mo.start <= a and b <= mo.end
    assert a in {s["start"] for s in segs()} and b in {s["end"] for s in segs()}


def test_cold_open_uses_short_start():
    mo = moment(0, 400, 1000, peak_index=120, short_start=404.0)
    a, b = run([mo], segments=segs())[0].open_span
    assert a == 404.0 and 15 <= b - a <= 40


def test_cold_open_never_replays_the_first_seconds_of_the_cut():
    # the best moment starts and peaks before the cut: clamping to the cut's start would make the open
    # the same 30 s the body begins with
    mo = moment(0, 100, 900, peak_index=30)  # the peak segment is at 120 s, before the cut at 400 s
    a, b = topics.cold_open(mo, 400.0, 700.0, segs())
    assert a >= 400.0 + topics.OPEN_MAX_S and b <= 700.0 and 15 <= b - a <= 40
    assert topics.cold_open(mo, 400.0, 400.0 + topics.OPEN_MAX_S + 10, segs()) is None  # nothing left after 40 s
    # a short_start before the cut is handled the same way
    early = moment(0, 100, 900, peak_index=30, short_start=380.0)
    assert topics.cold_open(early, 400.0, 700.0, segs())[0] >= 400.0 + topics.OPEN_MAX_S
    # a peak well inside the cut still opens on the peak
    inside = moment(0, 100, 900, peak_index=120, short_start=520.0)
    assert topics.cold_open(inside, 400.0, 700.0, segs())[0] == 520.0


def test_cold_open_absent_when_off_or_moment_is_short():
    assert run([moment(0, 400, 1000)], config=cfg(topic_cold_open=False))[0].open_span is None
    assert run([moment(0, 400, 410)])[0].open_span is None


# ------------------------------------------------------------ slice_edl


def edl(ranges, d=12, total=15000):
    return EditDecisionList(
        ranges=[EDLRange(start=a / FPS, end=b / FPS, start_frame=a, end_frame=b) for a, b in ranges],
        source_duration=total / FPS, fps="25", fade_frames=d, total_frames=total,
        silence_cuts=[(5.0, 5.5), (100.0, 100.4), (300.0, 300.4)],
    )


def frames(e):
    return [(r.start_frame, r.end_frame) for r in e.ranges]


def test_slice_is_frame_exact_and_keeps_the_grid():
    s = slice_edl(edl([(0, 1000), (1100, 3000), (3200, 6000)]), 500, 3600)
    assert frames(s) == [(500, 1000), (1100, 3000), (3200, 3600)]
    assert (s.fps, s.fade_frames, s.total_frames) == ("25", 12, 15000)
    assert [r.start for r in s.ranges] == [a / FPS for a, _ in frames(s)]
    validate_edl(s)


def test_slice_keeps_only_silence_cuts_inside():
    s = slice_edl(edl([(0, 1000), (1100, 6000)]), 2000, 4000)
    assert s.silence_cuts == [(100.0, 100.4)]


def test_slice_drops_tiny_edge_fragments():
    e = edl([(0, 1000), (1100, 3000), (3200, 6000)])
    assert frames(slice_edl(e, 990, 3210)) == [(1100, 3000)]  # 10 frames at each end


def test_slice_restores_a_tiny_fragment_of_a_short_range_only():
    e = edl([(0, 1000), (1100, 1140), (1250, 6000)])  # the middle range is 1.6 s
    s = slice_edl(e, 1130, 3000)
    assert frames(s)[0] == (1100, 1140)
    validate_edl(s)
    long = edl([(0, 1000), (1100, 3000), (3200, 6000)])
    assert frames(slice_edl(long, 2990, 5000)) == [(3200, 5000)]  # the range is too long to restore


def test_slice_custom_minimum_and_errors():
    e = edl([(0, 1000), (1100, 3000)], d=0)
    assert frames(slice_edl(e, 990, 2000, min_piece_frames=5)) == [(990, 1000), (1100, 2000)]
    with pytest.raises(ValueError):
        slice_edl(edl([(0, 1000), (1100, 3000)]), 3010, 3100)
    with pytest.raises(ValueError):
        slice_edl(EditDecisionList(ranges=[EDLRange(start=0, end=5)], source_duration=5), 0, 10)
