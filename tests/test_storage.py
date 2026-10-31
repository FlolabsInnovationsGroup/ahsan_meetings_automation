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


# ------------------------------------------------------------------ automatic deletion


def _aged(job: JobRecord, hours: float, now) -> None:
    from datetime import timedelta

    job.updated_at = now - timedelta(hours=hours)  # the cached record, as the sweep reads it


def test_finished_jobs_untouched_for_the_chosen_hours_are_deleted():
    from datetime import UTC, datetime, timedelta

    from ledger import prefs

    prefs.set_retention_hours(30)
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    jobs_dir = get_settings().jobs_dir
    old, fresh = _finished_job("old1"), _finished_job("fresh1")
    busy = _job("busy1", JobStatus.DONE, youtube_status="uploading")
    running = _job("running2", JobStatus.SLICING)
    for job, hours in ((old, 31), (fresh, 29), (busy, 40), (running, 40)):
        _aged(job, hours, now)
    assert api_main.sweep_old_jobs(now) == 1
    assert not (jobs_dir / "old1").exists()
    for kept in ("fresh1", "busy1", "running2"):
        assert (jobs_dir / kept / "job.json").exists(), kept
    prefs.set_retention_hours(0)  # never
    assert api_main.sweep_old_jobs(now + timedelta(days=30)) == 0


def test_the_hours_are_chosen_on_the_page_saved_and_applied_at_once(monkeypatch):
    from datetime import UTC, datetime

    client = TestClient(api_main.app)
    assert client.get("/storage").json()["retention_hours"] == 0  # tests run with JOB_RETENTION_HOURS=0
    _aged(_finished_job("week-old"), 24 * 7, datetime.now(UTC))
    body = client.post("/storage/retention", json={"hours": 48}).json()
    assert body["retention_hours"] == 48 and body["old_jobs"] == 0  # the week-old job went at once
    assert not (get_settings().jobs_dir / "week-old").exists()
    saved = (get_settings().storage_dir / "state" / "prefs.json").read_text(encoding="utf-8")
    assert '"job_retention_hours": 48' in saved
    assert client.post("/storage/retention", json={"hours": -1}).status_code == 422


def test_the_page_choice_wins_over_env_and_env_is_the_default(monkeypatch):
    from ledger import prefs

    monkeypatch.setenv("JOB_RETENTION_HOURS", "30")
    get_settings.cache_clear()
    assert prefs.retention_hours() == 30
    prefs.set_retention_hours(12.5)
    assert prefs.retention_hours() == 12.5


def test_below_4_gb_free_the_oldest_finished_jobs_go_first_until_there_is_room(monkeypatch):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    gb = 1024**3
    disk = {"free": 3.0 * gb}
    monkeypatch.setattr(api_main.shutil, "disk_usage", lambda path: SimpleNamespace(free=disk["free"]))
    real_delete = api_main.delete_job_files

    def delete_and_free(store, job_id):
        real_delete(store, job_id)
        disk["free"] += 0.6 * gb  # each deleted job frees 0.6 GB

    monkeypatch.setattr(api_main, "delete_job_files", delete_and_free)
    now = datetime.now(UTC)
    for name, hours in (("newest", 1), ("oldest", 50), ("middle", 20)):
        _aged(_finished_job(name), hours, now)
    busy = _job("uploading2", JobStatus.DONE, youtube_status="uploading")
    _aged(busy, 99, now)
    assert api_main.free_space_if_low() == 2  # 3.0 -> 3.6 -> 4.2 GB: stops there
    jobs_dir = get_settings().jobs_dir
    assert not (jobs_dir / "oldest").exists() and not (jobs_dir / "middle").exists()
    assert (jobs_dir / "newest" / "job.json").exists() and (jobs_dir / "uploading2" / "job.json").exists()
    assert api_main.free_space_if_low() == 0  # enough room now


def test_startup_uses_the_hours_chosen_on_the_page():
    import json
    from datetime import UTC, datetime, timedelta

    from api.jobs import JobStore
    from api.queue import reconcile_on_startup

    _finished_job("startup-old")
    path = get_settings().jobs_dir / "startup-old" / "job.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["updated_at"] = (datetime.now(UTC) - timedelta(hours=10)).isoformat()  # last touched 10 h ago
    path.write_text(json.dumps(record), encoding="utf-8")
    assert reconcile_on_startup(JobStore(), retention_hours=12)["deleted"] == 0
    assert reconcile_on_startup(JobStore(), retention_hours=8)["deleted"] == 1
