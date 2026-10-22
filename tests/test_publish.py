import json

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from api.jobs import JobStore
from core.config import get_settings
from core.models import JobRecord, JobStatus
from publish import delete_job_files
from publish import youtube as yt


def _done_job(job_id="pub-1") -> JobRecord:
    """A finished job with a real source video, output and transcripts on disk."""
    settings = get_settings()
    source = settings.videos_dir / f"{job_id}.mp4"
    source.write_bytes(b"source")
    uploaded_vtt = settings.transcripts_dir / f"{job_id}.vtt"
    uploaded_vtt.write_text("WEBVTT")
    job_dir = settings.jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    out = job_dir / "output.mp4"
    out.write_bytes(b"cut")
    (job_dir / "transcript.json").write_text("[]")
    (job_dir / "edl.json").write_text("{}")
    job = JobRecord(
        job_id=job_id,
        status=JobStatus.DONE,
        source_path=str(source),
        native_transcript_path=str(uploaded_vtt),
        output_video_path=str(out),
        transcript_path=str(job_dir / "transcript.json"),
        edl_path=str(job_dir / "edl.json"),
    )
    api_main.job_store.create(job)
    return job


def _connect_fake_channel(title="FloLabs"):
    path = get_settings().storage_dir / "youtube_token.json"
    path.write_text(json.dumps({"refresh_token": "x", "channel_title": title}))


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "id")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://cutter.example.com/")
    get_settings.cache_clear()


def test_status_reports_not_configured_and_not_connected():
    assert yt.youtube_status() == {"configured": False, "connected": False, "channel_title": None}


def test_status_reports_connected_channel(configured):
    _connect_fake_channel("FloLabs Channel")
    status = TestClient(api_main.app).get("/youtube/status").json()
    assert status == {"configured": True, "connected": True, "channel_title": "FloLabs Channel"}


def test_publish_requires_connected_channel():
    job = _done_job()
    res = TestClient(api_main.app).post(f"/jobs/{job.job_id}/publish", json={"title": "Weekly sync"})
    assert res.status_code == 409


def test_publish_rejects_unfinished_job():
    _connect_fake_channel()
    api_main.job_store.create(JobRecord(job_id="busy", status=JobStatus.SLICING, source_path="x.mp4"))
    res = TestClient(api_main.app).post("/jobs/busy/publish", json={"title": "x"})
    assert res.status_code == 409


def test_publish_rejects_angle_brackets_in_title():
    _connect_fake_channel()
    job = _done_job()
    res = TestClient(api_main.app).post(f"/jobs/{job.job_id}/publish", json={"title": "a <b>"})
    assert res.status_code == 400


def test_publish_uploads_then_removes_files(monkeypatch):
    _connect_fake_channel()
    job = _done_job()
    calls = {}

    def fake_upload(path, title, description, privacy, on_progress=None):
        calls.update(path=path.read_bytes(), title=title, privacy=privacy)
        on_progress(50)
        return "abc123"

    monkeypatch.setattr(yt, "upload_video", fake_upload)
    res = TestClient(api_main.app).post(
        f"/jobs/{job.job_id}/publish",
        json={"title": "Weekly sync", "description": "notes", "privacy": "unlisted"},
    )
    assert res.status_code == 200
    assert calls == {"path": b"cut", "title": "Weekly sync", "privacy": "unlisted"}

    reread = JobStore().get(job.job_id)  # from disk, as after a restart
    assert reread.youtube_status == "published"
    assert reread.youtube_url == "https://youtu.be/abc123"
    assert reread.files_deleted is True
    assert reread.output_video_path is None

    settings = get_settings()
    assert not list(settings.videos_dir.iterdir())
    assert not list(settings.transcripts_dir.iterdir())
    assert [p.name for p in (settings.jobs_dir / job.job_id).iterdir()] == ["job.json"]


