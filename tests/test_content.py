"""The "is there a meeting at all?" check (decide/content.py): an hour of an
empty room is kept on the server but never cut or posted."""

from datetime import timedelta

import pytest

import pipeline as pipeline_module
from core.config import get_settings
from core.models import JobStatus, Segment
from decide.content import check_content, transcript_sample


class FakeCaller:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, []

    def generate(self, system, contents, schema=None):
        self.calls.append(contents)
        if self.error:
            raise self.error
        return type("Answer", (), {"text": self.answer})()


def _talk(n_lines, words_per_line=12, gap=10.0):
    return [Segment(speaker="A", start=i * gap, end=i * gap + 8, text=" ".join(["word"] * words_per_line))
            for i in range(n_lines)]


def test_a_recording_where_nobody_speaks_is_empty_without_asking_the_ai():
    caller = FakeCaller(error=AssertionError("must not be asked"))
    found = check_content([Segment(speaker="A", start=5, end=7, text="Hello, can you hear me?")], 3600, caller, 150)
    assert not found.has_meeting and not found.asked_ai and caller.calls == []
    assert "5 words" in found.reason and "60:00" in found.reason


@pytest.mark.parametrize("answer, has_meeting", [
    ("EMPTY | only people waiting for others to join", False),
    ("**EMPTY**|a sound test", False),
    ("MEETING | the team plans the next release", True),
    ("I think this is probably fine", True),  # unclear: never drop a meeting over it
    ("", True),
])
def test_the_ai_decides_and_an_unclear_answer_goes_ahead(answer, has_meeting):
    found = check_content(_talk(40), 600, FakeCaller(answer), 150)
    assert found.has_meeting is has_meeting and found.asked_ai


def test_a_failed_ai_call_processes_the_meeting_anyway():
    found = check_content(_talk(40), 600, FakeCaller(error=RuntimeError("quota")), 150)
    assert found.has_meeting and "processed anyway" in found.reason


def test_the_verdict_is_saved_so_a_rerender_asks_nothing(tmp_path):
    saved = tmp_path / "content_check.json"
    assert not check_content(_talk(40), 600, FakeCaller("EMPTY | nobody there"), 150, saved=saved).has_meeting
    again = check_content(_talk(40), 600, FakeCaller(error=AssertionError("asked twice")), 150, saved=saved)
    assert not again.has_meeting and again.reason == "nobody there"


def test_a_long_transcript_is_sampled_from_the_start_middle_and_end():
    sample = transcript_sample(_talk(1000), max_words=600)
    lines = sample.splitlines()
    assert sum(len(line.split()) for line in lines) < 900
    assert lines[0].startswith("[0:00]") and lines[-1].startswith("[166:30]") and lines.count("[…]") == 2


def test_an_empty_recording_stops_the_job_before_any_judging(monkeypatch):
    from tests.test_pipeline import _make_job, _patch_common

    monkeypatch.setenv("CONTENT_CHECK_ENABLED", "true")
    get_settings.cache_clear()
    transcribe_calls, judged = [], []
    _patch_common(monkeypatch, transcribe_calls, judged)  # the fake transcript is the word "hi"
    job = _make_job("job-empty-room")
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.SKIPPED_EMPTY and job.error_code == "no_content"
    assert job.error.startswith("No meeting to post: only 1 words") and judged == []
    assert (get_settings().jobs_dir / job.job_id / "content_check.json").exists()


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



def test_an_ai_verdict_says_so_with_the_word_count_and_the_whole_reason(monkeypatch):
    from decide.content import ContentCheck
    from tests.test_pipeline import _make_job, _patch_common

    monkeypatch.setenv("CONTENT_CHECK_ENABLED", "true")
    get_settings.cache_clear()
    _patch_common(monkeypatch, [], [])
    long_reason = "only greetings and waiting for others " * 20
    monkeypatch.setattr(pipeline_module, "check_content",
                        lambda *a, **k: ContentCheck(False, long_reason.strip(), 5123, True))
    job = _make_job("job-ai-empty")
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.SKIPPED_EMPTY
    assert job.error.startswith("No meeting to post: the AI judged it empty (5123 words spoken")
    assert job.error.endswith(long_reason.strip())  # never cut short


def test_the_ai_reason_is_kept_in_full():
    long_reason = "x " * 400
    found = check_content(_talk(40), 600, FakeCaller(f"EMPTY | {long_reason}"), 150)
    assert found.reason == long_reason.strip()


def test_the_zoom_list_gives_the_whole_reason_a_meeting_was_skipped(monkeypatch):
    from fastapi.testclient import TestClient

    import api.main as api_main
    from ingest import zoom
    from tests.test_autopilot import T0, World, _meeting

    world = World(monkeypatch)
    world.zoom.meetings["m1"] = _meeting("m1", T0 - timedelta(hours=2))
    world.step()
    job = api_main.job_store.get(world.meeting("m1").job_id)
    reason = "No meeting to post: the AI judged it empty. " + "a long reason " * 60
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
