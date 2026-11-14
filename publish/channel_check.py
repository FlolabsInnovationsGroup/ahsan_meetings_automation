"""The "Check the channel" scan: look at every video on the company channel and
report two kinds of problem.

- "listed": a video that is public or unlisted (or private but scheduled to go public)
  whose title matches the never-post list. A leak, to be fixed in YouTube Studio.
- "archive_public": a public (or scheduled) video of a team set to archive, which
  should be unlisted. Public videos of a team set to never are "listed" too.
- "unknown": a video the app has no record of posting (uploaded by hand or by
  something else), so a person can tell whether it belongs there.

When the channel has more than MAX_PAGES pages of uploads the scan stops there
and says so ("truncated").

The last result is kept in storage_dir/state/channel_check.json. Errors (not
connected, network, quota) are raised; the caller turns them into a message.
"""

from __future__ import annotations

from autopilot import never_post
from core.logging import get_logger
from ledger import files
from ledger.uploads import get_ledger

logger = get_logger(__name__)

PAGE_SIZE = 50
MAX_PAGES = 40
STUDIO = "https://studio.youtube.com/video/{}/edit"


def _path():
    return files.state_dir() / "channel_check.json"


def last_result() -> dict:
    """The last saved scan, or {"checked_at": None} when none ran yet."""
    data = files.read_json(_path(), None)
    return data if isinstance(data, dict) and data.get("checked_at") else {"checked_at": None}


def _build_service():
    from googleapiclient.discovery import build

    from publish import youtube

    return build("youtube", "v3", credentials=youtube._load_credentials(), cache_discovery=False)


def _spend(units: int, kind: str) -> None:
    """Count the quota of a list call (an essential call: the scan always runs)."""
    try:
        from publish import quota

        quota.spend(units, kind, essential=True)
    except Exception as exc:  # noqa: BLE001 - counting must never stop the scan
        logger.warning("could not count %d quota unit(s) for %s: %s", units, kind, exc)


def _videos(service) -> tuple[list[dict], bool]:
    """Every upload of the channel (the newest 2000 at most), and whether the
    scan stopped at that cap with more still left."""
    _spend(1, "channels.list")
    channels = service.channels().list(part="contentDetails", mine=True).execute().get("items", [])
    if not channels:
        return [], False
    playlist = channels[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    found: list[dict] = []
    token = None
    for _ in range(MAX_PAGES):
        params = {"part": "contentDetails", "playlistId": playlist, "maxResults": PAGE_SIZE}
        if token:
            params["pageToken"] = token
        _spend(1, "playlistItems.list")
        page = service.playlistItems().list(**params).execute()
        ids = [item["contentDetails"]["videoId"] for item in page.get("items", [])]
        if ids:
            _spend(1, "videos.list")
            listing = service.videos().list(part="snippet,status", id=",".join(ids)).execute()
            for video in listing.get("items", []):
                snippet, status = video.get("snippet") or {}, video.get("status") or {}
                found.append({
                    "id": video["id"],
                    "title": snippet.get("title") or "",
                    "privacy": status.get("privacyStatus") or "",
                    "publish_at": status.get("publishAt"),
                    "published_at": snippet.get("publishedAt"),
                })
        token = page.get("nextPageToken")
        if not token:
            break
    return found, token is not None


def _match_known(known, listed_words) -> str | None:
    job = None
    if known.job_id:
        try:
            from api.jobs import job_store

            job = job_store.get(known.job_id)
        except Exception:  # noqa: BLE001 - a missing or unreadable job: use what the ledger kept
            job = None
    if job is not None:
        why = never_post.match_job(job, listed_words)
        if why:
            return why
    elif known.source.startswith("zoom:"):
        try:
            from autopilot.store import store as default_store

            m = default_store.read().meetings.get(known.source[len("zoom:"):])
        except Exception:  # noqa: BLE001
            m = None
        if m is not None:
            why = never_post.match(m.topic, m.meeting_id, listed_words)
            if why:
                return why
    return never_post.match(known.meeting, None, listed_words)


def _team_tier(known) -> tuple[str, str]:
    """(tier, team name) of the team behind a video the app posted:
    'public' | 'archive' | 'never' | 'unknown'. Never raises."""
    try:
        from publish import series

        job = None
        if known.job_id:
            try:
                from api.jobs import job_store

                job = job_store.get(known.job_id)
            except Exception:  # noqa: BLE001 - a missing job: use what the ledger kept
                job = None
        topic = known.meeting
        if job is not None:
            info = getattr(job, "zoom_meeting", None)
            topic = (info.get("topic") if isinstance(info, dict) else None) or job.title or topic
            tier = series.tier_for_job(job)
        else:
            tier = series.tier_of(topic, None)
        row = series.lookup(topic)
        return tier, (row.label if row and row.label else series.series_key(topic))
    except Exception as exc:  # noqa: BLE001 - the team check must never stop the scan
        logger.warning("could not look up the team of a posted video: %s", exc)
        return "unknown", ""


def check_channel(service=None) -> dict:
    """Scan the channel now, save the result and return it."""
    videos, truncated = _videos(service or _build_service())
    posted = {u.video_id: u for u in get_ledger().posted() if u.video_id}
    ours = set(posted)
    listed_words = never_post.entries()
    listed, unknown, archive_public = [], [], []
    counts = {"total": len(videos), "public": 0, "private": 0, "unlisted": 0, "scheduled": 0}
    for v in videos:
        scheduled = v["privacy"] == "private" and bool(v["publish_at"])
        if v["privacy"] in ("public", "private", "unlisted"):
            counts[v["privacy"]] += 1
        counts["scheduled"] += scheduled
        item = {**v, "scheduled": scheduled, "studio_url": STUDIO.format(v["id"])}
        if v["privacy"] in ("public", "unlisted") or scheduled:
            why = never_post.match(v["title"], None, listed_words)
            known = posted.get(v["id"])
            if not why and known is not None:
                # Shorts carry hook-line titles that never name the meeting, so also
                # look at the meeting the app cut the video from, and at what its job
                # knows of it (the Zoom topic and meeting ID)
                why = _match_known(known, listed_words)
            if not why and known is not None and (v["privacy"] == "public" or scheduled):
                tier, team = _team_tier(known)
                if tier == "never":
                    why = f'Team "{team}" is set to never'
                elif tier == "archive":
                    archive_public.append(item)
            if why:
                listed.append({**item, "reason": why[0].upper() + why[1:]})
        if v["id"] not in ours:
            unknown.append(item)
    result = {"checked_at": files.now_iso(), "counts": counts, "listed": listed, "unknown": unknown,
              "archive_public": archive_public, "truncated": truncated}
    files.write_json(_path(), result)
    logger.info("channel check: %d videos, %d from never-post meetings, %d not uploaded by the app",
                len(videos), len(listed), len(unknown))
    return result
