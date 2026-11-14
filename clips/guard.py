"""The brakes: folder isolation, the kill switch, the channel check, the breaker.

Nothing here talks to YouTube. The uploader calls these before it sends a byte.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from clips.config import MARKER, ClipsSettings, clips_settings


class GuardError(RuntimeError):
    """A guard refused. The message says why, in plain words."""


# ----------------------------------------------------------------- the folder

def init_storage(path: Path) -> None:
    """Make the clips data folder and mark it. Refuses the company app's folder."""
    path = Path(path)
    why = _looks_like_company_data(path)
    if why:
        raise GuardError(
            f"{path} looks like the company app's data folder ({why}). "
            "Pick a new folder for the clips data, for example storage-clips."
        )
    path.mkdir(parents=True, exist_ok=True)
    (path / MARKER).write_text("clips\n", encoding="utf-8")


def looks_like_company_data(path: Path) -> str:
    """Why this folder looks like the company app's data folder, or ''."""
    return _looks_like_company_data(Path(path))


def _looks_like_company_data(path: Path) -> str:
    if (path / "state" / "autopilot.json").exists():
        return "it holds state/autopilot.json"
    jobs = path / "jobs"
    if jobs.is_dir() and any(jobs.glob("*/job.json")):
        return "it holds jobs with job.json files"
    return ""


def assert_isolated() -> None:
    """The marker is in the clips folder and the app's STORAGE_DIR is that same folder.

    The reused app code keeps its YouTube token, journal and quota file under
    STORAGE_DIR, so a different value would put them in the company's folder."""
    settings = clips_settings()
    folder = settings.storage_dir
    if not settings.marker_path.exists():
        raise GuardError(
            f"{folder} is not marked as the clips folder. Run `python -m clips init` "
            "(or set CLIPS_STORAGE_DIR to the folder made by it)."
        )
    from core.config import get_settings

    app_dir = get_settings().storage_dir
    if _resolve(app_dir) != _resolve(folder):
        raise GuardError(
            f"STORAGE_DIR is {app_dir} but the clips folder is {folder}. "
            f"Start the clips process with STORAGE_DIR={folder} so the YouTube token, "
            "upload journal and quota file stay inside the clips folder."
        )


def _resolve(path: Path) -> Path:
    return Path(path).expanduser().resolve()


# ---------------------------------------------------------------- kill switch

def kill_switch_on() -> bool:
    return clips_settings().kill_path.exists()


def kill_reason() -> str:
    try:
        return clips_settings().kill_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def set_kill_switch(on: bool, reason: str = "") -> dict | None:
    """Switch posting off (a file whose text is the reason) or back on.

    Switching on also takes uploaded, not-yet-public videos back to private, and returns what
    happened: {"pulled_back": [clip ids], "failed": [clip ids], "skipped": reason or ""}."""
    path = clips_settings().kill_path
    if on:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(reason or "stopped by hand", encoding="utf-8")
        return _pull_back_best_effort()
    path.unlink(missing_ok=True)
    return None


def pending_releases(store, now: datetime | None = None) -> list:
    """Uploaded clips that are private on YouTube but will go public later: the ones the
    kill switch cannot stop by itself. Earliest first."""
    from clips.models import ClipStatus

    now = now or datetime.now(UTC)
    found = []
    for clip in store.list_clips(status=ClipStatus.UPLOADED):
        when = clip.publish_at
        if when is None or not clip.youtube_id:
            continue
        when = when if when.tzinfo else when.replace(tzinfo=UTC)
        if when > now:
            found.append((when, clip))
    return [clip for _, clip in sorted(found, key=lambda pair: pair[0])]


def _pull_back_best_effort() -> dict:
    """Switching posting off also takes already-uploaded, not-yet-public videos back to private.
    It never raises: the switch itself is already on."""
    try:
        settings = clips_settings()
        if not settings.db_path.exists():
            return _no_pull_back()
        from clips.store import Store

        return pull_back_now(Store(settings.db_path))
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("kill switch could not pull back scheduled videos: %s", exc)
        return _no_pull_back(str(exc))


def _no_pull_back(skipped: str = "") -> dict:
    return {"pulled_back": [], "failed": [], "skipped": skipped}


def pull_back_now(store) -> dict:
    """Take every uploaded clip that is still private-and-scheduled back to private with no release
    time. Never raises. Does nothing when this process is not on the clips folder: the YouTube token
    it would read (and maybe refresh and rewrite) would be the company's."""
    try:
        if not pending_releases(store):
            return _no_pull_back()
        try:
            assert_isolated()
        except GuardError as exc:
            message = f"Kill switch: scheduled videos were NOT pulled back, this process is not on the clips folder. {exc}"
            store.add_event("kill", message)
            return _no_pull_back(str(exc))
        from clips import uploader

        done, failed = uploader.pull_back_pending(store)
        store.add_event(
            "kill",
            f"Kill switch: {len(done)} scheduled video(s) pulled back to private, {len(failed)} could not be.",
            data={"pulled_back": done, "failed": failed},
        )
        return {"pulled_back": done, "failed": failed, "skipped": ""}
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("could not pull back scheduled videos: %s", exc)
        return _no_pull_back(str(exc))


# -------------------------------------------------------------------- channel

def check_channel(channel_id: str, settings: ClipsSettings | None = None) -> None:
    """Refuse anything but the one configured clips channel."""
    settings = settings or clips_settings()
    if not channel_id:
        raise GuardError("No YouTube channel is connected, so nothing is uploaded.")
    if not settings.channel_id:
        raise GuardError("CLIPS_CHANNEL_ID is not set. Set it to the clips channel's id (UC...) before uploading.")
    if channel_id in settings.forbidden_ids:
        raise GuardError(f"Channel {channel_id} is on the never-post-to list.")
    if channel_id != settings.channel_id:
        raise GuardError(
            f"The connected channel {channel_id} is not the configured clips channel {settings.channel_id}."
        )


# -------------------------------------------------------------------- breaker

def note_upload_result(store, ok: bool, detail: str = "") -> None:
    """Count an upload. A success resets the run of failures; it never closes a strike breaker."""
    if ok:
        store.set_flag("upload_failures", "0")
        return
    failures = _int(store.get_flag("upload_failures", "0")) + 1
    store.set_flag("upload_failures", str(failures))
    limit = clips_settings().max_upload_failures
    if failures >= limit and not breaker_open(store):
        _open(store, f"{failures} uploads failed in a row. Last problem: {detail}".strip())


def note_strike(store, detail: str) -> None:
    """A copyright strike or claim was seen: stop at once."""
    _open(store, f"Copyright strike or claim: {detail}".strip())


def breaker_open(store) -> bool:
    return store.get_flag("breaker_open", "") == "1"


def breaker_reason(store) -> str:
    return store.get_flag("breaker_reason", "")


def reset_breaker(store) -> None:
    store.set_flag("breaker_open", "")
    store.set_flag("breaker_reason", "")
    store.set_flag("upload_failures", "0")
    store.add_event("breaker", "Breaker closed by hand.")


def _open(store, reason: str) -> None:
    store.set_flag("breaker_open", "1")
    store.set_flag("breaker_reason", reason)
    store.set_flag("breaker_opened_at", datetime.now(UTC).isoformat())
    store.add_event("breaker", f"Breaker opened. {reason}")


def _int(text: str) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        return 0
