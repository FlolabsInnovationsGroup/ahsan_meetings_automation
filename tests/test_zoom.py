import time

from fastapi.testclient import TestClient

import api.main as api_main
import pipeline
from core.config import get_settings
from core.models import JobRecord, JobStatus
from ingest import zoom
from transcribe import load_uploaded_transcript

ZOOM_VTT = """WEBVTT

1
00:00:01.000 --> 00:00:04.500
Mohamed: Hello everyone, let's start with the budget.

2
00:00:05.000 --> 00:00:08.000
Antoine: Thanks. I have an update on the sensors.
"""


def _meeting(uuid="abc==", files=None, topic="Weekly sync"):
    return {
        "uuid": uuid,
        "topic": topic,
        "start_time": "2026-10-22T20:00:00Z",
        "duration": 71,
        "recording_files": files if files is not None else [
            {"file_type": "MP4", "recording_type": "gallery_view", "status": "completed",
             "file_size": 104857600, "download_url": "https://zoom.example/gallery"},
            {"file_type": "MP4", "recording_type": "shared_screen_with_speaker_view", "status": "completed",
             "file_size": 209715200, "download_url": "https://zoom.example/shared"},
            {"file_type": "M4A", "recording_type": "audio_only", "status": "completed",
             "download_url": "https://zoom.example/audio"},
            {"file_type": "CC", "recording_type": "closed_caption", "status": "completed",
             "download_url": "https://zoom.example/cc"},
            {"file_type": "TRANSCRIPT", "recording_type": "audio_transcript", "status": "completed",
             "download_url": "https://zoom.example/transcript"},
        ],
    }


def _configure(monkeypatch):
    monkeypatch.setenv("ZOOM_ACCOUNT_ID", "acct")
    monkeypatch.setenv("ZOOM_CLIENT_ID", "cid")
    monkeypatch.setenv("ZOOM_CLIENT_SECRET", "secret")
    get_settings.cache_clear()


def test_picks_best_layout_and_real_transcript():
    files = _meeting()["recording_files"]
    assert zoom._pick_mp4(files)["download_url"] == "https://zoom.example/shared"
    assert zoom._pick_transcript(files)["file_type"] == "TRANSCRIPT"


def test_falls_back_to_closed_captions():
    files = [f for f in _meeting()["recording_files"] if f["file_type"] != "TRANSCRIPT"]
    assert zoom._pick_transcript(files)["file_type"] == "CC"


def test_summary_flags_processing_and_missing_transcript():
    ready = zoom.summarize(_meeting())
    assert ready["ready"] and not ready["processing"]
    assert ready["size_mb"] == 200

    processing = zoom.summarize(_meeting(files=[
        {"file_type": "MP4", "recording_type": "active_speaker", "status": "processing"},
    ]))
    assert processing["processing"] and not processing["ready"]

    no_transcript = zoom.summarize(_meeting(files=[
        {"file_type": "MP4", "recording_type": "active_speaker", "status": "completed"},
    ]))
    assert no_transcript["has_video"] and not no_transcript["ready"]
    assert no_transcript["transcript_type"] is None


def test_double_encodes_awkward_uuids():
    assert zoom._meeting_path_id("abc==") == "abc%3D%3D"
    assert zoom._meeting_path_id("/ab//c=") == "%252Fab%252F%252Fc%253D"


def test_list_recordings_pages_dedupes_and_sorts(monkeypatch):
    calls = []

    def fake_get(path, params=None):
        calls.append((path, dict(params)))
        if "next_page_token" not in params and len(calls) == 1:
            return {"meetings": [_meeting("old", topic="Old")], "next_page_token": "p2"}
        if params.get("next_page_token") == "p2":
            newer = _meeting("new", topic="New")
            newer["start_time"] = "2026-10-23T09:00:00Z"
            return {"meetings": [newer, _meeting("old", topic="Old")], "next_page_token": ""}
        return {"meetings": []}

    monkeypatch.setattr(zoom, "_api_get", fake_get)
    result = zoom.list_recordings(days=30)

    assert [m["uuid"] for m in result] == ["new", "old"]
    assert calls[0][0] == "users/me/recordings"
    assert calls[0][1]["page_size"] == 300


