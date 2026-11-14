"""New-style Shorts subtitle document (hook card, safe-band captions, title, end line). Synthetic text only."""

import re
from itertools import pairwise
from types import SimpleNamespace

from slice.captions import (
    Cue,
    ass_document,
    ass_document_new,
    cap_two_lines,
    choose_hook,
    wrap_two_lines,
)

FONT = "DejaVu Sans"


def look(**kw):
    base = {"hook_seconds": 2.5, "top_text": "title", "end_line": "Full meeting", "caption_px": 80}
    base.update(kw)
    return SimpleNamespace(**base)


def _t(stamp: str) -> float:
    h, m, rest = stamp.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def events(doc: str) -> list[dict]:
    out = []
    for line in doc.splitlines():
        if line.startswith("Dialogue:"):
            f = line[len("Dialogue: "):].split(",", 9)
            out.append({"layer": f[0], "start": _t(f[1]), "end": _t(f[2]), "style": f[3], "text": f[9]})
    return out


def styles(doc: str) -> dict[str, list[str]]:
    return {line[7:].split(",")[0]: line[7:].split(",") for line in doc.splitlines() if line.startswith("Style:")}


def build(**kw):
    args = {"cues": [Cue(0, 2, "a short caption")], "hook": "Why this matters", "title": "Quarterly planning",
            "end_line": "Full meeting", "duration_s": 30.0, "look": look(), "font_family": FONT,
            "width": 1080, "height": 1920}
    args.update(kw)
    return ass_document_new(args.pop("cues"), **args)


def of(doc, style):
    return [e for e in events(doc) if e["style"] == style]


def test_timeline_hook_title_end_line():
    doc = build()
    hooks = of(doc, "Hook")
    assert (hooks[0]["start"], hooks[0]["end"]) == (0, 2.5)
    assert (hooks[1]["start"], hooks[1]["end"]) == (27.5, 30.0)
    title = of(doc, "Title")[0]
    assert (title["start"], title["end"]) == (2.5, 27.5)


def test_title_runs_to_the_end_without_an_end_line():
    doc = build(end_line="")
    assert of(doc, "Title")[0]["end"] == 30.0
    assert len(of(doc, "Hook")) == 1


def test_no_title_when_top_text_none_or_title_empty():
    assert not of(build(look=look(top_text="none")), "Title")
    assert not of(build(title=""), "Title")


def test_no_hook_when_zero_seconds_or_empty_hook():
    doc = build(look=look(hook_seconds=0))
    assert [e["end"] for e in of(doc, "Hook")] == [30.0]  # only the end line
    assert of(doc, "Title")[0]["start"] == 0
    doc = build(hook="", end_line="")
    assert not of(doc, "Hook")
    assert of(doc, "Title")[0]["start"] == 0


def test_hook_fades_out():
    assert of(build(), "Hook")[0]["text"].startswith("{\\fad(0,250)}")


def test_styles_at_1080x1920():
    st = styles(build())
    cap, hook, title = st["Caption"], st["Hook"], st["Title"]
    assert cap[2] == "80" and cap[7] == "-1" and cap[15:20] == ["1", "7", "3", "2", "60"]
    assert int(cap[21]) == 1920 - 1330
    assert hook[2] == "86" and hook[3] == "&H00101010" and hook[5] == hook[6] == "&H0000D7FF"
    assert hook[15] == "3" and hook[18] == "8" and hook[21] == "250"
    assert title[2] == "46" and title[6] == "&H66000000" and title[15] == "3" and title[21] == "250"


def test_safe_band_at_1080x1920():
    st = styles(build())
    assert int(st["Hook"][21]) - int(st["Hook"][16]) >= 192
    assert int(st["Title"][21]) - int(st["Title"][16]) >= 192
    assert 1920 - int(st["Caption"][21]) == 1330  # the caption's bottom edge


def test_values_scale_at_720x1280():
    doc = build(width=720, height=1280)
    st = styles(doc)
    assert "PlayResX: 720" in doc and "PlayResY: 1280" in doc
    assert st["Caption"][2] == "53" and st["Caption"][19] == "40"
    assert int(st["Caption"][21]) == round(590 * 1280 / 1920)
    assert st["Hook"][2] == "57" and st["Hook"][21] == "167"
    assert st["Title"][2] == "31"


