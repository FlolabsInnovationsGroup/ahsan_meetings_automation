import shutil
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger
from core.models import JobRecord

logger = get_logger(__name__)


def delete_job_files(job: JobRecord) -> None:
    """Remove a job's recordings, transcripts and outputs from the server.

    Keeps only job.json (a few hundred bytes) so the job, and its YouTube link
    if it was published, can still be looked up. Paths outside the storage
    folder are never touched.
    """
    settings = get_settings()
    storage = settings.storage_dir.resolve()
    job_dir = (settings.jobs_dir / job.job_id).resolve()

    for raw in (job.source_path, job.native_transcript_path):
        if raw:
            _remove_inside(Path(raw), storage)

    if job_dir.is_dir():
        for child in job_dir.iterdir():
            if child.name == "job.json":
                continue
            _remove_inside(child, storage)

    job.output_video_path = None
    job.edl_path = None
    job.transcript_path = None
    job.clean_transcript_path = None
    job.files_deleted = True


def _remove_inside(path: Path, root: Path) -> None:
    resolved = path.resolve()
    if root not in resolved.parents:
        logger.warning("refusing to delete %s: outside storage dir", resolved)
        return
    if resolved.is_dir():
        shutil.rmtree(resolved, ignore_errors=True)
    else:
        resolved.unlink(missing_ok=True)
