"""Topic cuts: the ledger kind, the grounded title and description (publish/describe.py),
the link from a Short to its cut, and the never-post words. Synthetic data only."""

import json
import shutil

import pytest

import api.main as api_main
from core.config import get_settings
from core.models import ArtifactInfo, JobRecord, JobStatus
from ledger import files, uploads
from ledger.uploads import Ledger
from publish import describe
from publish import run as publish_run
from publish import settings as pub_settings
from tests.test_publish import OFFSET, _finished_job

CUT_TEXT = ("so we are moving the session store to redis because the old store kept timing out "
            "under load and the cache hit rate was low")


def topic_row(n=1, **extra) -> dict:
    """A manifest 'topics' row in the shape the render writes (TR4's contract)."""
    row = {
        "file": f"topic/topic_{n:02d}.mp4", "source_start": 100.0, "source_end": 200.0,
        "cleaned_start": 40.0, "cleaned_end": 140.0, "duration_s": 100.0, "open_s": 20.0, "card_s": 4.0,
        "title": "Moving the session store to Redis", "hook": "Why the old store kept timing out.",
        "screen_hook": "Session store", "best_score": 8.0, "stands_alone": True, "category": "decision",
        "thumb_src_s": 120.0, "text": CUT_TEXT,
        "chapters": [{"t": 0, "title": "Why the old store failed"}, {"t": 30, "title": "Moving to Redis"},
                     {"t": 70, "title": "Rollout risks"}],
    }
    row.update(extra)
    return row


def job_with_topics(rows=None, job_id="pub-1", **kwargs) -> JobRecord:
    """A finished job (tests/test_publish.py) whose manifest also has topic cuts and files."""
    job = _finished_job(job_id, **kwargs)
    job_dir = get_settings().jobs_dir / job_id
    rows = [topic_row()] if rows is None else rows
    (job_dir / "topic").mkdir(exist_ok=True)
    for row in rows:
        (job_dir / row["file"]).write_bytes(f"cut-{row['file']}".encode())
        job.artifacts[row["file"]] = ArtifactInfo(path=row["file"])
    manifest = json.loads((job_dir / "manifest.json").read_text())
    manifest["topics"] = rows
    (job_dir / "manifest.json").write_text(json.dumps(manifest))
    api_main.job_store.update(job)
    return job


@pytest.fixture(autouse=True)
def _no_posted(monkeypatch):
    monkeypatch.setattr(describe, "_posted_titles", lambda: set())


def _info(job, **extra):
    if extra:
        job_dir = get_settings().jobs_dir / job.job_id
        manifest = json.loads((job_dir / "manifest.json").read_text())
        manifest["topics"] = [topic_row(**extra)]
        (job_dir / "manifest.json").write_text(json.dumps(manifest))
    return describe.load_topics(job)[0]


# ------------------------------------------------------------------ the manifest rows


def test_load_topics_reads_the_rows_and_knows_where_the_topic_plays_in_the_full_video():
    job = job_with_topics()
    (info,) = describe.load_topics(job)
    assert info.index == 1 and info.file == "topic/topic_01.mp4"
    assert (info.open_s, info.card_s, info.duration_s) == (20.0, 4.0, 100.0)
    assert info.at_s == int(OFFSET + 40)  # the cleaned meeting starts at OFFSET; the topic 40 s in
    assert info.chapters[1] == (30.0, "Moving to Redis")
    assert info.best_score == 8.0 and info.stands_alone is True


def test_load_topics_skips_failed_artifacts_and_unreadable_rows():
    job = job_with_topics([topic_row(1), topic_row(2), {"file": "topic/topic_03.mp4"}])
    job.artifacts["topic/topic_02.mp4"].status = "failed"
    assert [t.index for t in describe.load_topics(job)] == [1]
    # an ok artifact counts as rendered even when its manifest row can't be read
    assert describe.rendered_topics(job) == {1, 3}


def test_a_job_without_topics_has_none():
    assert describe.load_topics(_finished_job("pub-9")) == []


def test_text_is_capped_at_2000_characters():
    job = job_with_topics([topic_row(text="word " * 1000)])
    assert len(describe.load_topics(job)[0].text) == 2000


# ------------------------------------------------------------------ the title


