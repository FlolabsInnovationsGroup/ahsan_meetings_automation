"""Topic-first titles and descriptions (publish/describe.py)."""

import json
import re

import pytest

from core.config import get_settings
from core.models import JobRecord, JobStatus
from publish import describe
from publish import settings as pub_settings

CHAPTERS = [(0, "Highlights"), (120, "Session store on Redis"), (300, "Camera calibration"),
            (500, "Open questions"), (700, "Next steps")]


def _job(title="Multi-modal perception team meeting", *, rows=None, highlights=None, chapters=True,
         job_id="d-1") -> JobRecord:
    job_dir = get_settings().jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"job_id": job_id, "shorts": rows or [], "highlights": highlights or []}
    (job_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if chapters:
        (job_dir / "chapters.json").write_text(json.dumps(
            {"entries": [{"t": t, "title": name} for t, name in CHAPTERS]}), encoding="utf-8")
    return JobRecord(job_id=job_id, status=JobStatus.DONE, title=title, meeting_date="2026-11-04")


def _row(n=1, title="Moving the session store to Redis", hook="Why the old store kept timing out.", **extra):
    return {"file": f"shorts/short_{n:02d}.mp4", "source_start": 10.0, "source_end": 50.0,
            "title": title, "hook": hook, **extra}


@pytest.fixture(autouse=True)
def _no_posted(monkeypatch):
    monkeypatch.setattr(describe, "_posted_titles", lambda: set())


# ----------------------------------------------------------------- series


@pytest.mark.parametrize("name, series", [
    ("Multi-modal perception team meeting", "Multi-modal perception"),
    ("IoT Team Meeting (2026-11-04)", "IoT"),
    ("Weekly Sync", ""),
    ("", ""),
    ("FloBrain Backend Standup - Oct 5, 2026", "FloBrain Backend"),
    ("All Tech Team Meeting | August 2, 2026", "All Tech"),
])
def test_series_of(name, series):
    assert describe.series_of(name) == series


def test_series_label():
    assert describe.series_label(_job("IoT Team Meeting")) == "FloLabs IoT"
    assert describe.series_label(_job("Weekly Sync")) == "FloLabs"
    assert describe.series_label(_job("FloLabs team meeting")) == "FloLabs"


# ------------------------------------------------------------------ title


def test_topic_first_title_from_the_first_short():
    job = _job(rows=[_row(1), _row(2, title="Second")])
    assert describe.full_title(job) == "Moving the session store to Redis | FloLabs Multi-modal perception"


def test_title_uses_the_reel_when_there_are_no_shorts():
    job = _job(highlights=[{"final_at_s": 1, "source_start": 1, "source_end": 9, "title": "Calibrating cameras"}])
    assert describe.full_title(job) == "Calibrating cameras | FloLabs Multi-modal perception"


def test_ungrounded_title_is_rejected_when_the_clip_text_is_there():
    rows = [_row(1, text="we talked about lunch plans and the weather today"),
            _row(2, title="Camera calibration drift", hook="", text="the camera calibration drift got worse")]
    assert describe.full_title(_job(rows=rows)).startswith("Camera calibration drift | ")


def test_grounded_title_is_kept_and_no_text_is_trusted():
    rows = [_row(1, text="the session store moved to redis last week")]
    assert describe.full_title(_job(rows=rows)).startswith("Moving the session store to Redis | ")


def test_with_no_usable_moment_the_title_is_todays():
    rows = [_row(1, text="nothing related whatsoever here")]
    assert describe.full_title(_job(rows=rows)) == "Multi-modal perception team meeting — 2026-11-04"
    assert describe.full_title(_job(rows=[])) == "Multi-modal perception team meeting — 2026-11-04"


def test_a_repeated_title_gets_the_date(monkeypatch):
    first = "Moving the session store to Redis | FloLabs Multi-modal perception"
    monkeypatch.setattr(describe, "_posted_titles", lambda: {first.lower()})
    title = describe.full_title(_job(rows=[_row(1)]))
    assert title == "Moving the session store to Redis (2026-11-04) | FloLabs Multi-modal perception"


def test_title_is_at_most_100_characters_and_keeps_the_label():
    job = _job(rows=[_row(1, title="A very long moment title " * 8)])
    title = describe.full_title(job)
    assert len(title) <= 100
    assert title.endswith(" | FloLabs Multi-modal perception")
    assert "<" not in title and ">" not in title


def test_meeting_date_style_keeps_todays_title():
    pub_settings.save({"title_style": "meeting_date"})
    assert describe.full_title(_job(rows=[_row(1)])) == "Multi-modal perception team meeting — 2026-11-04"


# ------------------------------------------------------------ description


def test_description_order_and_no_timestamp_above_the_chapters():
    pub_settings.save({"links_text": "Website: https://example.com"})
    text = describe.full_description(_job(rows=[_row(1)]))
    blocks = text.split("\n\n")
    assert blocks[0].splitlines() == [
        "Why the old store kept timing out.",
        "Also in this meeting: Session store on Redis, Camera calibration, Open questions",
        "Multi-modal perception team meeting, 4 November 2026. A real R&D team meeting from FloLabs Innovations Group.",
    ]
    assert blocks[1].splitlines()[0].startswith("00:00") or blocks[1].splitlines()[0].startswith("0:00")
    assert blocks[2] == "Website: https://example.com"
    assert blocks[3] == "#FloLabs #MultiModalPerception"
    assert not re.search(r"^\d{1,2}:\d{2}", blocks[0], re.MULTILINE)


def test_description_without_a_series_or_moment():
    text = describe.full_description(_job("Weekly Sync"))
    assert text.startswith("Also in this meeting:")
    assert "A real R&D team meeting from FloLabs Innovations Group." in text
    assert "team meeting," not in text
    assert text.endswith("#FloLabs")


def test_hashtags_can_be_turned_off():
    pub_settings.save({"hashtags": False})
    assert "#" not in describe.full_description(_job(rows=[_row(1)]))
    assert "#" not in describe.short_description(describe.load_shorts(_job(rows=[_row(1)]))[0], _job(rows=[_row(1)]))


def test_meeting_date_style_description_is_the_chapters_plus_links_and_tags():
    pub_settings.save({"title_style": "meeting_date", "links_text": "More: https://example.com"})
    text = describe.full_description(_job())
    chapters = "\n".join(f"{describe.fmt_clock(t)} {name}" for t, name in CHAPTERS)
    assert text == f"{chapters}\n\nMore: https://example.com\n\n#FloLabs #MultiModalPerception"


def test_long_links_still_leave_room_for_the_shorts_list(monkeypatch):
    pub_settings.save({"links_text": "\n".join(f"https://example.com/{i}/" + "é" * 20 for i in range(20))})
    monkeypatch.setattr(describe, "SHORTS_ROOM", 3500)  # squeeze: only part of the links fit
    text = describe.full_description(_job(rows=[_row(1)]))
    assert len(text.encode("utf-8")) <= 1500
    assert "https://example.com/0/" in text and "https://example.com/19/" not in text
    monkeypatch.undo()
    pub_settings.save({"links_text": "\n".join(f"https://example.com/{i}/" + "é" * 20 for i in range(20))})
    text = describe.full_description(_job(rows=[_row(1)]))
    shorts = [(i, "A reasonably long short title about " + "x" * 40, "https://youtu.be/abcdefghijk") for i in range(4)]
    combined, kept = describe.with_shorts_block(text, shorts)
    assert describe.description_problem(combined) is None
    assert kept == [0, 1, 2, 3]
    assert text.endswith("#FloLabs #MultiModalPerception")


def test_at_most_three_hashtags_never_shorts():
    job = _job(rows=[_row(1, title="ESP32 firmware update")])
    info = describe.load_shorts(job)[0]
    text = describe.short_description(info, job)
    tags = text.splitlines()[-1].split()
    assert tags == ["#FloLabs", "#MultiModalPerception", "#ESP32"]
    assert "#shorts" not in text.lower()


def test_short_description_without_a_proper_noun_has_two_hashtags():
    job = _job(rows=[_row(1, title="why the store was slow")])
    text = describe.short_description(describe.load_shorts(job)[0], job)
    assert text.splitlines()[-1] == "#FloLabs #MultiModalPerception"
    assert text.startswith("Why the old store kept timing out.\n\nFrom the full meeting:")


def test_no_time_anywhere_above_the_chapters_and_no_angle_brackets_from_a_meeting_name():
    row = _row(1, hook="We cut boot time from 1:20 to 0:40.")
    job = _job("Infra<->Cloud <b>sync</b>", rows=[row])
    text = describe.full_description(job)
    assert not re.search(r"\d{1,2}:\d{2}", text.split("\n\n")[0])
    assert "<" not in describe.full_title(job) and ">" not in describe.full_title(job)
    assert "<" not in text and ">" not in text
    assert describe.title_problem(describe.full_title(job)) is None
    assert describe.description_problem(text) is None


def test_a_number_the_clip_never_said_rejects_the_title():
    assert not describe._grounded("Latency drops to 800 milliseconds", "the latency drops a lot after the change")


def test_a_number_the_clip_said_passes_digits_or_words():
    assert describe._grounded("Latency drops to 800 milliseconds", "latency drops to 800 milliseconds now")
    assert describe._grounded("Three cameras drift", "the three cameras drift apart")
    assert describe._grounded("Cameras drift after 3 hours", "the cameras drift after three hours")
    assert describe._grounded("ESP32 camera drift", "the camera drift is bad")  # ESP32 is a name, not a number


def test_ordinals_are_not_numbers_the_clip_has_to_say():
    assert describe._grounded("Our 1st prototype works", "our first prototype works now")
    assert describe._grounded("The 2nd and 4th prototype works", "the second and fourth prototype works")
    assert not describe._grounded("Our 12 prototype works", "our first prototype works now")


def test_tens_and_hundred_count_as_spoken_numbers():
    assert describe._grounded("Why 30 FPS matters", "thirty frames per second matters here")
    assert describe._grounded("Reaching 100% accuracy", "a hundred percent accuracy matters")
    assert describe._grounded("Why 90 FPS matters", "ninety frames per second matters")
    assert not describe._grounded("Why 30 FPS matters", "forty frames per second matters")


def test_half_of_the_main_words_is_enough_and_one_in_four_is_not():
    text = "the camera calibration drifted again"
    assert describe._grounded("Camera calibration tooling pipeline", text)  # 2 of 4
    assert not describe._grounded("Camera tooling pipeline automation", text)  # 1 of 4


# ------------------------------------------------ packaging from the team registry


def _team(name, **fields):
    from publish import series

    series.set_row(name, **fields)


def test_a_label_override_changes_the_title_and_the_description_line():
    _team("multi modal perception", label="Perception Lab")
    job = _job(rows=[_row(1)])
    assert describe.series_label(job) == "FloLabs Perception Lab"
    assert describe.full_title(job).endswith("| FloLabs Perception Lab")
    assert "Perception Lab team meeting, 4 November 2026." in describe.full_description(job)


def test_a_label_with_a_flolabs_prefix_is_not_doubled():
    _team("multi modal perception", label="FloLabs Perception Lab")
    assert describe.series_label(_job()) == "FloLabs Perception Lab"


def test_registry_hashtags_are_used_capped_and_valid():
    _team("multi modal perception", hashtags=["#Percept", "#Vision"])
    job = _job(rows=[_row(1, title="Moving the session store to Redis ESP32")])
    tags = describe._hashtags(job, "#ESP32").split()
    assert tags == ["#FloLabs", "#Percept", "#Vision"]
    assert all(re.fullmatch(r"#[A-Za-z0-9]+", t) for t in tags)
    assert "#shorts" not in describe._hashtags(job, "#shorts").lower()


def test_without_a_registry_tag_the_camel_case_tag_stays():
    assert describe._series_tag(_job()) == "#MultiModalPerception"


def test_the_old_label_still_counts_as_a_duplicate_title():
    job = _job(rows=[_row(1)])
    old = "Moving the session store to Redis | FloLabs Multi-modal perception"
    _team("multi modal perception", label="Perception Lab")
    variants = describe.title_variants(job)
    assert old.lower() in variants
    assert "moving the session store to redis | flolabs perception lab" in variants


def test_short_info_style_defaults_to_classic_and_reads_new():
    job = _job(rows=[_row(1), _row(2, style="new", layout="speaker", screen_hook_shown=True)])
    first, second = describe.load_shorts(job)
    assert (first.style, first.layout, first.screen_hook_shown) == ("classic", "", False)
    assert (second.style, second.layout, second.screen_hook_shown) == ("new", "speaker", True)


def test_best_moment_range_is_the_moment_behind_the_title():
    rows = [_row(1, text="we talked about lunch plans and the weather today"),
            dict(_row(2, title="Camera calibration drift", hook="", text="the camera calibration drift got worse"),
                 source_start=70.0, source_end=95.0)]
    job = _job(rows=rows)
    assert describe.full_title(job).startswith("Camera calibration drift | ")
    assert describe.best_moment_range(job) == (70.0, 95.0)


def test_best_moment_range_falls_back_to_the_reel_and_none():
    job = _job(highlights=[{"final_at_s": 1, "source_start": 5, "source_end": 19, "title": "Calibrating cameras"}])
    assert describe.best_moment_range(job) == (5.0, 19.0)
    assert describe.best_moment_range(_job(job_id="d-2")) is None


def test_a_never_post_word_in_a_registry_label_is_still_caught():
    from autopilot import never_post

    never_post.set_entries(["projectx"])
    _team("multi modal perception", label="Projectx Lab")
    job = _job(rows=[_row(1)])
    assert never_post.match(describe.full_title(job)) is not None
    assert never_post.match(describe.series_label(job)) is not None
    assert never_post.match(describe.full_description(job)) is not None
