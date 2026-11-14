"""Putting AI cost events into the database: finished jobs.

Nothing here raises into its caller: a cost meter must never stop a job from
finishing or the app from starting.
"""

from __future__ import annotations

from datetime import UTC, datetime

from core.config import get_settings
from core.logging import get_logger
from core.models import JobRecord

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
    """Record (or update) the job's AI usage. Safe to call as
    often as you like: the rows are keyed by job, so it never counts twice."""
    try:
        _record_job(job)
    except Exception:
        logger.exception("cost meter: could not record job %s", getattr(job, "job_id", "?"))


def _record_job(job: JobRecord) -> None:
    usage = job.llm_usage or {}
    calls = int(usage.get("calls") or 0)
    if not calls:
        return
    moment = job.updated_at or job.created_at or datetime.now(UTC)
    at = (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(UTC).isoformat(timespec="seconds")
    day = db.local_day(moment)
    ai_key = f"ai:{job.job_id}"
    with db.connect() as con:
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


def record_call(key: str, usage: dict, *, job_id: str = "", meeting: str = "") -> None:
    """Record the AI usage of one extra call that no job's own usage covers
    (translations). `key` is the event's identity ("ai:translate:<item key>"):
    recording the same key again updates that row, never adds one."""
    try:
        _record_call(key, usage or {}, job_id, meeting)
    except Exception:
        logger.exception("cost meter: could not record %s", key)


def _record_call(key: str, usage: dict, job_id: str, meeting: str) -> None:
    calls = int(usage.get("calls") or 0)
    if not calls:
        return
    moment = datetime.now(UTC)
    fields = {
        "calls": calls, "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_tokens") or 0),
        "cache_write_tokens": int(usage.get("cache_write_tokens") or 0)}
    with db.connect() as con:
        provider, model = _llm_now()
        usd = db.usd_for(db.load_prices(con), model, calls=calls, input_tokens=fields["input_tokens"],
                         output_tokens=fields["output_tokens"], cache_read=fields["cache_read_tokens"],
                         cache_write=fields["cache_write_tokens"])
        db.upsert_event(con, kind="ai", key=key, at=moment.isoformat(timespec="seconds"),
                        day=db.local_day(moment), job_id=job_id, meeting=meeting, provider=provider,
                        model=model, usd=usd, grows_by="calls", **fields)


def backfill(jobs) -> None:
    """Startup: record every job still on disk."""
    for job in jobs:
        if job is not None:
            record_job(job)