def test_the_title_is_the_best_moment_then_the_team():
    job = job_with_topics()
    assert describe.topic_title(_info(job), job) == "Moving the session store to Redis | FloLabs"


def test_a_title_about_something_else_falls_back_to_the_first_chapter():
    job = job_with_topics([topic_row(title="Quarterly budget planning discussion")])
    assert describe.topic_title(_info(job), job) == "Why the old store failed | FloLabs"


def test_when_nothing_is_grounded_the_title_names_the_meeting():
    row = topic_row(title="Quarterly budget planning", chapters=[{"t": 0, "title": "Holiday rota planning"}])
    job = job_with_topics([row])
    title = describe.topic_title(_info(job), job)
    assert title == "Weekly Sync — topic 1" and "|" not in title


def test_a_number_the_cut_never_said_is_not_allowed():
    job = job_with_topics([topic_row(title="Moving the session store to Redis in 800 ms")])
    assert describe.topic_title(_info(job), job) == "Why the old store failed | FloLabs"
    said = job_with_topics([topic_row(title="Moving the session store to Redis in 800 ms",
                                      text=CUT_TEXT + " about 800 ms")], job_id="pub-2")
    assert "800 ms" in describe.topic_title(_info(said), said)


def test_a_title_that_names_a_speaker_is_passed_over(monkeypatch):
    job = job_with_topics([topic_row(title="Priya explains the session store")])
    monkeypatch.setattr(describe, "_speaker_names", lambda j: {"priya"})
    assert describe.topic_title(_info(job), job) == "Why the old store failed | FloLabs"


def test_a_title_already_on_the_channel_gets_the_day(monkeypatch):
    job = job_with_topics()
    monkeypatch.setattr(describe, "_posted_titles", lambda: {"moving the session store to redis | flolabs"})
    assert describe.topic_title(_info(job), job) == "Moving the session store to Redis (2026-10-05) | FloLabs"


def test_a_title_equal_to_the_full_videos_gets_the_day(monkeypatch):
    job = job_with_topics()
    monkeypatch.setattr(describe, "full_title", lambda j: "Moving the session store to Redis | FloLabs")
    assert "(2026-10-05)" in describe.topic_title(_info(job), job)


def test_the_title_never_has_angle_brackets_and_fits_the_limit():
    job = job_with_topics([topic_row(title="Moving <the> session store to Redis " + "and more " * 30)])
    title = describe.topic_title(_info(job), job)
    assert "<" not in title and ">" not in title
    assert describe.title_problem(title) is None and len(title) <= 100


def test_the_team_comes_from_the_meeting_name():
    job = job_with_topics()
    job.title = "IoT Team Meeting"
    assert describe.topic_title(_info(job), job) == "Moving the session store to Redis | FloLabs IoT"


# ------------------------------------------------------------------ the description


def test_the_description_has_the_hook_where_it_is_from_and_the_full_meeting_link():
    job = job_with_topics()
    job.title = "IoT Team Meeting"
    text = describe.topic_description(_info(job), job, "https://youtu.be/FULL1234567")
    lines = text.splitlines()
    assert lines[0] == "Why the old store kept timing out."
    assert "From the IoT team meeting of 5 October 2026." in lines
    assert describe.ORG_LINE in lines
    assert "Full meeting: https://youtu.be/FULL1234567?t=100" in lines  # 60 + 40
    assert "#FloLabs" in lines[-1] and "#shorts" not in text.lower()


def test_the_full_meeting_link_can_be_given_a_time():
    job = job_with_topics()
    text = describe.topic_description(_info(job), job, "https://youtu.be/FULL1234567", full_at_s=777)
    assert "Full meeting: https://youtu.be/FULL1234567?t=777" in text


def test_no_link_while_the_full_video_is_not_on_the_channel():
    job = job_with_topics()
    assert "Full meeting" not in describe.topic_description(_info(job), job)


def test_the_cuts_chapters_are_shifted_by_the_open_and_the_card_and_start_at_zero():
    job = job_with_topics()
    text = describe.topic_description(_info(job), job)
    assert "00:00 Why the old store failed\n00:54 Moving to Redis\n01:34 Rollout risks" in text  # 30+24, 70+24


