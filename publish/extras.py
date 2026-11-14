"""The follow-up queue: thumbnails, caption tracks and playlist adds run
later, from a saved list, and never inside an upload, so they can never make
an upload fail or slow it down.

Feature modules (publish.thumbnail, publish.caption_track, publish.playlists)
call register(kind, builder, runner) when imported:

- builder(job, item, posted, *, video_kind, index, cfg) -> Task | None runs
  right after an upload and copies what it needs into state/extras/ (see
  stash_file), since the job folder may be deleted before the task runs;
- runner(task) -> optional.Outcome does the call (through optional.call).

Before every task run_due re-checks the never-post list and that the ledger
still has the video as published, so a meeting added to the list after the
upload is never touched. Tasks wait as needs_reconnect when a permission is
missing and run again when the channel is reconnected (wake).

State: storage_dir/state/extras.json. Files: storage_dir/state/extras/.
"""

from __future__ import annotations

import importlib
import shutil
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from core.logging import get_logger
from ledger import files
from publish import optional, youtube

logger = get_logger(__name__)

KINDS = ("thumbnail", "captions", "playlist")
WAITING, DONE, SKIPPED, NEEDS_RECONNECT, GIVEN_UP = "waiting", "done", "skipped", "needs_reconnect", "given_up"
_FINISHED = (DONE, SKIPPED, GIVEN_UP)
PRUNE_AFTER_DAYS = 60
_FEATURE_MODULES = ("publish.thumbnail", "publish.caption_track", "publish.playlists")
_KIND_WORDS = {"thumbnail": "Thumbnail", "captions": "Caption track", "playlist": "Playlist"}

_lock = threading.RLock()  # the saved list
_run_lock = threading.Lock()  # one run_due at a time
_builders: dict[str, Callable] = {}
_runners: dict[str, Callable] = {}
_loaded = False


@dataclass
class Task:
    kind: str
    item_key: str = ""
    video_id: str = ""
    job_id: str = ""
    meeting: str = ""
    topic: str | None = None
    meeting_id: str | None = None
    series_key: str | None = None
    video_kind: str = ""  # full | short | topic; filled from the upload when blank
    index: int | None = None
    file: str | None = None
    not_before: str = ""
    created_at: str = ""
    attempts: int = 0
    state: str = WAITING
    reason: str = ""
    units: int = 0
    data: dict = field(default_factory=dict)
    id: str = field(default_factory=files.new_id)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")


def _parse(text: str | None) -> datetime | None:
    try:
        value = datetime.fromisoformat(text) if text else None
    except (TypeError, ValueError):
        return None
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value


# ------------------------------------------------------------------ storage


def _path() -> Path:
    return files.state_dir() / "extras.json"


def files_dir() -> Path:
    return files.state_dir() / "extras"


def _read() -> dict:
    data = files.read_json(_path(), None)
    if not isinstance(data, dict):
        data = {}
    names = {f.name for f in fields(Task)}
    tasks = []
    for raw in data.get("tasks") or []:
        if isinstance(raw, dict) and raw.get("kind") and raw.get("id"):
            tasks.append({k: v for k, v in raw.items() if k in names})
    blocked = data.get("blocked") if isinstance(data.get("blocked"), dict) else {}
    return {"tasks": tasks, "blocked": blocked}


def _write(state: dict) -> None:
    files.write_json(_path(), state)


def _task(raw: dict) -> Task:
    return Task(**raw)


def _update(task_id: str, **changes) -> Task | None:
    with _lock:
        state = _read()
        for raw in state["tasks"]:
            if raw["id"] == task_id:
                raw.update(changes)
                _write(state)
                return _task(raw)
    return None


def _get(task_id: str) -> Task | None:
    with _lock:
        for raw in _read()["tasks"]:
            if raw["id"] == task_id:
                return _task(raw)
    return None


def all_tasks() -> list[Task]:
    with _lock:
        return [_task(raw) for raw in _read()["tasks"]]


