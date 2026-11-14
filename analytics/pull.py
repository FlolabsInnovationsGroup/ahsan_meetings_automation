"""The daily read-only statistics pull: views, likes and comments per video.

Uses only the youtube.readonly permission. videos.list costs 1 unit per call
and takes 50 ids, so the daily pull is a handful of units. Every call goes
through publish/optional.call, which keeps the daily quota reserve for
uploads, counts the units and never raises. A quota or sign-in problem stops
the pull, is written to the pulls table and is not retried in a loop.

Raw numbers only: no ratios or rankings here.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from core.logging import get_logger
from costs.db import local_day

from . import db

logger = get_logger(__name__)

BATCH = 50
PLAYLIST_MAX_PAGES = 10
STALE_DAYS = 30
CHANNEL_FILE_MAX_AGE = timedelta(days=2)
MISSING_LIMIT = 2
KIND = "views"

_DURATION = re.compile(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def _seconds(iso: str | None) -> float | None:
    """'PT1H2M3S' -> 3723.0 (None when it can't be read)."""
    m = _DURATION.match(iso or "")
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return float(d * 86400 + h * 3600 + mi * 60 + s)


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse(text) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _build_service():
    from publish import channel_check

    return channel_check._build_service()


def _channel_file_age() -> tuple[dict, bool]:
    """(the saved channel scan, whether it is older than 2 days or missing)."""
    from publish import channel_check

    data = channel_check.last_result()
    when = _parse(data.get("checked_at")) if data.get("checked_at") else None
    return data, when is None or datetime.now(UTC) - when > CHANNEL_FILE_MAX_AGE


class _Stop(Exception):
    """A call did not run or failed in a way that ends the pull."""


class _Run:
    def __init__(self, service) -> None:
        self.service = service
        self.calls = 0
        self.units = 0

    def call(self, units: int, fn):
        from publish import optional

        outcome = optional.call(KIND, units, fn)
        if outcome.status == optional.DONE:
            self.calls += 1
            self.units += units
            return outcome.value
        raise _Stop(outcome.reason or outcome.status)

    def uploads_playlist_ids(self) -> list[str]:
        channels = self.call(1, lambda: self.service.channels().list(part="contentDetails", mine=True).execute())
        items = (channels or {}).get("items") or []
        if not items:
            return []
        playlist = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        ids: list[str] = []
        token = None
        for _ in range(PLAYLIST_MAX_PAGES):
            params = {"part": "contentDetails", "playlistId": playlist, "maxResults": BATCH}
            if token:
                params["pageToken"] = token
            page = self.call(1, lambda p=params: self.service.playlistItems().list(**p).execute())
            ids += [i["contentDetails"]["videoId"] for i in (page or {}).get("items", [])]
            token = (page or {}).get("nextPageToken")
            if not token:
                break
        return ids

    def stats(self, ids: list[str]) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for start in range(0, len(ids), BATCH):
            batch = ids[start:start + BATCH]
            answer = self.call(1, lambda b=batch: self.service.videos().list(
                part="statistics,status,contentDetails,snippet", id=",".join(b)).execute())
            for video in (answer or {}).get("items", []):
                found[video["id"]] = video
        return found


def _wanted_ids(run: _Run, include_other: bool) -> list[str]:
    from ledger.uploads import get_ledger

    ids = [u.video_id for u in get_ledger().posted() if u.video_id]
    with db.connect() as con:
        if not include_other:
            # the choice is off: other people's videos are not kept
            for row in con.execute("SELECT video_id FROM videos WHERE ours=0").fetchall():
                db.delete_video(con, row["video_id"])
        # every stored row is refreshed daily or goes through the 2-miss rule
        ids += [row["video_id"] for row in con.execute("SELECT video_id FROM videos").fetchall()]
    if include_other:
        data, old = _channel_file_age()
        for key in ("listed", "unknown"):
            ids += [v["id"] for v in data.get(key) or [] if isinstance(v, dict) and v.get("id")]
        if old:
            ids += run.uploads_playlist_ids()
    return list(dict.fromkeys(ids))


def _store(found: dict[str, dict], asked: list[str]) -> None:
    from ledger.uploads import get_ledger

    ours = {u.video_id: u for u in get_ledger().posted() if u.video_id}
    day = local_day()
    with db.connect() as con:
        for video_id in asked:
            video = found.get(video_id)
            if video is None:
                row = con.execute("SELECT missing_count FROM videos WHERE video_id=?", (video_id,)).fetchone()
                if row is not None:
                    if row["missing_count"] + 1 >= MISSING_LIMIT:
                        db.delete_video(con, video_id)
                    else:
                        con.execute("UPDATE videos SET missing_count=missing_count+1 WHERE video_id=?", (video_id,))
                continue
            snippet, status = video.get("snippet") or {}, video.get("status") or {}
            stats = video.get("statistics") or {}
            known = ours.get(video_id)
            db.upsert_video(
                con, video_id, title=snippet.get("title"), published_at=snippet.get("publishedAt"),
                privacy=status.get("privacyStatus"), publish_at=status.get("publishAt"),
                duration_s=_seconds((video.get("contentDetails") or {}).get("duration")), missing_count=0)
            if known is not None:
                db.upsert_video(con, video_id, only_missing=True, ours=1, kind=known.kind, source=known.source,
                                job_id=known.job_id, meeting=known.meeting)
            else:
                db.upsert_video(con, video_id, only_missing=True, kind="other")
            db.upsert_snapshot(con, video_id, day, _int(stats.get("viewCount")), _int(stats.get("likeCount")),
                               _int(stats.get("commentCount")))


def pull_stats(service=None) -> dict:
    """Fetch today's numbers and store them. Never raises; returns
    {ok, calls, units, videos, error} and writes one pulls row."""
    run = _Run(service)
    videos, error = 0, ""
    try:
        from publish import settings as publishing_settings

        cfg = publishing_settings.current()
        if run.service is None:
            run.service = _build_service()
        asked = _wanted_ids(run, cfg.views_include_other)
        found = run.stats(asked)
        _store(found, asked)
        videos = len(found)
    except _Stop as stop:
        error = str(stop)
        logger.warning("views pull stopped: %s", error)
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:200]}"
        logger.exception("views pull failed")
    ok = not error
    try:
        db.add_pull(kind=KIND, calls=run.calls, units=run.units, videos=videos, ok=ok, error=error)
        if ok:
            db.set_meta("last_ok_pull", db.now_iso())
    except Exception:
        logger.exception("views: could not write the pulls row")
    watch_status = ""
    if ok:
        try:
            from . import yta

            watch_status = yta.pull_daily().get("status", "")
        except Exception:
            logger.exception("views: the watch-time pull could not start")
    return {"ok": ok, "calls": run.calls, "units": run.units, "videos": videos, "error": error,
            "watch_status": watch_status}


def purge_if_stale(connected: bool | None = None) -> bool:
    """No successful pull for 30 days, or YouTube disconnected for 30 days:
    delete every row (the API data policy). Also records that YouTube is
    connected now. Returns True when it deleted. Never raises."""
    try:
        if connected is None:
            from publish import youtube

            connected = bool(youtube.youtube_status().get("connected"))
        now = datetime.now(UTC)
        if connected:
            db.set_meta("last_connected", now.isoformat(timespec="seconds"))
        limit = timedelta(days=STALE_DAYS)
        last_ok = _parse(db.get_meta("last_ok_pull") or "")
        last_conn = _parse(db.get_meta("last_connected") or "")
        stale = (last_ok is not None and now - last_ok > limit) or (
            not connected and last_conn is not None and now - last_conn > limit)
        if stale:
            db.purge_all()
            logger.warning("views: no fresh data for %d days; the stored YouTube numbers were deleted", STALE_DAYS)
        return stale
    except Exception:
        logger.exception("views: stale check failed")
        return False