def test_chapters_are_left_out_when_there_are_fewer_than_three_or_they_are_too_close():
    few = job_with_topics([topic_row(chapters=[{"t": 0, "title": "One"}, {"t": 30, "title": "Two"}])])
    assert "00:00" not in describe.topic_description(_info(few), few)
    close = job_with_topics([topic_row(chapters=[{"t": 0, "title": "One"}, {"t": 4, "title": "Two"},
                                                 {"t": 8, "title": "Three"}])], job_id="pub-2")
    assert "00:00" not in describe.topic_description(_info(close), close)
    bad = job_with_topics([topic_row(chapters=[{"t": 0, "title": "One"}, {"t": 30, "title": "T" * 80},
                                               {"t": 60, "title": "Three"}])], job_id="pub-3")
    assert "00:00" not in describe.topic_description(_info(bad), bad)


def test_chapter_lines_may_be_pairs_and_no_other_line_starts_with_a_time():
    job = job_with_topics([topic_row(chapters=[[0, "One"], [30, "Two"], [70, "Three"]],
                                     hook="At 9:30 the cache broke.")])
    text = describe.topic_description(_info(job), job)
    assert "00:00 One" in text
    first_lines = [ln for ln in text.splitlines() if ln[:1].isdigit()]
    assert all(ln.split()[0].count(":") == 1 and ln[2] == ":" for ln in first_lines)  # only the chapters
    assert "At 9.30 the cache broke." in text


def test_the_description_has_no_angle_brackets_and_fits_the_limit(monkeypatch):
    job = job_with_topics([topic_row(hook="a <b> hook")])
    links = chr(10).join(f"Link {i}: https://example.com/{i}" for i in range(30))
    pub_settings.save({"links_text": links})
    full = describe.topic_description(_info(job), job, "https://youtu.be/FULL1234567")
    assert "<" not in full and ">" not in full and "Link 29" in full
    monkeypatch.setattr(describe, "DESCRIPTION_LIMIT", 700)
    text = describe.topic_description(_info(job), job, "https://youtu.be/FULL1234567")
    assert len(text.encode("utf-8")) <= 700 and "Link 0:" in text and "Link 29" not in text  # links give way
    assert "00:00 Why the old store failed" in text  # the chapters are never cut


def test_hashtags_follow_the_setting():
    job = job_with_topics()
    pub_settings.save({"hashtags": False})
    assert "#" not in describe.topic_description(_info(job), job)


# ------------------------------------------------------------------ Shorts link to the cut


def test_a_short_inside_the_cut_links_to_it_at_its_own_moment():
    job = job_with_topics()
    short = describe.load_shorts(job)[0]  # source 110-150, plays at 110 s in the full video
    cut = describe.load_topics(job)[0]
    at = describe.short_cut_position(short, cut, job)
    assert at == 34  # 20 open + 4 card + (110 - 60 - 40)
    text = describe.short_description(short, job, "https://youtu.be/FULL1234567",
                                      cut_url="https://youtu.be/CUT12345678", cut_at_s=at)
    assert "https://youtu.be/CUT12345678?t=34" in text
    assert "FULL1234567" not in text


def test_a_short_outside_the_cut_has_no_position():
    job = job_with_topics()
    cut = describe.load_topics(job)[0]
    shorts = describe.load_shorts(job)
    assert describe.short_cut_position(shorts[1], cut, job) is None  # source 30-70
    assert describe.short_cut_position(shorts[2], cut, job) is None  # cut out of the meeting


def test_the_short_description_is_unchanged_without_a_cut():
    job = job_with_topics()
    short = describe.load_shorts(job)[0]
    text = describe.short_description(short, job, "https://youtu.be/F")
    assert "in the full video." + chr(10) + "https://youtu.be/F?t=110" in text


# ------------------------------------------------------------------ never-post