def test_download_meeting_saves_video_and_transcript(monkeypatch):
    monkeypatch.setattr(zoom, "get_meeting", lambda uuid: _meeting(uuid))
    fetched = []

    def fake_download(url, dest):
        fetched.append(url)
        dest.write_text(ZOOM_VTT if dest.suffix == ".vtt" else "video", encoding="utf-8")

    monkeypatch.setattr(zoom, "_download", fake_download)
    video, transcript, summary = zoom.download_meeting("abc==")

    assert fetched == ["https://zoom.example/transcript", "https://zoom.example/shared"]
    assert video.parent == get_settings().videos_dir and video.suffix == ".mp4"
    assert transcript.parent == get_settings().transcripts_dir
    assert summary["topic"] == "Weekly sync"


def test_download_meeting_refuses_without_transcript(monkeypatch):
    files = [{"file_type": "MP4", "recording_type": "active_speaker", "status": "completed",
              "download_url": "https://zoom.example/v"}]
    monkeypatch.setattr(zoom, "get_meeting", lambda uuid: _meeting(uuid, files=files))
    try:
        zoom.download_meeting("abc==")
    except zoom.ZoomError as exc:
        assert "transcript" in str(exc)
    else:
        raise AssertionError("expected ZoomError")


def test_zoom_transcript_parses_as_native_transcript(tmp_path):
    path = tmp_path / "zoom.vtt"
    path.write_text(ZOOM_VTT, encoding="utf-8")
    parsed = load_uploaded_transcript(path)
    assert parsed is not None
    _, segments = parsed
    assert segments
    assert "budget" in " ".join(s.text for s in segments)


def test_zoom_pipeline_hands_files_to_main_pipeline(monkeypatch, tmp_path):
    video, vtt = tmp_path / "v.mp4", tmp_path / "t.vtt"
    monkeypatch.setattr(pipeline.zoom, "download_meeting", lambda uuid: (video, vtt, {}))
    seen = {}

    def fake_run(job, update):
        seen["source"], seen["transcript"] = job.source_path, job.native_transcript_path

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run)
    job = JobRecord(job_id="j1", zoom_meeting_uuid="abc==")
    pipeline.run_zoom_pipeline(job, lambda j: None)
    assert seen == {"source": str(video), "transcript": str(vtt)}


def test_zoom_pipeline_marks_download_failure(monkeypatch):
    def boom(uuid):
        raise zoom.ZoomError("Zoom download failed (403)")

    monkeypatch.setattr(pipeline.zoom, "download_meeting", boom)
    job = JobRecord(job_id="j2", zoom_meeting_uuid="abc==")
    pipeline.run_zoom_pipeline(job, lambda j: None)
    assert job.status == JobStatus.FAILED
    assert "403" in job.error


def test_recordings_endpoint_when_not_configured():
    client = TestClient(api_main.app)
    assert client.get("/zoom/recordings").json() == {"configured": False, "meetings": []}


def test_recordings_endpoint_lists_meetings(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(zoom, "list_recordings", lambda days=30: [zoom.summarize(_meeting())])
    body = TestClient(api_main.app).get("/zoom/recordings").json()
    assert body["configured"] is True
    assert body["meetings"][0]["ready"] is True


def test_recordings_endpoint_reports_zoom_errors(monkeypatch):
    _configure(monkeypatch)

    def boom(days=30):
        raise zoom.ZoomError("Zoom API failed (401)")

    monkeypatch.setattr(zoom, "list_recordings", boom)
    response = TestClient(api_main.app).get("/zoom/recordings")
    assert response.status_code == 502


def test_create_zoom_job(monkeypatch):
    _configure(monkeypatch)

    def fake_run(job, update):
        job.status = JobStatus.DONE
        update(job)

    monkeypatch.setattr(api_main, "run_zoom_pipeline", fake_run)
    client = TestClient(api_main.app)
    response = client.post(
        "/jobs/zoom", json={"meeting_uuid": "abc==", "mode": "highlights", "title": "Weekly <sync> (2026-10-22)"}
    )
    assert response.status_code == 200
    job = response.json()
    assert job["zoom_meeting_uuid"] == "abc=="
    assert job["source_title"] == "Weekly sync (2026-10-22)"

    for _ in range(50):
        if client.get(f"/jobs/{job['job_id']}").json()["status"] == "done":
            break
        time.sleep(0.05)
    assert client.get(f"/jobs/{job['job_id']}").json()["status"] == "done"


def test_create_zoom_job_requires_config():
    response = TestClient(api_main.app).post("/jobs/zoom", json={"meeting_uuid": "abc=="})
    assert response.status_code == 409
