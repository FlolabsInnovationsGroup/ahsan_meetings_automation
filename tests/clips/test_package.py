from clips.config import clips_settings
from clips.models import ClipKind, Copy, Moment, Platform, Source, SourceVideo
from clips.package import FAN_LINE, TITLE_MAX, check_copy, write_copy

CREDIT = "Clip from Some Creator (https://youtube.com/@some)"
SRC = Source(id="s", name="Some Creator", platform=Platform.YOUTUBE)
VID = SourceVideo(id="youtube:x", source_id="s", platform=Platform.YOUTUBE, native_id="x", title="Vice City tour")
MOM = Moment(start_s=1, end_s=30, summary="Lucia drives a Cheetah along the beach", hook="Beach drive")


class Fake:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def json(self, system, user, *, images=None, max_tokens=2000):
        self.calls += 1
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def good(title="Lucia takes the Cheetah down the beach in GTA 6"):
    return {"title": title, "description_first_line": "A drive along Vice City beach.", "tags": ["vice city", "GTA 6"]}


def run(llm, kind=ClipKind.SHORT, credit=CREDIT):
    return write_copy(MOM, SRC, VID, kind=kind, credit=credit, llm=llm)


def test_good_answer_used_and_built():
    c = run(Fake(good()))
    assert c.title.startswith("Lucia takes")
    lines = c.description.split("\n")
    assert lines[0] == "A drive along Vice City beach." and lines[1] == ""
    assert CREDIT in c.description and FAN_LINE in c.description
    assert "#shorts" not in c.hashtags and len(c.hashtags) <= 3
    assert c.category_id == "20" and c.language == "en"
    assert check_copy(c, kind=ClipKind.SHORT, credit=CREDIT) == []


def test_long_has_no_shorts_tag_and_no_credit_line_when_empty():
    c = run(Fake(good()), kind=ClipKind.LONG, credit="")
    assert "#shorts" not in c.description
    assert "\n\n\n" not in c.description


def test_bad_then_good_is_repaired():
    f = Fake(good("X" * 90), good())
    c = run(f)
    assert f.calls == 2 and c.title.startswith("Lucia")


def test_two_bad_answers_fall_back():
    f = Fake(good("WOW LOOK AT THIS"), good("Official Rockstar clip"))
    c = run(f)
    assert f.calls == 2
    assert c.title == "GTA 6: Beach drive"
    assert check_copy(c, kind=ClipKind.SHORT, credit=CREDIT) == []


def test_model_error_and_no_model_fall_back():
    assert run(Fake(RuntimeError("down"))).title == "GTA 6: Beach drive"
    assert run(None).title == "GTA 6: Beach drive"


def test_non_dict_answer():
    assert run(Fake(["nope"], ["nope"])).title == "GTA 6: Beach drive"


def test_fallback_uncaps_and_strips_official():
    m = Moment(start_s=0, end_s=5, hook="OFFICIAL CRAZY CHASE", summary="A chase")
    c = write_copy(m, SRC, VID, kind=ClipKind.SHORT, credit="", llm=None)
    assert c.title == "GTA 6: Crazy chase"


def test_fallback_long_title_fits():
    m = Moment(start_s=0, end_s=5, summary="word " * 60)
    c = write_copy(m, SRC, VID, kind=ClipKind.LONG, credit="")
    assert len(c.title) <= TITLE_MAX[ClipKind.LONG]


def test_hashtags_capped_at_three(monkeypatch):
    monkeypatch.setenv("CLIPS_DEFAULT_HASHTAGS", "#a,#b,#c,#d,#e,#f,#g")
    clips_settings.cache_clear()
    c = run(None)
    assert len(c.hashtags) == 3 and "#shorts" not in c.hashtags
    assert check_copy(c, kind=ClipKind.SHORT, credit=CREDIT) == []


def base(**kw):
    d = {
        "title": "Nice drive in GTA 6",
        "description": f"Hi\n\n{CREDIT}\n\n{FAN_LINE}\n\n#gta6",
        "tags": ["a"],
        "hashtags": ["#gta6"],
    }
    d.update(kw)
    return Copy(**d)


def probs(copy, kind=ClipKind.SHORT):
    return " ".join(check_copy(copy, kind=kind, credit=CREDIT))


def test_checks():
    assert check_copy(base(), kind=ClipKind.SHORT, credit=CREDIT) == []
    assert "empty" in probs(base(title=" "))
    assert "limit" in probs(base(title="a" * 61))
    assert "limit" in probs(base(title="a" * 71), ClipKind.LONG)
    assert check_copy(base(title="a" * 70), kind=ClipKind.LONG, credit=CREDIT) == []
    assert "capitals" in probs(base(title="GTA SIX DRIVE"))
    assert check_copy(base(title="GTA 6"), kind=ClipKind.SHORT, credit=CREDIT) == []
    assert "credit" in probs(base(description="Hi"))
    assert check_copy(base(description="Hi"), kind=ClipKind.SHORT, credit="") == []
    assert "hashtags" in probs(base(hashtags=["#a", "#b", "#c", "#d"]))
    assert "hashtags" in probs(base(description=base().description + " #a #b #c #d"))
    assert "tags" in probs(base(tags=["x" * 30] * 20))
    assert "description" in probs(base(description=base().description + "z" * 4600))
    assert "official" in probs(base(title="Official GTA 6 clip"))
    assert "< or >" in probs(base(title="a <b>"))
    # a credit that names an official source is allowed
    c = base(description=f"Hi\n\nCredit: Rockstar Official\n\n{FAN_LINE}")
    assert check_copy(c, kind=ClipKind.SHORT, credit="Credit: Rockstar Official") == []


def _yt(url):
    return SourceVideo(id="youtube:x", source_id="s", platform=Platform.YOUTUBE, native_id="x", url=url)


def test_timed_credit_adds_the_second_to_the_link():
    from clips.package import timed_credit

    credit = "Clip from Some Creator - https://www.youtube.com/watch?v=x"
    assert timed_credit(credit, _yt("https://www.youtube.com/watch?v=x"), 83.9) == credit + "&t=83"
    assert timed_credit("Clip from S - https://youtu.be/x", _yt("https://youtu.be/x"), 90) == "Clip from S - https://youtu.be/x?t=90"


def test_timed_credit_leaves_other_cases_alone():
    from clips.package import timed_credit

    url = "https://www.youtube.com/watch?v=x"
    credit = f"Clip from S - {url}"
    assert timed_credit(credit, _yt(url), 0) == credit
    assert timed_credit("", _yt(url), 50) == ""
    assert timed_credit("Clip from S", _yt(url), 50) == "Clip from S"
    twitch = SourceVideo(id="t:1", source_id="s", platform=Platform.TWITCH, native_id="1", url=url)
    assert timed_credit(credit, twitch, 50) == credit


def test_description_keeps_the_credit_line_with_the_time():
    from clips.package import timed_credit

    url = "https://www.youtube.com/watch?v=x"
    credit = timed_credit(f"Clip from Some Creator - {url}", _yt(url), 3725)
    copy = run(None, credit=credit)
    assert f"{url}&t=3725" in copy.description
    assert check_copy(copy, kind=ClipKind.SHORT, credit=credit) == []
