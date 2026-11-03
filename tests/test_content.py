"""The "is there a meeting at all?" check (decide/content.py): a word count
only. Fewer than CONTENT_MIN_WORDS (200) in the whole transcript is an empty
room, kept on the server but never cut or posted; that many or more is a
meeting, always (no AI question)."""

import json
from datetime import timedelta

import pytest

import pipeline as pipeline_module
from core.config import get_settings
from core.models import JobStatus, Segment
from decide.content import check_content


def _talk(n_lines, words_per_line=12, gap=10.0):
    return [Segment(speaker="A", start=i * gap, end=i * gap + 8, text=" ".join(["word"] * words_per_line))
            for i in range(n_lines)]


def test_a_recording_where_nobody_speaks_is_empty():
    found = check_content([Segment(speaker="A", start=5, end=7, text="Hello, can you hear me?")], 3600, 200)
    assert not found.has_meeting and found.words == 5
    assert "only 5 words" in found.reason and "60:00" in found.reason and "at least 200" in found.reason


@pytest.mark.parametrize("words, has_meeting", [(0, False), (199, False), (200, True), (300, True), (14212, True)])
def test_200_words_or_more_is_always_a_meeting(words, has_meeting):
    segments = [Segment(speaker="A", start=0, end=5, text=" ".join(["word"] * words))] if words else []
    found = check_content(segments, 6000, 200)
    assert found.has_meeting is has_meeting and found.words == words


def test_the_default_is_200_words():
    from core.config import Settings

    assert Settings.model_fields["content_min_words"].default == 200


def test_an_old_ai_verdict_never_skips_a_meeting(tmp_path):
    """Jobs from before the change saved the AI's "empty" answer: the words
    are counted again, and the count is what is saved now."""
    saved = tmp_path / "content_check.json"
    saved.write_text(json.dumps({"has_meeting": False, "reason": "only greetings", "words": 480, "asked_ai": True}))
    found = check_content(_talk(40), 600, 200, saved=saved)
    assert found.has_meeting and found.words == 480
    assert json.loads(saved.read_text()) == {"has_meeting": True, "words": 480,
                                             "reason": "480 words are spoken in 10:00 of recording"}


def test_an_empty_recording_stops_the_job_before_any_judging(monkeypatch):
    from tests.test_pipeline import _make_job, _patch_common

    monkeypatch.setenv("CONTENT_CHECK_ENABLED", "true")
    get_settings.cache_clear()
    transcribe_calls, judged = [], []
    _patch_common(monkeypatch, transcribe_calls, judged)  # the fake transcript is the word "hi"
    job = _make_job("job-empty-room")
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.SKIPPED_EMPTY and job.error_code == "no_content"
    assert job.error.startswith("No meeting to post: only 1 words are spoken in 0:10 of recording "
                                "(a meeting needs at least 200) (transcript: ") and judged == []
    assert (get_settings().jobs_dir / job.job_id / "content_check.json").exists()


def test_a_recording_with_enough_words_goes_on_to_judging(monkeypatch):
    from tests.test_pipeline import _make_job, _patch_common

    monkeypatch.setenv("CONTENT_CHECK_ENABLED", "true")
    monkeypatch.setenv("CONTENT_MIN_WORDS", "1")  # the fake transcript is the one word "hi"
    get_settings.cache_clear()
    judged = []
    _patch_common(monkeypatch, [], judged)
    job = _make_job("job-real-meeting")
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DONE and judged


def test_the_autopilot_skips_an_empty_meeting_and_never_posts_or_retries_it(monkeypatch):
    import api.main as api_main
    from tests.test_autopilot import T0, World, _meeting

    world = World(monkeypatch)
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2))
    world.step()
    job = api_main.job_store.get(world.meeting("m1").job_id)
    job.status, job.error = JobStatus.SKIPPED_EMPTY, "No meeting to post: only 12 words are spoken in 61:00."
    api_main.job_store.update(job)
    for minutes in (1, 60, 600):
        world.step(minutes=minutes)
    m = world.meeting("m1")
    assert m.state == "skipped" and "12 words" in m.error
    assert len(world.submitted) == 1 and world.channel.videos == {}


def test_the_zoom_list_gives_the_whole_reason_a_meeting_was_skipped(monkeypatch):
    from fastapi.testclient import TestClient

    import api.main as api_main
    from ingest import zoom
    from tests.test_autopilot import T0, World, _meeting

    world = World(monkeypatch)
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2))
    world.step()
    job = api_main.job_store.get(world.meeting("m1").job_id)
    reason = "No meeting to post: only 12 words are spoken in 61:00 of recording. " + "a long reason " * 60
    job.status, job.error = JobStatus.SKIPPED_EMPTY, reason
    api_main.job_store.update(job)
    world.step(minutes=1)

    class Client:
        def list_recordings(self, host, start, end):
            return [{"uuid": "m1", "topic": "Weekly Sync", "status": "ready"},
                    {"uuid": "m2", "topic": "Other", "status": "ready"}]

    monkeypatch.setattr(zoom, "get_client", lambda: Client())
    rows = TestClient(api_main.app).get("/zoom/recordings?host=pm@flomad.com").json()["meetings"]
    assert [(r["autopilot"], r["autopilot_reason"]) for r in rows] == [("skipped", reason), (None, None)]