def test_listed_words_finds_a_never_post_word_in_the_cut_words(monkeypatch):
    from autopilot import never_post

    monkeypatch.setattr(never_post, "entries", lambda: ["investor", "salary"])
    job = job_with_topics([topic_row(hook="The investor wanted a cheaper store.")])
    info = _info(job)
    assert publish_run.listed_words(describe.topic_title(info, job), describe.topic_description(info, job)) == "investor"
    clean = job_with_topics([topic_row()], job_id="pub-2")
    info = _info(clean)
    assert publish_run.listed_words(describe.topic_title(info, clean), describe.topic_description(info, clean)) is None
    word_in_title = job_with_topics([topic_row(title="Salary review for the session store",
                                               text=CUT_TEXT + " salary review")], job_id="pub-3")
    info = _info(word_in_title)
    assert publish_run.listed_words(describe.topic_title(info, word_in_title), "") == "salary"


# ------------------------------------------------------------------ the ledger


def _ledger_job(job_id="j1", uuid="u1") -> JobRecord:
    return JobRecord(job_id=job_id, status=JobStatus.DONE, title="Weekly Sync", meeting_date="2026-10-05",
                     zoom_meeting_uuid=uuid, zoom_meeting={"topic": "Weekly Sync", "meeting_id": "12345"})


def test_topic_item_keys_and_names():
    item = uploads.topic_item(_ledger_job(), 2, 100.0, 200.0)
    assert item.key == "zoom:u1|topic|j1|2" and item.kind == "topic"
    assert item.clip == (100.0, 200.0) and item.index == 2
    assert item.topic == "Weekly Sync" and item.meeting_id == "12345"  # for the last never-post check
    assert uploads.topic_item(_ledger_job(), 2).clip is None


def test_a_topic_url_is_a_normal_video_link():
    assert uploads.video_url("topic", "abcdefghijk") == "https://youtu.be/abcdefghijk"


def _post(ledger, item):
    ledger.begin(item, title="t", privacy="private", by="person")
    ledger.published(item.key, "abcdefghijk")


def test_a_topic_is_never_counted_as_the_meetings_full_video(tmp_path):
    ledger = Ledger(tmp_path)
    job = _ledger_job()
    _post(ledger, uploads.topic_item(job, 1, 1.0, 9.0))
    assert ledger.meeting_state(uploads.source_of(job)) == "partly"
    assert ledger.blocker(uploads.full_item(job)) is None
    _post(ledger, uploads.full_item(job))
    assert ledger.meeting_state(uploads.source_of(job)) == "posted"


def test_topic_cuts_follow_the_one_run_rule_among_themselves_only(tmp_path):
    ledger = Ledger(tmp_path)
    first, other = _ledger_job("j1"), _ledger_job("j2")
    _post(ledger, uploads.topic_item(first, 1, 1.0, 9.0))
    assert ledger.blocker(uploads.topic_item(other, 1, 1.0, 9.0)) is not None  # another run's cut
    assert ledger.blocker(uploads.topic_item(first, 2, 20.0, 29.0)) is None  # the first run's other cut
    assert ledger.blocker(uploads.short_item(other, 1, 1.0, 9.0)) is None  # Shorts are a separate pool
    assert ledger.blocker(uploads.full_item(other)) is None
    with pytest.raises(uploads.Refused) as refused:
        ledger.begin(uploads.topic_item(other, 1, 1.0, 9.0), title="t", privacy="private", by="person")
    assert "Topic cuts" in str(refused.value)


def test_the_posted_list_keeps_the_topic_kind_and_an_old_journal_loads(tmp_path):
    ledger = Ledger(tmp_path)
    job = _ledger_job()
    _post(ledger, uploads.topic_item(job, 1, 1.0, 9.0))
    again = Ledger(tmp_path)
    (row,) = again.posted()
    assert row.kind == "topic" and row.index == 1 and row.clip == (1.0, 9.0)
    assert [line["kind"] for line in files.read_jsonl(tmp_path / "posted.jsonl")] == ["topic"]
    # a journal from before topic cuts: only full and short rows
    old = tmp_path / "old"
    old.mkdir()
    shutil.rmtree(files.backup_dir(), ignore_errors=True)  # the backup is keyed by file name
    _post(Ledger(old), uploads.full_item(job))
    _post(Ledger(old), uploads.short_item(job, 1, 1.0, 9.0))
    assert {u.kind for u in Ledger(old).posted()} == {"full", "short"}


def test_a_job_file_from_before_topic_cuts_loads():
    job = JobRecord.model_validate({"job_id": "old"})
    assert job.youtube_topics == {}
    assert job.youtube_busy is False
