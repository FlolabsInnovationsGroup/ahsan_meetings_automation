"""The side panel's "Clear old jobs": deletes every finished job, never one
that runs, uploads, is kept with .keep, waits at the AI's picks, or that the
autopilot is still posting; what is on YouTube stays in the posted list."""

from fastapi.testclient import TestClient

import api.main as api_main
from autopilot.store import Meeting
from core.config import get_settings
from core.models import JobRecord, JobStatus
from ledger import uploads
from ledger.uploads import get_ledger
from tests.test_publish import _finished_job


def _job(job_id: str, status: JobStatus, **fields) -> JobRecord:
    job = JobRecord(job_id=job_id, status=status, **fields)
    api_main.job_store.create(job)
    return job


def test_clear_old_jobs_deletes_only_finished_jobs_nobody_needs(monkeypatch):
    jobs_dir = get_settings().jobs_dir
    done = _finished_job("done1")
    _finished_job("failed1").status = JobStatus.FAILED
    api_main.job_store.update(api_main.job_store.get("failed1"))
    _job("running1", JobStatus.RENDERING_SHORTS)
    _job("picks1", JobStatus.DECIDED)
    _job("uploading1", JobStatus.DONE, youtube_status="uploading")
    _job("kept1", JobStatus.DONE)
    (jobs_dir / "kept1" / ".keep").write_text("")
    _job("autopilot1", JobStatus.DONE)
    with api_main.autopilot_store.editing() as state:
        state.meetings["m1"] = Meeting(uuid="m1", state="posting", job_id="autopilot1")
    # a video it posted stays on the list after its job is gone
    get_ledger().record_existing(uploads.full_item(done), title="Weekly", url="https://youtu.be/dQw4w9WgXcQ")

    client = TestClient(api_main.app)
    before = client.get("/storage").json()
    assert before["old_jobs"] == 2 and before["old_jobs_bytes"] > 0 and before["disk_free_bytes"] > 0

    body = client.post("/storage/clear-jobs").json()
    assert body["deleted"] == 2 and body["freed_bytes"] == before["old_jobs_bytes"]
    assert body["old_jobs"] == 0
    assert not (jobs_dir / "done1").exists() and not (jobs_dir / "failed1").exists()
    for kept in ("running1", "picks1", "uploading1", "kept1", "autopilot1"):
        assert (jobs_dir / kept / "job.json").exists(), kept
    assert client.get("/posted").json()["meetings"][0]["full"]["url"] == "https://youtu.be/dQw4w9WgXcQ"


def test_a_job_in_the_queue_is_never_cleared(monkeypatch):
    _finished_job("queued-rerun")
    monkeypatch.setattr(api_main.job_queue, "contains", lambda job_id: job_id == "queued-rerun")
    body = TestClient(api_main.app).post("/storage/clear-jobs").json()
    assert body["deleted"] == 0
    assert (get_settings().jobs_dir / "queued-rerun" / "job.json").exists()
