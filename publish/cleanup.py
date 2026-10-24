import shutil
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger
from core.models import JobRecord

logger = get_logger(__name__)

# A job folder holding this file is never deleted (api/queue.py KEEP_MARKER).
KEEP_MARKER = ".keep"


def delete_job_files(job: JobRecord) -> bool:
    """Remove a job's recordings, transcripts and outputs from the server.

    Keeps only job.json (a few hundred bytes) so the job, and its YouTube
    links if it was published, can still be looked up. Paths outside the
    storage folder are never touched, and neither is a `.keep`-protected job
    (returns False then). Returns True once the files are gone.
    """
    settings = get_settings()
    storage = settings.storage_dir.resolve()
    job_dir = (settings.jobs_dir / job.job_id).resolve()
    if (job_dir / KEEP_MARKER).exists():
        logger.warning("job %s has a %s marker: its files stay on the server", job.job_id, KEEP_MARKER)
        return False

    for raw in (job.source_path, job.native_transcript_path):
        if raw:
            _remove_inside(Path(raw), storage)

    if job_dir.is_dir():
        for child in job_dir.iterdir():
            if child.name == "job.json":
                continue
            _remove_inside(child, storage)

    # nothing these point at exists any more: routes answer "not available"
    # instead of failing inside a FileResponse
    job.output_video_path = None
    job.edl_path = None
    job.removed_edl_path = None
    job.render_manifest_path = None
    job.transcript_path = None
    job.clean_transcript_path = None
    job.decisions_path = None
    job.selection_path = None
    job.chapters_path = None
    job.bundle_path = None
    for info in job.artifacts.values():
        info.on_disk = False
    job.files_deleted = True
    return True


def _remove_inside(path: Path, root: Path) -> None:
    resolved = path.resolve()
    if root not in resolved.parents:
        logger.warning("refusing to delete %s: outside storage dir", resolved)
        return
    if resolved.is_dir():
        shutil.rmtree(resolved, ignore_errors=True)
    else:
        resolved.unlink(missing_ok=True)
