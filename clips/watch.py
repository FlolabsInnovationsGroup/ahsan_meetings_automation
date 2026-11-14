"""Find new videos of a source: a YouTube channel or a watched folder.

Nothing here decides whether we may clip the source; the runner checks the
rights record first (clips.rights). This module only lists what is new.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from clips.models import Platform, RightsBasis, Source, SourceVideo

logger = logging.getLogger(__name__)

VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm"}
STABLE_SECONDS = 60.0  # a file must be untouched this long, so a half-copied file is not picked up
SHORTS_MAX_S = 60.0
_CC_LICENSE_MARK = "creative commons"
_NOT_READY = ("is_live", "is_upcoming", "post_live")


class WatchError(RuntimeError):
    """Listing a source failed. The message says what to do."""


def list_new_videos(source: Source, known_native_ids: set[str], *, limit: int = 15) -> list[SourceVideo]:
    """New videos of `source`, newest first, never one whose id is already known."""
    if source.platform is Platform.YOUTUBE:
        videos = _youtube_videos(source, known_native_ids, limit)
    elif source.platform is Platform.FOLDER:
        videos = _folder_videos(source, known_native_ids, limit)
    else:
        logger.info("source %s: Twitch is not supported in this version, so nothing is listed", source.id)
        return []
    return [v for v in videos if v.native_id not in known_native_ids][:limit]


# --- YouTube

_CHANNEL_TABS = ("videos", "streams", "shorts", "featured", "live", "playlists", "community", "about", "releases")


def _channel_root(source: Source) -> tuple[str, bool]:
    """The channel's own address without a tab, and whether it is a real channel
    link. A playlist or single-video link (only allowed for our own footage) is
    returned as given, flagged False, because it has no tabs."""
    url = source.url.strip()
    own = source.rights is RightsBasis.OWN
    if not own and not source.channel_id:
        raise WatchError(
            f"source {source.id} needs its channel id (UC...) so only that channel's videos are listed; add one in its settings"
        )
    if not own and ("list=" in url or "watch?v=" in url):
        raise WatchError(
            f"source {source.id}: a playlist or single-video link can hold other creators' videos; use the channel's own address"
        )
    if not url and source.channel_id:
        url = f"https://www.youtube.com/channel/{source.channel_id}"
    if not url:
        raise WatchError(f"source {source.id} has no channel URL or channel id; add one in its settings")
    if "watch?v=" in url or "list=" in url:
        return url, False
    if url.startswith("@"):
        url = "https://www.youtube.com/" + url
    elif "://" not in url:
        url = "https://" + url
    url = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    head, _, last = url.rpartition("/")
    if last.lower() in _CHANNEL_TABS and head.count("/") >= 3:
        url = head  # the link named a tab; keep only the channel
    return url, True


def _channel_videos_url(source: Source) -> str:
    """The channel's long-video tab, so Shorts and streams are mostly left out."""
    root, is_channel = _channel_root(source)
    return root + "/videos" if is_channel else root


def _channel_streams_url(source: Source) -> str | None:
    """The channel's Live tab (finished streams), or None for a link with no tabs."""
    root, is_channel = _channel_root(source)
    return root + "/streams" if is_channel else None