def test_no_row_over_two_after_cap_two_lines():
    cues = [Cue(0, 3, "one two three four five six seven eight nine ten eleven twelve thirteen fourteen"),
            Cue(3, 4, "tiny")]
    out = cap_two_lines(cues, width=1080, size_px=80, margin_px=60, char_em=0.6)
    assert all(c.text.count("\\N") <= 1 for c in out)
    per_line = int((1080 - 120) / (0.6 * 80))
    assert all(len(row) <= per_line for c in out for row in c.text.split("\\N"))


def test_long_cue_is_split_into_contiguous_cues():
    text = " ".join(["word"] * 30)
    out = cap_two_lines([Cue(1.0, 7.0, text)], width=1080, size_px=80, margin_px=60, char_em=0.6)
    assert len(out) >= 2
    assert out[0].start == 1.0 and out[-1].end == 7.0
    for a, b in pairwise(out):
        assert a.end == b.start
    assert all(c.end > c.start for c in out)


def test_wrap_two_lines():
    assert wrap_two_lines("short", 20) == "short"
    assert wrap_two_lines("aaaa bbbb cccc dddd", 10) == "aaaa bbbb\\Ncccc dddd"
    assert wrap_two_lines("aaaa bbbb cccc dddd eeee ffff", 10) is None
    assert wrap_two_lines("x" * 30, 10) is None


def test_choose_hook():
    assert choose_hook("Big claim", "A title") == "Big claim"
    assert choose_hook("", "A short title") == "A short title"
    assert choose_hook(None, "A short title") == "A short title"
    assert choose_hook("", "t" * 42) == "t" * 42
    assert choose_hook("", "t" * 43) == ""
    assert choose_hook("", "") == ""


def test_untrusted_text_is_escaped():
    doc = build(hook="Boom {\\an5\\fs200} now", end_line="Go {\\p1}m 0 0{\\p0}", title="T {x}")
    for e in events(doc):
        if e["style"] in ("Hook", "Title"):
            body = re.sub(r"^\{\\fad\(0,250\)\}", "", e["text"])
            body = re.sub(r"^\{\\fs\d+\}", "", body)
            assert "{" not in body and "}" not in body
            assert body.replace("\\N", "").count("\\") == 0


def test_caption_text_is_escaped_and_row_break_kept():
    doc = build(cues=[Cue(0, 2, "alpha {bad} beta gamma delta epsilon zeta eta theta iota kappa")])
    cap = of(doc, "Caption")
    assert all("{" not in e["text"] for e in cap)
    assert any("\\N" in e["text"] for e in cap)


def test_long_hook_fits_two_rows():
    doc = build(hook="x" + " abcdefgh" * 4)
    assert of(doc, "Hook")[0]["text"].count("\\N") <= 1


def test_all_caps_hook_leaves_room_for_wide_glyphs():
    # capitals are wider than average, so an all-caps row holds fewer characters than the plain estimate
    assert wrap_two_lines("WHY WE MOVED THE WORKSHOP DOWN TO MARCH NOW", 32) is not None
    assert wrap_two_lines("WHY WE MOVED THE WORKSHOP DOWN TO MARCH NOW", 28) is None
    doc = build(hook="WHY WE MOVED THE WORKSHOP DOWN TO MARCH NOW")
    assert of(doc, "Hook")[0]["text"].count("\\N") <= 1


def test_classic_document_is_unchanged():
    doc = ass_document([Cue(0, 1.5, "Hello {x} there"), Cue(1.5, 3, "second cue")], title="My title",
                       duration_s=10, font_family="DejaVu Sans", width=1080, height=1920)
    assert doc == (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        "Style: Caption,DejaVu Sans,76,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,0,2,"
        "90,90,430,1\n"
        "Style: Title,DejaVu Sans,62,&H0000E5FF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,4,0,8,"
        "90,90,300,1\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.00,0:00:10.00,Title,,0,0,0,,My title\n"
        "Dialogue: 0,0:00:00.00,0:00:01.50,Caption,,0,0,0,,Hello (x) there\n"
        "Dialogue: 0,0:00:01.50,0:00:03.00,Caption,,0,0,0,,second cue\n"
    )
