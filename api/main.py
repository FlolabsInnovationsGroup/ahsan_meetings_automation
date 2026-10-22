import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from core.logging import configure_logging, get_logger
from core.models import EditMode, JobRecord, JobStatus, YoutubePrivacy
from ingest.store import store_transcript, store_video
from pipeline import run_pipeline, run_youtube_pipeline
from publish import delete_job_files, youtube
from publish.youtube import YoutubeNotConnectedError

from .jobs import job_store

configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    interrupted = job_store.fail_interrupted()
    if interrupted:
        logger.warning("marked %d interrupted job(s) as failed after restart", interrupted)
    yield


app = FastAPI(title="Meeting Crosstalk Remover / Highlight Cutter", lifespan=lifespan)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class YoutubeJobRequest(BaseModel):
    url: str
    mode: EditMode = "highlights"


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/privacy", response_class=HTMLResponse)
async def privacy() -> str:
    """Public privacy policy (linked from the Google OAuth consent screen)."""
    return (WEB_DIR / "privacy.html").read_text(encoding="utf-8")


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness probe for the container healthcheck."""
    return {"status": "ok"}


@app.post("/jobs", response_model=JobRecord)
async def create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    mode: EditMode = Form("crosstalk"),  # noqa: B008
    transcript: UploadFile | None = None,
) -> JobRecord:
    try:
        job_id, path = store_video(file.filename or "upload.mp4", file.file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    native_transcript_path = None
    if transcript is not None and transcript.filename:
        try:
            native_transcript_path = str(store_transcript(transcript.filename, transcript.file))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = JobRecord(
        job_id=job_id,
        status=JobStatus.QUEUED,
        mode=mode,
        source_path=str(path),
        native_transcript_path=native_transcript_path,
    )
    job_store.create(job)

    background_tasks.add_task(run_pipeline, job, job_store.update)
    return job


@app.post("/jobs/youtube", response_model=JobRecord)
async def create_youtube_job(background_tasks: BackgroundTasks, body: YoutubeJobRequest) -> JobRecord:
    url = body.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    job = JobRecord(job_id=uuid.uuid4().hex, status=JobStatus.QUEUED, mode=body.mode, source_url=url)
    job_store.create(job)

    background_tasks.add_task(run_youtube_pipeline, job, job_store.update, url)
    return job


@app.get("/jobs/{job_id}", response_model=JobRecord)
async def get_job(job_id: str) -> JobRecord:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/jobs/{job_id}/video")
async def get_job_video(job_id: str) -> FileResponse:
    job = _require_done(job_id)
    if not job.output_video_path:
        raise HTTPException(status_code=404, detail="output video not available")
    return FileResponse(job.output_video_path, media_type="video/mp4")


@app.get("/jobs/{job_id}/edl")
async def get_job_edl(job_id: str) -> FileResponse:
    job = _require_done(job_id)
    if not job.edl_path:
        raise HTTPException(status_code=404, detail="EDL not available")
    return FileResponse(job.edl_path, media_type="application/json")


@app.get("/jobs/{job_id}/transcript")
async def get_job_transcript(job_id: str, clean: bool = True) -> FileResponse:
    job = _require_done(job_id)
    path = job.clean_transcript_path if clean else job.transcript_path
    if not path:
        raise HTTPException(status_code=404, detail="transcript not available")
    return FileResponse(path, media_type="application/json")


class PublishRequest(BaseModel):
    # YouTube's own limits: 100 characters for titles, 5000 bytes for descriptions.
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=5000)
    privacy: YoutubePrivacy = "private"
    delete_after: bool = True


@app.post("/jobs/{job_id}/publish", response_model=JobRecord)
async def publish_to_youtube(job_id: str, body: PublishRequest, background_tasks: BackgroundTasks) -> JobRecord:
    job = _require_done(job_id)
    if job.files_deleted or not job.output_video_path:
        raise HTTPException(status_code=409, detail="the video was already removed from the server")
    if job.youtube_status == "uploading":
        raise HTTPException(status_code=409, detail="already uploading")
    if not youtube.youtube_status()["connected"]:
        raise HTTPException(status_code=409, detail="no YouTube channel connected yet")
    title = body.title.strip()
    if not title or "<" in title or ">" in title:
        # YouTube rejects titles containing angle brackets.
        raise HTTPException(status_code=400, detail="title must not be empty or contain < or >")

    job.youtube_status = "uploading"
    job.youtube_progress = 0
    job.youtube_error = None
    job_store.update(job)
    background_tasks.add_task(
        youtube.publish_job, job, job_store.update, title, body.description, body.privacy, body.delete_after
    )
    return job


@app.delete("/jobs/{job_id}/files", response_model=JobRecord)
async def delete_files(job_id: str) -> JobRecord:
    """Remove a finished job's videos and transcripts from the server, keep its record."""
    job = _require_finished(job_id)
    delete_job_files(job)
    job_store.update(job)
    return job


@app.delete("/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str) -> Response:
    """Remove a finished job completely (files and record)."""
    job = _require_finished(job_id)
    delete_job_files(job)
    job_store.delete(job_id)
    return Response(status_code=204)


@app.get("/youtube/status")
async def get_youtube_status() -> dict:
    return youtube.youtube_status()


@app.get("/youtube/connect")
async def youtube_connect(return_to: str = "/") -> RedirectResponse:
    try:
        url = youtube.start_connect(return_to)
    except YoutubeNotConnectedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(url, status_code=302)


@app.get("/youtube/callback")
async def youtube_callback(state: str = "", code: str = "", error: str = "") -> RedirectResponse:
    if error:
        return RedirectResponse(f"/?youtube_error={quote(error)}", status_code=302)
    try:
        return_to = youtube.finish_connect(state, code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(return_to, status_code=302)


@app.post("/youtube/disconnect")
async def youtube_disconnect() -> dict:
    youtube.disconnect()
    return youtube.youtube_status()


def _require_finished(job_id: str) -> JobRecord:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in (JobStatus.DONE, JobStatus.FAILED, JobStatus.SKIPPED_DESYNC):
        raise HTTPException(status_code=409, detail="job is still processing")
    if job.youtube_status == "uploading":
        raise HTTPException(status_code=409, detail="job is uploading to YouTube")
    return job


def _require_done(job_id: str) -> JobRecord:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != JobStatus.DONE:
        raise HTTPException(status_code=409, detail=f"job is not done yet (status={job.status.value})")
    return job