def _ytdlp_list(url: str, limit: int) -> list[dict]:
    """Ask yt-dlp for the newest entries of a channel tab, metadata only.
    One small function so tests can replace it."""
    import yt_dlp

    from ingest.youtube import ydl_base_opts

    opts = {
        **ydl_base_opts(),
        "extract_flat": True,
        "skip_download": True,
        "noplaylist": False,
        "playlistend": max(limit * 3, 20),  # room for Shorts and streams that get dropped
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
    except Exception as exc:
        raise WatchError(_explain(url, exc)) from exc
    return [e for e in (info.get("entries") or []) if isinstance(e, dict)]


def _ytdlp_video_info(url: str) -> dict:
    """Full metadata of one video (for its licence). Empty when yt-dlp fails."""
    import yt_dlp

    from ingest.youtube import ydl_base_opts

    try:
        with yt_dlp.YoutubeDL({**ydl_base_opts(), "skip_download": True}) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not read the licence of %s: %s", url, exc)
        return {}


def _explain(url: str, exc: Exception) -> str:
    """A plain message for a yt-dlp failure."""
    text = str(exc)
    low = text.lower()
    if "not a bot" in low or "sign in" in low:
        hint = (
            "YouTube wants a sign-in (bot check). Set YTDLP_COOKIES_FILE to a cookies file, "
            "or run from a home connection."
        )
    elif "javascript" in low or "js runtime" in low:
        hint = "Install Node or Deno so yt-dlp can solve the YouTube challenge."
    elif "429" in low or "too many" in low:
        hint = "YouTube is rate limiting this address. Wait and try again later."
    else:
        hint = "Check the channel URL and that yt-dlp is up to date."
    first = text.splitlines()[0] if text else "unknown error"
    return f"could not list {url}: {first}. {hint}"


def map_license(raw: object) -> str:
    """YouTube's licence text as 'creativeCommon', 'youtube', or '' when unknown."""
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    if text == "creativecommon" or _CC_LICENSE_MARK in text:
        return "creativeCommon"
    return "youtube" if "youtube" in text else ""


def _published(entry: dict) -> datetime | None:
    ts = entry.get("timestamp") or entry.get("release_timestamp")
    if ts:
        try:
            return datetime.fromtimestamp(float(ts), tz=UTC)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    day = str(entry.get("upload_date") or "")
    if len(day) == 8 and day.isdigit():
        try:
            return datetime(int(day[:4]), int(day[4:6]), int(day[6:]), tzinfo=UTC)
        except ValueError:
            return None
    return None


def _is_unwanted(entry: dict, duration: float) -> bool:
    """Shorts, live and upcoming streams, and entries with no length (not ready yet)."""
    url = str(entry.get("url") or entry.get("webpage_url") or "")
    if "/shorts/" in url:
        return True
    # Live now, upcoming, or ended but not processed yet: picked up on a later pass.
    if entry.get("is_live") or entry.get("is_upcoming") or entry.get("live_status") in _NOT_READY:
        return True
    return duration <= SHORTS_MAX_S


def _entry_channel_ok(source: Source, entry: dict) -> bool:
    """A video of a non-own source counts only when it carries the source's channel id."""
    if source.rights is RightsBasis.OWN and not source.channel_id:
        return True
    wanted = source.channel_id
    return bool(wanted) and wanted in (str(entry.get("channel_id") or ""), str(entry.get("uploader_id") or ""))


def _include_streams() -> bool:
    from clips.config import clips_settings

    return bool(clips_settings().include_streams)


def _youtube_videos(source: Source, known: set[str], limit: int) -> list[SourceVideo]:
    """Uploads plus finished streams, each tab newest first. One tab failing keeps the other."""
    tabs = [(_channel_videos_url(source), False)]
    streams_url = _channel_streams_url(source) if _include_streams() else None
    if streams_url:
        tabs.append((streams_url, True))
    merged: dict[str, SourceVideo] = {}
    per_tab: list[list[SourceVideo]] = []
    errors: list[WatchError] = []
    for url, is_streams_tab in tabs:
        try:
            videos = _youtube_tab(source, known, limit, url, is_streams_tab)
        except WatchError as exc:
            errors.append(exc)
            logger.warning("source %s: %s", source.id, exc)
            continue
        fresh = [v for v in videos if v.native_id not in merged]
        merged.update({v.native_id: v for v in fresh})
        per_tab.append(fresh)
    if errors and len(errors) == len(tabs):
        raise errors[0]
    # Each tab is already newest first and flat listings often carry no date, so
    # alternate between tabs: streams are not starved by the limit.
    mixed: list[SourceVideo] = []
    for i in range(max((len(t) for t in per_tab), default=0)):
        mixed.extend(t[i] for t in per_tab if i < len(t))
    return mixed


def _youtube_tab(
    source: Source, known: set[str], limit: int, list_url: str, is_streams_tab: bool
) -> list[SourceVideo]:
    entries = _ytdlp_list(list_url, limit)
    found: list[SourceVideo] = []
    for entry in entries:
        native_id = str(entry.get("id") or "")
        if not native_id or native_id in known:
            continue
        if not _entry_channel_ok(source, entry):
            continue
        try:
            duration = float(entry.get("duration") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        if _is_unwanted(entry, duration):
            continue
        url = f"https://www.youtube.com/watch?v={native_id}"
        license_ = map_license(entry.get("license"))
        published = _published(entry)
        if not license_ and source.rights is RightsBasis.CREATIVE_COMMONS:
            # The flat listing has no licence; a Creative Commons source needs it per video.
            detail = _ytdlp_video_info(url)
            license_ = map_license(detail.get("license"))
            published = published or _published(detail)
        found.append(
            SourceVideo(
                id=f"{source.id}:{Platform.YOUTUBE.value}:{native_id}",
                source_id=source.id,
                platform=Platform.YOUTUBE,
                native_id=native_id,
                url=url,
                title=str(entry.get("title") or ""),
                published_at=published,
                duration_s=duration,
                license=license_,
                was_live=is_streams_tab or bool(entry.get("was_live")) or entry.get("live_status") == "was_live",
            )
        )
        if len(found) >= limit:
            break
    return found


# --- Folder

def _folder_videos(source: Source, known: set[str], limit: int) -> list[SourceVideo]:
    folder = Path(source.folder) if source.folder else None
    if folder is None or not folder.is_dir():
        logger.warning("source %s: folder %r does not exist", source.id, source.folder)
        return []
    now = time.time()
    files = []
    for path in folder.iterdir():
        if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES or path.name in known:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if now - stat.st_mtime < STABLE_SECONDS:
            continue  # still being written or copied
        files.append((stat.st_mtime, path))
    files.sort(key=lambda item: item[0], reverse=True)
    return [
        SourceVideo(
            id=f"{source.id}:{Platform.FOLDER.value}:{path.name}",
            source_id=source.id,
            platform=Platform.FOLDER,
            native_id=path.name,
            path=str(path),
            title=path.stem,
            published_at=datetime.fromtimestamp(mtime, tz=UTC),
        )
        for mtime, path in files[:limit]
    ]
