"""Putting cost events into the database: finished jobs and uploads.

Nothing here raises into its caller: a cost meter must never stop a job from
finishing or the app from starting.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from core.config import get_settings
from core.logging import get_logger
from core.models import JobRecord
from ledger import files

from . import db

logger = get_logger(__name__)


def _llm_now() -> tuple[str, str]:
    """The provider and model the settings name now. A job doesn't record
    which model it used, so this is the best answer at the time the cost is
    recorded (a finished job is recorded right away, so it is almost always
    the model that did the work)."""
    from decide.gemini_client import llm_model
    settings = get_settings()
    return (settings.llm_provider or "").strip().lower(), llm_model(settings)


def record_job(job: JobRecord) -> None:
    """Record (or update) the job's AI usage and compute time. Safe to call as
    often as you like: the rows are keyed by job, so it never counts twice."""
    try:
        _record_job(job)
    except Exception:
        logger.exception("cost meter: could not record job %s", getattr(job, "job_id", "?"))


def _record_job(job: JobRecord) -> None:
    usage = job.llm_usage or {}
    calls = int(usage.get("calls") or 0)
    # some timings sit inside others (each render_* part is inside "rendering",
    # "silence" is inside "building_edl"), so adding them all would count that
    # time twice
    seconds = round(sum(float(v or 0) for k, v in (job.stage_timings or {}).items()
                        if not k.startswith("render_") and k != "silence"), 2)
    if not calls and seconds <= 0:
        return
    moment = job.updated_at or job.created_at or datetime.now(UTC)
    at = (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(UTC).isoformat(timespec="seconds")
    day = db.local_day(moment)
    ai_key = f"ai:{job.job_id}"
    with db.connect() as con:
        if calls:
            prior = con.execute("SELECT provider, model, calls FROM events WHERE key=?", (ai_key,)).fetchone()
            if prior and prior["model"] and int(prior["calls"]) == calls:
                provider, model = prior["provider"], prior["model"]  # nothing new: keep the model it had
            else:
                provider, model = _llm_now()
            fields = {
                "calls": calls, "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cache_read_tokens": int(usage.get("cache_read_tokens") or 0),
                "cache_write_tokens": int(usage.get("cache_write_tokens") or 0)}
            usd = db.usd_for(db.load_prices(con), model, calls=calls, input_tokens=fields["input_tokens"],
                             output_tokens=fields["output_tokens"], cache_read=fields["cache_read_tokens"],
                             cache_write=fields["cache_write_tokens"])
            db.upsert_event(con, kind="ai", key=ai_key, at=at, day=day, job_id=job.job_id, meeting=job.title,
                            provider=provider, model=model, usd=usd, grows_by="calls", **fields)
        if seconds > 0:
            # the server is a fixed cost, so the time costs nothing extra
            db.upsert_event(con, kind="compute", key=f"compute:{job.job_id}", at=at, day=day, job_id=job.job_id,
                            meeting=job.title, seconds=seconds, usd=0.0, grows_by="seconds")


def sync_uploads() -> int:
    """Add an "upload" event for every video in posted.jsonl that the database
    doesn't have yet (one upload = 1 unit of YouTube's upload quota, no
    dollars). Returns how many were added; 0 on any trouble."""
    try:
        return _sync_uploads()
    except Exception:
        logger.exception("cost meter: could not read the posted list")
        return 0


def _sync_uploads() -> int:
    path = files.state_dir() / "posted.jsonl"
    if not path.exists():
        return 0
    lines = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if isinstance(line, dict) and (line.get("video_id") or line.get("key")):
            lines.append(line)
    added = 0
    with db.connect() as con:
        known = {r["key"] for r in con.execute("SELECT key FROM events WHERE kind='upload'")}
        for line in lines:
            key = f"upload:{line.get('video_id') or line['key']}"
            if key in known:
                continue
            known.add(key)
            posted_at = line.get("posted_at") or db.now_iso()
            db.upsert_event(con, kind="upload", key=key, at=posted_at, day=db.local_day(posted_at),
                            job_id=line.get("job_id") or "", meeting=line.get("meeting") or "", units=1,
                            usd=0.0, note="short" if line.get("kind") == "short" else "full")
            added += 1
    return added


def backfill(jobs) -> None:
    """Startup: record every job still on disk, and the uploads so far."""
    for job in jobs:
        if job is not None:
            record_job(job)
    sync_uploads()
