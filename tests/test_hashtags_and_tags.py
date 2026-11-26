"""Many hashtags per video and the hidden search tags (publish/describe.py,
publish/once.py, publish/run.py). Synthetic data only; no AI call is made."""

import json
import re

import pytest

from autopilot import never_post
from core.config import get_settings
from core.models import JobRecord, JobStatus
from publish import describe, once, run
from publish import settings as pub_settings

CHAPTERS = [(0, "Highlights"), (120, "Session store on Redis"), (300, "Camera calibration"),
            (500, "Open questions"), (700, "Next steps")]


def _job(title="Robotics and Dev team meeting", *, rows=None, chapters=CHAPTERS, speakers=(), job_id="h-1") -> JobRecord:
    job_dir = get_settings().jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "manifest.json").write_text(json.dumps({"job_id": job_id, "shorts": rows or [], "highlights": []}),
                                           encoding="utf-8")
    if chapters:
        (job_dir / "chapters.json").write_text(json.dumps(
            {"entries": [{"t": t, "title": name} for t, name in chapters]}), encoding="utf-8")
    if speakers:
        (job_dir / "segments.json").write_text(json.dumps(
            [{"speaker": s, "start": 0, "end": 1, "text": "hello"} for s in speakers]), encoding="utf-8")
    return JobRecord(job_id=job_id, status=JobStatus.DONE, title=title, meeting_date="2026-11-04")


def _row(n=1, title="Moving the session store to Redis", hook="Why the old store kept timing out.", **extra):
    return {"file": f"shorts/short_{n:02d}.mp4", "source_start": 10.0, "source_end": 50.0,
            "title": title, "hook": hook, **extra}


@pytest.fixture(autouse=True)
def _no_posted(monkeypatch):
    monkeypatch.setattr(describe, "_posted_titles", lambda: set())


def _full_tags(job) -> list[str]:
    return describe._hashtags(job, describe.named_hashtags(job, "full")).split()


def _short_tags(job, n=1) -> list[str]:
    return describe._hashtags(job, describe.named_hashtags(job, "short", n)).split()


# ------------------------------------------------------- order, count, shape


def test_order_is_named_tools_then_field_then_team_with_the_brand_last():
    job = _job(rows=[_row(1)])
    assert _full_tags(job) == ["#Redis", "#Robotics", "#Engineering", "#RoboticsAndDev", "#FloLabs"]


def test_default_is_five_and_the_line_never_passes_the_setting():
    assert pub_settings.DEFAULTS.hashtags_max == 5
    row = _row(1, title="Moving ESP32 and FloBrain from Redis to Kafka with Docker", hook="Why Postgres and Grafana beat Nginx with Celery, Pytest and Vite.")
    tags = _short_tags(_job(rows=[row]))
    assert len(tags) == 5 and tags[-1] == "#FloLabs" and tags[0] == "#ESP32"


def test_three_hashtags_are_two_topic_ones_and_the_brand_last():
    pub_settings.save({"hashtags_max": 3})
    assert _full_tags(_job(rows=[_row(1)])) == ["#Redis", "#Robotics", "#FloLabs"]


def test_max_15_caps_a_long_list():
    from publish import series

    series.set_row("robotics and dev", hashtags=["#RoboLab", "#Builders"])
    pub_settings.save({"hashtags_max": 15})
    tags = _short_tags(_job(rows=[_row(1, title="Moving ESP32 and FloBrain from Redis to Kafka with Docker", hook="Why Postgres and Grafana beat Nginx with Celery, Pytest and Vite.")]))
    assert len(tags) == 15 and tags[-1] == "#FloLabs"  # the ten names the finder keeps, 2 field, 2 team, the brand
    assert len({t.lower() for t in tags}) == len(tags)
    pub_settings.save({"hashtags_max": 6})
    assert len(_short_tags(_job(rows=[_row(1, title="Moving ESP32 and FloBrain from Redis to Kafka with Docker", hook="Why Postgres and Grafana beat Nginx with Celery, Pytest and Vite.")]))) == 6


def test_setting_zero_gives_no_hashtags_anywhere():
    pub_settings.save({"hashtags_max": 0})
    job = _job(rows=[_row(1)])
    assert describe._hashtags(job, ["#Redis"]) == ""
    assert "#" not in describe.full_description(job)
    assert "#" not in describe.short_description(describe.load_shorts(job)[0], job)


def test_the_switch_still_turns_them_off():
    pub_settings.save({"hashtags": False})
    assert "#" not in describe.full_description(_job(rows=[_row(1)]))


