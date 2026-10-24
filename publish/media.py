"""Where the file of an upload is: on disk, or still inside bundle.zip.

With SERVE_INDIVIDUAL_ARTIFACTS=false (the server setting) only the zip and
the manifest stay on disk once a job is done, so the final video and the
shorts are unpacked from the zip, one at a time, just for their upload. mp4
files are STORED in the zip (bundle/package.py), so unpacking is a plain copy.
"""

from __future__ import annotations

import shutil
import zipfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from core.config import get_settings
from core.models import JobRecord
from core.naming import FINAL_KEY

CHUNK = 1024 * 1024
# Free space to keep on top of the unpacked file, at least. Publishing runs
# beside the render queue, so it also leaves the room a new job needs to start
# (MIN_FREE_DISK_BYTES): unpacking must never starve a meeting being cut.
UNPACK_HEADROOM = 512 * 1024**2


class MediaMissing(RuntimeError):
    """The file to upload is gone; the message is for the person publishing."""


def short_member(index: int) -> str:
    return f"shorts/short_{index:02d}.mp4"


def final_member(job: JobRecord) -> str | None:
    """The final video's name in the zip (Final_<Meeting>_<date>_Youtube.mp4;
    just "final.mp4" for old jobs), or None when the job has none."""
    info = job.artifacts.get(FINAL_KEY)
    if info is not None and info.status == "ok":
        return info.path
    return Path(job.output_video_path).name if job.output_video_path else None


def available(job: JobRecord, members: Iterable[str]) -> set[str]:
    """Which of `members` can be uploaded now: on disk, or inside bundle.zip."""
    members = list(members)
    if job.files_deleted:
        return set()
    found = {m for m in members if any(p.is_file() for p in _on_disk(job, m))}
    missing = [m for m in members if m not in found]
    if missing and job.bundle_path and Path(job.bundle_path).is_file():
        try:
            with zipfile.ZipFile(job.bundle_path) as zf:
                names = set(zf.namelist())
        except (zipfile.BadZipFile, OSError):
            names = set()
        found |= {m for m in missing if m in names}
    return found


@contextmanager
def open_media(job: JobRecord, member: str) -> Iterator[Path]:
    """The file to upload: the one on disk, else a copy unpacked from
    bundle.zip that is deleted again afterwards."""
    for path in _on_disk(job, member):
        if path.is_file():
            yield path
            return
    bundle = Path(job.bundle_path) if job.bundle_path else None
    if bundle is None or not bundle.is_file():
        raise MediaMissing(f"{Path(member).name} is no longer on the server.")
    try:
        zf = zipfile.ZipFile(bundle)
    except (zipfile.BadZipFile, OSError) as exc:
        raise MediaMissing(f"Can't read the job's zip file: {exc}") from exc
    with zf:
        try:
            info = zf.getinfo(member)
        except KeyError as exc:
            raise MediaMissing(f"{Path(member).name} is not in the job's zip file.") from exc
        settings = get_settings()
        job_dir = settings.jobs_dir / job.job_id
        free = shutil.disk_usage(job_dir).free
        needed = info.file_size + max(UNPACK_HEADROOM, settings.min_free_disk_bytes)
        if free < needed:
            raise MediaMissing(
                f"Not enough free disk space to unpack {Path(member).name} for the upload "
                f"({free / 1024**3:.1f} GiB free, {needed / 1024**3:.1f} GiB needed)."
            )
        work = job_dir / "publish_tmp"
        # whatever an earlier run left there (a restart mid-upload) is garbage:
        # a job's uploads never overlap, and this run holds no other copy yet
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        target = work / Path(member).name
        try:
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
            yield target
        finally:
            target.unlink(missing_ok=True)
            try:
                work.rmdir()
            except OSError:
                pass  # another file of this run is still there


def _on_disk(job: JobRecord, member: str) -> list[Path]:
    paths = [get_settings().jobs_dir / job.job_id / member]
    if job.output_video_path and member == final_member(job):
        paths.insert(0, Path(job.output_video_path))
    return paths
