"""Watch time and engaged views from the YouTube Analytics API (after the
reconnect that adds the yt-analytics.readonly permission).

One query per day: youtubeAnalytics.reports.query for that single day with
dimensions=video, sort=-views, maxResults=200 (the documented "top videos"
report, which allows these metrics). Each run covers 5 Pacific days ending 2
days back and replaces those days' rows, because YouTube fills and corrects
late days.

Fails soft: no permission, the API not enabled in the Cloud project, a quota
stop or any other error gives a status line and never an exception. If
YouTube rejects engagedViews, the query is repeated with plain views and
engaged views are stored as views.
"""

from __future__ import annotations

from datetime import date, timedelta

from core.logging import get_logger

from . import db

logger = get_logger(__name__)

KIND = "analytics"
DAYS = 5          # days re-read each run, ending LAG_DAYS back (Pacific time)
LAG_DAYS = 2
META_FALLBACK = "yta_plain_views"
MAX_RESULTS = 200
UNITS = 1
SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
METRICS = "engagedViews,views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained"
FALLBACK_METRICS = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained"
META_STATUS = "yta_status"
NOT_ENABLED = ("The YouTube Analytics API is not turned on in the Google Cloud project. "
               "Turn it on there; watch time is skipped until then.")
RECONNECT = "Reconnect to turn on view analytics"


def _build_service():
    from googleapiclient.discovery import build

    from publish import youtube

    return build("youtubeAnalytics", "v2", credentials=youtube._load_credentials(), cache_discovery=False)


def _number(value, cast=float):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def last_status() -> str:
    """The status line of the last watch-time pull ('' when none ran)."""
    try:
        return db.get_meta(META_STATUS) or ""
    except Exception:  # noqa: BLE001
        return ""


def _finish(result: dict, status: str) -> dict:
    result["status"] = status
    try:
        db.set_meta(META_STATUS, status)
    except Exception:
        logger.exception("views: could not store the watch-time status")
    return result


def _days(day_from: str, day_to: str) -> list[str]:
    a, b = date.fromisoformat(day_from), date.fromisoformat(day_to)
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


def _rows(answer, fallback: bool) -> list[tuple]:
    out = []
    for row in (answer or {}).get("rows") or []:
        if not isinstance(row, list | tuple) or len(row) < 6:
            continue
        if fallback:
            video, views, minutes, dur, pct, subs = row[:6]
            engaged = views
        else:
            if len(row) < 7:
                continue
            video, engaged, views, minutes, dur, pct, subs = row[:7]
        out.append((str(video), _number(engaged, int), _number(views, int), _number(minutes),
                    _number(dur), _number(pct), _number(subs, int)))
    return out


def pull_daily(day_from: str | None = None, day_to: str | None = None, service=None) -> dict:
    """Fetch the watch-time numbers for each day from day_from to day_to
    (default: the last 3 days, today included) and replace those days' rows.
    Never raises; returns {ok, ran, status, calls, units, rows}."""
    result = {"ok": False, "ran": False, "status": "", "calls": 0, "units": 0, "rows": 0}
    try:
        from publish import optional, quota, youtube
        from publish import settings as publishing_settings

        if not publishing_settings.current().views_watch_time:
            return _finish(result, "Watch time is switched off")
        if not youtube.has_scope(SCOPE):
            return _finish(result, RECONNECT)
        # YouTube Analytics is keyed to Pacific days and fills in about 2 days late
        day_to = day_to or (date.fromisoformat(quota.day_of()) - timedelta(days=LAG_DAYS)).isoformat()
        day_from = day_from or (date.fromisoformat(day_to) - timedelta(days=DAYS - 1)).isoformat()
        days = _days(day_from, day_to)
        if service is None:
            service = _build_service()
        with db.connect() as con:
            known = {r["video_id"] for r in con.execute("SELECT video_id FROM videos").fetchall()}
        remembered = False
        try:
            remembered = db.get_meta(META_FALLBACK) == "1"
        except Exception:
            logger.exception("views: could not read the plain-views fallback flag")
        metrics_now, fallback, stored, error = [FALLBACK_METRICS if remembered else METRICS], remembered, 0, ""
        result["ran"] = True
        for day in days:
            def query(d=day, m=None):
                return service.reports().query(
                    ids="channel==MINE", startDate=d, endDate=d, metrics=m or metrics_now[0], dimensions="video",
                    sort="-views", maxResults=MAX_RESULTS).execute()

            outcome = optional.call(KIND, UNITS, query, forbidden_reason=NOT_ENABLED)
            if outcome.status == optional.DONE:
                result["calls"] += 1
            if outcome.status == optional.SKIPPED and not fallback and outcome.reason != NOT_ENABLED:
                # a generic "query not supported" 400 names no metric, so retry plain whatever the message
                metrics_now[0], fallback = FALLBACK_METRICS, True
                outcome = optional.call(KIND, UNITS, query, forbidden_reason=NOT_ENABLED)
                if outcome.status == optional.DONE:
                    result["calls"] += 1
                    try:
                        db.set_meta(META_FALLBACK, "1")
                    except Exception:
                        logger.exception("views: could not remember the plain-views fallback")
            if outcome.status != optional.DONE:
                error = RECONNECT if outcome.status == optional.NEEDS_RECONNECT else (outcome.reason or outcome.status)
                break
            rows = [r for r in _rows(outcome.value, fallback) if r[0] in known]
            with db.connect() as con:
                con.execute("DELETE FROM yta_daily WHERE day=?", (day,))
                con.executemany(
                    "INSERT OR REPLACE INTO yta_daily (video_id, day, engaged_views, views, minutes_watched, "
                    "avg_view_duration_s, avg_view_pct, subs_gained) VALUES (?,?,?,?,?,?,?,?)",
                    [(r[0], day, r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows])
            stored += len(rows)
        result["units"] = result["calls"] * UNITS
        result["rows"] = stored
        result["ok"] = not error
        try:
            db.add_pull(kind=KIND, calls=result["calls"], units=result["units"], videos=stored,
                        ok=result["ok"], error=error)
        except Exception:
            logger.exception("views: could not write the watch-time pulls row")
        if error:
            return _finish(result, f"Watch time stopped: {error}")
        note = " (engaged views are plain views: YouTube refused that number)" if fallback else ""
        return _finish(result, f"Watch time updated for {len(days)} day{'s' if len(days) != 1 else ''}{note}")
    except Exception as exc:
        logger.exception("views: watch-time pull failed")
        return _finish(result, f"Watch time failed: {type(exc).__name__}: {str(exc)[:150]}")