def stash_file(task_id: str, source: Path | str, ext: str | None = None) -> str:
    """Copy `source` into state/extras/<task_id>.<ext>; returns the new path
    (put it in Task.file)."""
    src = Path(source)
    suffix = ext if ext is not None else src.suffix.lstrip(".")
    dest = files_dir() / (f"{task_id}.{suffix}" if suffix else task_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return str(dest)


def _remove_file(task: Task) -> None:
    if task.file:
        try:
            Path(task.file).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("could not remove %s: %s", task.file, exc)


# ------------------------------------------------------------------ features


def register(kind: str, builder: Callable, runner: Callable) -> None:
    _builders[kind] = builder
    _runners[kind] = runner


def _load_features() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for name in _FEATURE_MODULES:
        try:
            importlib.import_module(name)
        except ImportError as exc:
            # a feature that is not built yet is fine; one that fails inside is logged
            if exc.name != name:
                logger.warning("follow-up feature %s failed to load: %s", name, exc)
        except Exception:
            logger.exception("follow-up feature %s failed to load", name)


def _retry_days() -> int:
    from publish import settings as publishing_settings

    return publishing_settings.current().extras_retry_days


def enqueue_for_upload(job, item, posted, *, video_kind: str, index: int | None = None, cfg=None) -> None:
    """Called right after a video is uploaded. Runs every builder; a builder
    that fails is logged and skipped. Never raises."""
    try:
        _load_features()
        if cfg is None:
            from publish import settings as publishing_settings

            cfg = publishing_settings.current()
        for kind, builder in list(_builders.items()):
            try:
                task = builder(job, item, posted, video_kind=video_kind, index=index, cfg=cfg)
                if task is None:
                    continue
                _add(task, job, item, posted, video_kind, index)
            except Exception:
                logger.exception("follow-up builder %s failed", kind)
    except Exception:
        logger.exception("follow-up queue could not take a video")


def _add(task: Task, job, item, posted, video_kind: str, index: int | None) -> None:
    """Fill what the builder left blank from the upload and save the task."""
    task.item_key = task.item_key or getattr(item, "key", "")
    task.video_id = task.video_id or getattr(posted, "video_id", None) or ""
    task.job_id = task.job_id or getattr(item, "job_id", "") or getattr(job, "id", "") or ""
    task.meeting = task.meeting or getattr(item, "meeting", "") or ""
    if task.topic is None:
        task.topic = getattr(item, "topic", None)
    if task.meeting_id is None:
        task.meeting_id = getattr(item, "meeting_id", None)
    if task.index is None:
        task.index = index
    task.video_kind = task.video_kind or video_kind
    now = _now()
    task.created_at = task.created_at or _iso(now)
    task.not_before = task.not_before or _iso(now)
    with _lock:
        state = _read()
        for raw in state["tasks"]:
            if raw["kind"] == task.kind and raw.get("item_key") == task.item_key and raw.get("state") != GIVEN_UP:
                _remove_file(task)  # the same follow-up is already queued
                return
        state["tasks"].append(asdict(task))
        _write(state)


# ------------------------------------------------------------------ running


def _never_post(task: Task) -> str | None:
    from autopilot import never_post

    if task.meeting and never_post.match(task.meeting):
        return "never-post list"
    if (task.topic or task.meeting_id) and never_post.match(task.topic, task.meeting_id):
        return "never-post list"
    if task.topic and never_post.match_words(task.topic):
        return "never-post list"
    return None


def _still_published(task: Task) -> bool:
    from ledger.uploads import get_ledger

    upload = get_ledger().get(task.item_key)
    return (upload is not None and upload.state == "published" and bool(task.video_id)
            and upload.video_id == task.video_id)


def _blocked_kinds(state: dict, now: datetime) -> set[str]:
    out = set()
    for kind, until in (state.get("blocked") or {}).items():
        when = _parse(until)
        if when is not None and when > now:
            out.add(kind)
    return out


def block_kind(kind: str, hours: float) -> None:
    """Hold back a kind of task for a while (thumbnails after a 403, so the
    quota is not wasted on calls that will be refused again)."""
    with _lock:
        state = _read()
        state["blocked"][kind] = _iso(_now() + timedelta(hours=hours))
        _write(state)


def _order(task: Task):
    return (0 if task.video_kind == "full" else 1, task.created_at)


def run_due(now: datetime | None = None) -> int:
    """Run every task that is due. Single-flight: a second call while one is
    running returns at once. Returns how many tasks were run."""
    if not _run_lock.acquire(blocking=False):
        return 0
    try:
        return _run(now or _now())
    finally:
        _run_lock.release()


def _expire(now: datetime) -> None:
    """Give up on waiting tasks older than the retry window; drop old records."""
    limit = timedelta(days=_retry_days())
    prune = timedelta(days=PRUNE_AFTER_DAYS)
    with _lock:
        state = _read()
        keep = []
        changed = False
        for raw in state["tasks"]:
            task = _task(raw)
            created = _parse(task.created_at) or now
            # Tasks waiting for the owner to reconnect are kept (with their file):
            # wake() starts their retry window over once the channel is back.
            if task.state == WAITING and now - created > limit:
                raw["state"], raw["reason"] = GIVEN_UP, (task.reason or "Waited too long")[:200]
                _remove_file(task)
                changed = True
            if raw["state"] in _FINISHED and now - created > prune:
                _remove_file(task)
                changed = True
                continue
            keep.append(raw)
        if changed:
            state["tasks"] = keep
            _write(state)


def _run(now: datetime) -> int:
    from publish import quota

    _load_features()
    _expire(now)
    with _lock:
        state = _read()
    blocked = _blocked_kinds(state, now)
    due = [t for t in (_task(r) for r in state["tasks"])
           if t.state == WAITING and t.kind not in blocked and (_parse(t.not_before) or now) <= now]
    due.sort(key=_order)
    ran = 0
    for stale in due:
        task = _get(stale.id)
        if task is None or task.state != WAITING:
            continue
        why = _never_post(task)
        if why:
            _finish(task, SKIPPED, why)
            continue
        if not _still_published(task):
            _finish(task, SKIPPED, "the video is no longer listed as posted")
            continue
        if quota.today().get("exhausted_until"):
            break
        try:
            if not youtube.youtube_status().get("connected"):
                break
        except Exception:  # noqa: BLE001
            break
        runner = _runners.get(task.kind)
        if runner is None:
            _finish(task, SKIPPED, "this follow-up is not available")
            continue
        try:
            outcome = runner(task)
        except Exception as exc:
            logger.exception("follow-up %s failed", task.kind)
            outcome = optional.Outcome(optional.RETRY, reason=f"It failed: {str(exc)[:150]}",
                                       retry_at=optional._backoff(task.attempts))
        ran += 1
        if _apply(task, outcome, now) == "stop":
            break
    return ran


def _finish(task: Task, state: str, reason: str) -> None:
    _update(task.id, state=state, reason=reason[:300])
    if state in (DONE, GIVEN_UP):
        _remove_file(task)


def _apply(task: Task, outcome: optional.Outcome, now: datetime) -> str:
    status = outcome.status
    reason = outcome.reason or ""
    if status == optional.DONE:
        _finish(task, DONE, reason)
        return "next"
    if status == optional.SKIPPED:
        _finish(task, SKIPPED, reason or "skipped")
        return "next"
    if status == optional.NEEDS_RECONNECT:
        _update(task.id, state=NEEDS_RECONNECT, reason=reason)
        return "next"
    if status == optional.STOP_ALL:
        changes: dict[str, Any] = {"reason": reason}
        if outcome.retry_at is not None:
            changes["not_before"] = _iso(outcome.retry_at)
        else:
            # No time given (for example the channel needs reconnecting): back
            # off so the 5-minute sweep does not retry it every round.
            changes["not_before"] = _iso(optional._backoff(task.attempts))
            changes["attempts"] = task.attempts + 1
        _update(task.id, **changes)
        return "stop"
    # retry
    created = _parse(task.created_at) or now
    if now - created > timedelta(days=_retry_days()):
        _finish(task, GIVEN_UP, reason or "kept failing")
        return "next"
    when = outcome.retry_at or optional._backoff(task.attempts)
    _update(task.id, attempts=task.attempts + 1, not_before=_iso(when), reason=reason)
    return "next"


def wake() -> None:
    """The channel was reconnected: tasks that waited for a permission are due
    now, and a run starts in the background."""
    with _lock:
        state = _read()
        now = _iso(_now())
        for raw in state["tasks"]:
            if raw["state"] == NEEDS_RECONNECT:
                raw["state"], raw["not_before"] = WAITING, now
                raw["created_at"] = now  # the reconnect starts a fresh retry window
        _write(state)
    threading.Thread(target=run_due, name="extras-wake", daemon=True).start()


def retry(task_id: str) -> bool:
    """Put a skipped or given-up task back in the queue, due now. False when
    the id is unknown."""
    with _lock:
        state = _read()
        for raw in state["tasks"]:
            if raw["id"] == task_id:
                if raw["state"] in (SKIPPED, GIVEN_UP):
                    now = _iso(_now())
                    raw["state"], raw["reason"], raw["attempts"] = WAITING, "", 0
                    raw["not_before"], raw["created_at"] = now, now
                    _write(state)
                return True
    return False


# ------------------------------------------------------------------ the page


def _when_text(task: Task) -> str:
    when = _parse(task.not_before)
    if when is None:
        return ""
    try:
        from publish import schedule

        return schedule.team_time_text(when)
    except Exception:  # noqa: BLE001
        return when.strftime("%a %H:%M UTC")


def _line(task: Task) -> str:
    word = _KIND_WORDS.get(task.kind, task.kind.title())
    reason = f": {task.reason}" if task.reason else ""
    if task.state == DONE:
        return {"thumbnail": "Thumbnail set", "captions": "Caption track added",
                "playlist": "Added to a playlist"}.get(task.kind, f"{word} done")
    if task.state == SKIPPED:
        return f"{word} skipped{reason}"
    if task.state == GIVEN_UP:
        return f"{word} gave up{reason}"
    if task.state == NEEDS_RECONNECT:
        return f"{word} waiting for the reconnect"
    if task.kind == "playlist" and task.data.get("waits_for_public"):
        return f"Playlist: goes in when it is public ({_when_text(task)})"
    return f"{word} waiting"


def tasks_for_page() -> list[dict]:
    """One plain line per video, newest first, with the tasks behind it."""
    videos: dict[str, dict] = {}
    for task in all_tasks():
        key = task.video_id or task.item_key
        row = videos.setdefault(key, {
            "video_id": task.video_id, "item_key": task.item_key, "meeting": task.meeting,
            "video_kind": task.video_kind, "index": task.index, "created_at": task.created_at, "tasks": []})
        row["tasks"].append({"id": task.id, "kind": task.kind, "state": task.state,
                             "text": _line(task), "can_retry": task.state in (SKIPPED, GIVEN_UP)})
    rows = sorted(videos.values(), key=lambda r: r["created_at"], reverse=True)
    for row in rows:
        row["text"] = "; ".join(t["text"] for t in row["tasks"])
    return rows
