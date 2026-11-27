"""Stop everything, then resume: the meetings whose jobs it cancelled start
again from the beginning, and are not counted as failures (owner, 2026-11-27)."""

from datetime import timedelta

import pytest

import api.main as api_main
from core.models import JobStatus
from tests.test_autopilot import T0, World, _meeting


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


def _cancel_by_stop_all(job_id):
    job = api_main.job_store.get(job_id)
    job.status = JobStatus.CANCELLED
    job.error = "Cancelled by Stop everything on Nov 27, 11:16 PM, before it started."
    api_main.job_store.update(job)


def _started(world, uuid="m1"):
    world.zoom.meetings[uuid] = _meeting(uuid, T0 - timedelta(hours=2))
    world.step()
    m = world.meeting(uuid)
    assert m.state == "processing" and m.job_id
    return m


def test_a_job_stopped_by_stop_everything_starts_again_on_resume(world):
    m = _started(world)
    first_job, attempts = m.job_id, m.attempts
    world.engine.set_paused(True)
    _cancel_by_stop_all(first_job)
    world.step(minutes=5)
    assert world.meeting("m1").state == "processing"  # paused: nothing is looked at
    world.engine.set_paused(False)
    world.step(minutes=1)
    m = world.meeting("m1")
    assert m.state == "processing" and m.job_id != first_job  # a new job, from the start
    assert m.attempts == attempts  # the stopped run doesn't count as a failed attempt
    assert len(world.submitted) == 2


def test_a_meeting_left_failed_by_an_older_stop_everything_is_picked_up_on_resume(world):
    m = _started(world)
    with world.engine.store.editing() as state:
        old = state.meetings["m1"]
        old.state, old.job_id, old.next_try_at = "needs_attention", None, None
        old.error = "Cancelled by Stop everything on Nov 20, 9:00 AM."
    world.engine.set_paused(True)
    world.engine.set_paused(False)
    m = world.meeting("m1")
    assert m.state == "ready" and m.error.startswith("Stopped by Stop everything")
    world.step(minutes=1)
    assert world.meeting("m1").state == "processing"


def test_an_ordinary_cancel_or_failure_still_counts(world):
    m = _started(world)
    world.fail(m.job_id)
    world.step(minutes=1)
    m = world.meeting("m1")
    assert m.state == "ready" and m.next_try_at is not None  # waits before trying again, as before
    assert not m.error.startswith("Stopped by Stop everything")