def test_publish_can_keep_files(monkeypatch):
    _connect_fake_channel()
    job = _done_job()
    monkeypatch.setattr(yt, "upload_video", lambda *a, **k: "keep1")
    TestClient(api_main.app).post(f"/jobs/{job.job_id}/publish", json={"title": "t", "delete_after": False})
    reread = JobStore().get(job.job_id)
    assert reread.youtube_status == "published"
    assert reread.files_deleted is False
    assert (get_settings().jobs_dir / job.job_id / "output.mp4").exists()


def test_failed_upload_keeps_files_and_reports_error(monkeypatch):
    _connect_fake_channel()
    job = _done_job()

    def boom(*a, **k):
        raise RuntimeError("quotaExceeded")

    monkeypatch.setattr(yt, "upload_video", boom)
    TestClient(api_main.app).post(f"/jobs/{job.job_id}/publish", json={"title": "t"})
    reread = JobStore().get(job.job_id)
    assert reread.youtube_status == "failed"
    assert "limit" in reread.youtube_error
    assert reread.files_deleted is False
    assert (get_settings().jobs_dir / job.job_id / "output.mp4").exists()


def test_delete_files_endpoint_keeps_record():
    job = _done_job()
    res = TestClient(api_main.app).delete(f"/jobs/{job.job_id}/files")
    assert res.status_code == 200
    assert res.json()["files_deleted"] is True
    assert TestClient(api_main.app).get(f"/jobs/{job.job_id}").status_code == 200
    assert TestClient(api_main.app).get(f"/jobs/{job.job_id}/video").status_code == 404


def test_delete_job_endpoint_removes_everything():
    job = _done_job()
    client = TestClient(api_main.app)
    assert client.delete(f"/jobs/{job.job_id}").status_code == 204
    assert client.get(f"/jobs/{job.job_id}").status_code == 404
    assert not (get_settings().jobs_dir / job.job_id).exists()


def test_cannot_delete_job_while_processing():
    api_main.job_store.create(JobRecord(job_id="running", status=JobStatus.DECIDING, source_path="x.mp4"))
    assert TestClient(api_main.app).delete("/jobs/running").status_code == 409


def test_delete_job_files_never_touches_paths_outside_storage(tmp_path):
    outside = tmp_path / "precious.mp4"
    outside.write_bytes(b"keep me")
    job = JobRecord(job_id="evil", status=JobStatus.DONE, source_path=str(outside))
    delete_job_files(job)
    assert outside.exists()


def test_restart_marks_interrupted_upload_failed_but_keeps_job_done():
    JobStore().create(
        JobRecord(job_id="up", status=JobStatus.DONE, source_path="x.mp4", youtube_status="uploading")
    )
    assert JobStore().fail_interrupted() == 1
    reread = JobStore().get("up")
    assert reread.status == JobStatus.DONE
    assert reread.youtube_status == "failed"


def test_return_to_only_allows_local_paths():
    assert yt._safe_return_to("/#job=abc") == "/#job=abc"
    assert yt._safe_return_to("//evil.com") == "/"
    assert yt._safe_return_to("https://evil.com") == "/"
    assert yt._safe_return_to(None) == "/"


def test_connect_requires_configuration():
    res = TestClient(api_main.app).get("/youtube/connect", follow_redirects=False)
    assert res.status_code == 409


def test_connect_redirects_to_google_with_offline_access(configured):
    pytest.importorskip("google_auth_oauthlib")
    res = TestClient(api_main.app).get("/youtube/connect?return_to=/%23job=abc", follow_redirects=False)
    assert res.status_code == 302
    location = res.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/auth")
    assert "access_type=offline" in location
    assert "redirect_uri=https%3A%2F%2Fcutter.example.com%2Fyoutube%2Fcallback" in location
    assert "youtube.upload" in location


def test_callback_rejects_unknown_state(configured):
    res = TestClient(api_main.app).get("/youtube/callback?state=nope&code=x", follow_redirects=False)
    assert res.status_code == 400
