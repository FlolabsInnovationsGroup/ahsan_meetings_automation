from clips import captions as cap
from clips.models import Caption, Layout


def _write(tmp_path, **kw):
    args = {"hook": "Wild chase", "credit": "Gameplay: Someone", "captions": [], "start_s": 10.0,
            "end_s": 30.0, "layout": Layout.SQUARE}
    args.update(kw)
    return cap.write_ass(tmp_path / "x.ass", **args).read_text(encoding="utf-8")


def _style(text, name):
    return next(line for line in text.splitlines() if line.startswith(f"Style: {name}"))


def test_ass_time():
    assert cap.ass_time(0) == "0:00:00.00"
    assert cap.ass_time(61.5) == "0:01:01.50"
    assert cap.ass_time(-3) == "0:00:00.00"


def test_escape_removes_special_characters():
    out = cap.ass_escape("a {\\b1}b\nc\\N")
    assert "{" not in out and "}" not in out and "\\" not in out and "\n" not in out


def test_hook_wraps_to_two_lines_and_truncates():
    out = cap.wrap_hook("this is a very long hook that keeps going and going and going on")
    assert out.count("\\N") <= 1
    assert out.endswith("…")


def test_file_has_three_styles_and_whole_clip_hook(tmp_path):
    text = _write(tmp_path)
    assert "PlayResX: 1080" in text and "PlayResY: 1920" in text
    for name in ("Hook", "Credit", "Caption"):
        assert f"Style: {name}," in text
    assert "Dialogue: 1,0:00:00.00,0:00:20.00,Hook,,0,0,0,,Wild chase" in text
    assert "Gameplay: Someone" in text


def test_hook_stays_below_safe_top(tmp_path):
    assert int(_style(_write(tmp_path), "Hook").split(",")[-2]) >= 288


def test_caption_bottom_is_above_the_player_labels(tmp_path):
    assert 1920 - int(_style(_write(tmp_path), "Caption").split(",")[-2]) <= 1248


def test_credit_bottom_is_above_the_player_labels(tmp_path):
    assert 1920 - int(_style(_write(tmp_path), "Credit").split(",")[-2]) <= 1248


def test_stack_puts_captions_low_in_the_gameplay_panel(tmp_path):
    text = _write(tmp_path, layout=Layout.FACECAM_STACK)
    assert 1920 - int(_style(text, "Caption").split(",")[-2]) == 1450


def test_stack_captions_are_clear_of_the_hook_and_credit_stays(tmp_path):
    text = _write(tmp_path, layout=Layout.FACECAM_STACK)
    hook_top = int(_style(text, "Hook").split(",")[-2])
    caption_bottom = 1920 - int(_style(text, "Caption").split(",")[-2])
    credit_bottom = 1920 - int(_style(text, "Credit").split(",")[-2])
    assert hook_top == 660
    # two caption lines at 66 px stay well below the two hook lines (64 px each) that start at the hook margin
    assert caption_bottom - 2 * 80 > hook_top + 2 * 80
    assert credit_bottom == 1240


def test_captions_are_short_groups_relative_to_clip_start(tmp_path):
    caps = [Caption(start_s=10.0, end_s=14.0, text="you will not believe what just happened here")]
    text = _write(tmp_path, captions=caps)
    lines = [ln for ln in text.splitlines() if ",Caption,,0,0,0,," in ln]
    assert len(lines) >= 3
    assert lines[0].startswith("Dialogue: 0,0:00:00.00")
    assert all(len(ln.rsplit(",,", 1)[1].split()) <= cap.CAPTION_MAX_WORDS for ln in lines)


def test_captions_outside_clip_are_dropped(tmp_path):
    caps = [Caption(start_s=0, end_s=5, text="before"), Caption(start_s=40, end_s=45, text="after")]
    assert ",Caption,,0,0,0,," not in _write(tmp_path, captions=caps)


def test_caption_text_is_escaped(tmp_path):
    caps = [Caption(start_s=10, end_s=12, text="{\\an9}hi")]
    line = next(ln for ln in _write(tmp_path, captions=caps).splitlines() if ",Caption,,0,0,0,," in ln)
    assert "{" not in line.rsplit(",,", 1)[1]


def test_no_hook_or_credit_means_no_such_lines(tmp_path):
    text = _write(tmp_path, hook="", credit="")
    assert ",Hook,,0" not in text and ",Credit,,0" not in text


def test_landscape_credit_only(tmp_path):
    text = _write(tmp_path, hook="", out_w=1920, out_h=1080)
    assert "PlayResX: 1920" in text and "Gameplay: Someone" in text