@pytest.mark.parametrize("value", [-1, 16, 2.5 * 10, "8", True])
def test_setting_range(value):
    with pytest.raises(ValueError):
        pub_settings.save({"hashtags_max": value})


def test_every_hashtag_is_plain_letters_and_digits_within_30_characters():
    row = _row(1, title="Wi-Fi and Zürich FloBrain-ESP32 AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    for tag in _short_tags(_job(rows=[row])):
        assert re.fullmatch(r"#[A-Za-z0-9]{1,29}", tag), tag
    assert "#shorts" not in " ".join(_short_tags(_job(rows=[_row(1, title="Shorts ESP32")]))).lower()


def test_dedup_ignores_case_and_the_brand_and_field_words():
    row = _row(1, title="FloLabs ROBOTICS robotics ESP32 esp32", hook="")
    tags = _short_tags(_job(rows=[row]))
    assert [t.lower() for t in tags].count("#flolabs") == 1
    assert [t.lower() for t in tags].count("#robotics") == 1
    assert [t.lower() for t in tags].count("#esp32") == 1


# ------------------------------------------------------------- the field map


@pytest.mark.parametrize("team, field", [
    ("Robotics and Dev", ["#Robotics", "#Engineering"]),
    ("Humanoid Robotics", ["#HumanoidRobots", "#Robotics"]),
    ("IoT", ["#IoT", "#EmbeddedSystems"]),
    ("Internet of Things", ["#IoT", "#EmbeddedSystems"]),
    ("Space", ["#SpaceTech", "#Astronomy"]),
    ("Multi-modal perception", ["#ComputerVision", "#AI"]),
    ("Vision", ["#ComputerVision", "#AI"]),
    ("FloBrain Backend", ["#AI", "#Backend"]),
    ("Backend", ["#AI", "#Backend"]),
    ("MoodChanger", ["#MentalHealth", "#AppDevelopment"]),
    ("Neuroscience", ["#Neuroscience"]),
    ("Automation", ["#Automation", "#AI"]),
    ("Design", ["#Design", "#UX"]),
    ("Data", ["#DataAnalytics"]),
    ("Analytics", ["#DataAnalytics"]),
    ("Front-end", ["#WebDevelopment"]),
    ("Frontend", ["#WebDevelopment"]),
    ("AI", ["#AI", "#MachineLearning"]),
    ("Zebra Crossing", ["#Tech", "#Innovation"]),
    ("", ["#Tech", "#Innovation"]),
])
def test_the_field_map(team, field):
    job = _job(f"{team} Team Meeting" if team else "Weekly Sync")
    assert [h for h, _ in describe._field_pairs(job)] == field


def test_a_word_inside_another_does_not_pick_a_field():
    assert [h for h, _ in describe._field_pairs(_job("Main Team Meeting"))] == ["#Tech", "#Innovation"]
    assert [h for h, _ in describe._field_pairs(_job("Maintenance Team"))] == ["#Tech", "#Innovation"]


# ------------------------------------------------- named tools and topics


def test_titles_in_title_case_do_not_make_hashtags_of_ordinary_words():
    row = _row(1, title="Moving The Session Store To Redis", hook="Why It Kept Timing Out")
    assert "#Session" not in _short_tags(_job(rows=[row]))
    assert "#Store" not in _short_tags(_job(rows=[row]))


def test_names_are_digits_camel_case_acronyms_and_capitalised_words_not_sentence_starters():
    found = describe._names_in("Update the ESP32 firmware. FloBrain talks MQTT to Redis and IoT nodes via the Pi")
    assert found == ["ESP32", "FloBrain", "MQTT", "Redis", "IoT", "Pi"][: len(found)]
    assert {"ESP32", "FloBrain", "MQTT", "Redis", "IoT"} <= set(found)
    assert "Update" not in found and "Pi" not in found  # a two-letter capitalised word is no name
    assert describe._names_in("We bought 3rd and 12ms things") == []


def test_a_person_never_becomes_a_hashtag():
    row = _row(1, title="Ahsan explains the Redis cache", hook="Why Zoya moved Kafka.")
    job = _job(rows=[row], speakers=["Ahsan Yasin", "Zoya Khan", "SPEAKER_01"])
    tags = _short_tags(job)
    assert "#Redis" in tags and "#Kafka" in tags
    assert "#Ahsan" not in tags and "#Zoya" not in tags
    assert describe._names_someone("Ahsan explains the cache", describe._speaker_names(job))


def test_a_word_the_clip_never_said_is_dropped_and_a_said_one_kept():
    row = _row(1, title="Why Redis beats Kafka", hook="", text="so we moved the session store to redis last week")
    tags = _short_tags(_job(rows=[row]))
    assert "#Redis" in tags and "#Kafka" not in tags


def test_a_name_with_a_digit_is_found_when_said_with_a_space():
    row = _row(1, title="The ESP32 board", hook="", text="the esp 32 board boots slowly")
    assert "#ESP32" in _short_tags(_job(rows=[row]))


def test_without_clip_text_the_names_are_trusted():
    assert "#Redis" in _short_tags(_job(rows=[_row(1)]))


def test_a_never_post_word_is_not_a_hashtag_or_a_tag(monkeypatch):
    monkeypatch.setattr(never_post, "match_words", lambda text, listed=None: "kafka" if "kafka" in text.lower() else None)
    row = _row(1, title="Why Redis beats Kafka", hook="")
    job = _job(rows=[row])
    assert "#Kafka" not in _short_tags(job) and "#Redis" in _short_tags(job)
    assert "Kafka" not in describe.search_tags(job, "short", 1)


def test_the_full_video_takes_names_from_its_title_hook_and_chapter_titles():
    job = _job(rows=[_row(1, hook="Why FloBrain timed out.")],
               chapters=[*CHAPTERS, (900, "Moving to Grafana")])
    tags = _full_tags(job)
    assert tags[:3] == ["#Redis", "#FloBrain", "#Grafana"] and tags[-1] == "#FloLabs"


def test_the_full_video_checks_title_words_against_the_shorts_text_but_trusts_its_chapters():
    rows = [_row(1, title="Why Redis beats Kafka", hook="", text="the redis move went well")]
    job = _job(rows=rows, chapters=[*CHAPTERS, (900, "Moving to Grafana")])
    tags = _full_tags(job)
    assert "#Redis" in tags and "#Grafana" in tags and "#Kafka" not in tags


def test_a_topic_cut_and_a_short_get_hashtags_too():
    from tests.test_topic_packaging import job_with_topics

    job = job_with_topics()
    info = describe.load_topics(job)[0]
    last = describe.topic_description(info, job).splitlines()[-1]
    assert last.endswith(" #FloLabs") and "#Redis" in last.split()
    short = describe.load_shorts(job)[0]
    assert describe.short_description(short, job).splitlines()[-1].endswith(" #FloLabs")


def test_a_registry_team_keeps_its_own_hashtags_after_the_field_and_the_brand_last():
    from publish import series

    series.set_row("robotics and dev", hashtags=["#RoboLab", "#Builders"])
    assert _short_tags(_job(rows=[_row(1)])) == ["#Redis", "#Robotics", "#Engineering", "#RoboLab", "#FloLabs"]


# ------------------------------------------------------ description limits


def test_descriptions_stay_within_youtubes_limits_with_15_hashtags():
    pub_settings.save({"hashtags_max": 15, "links_text": "\n".join(f"https://example.com/{i}/" + "é" * 20 for i in range(20))})
    job = _job(rows=[_row(1, title="Moving ESP32 and FloBrain from Redis to Kafka with Docker", hook="Why Postgres and Grafana beat Nginx with Celery, Pytest and Vite.")])
    full = describe.full_description(job)
    assert describe.description_problem(full) is None
    assert len(full.encode("utf-8")) <= describe.DESCRIPTION_LIMIT - describe.SHORTS_ROOM
    assert full.splitlines()[-1].endswith(" #FloLabs") and full.splitlines()[-1].startswith("#ESP32 ")
    shorts = [(i, "A reasonably long short title about " + "x" * 40, "https://youtu.be/abcdefghijk") for i in range(4)]
    combined, kept = describe.with_shorts_block(full, shorts)
    assert describe.description_problem(combined) is None and kept == [0, 1, 2, 3]
    assert describe.description_problem(describe.short_description(describe.load_shorts(job)[0], job)) is None


def test_hashtags_give_way_last_when_the_chapters_fill_the_room(monkeypatch):
    pub_settings.save({"hashtags_max": 15})
    monkeypatch.setattr(describe, "SHORTS_ROOM", describe.DESCRIPTION_LIMIT - 400)
    long_chapters = [(i * 30, f"Chapter number {i} about boards") for i in range(12)]
    text = describe.full_description(_job(rows=[_row(1)], chapters=long_chapters))
    assert text.startswith("00:00") and "05:30 Chapter number 11 about boards" in text  # never cut
    assert text.splitlines()[-1] == "#FloLabs"  # the chapters fill the room: the topic hashtags went first, the brand stays


# ------------------------------------------------------ hidden search tags


def test_search_tags_are_topic_first_with_one_brand_tag_and_the_marker_last():
    job = _job(rows=[_row(1)])
    extra = describe.search_tags(job, "full")
    assert extra == ["Redis", "Robotics", "Engineering", "Robotics and Dev", "FloLabs"]
    assert not {"Flolabs", "FloLabs Innovations", "Flomad Labs"} & set(extra)
    tags = once.tag_list("hcabc123", extra)
    assert tags[-1] == "hcabc123" and tags[:-1] == extra
    assert len(tags) <= 12


def test_search_tags_for_a_short_use_its_own_words_and_never_shorts():
    job = _job(rows=[_row(1, title="Shorts about ESP32", hook="")])
    extra = describe.search_tags(job, "short", 1)
    assert "ESP32" in extra and "Shorts" not in extra and "shorts" not in [t.lower() for t in extra]


def test_search_tags_still_come_when_hashtags_are_off():
    pub_settings.save({"hashtags": False, "hashtags_max": 0})
    assert "FloLabs" in describe.search_tags(_job(rows=[_row(1)]), "full")


def test_search_tags_are_at_most_eleven_beside_the_marker():
    extra = describe.search_tags(_job(rows=[_row(1, title="Moving ESP32 and FloBrain from Redis to Kafka with Docker", hook="Why Postgres and Grafana beat Nginx with Celery, Pytest and Vite.")]), "short", 1)
    assert len(extra) == 11 and extra[-1] == "FloLabs"
    assert len(once.tag_list("hcabc123", extra)) == 12


def test_the_tag_text_is_capped_at_450_characters_and_the_marker_stays():
    marker = "hc0123456789ab"
    many = [f"word{i:03d}" + "x" * 40 for i in range(40)]
    tags = once.tag_list(marker, many)
    assert tags[-1] == marker
    assert sum(len(t) for t in tags) <= 450 and len(tags) <= 12
    long_ones = ["y" * 100] * 3 + ["short one", "z" * 90, "w" * 90, "v" * 90]
    tags = once.tag_list(marker, long_ones)
    assert tags[-1] == marker and sum(len(t) + (2 if " " in t else 0) for t in tags) <= 450
    assert once.tag_list(marker, []) == [marker]
    assert once.tag_list(marker, ["a", "a", "<b>"]) == ["a", "b", marker]


def test_the_upload_carries_the_search_tags_to_upload_once():
    fields = run._upload_fields(_job(), pub_settings.current(), None, tags=["FloLabs", "Robotics"])
    assert fields["tags"] == ["FloLabs", "Robotics"]
    assert "tags" not in run._upload_fields(_job(), pub_settings.current(), None)


def test_plain_capitalised_words_are_never_names():
    for text in ["Next, Sarah shows how we cut costs.", "Demo by Ali", "The Big Battery Fix",
                 "How We Cut Costs", "A Tour of Our New Lab", "Fixing the Camera Lag"]:
        assert describe._names_in(text) == []
    assert describe._names_in("Waiting on Sarah for the PCB") == ["PCB"]


# ----------------------------------------------- multi-word names, fragments


def test_a_name_of_two_words_stays_together():
    assert describe._names_in("We run Claude Code on a Raspberry Pi 5 with Redis") == ["Claude Code", "Raspberry Pi", "Redis"]
    row = _row(1, title="Remote access to a Raspberry Pi", hook="How the Raspberry Pi runs Claude Code.",
               text="the raspberry pi runs claude code remotely")
    job = _job(rows=[row])
    assert "#RaspberryPi" in _short_tags(job) and "#Raspberry" not in _short_tags(job)
    assert "Raspberry Pi" in describe.search_tags(job, "short", 1)


def test_fragments_are_never_names_unless_said_as_an_acronym():
    assert describe._names_in("the IoC container and IoT nodes") == ["IoT"]
    assert describe._names_in("the IOC container") == ["IOC"]


def test_only_one_brand_tag_is_public():
    job = _job(rows=[_row(1)])
    tags = describe.search_tags(job, "full")
    assert [t for t in tags if "flolabs" in t.lower() or "flomad" in t.lower()] == ["FloLabs"]
