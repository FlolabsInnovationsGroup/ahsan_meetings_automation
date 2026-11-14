"""What to keep about a video at the moment it is uploaded.

The job files are deleted a day or so later, so the team, the Short style,
the title style, the hook and the thumbnail arm are written to the views
database now. Nothing here raises into its caller: bookkeeping must never
stop an upload.
"""

from __future__ import annotations

from core.logging import get_logger

from . import db

logger = get_logger(__name__)


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def record_upload(item, upload, job=None) -> None:
    """Store the video `upload` (a ledger Upload with a video_id) made from `item`."""
    try:
        video_id = getattr(upload, "video_id", None)
        if not video_id:
            return
        from publish import series
        from publish import settings as publishing_settings

        topic = getattr(item, "topic", None) or getattr(item, "meeting", None)
        row = series.lookup(topic)
        kind = getattr(item, "kind", None)
        fields = {
            "ours": 1,
            "kind": kind if kind in db.KINDS else "other",
            "source": _text(getattr(item, "source", None)),
            "job_id": _text(getattr(item, "job_id", None)),
            "meeting": _text(getattr(item, "meeting", None)),
            "series_key": row.key if row else (series.series_key(topic) or None),
            "series_label": row.label if row else None,
            "category": _text(getattr(item, "category", None)),
            "title": _text(getattr(upload, "title", None)),
            "title_style": publishing_settings.current().title_style,
            "short_style": _text(getattr(item, "style", None)),
            "layout": _text(getattr(item, "layout", None)),
            "stands_alone": _number(getattr(item, "stands_alone", None)),
            "screen_hook": _text(getattr(item, "screen_hook", None)),
            "published_at": _text(getattr(upload, "posted_at", None)),
            "publish_at": _text(getattr(upload, "publish_at", None)),
            "privacy": _text(getattr(upload, "privacy", None)),
        }
        with db.connect() as con:
            db.upsert_video(con, video_id, **fields)
    except Exception:
        logger.exception("views: could not record the upload")


def set_arm(video_id: str, feature: str, arm: str) -> None:
    """Remember which variant a video got, e.g. set_arm(id, 'thumb', 'B')."""
    try:
        column = {"thumb": "thumb_arm", "thumbnail": "thumb_arm", "thumb_arm": "thumb_arm"}.get(feature)
        if not column or not video_id:
            logger.warning("views: no arm named %r", feature)
            return
        with db.connect() as con:
            db.upsert_video(con, video_id, **{column: str(arm)})
    except Exception:
        logger.exception("views: could not record the %s arm", feature)
