"""The YouTube calls that are not part of an upload: thumbnails, caption
tracks, playlists, and checks on a finished video.

Every function here is a raw call: it raises what Google raises. The follow-up
queue (publish/extras.py) runs them through optional.call, which keeps the
permission check, the quota count and the error mapping in one place.

Unit costs (for optional.call): thumbnails.set 50, captions.insert 400,
playlistItems.insert 50, playlists.insert 50, every list call 1.
"""

from __future__ import annotations

from pathlib import Path

from publish import youtube

UNITS_THUMBNAIL = 50
UNITS_CAPTION_INSERT = 400
UNITS_INSERT = 50
UNITS_LIST = 1

_MAX_PLAYLIST_PAGES = 4


def _service():
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=youtube._load_credentials(), cache_discovery=False)


def set_thumbnail(video_id: str, jpg: Path | str) -> dict:
    """Set a custom thumbnail from a JPEG file."""
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(jpg), mimetype="image/jpeg")
    return _service().thumbnails().set(videoId=video_id, media_body=media).execute()


def processing_status(video_id: str) -> dict | None:
    """The video's processingDetails (processingStatus, ...), or None when
    the video is not found."""
    items = _service().videos().list(part="processingDetails", id=video_id).execute().get("items", [])
    return (items[0].get("processingDetails") or {}) if items else None


def video_privacy(video_id: str) -> dict | None:
    """{"privacyStatus": ..., "publishAt": ...} (publishAt None when not
    scheduled), or None when the video is not found."""
    items = _service().videos().list(part="status", id=video_id).execute().get("items", [])
    if not items:
        return None
    status = items[0].get("status") or {}
    return {"privacyStatus": status.get("privacyStatus"), "publishAt": status.get("publishAt")}


# What videos.update may set in "status"; every one present is sent back as it
# is, because a field left out of an update is reset to YouTube's default.
_STATUS_WRITABLE = ("privacyStatus", "embeddable", "license", "publicStatsViewable", "selfDeclaredMadeForKids",
                    "containsSyntheticMedia")


def set_publish_at(video_id: str, publish_at: str) -> bool:
    """Move a scheduled video's time to go public to `publish_at` (RFC 3339,
    UTC). Only a video that is still private with a time to go public is
    changed; returns False (nothing changed) for any other, or when it is not
    found. Costs 1 + 50 quota units. Needs the youtube.force-ssl permission."""
    service = _service()
    items = service.videos().list(part="status", id=video_id).execute().get("items", [])
    if not items:
        return False
    status = items[0].get("status") or {}
    if status.get("privacyStatus") != "private" or not status.get("publishAt"):
        return False  # already public, or a private video nobody scheduled
    body = {k: status[k] for k in _STATUS_WRITABLE if k in status}
    body["publishAt"] = publish_at
    service.videos().update(part="status", body={"id": video_id, "status": body}).execute()
    return True


def make_private(video_id: str) -> bool:
    """Set a video to private and drop any time it was to go public. Returns
    False when the video is not found or already private and unscheduled.
    Costs 1 + 50 quota units. Needs the youtube.force-ssl permission."""
    service = _service()
    items = service.videos().list(part="status", id=video_id).execute().get("items", [])
    if not items:
        return False
    status = items[0].get("status") or {}
    if status.get("privacyStatus") == "private" and not status.get("publishAt"):
        return False
    body = {k: status[k] for k in _STATUS_WRITABLE if k in status}
    body["privacyStatus"] = "private"  # no publishAt: it stays private
    service.videos().update(part="status", body={"id": video_id, "status": body}).execute()
    return True


def insert_caption(video_id: str, srt_path: Path | str, language: str, name: str) -> dict:
    """Add a published (not draft) caption track from a subtitle file."""
    from googleapiclient.http import MediaFileUpload

    body = {"snippet": {"videoId": video_id, "language": language, "name": name, "isDraft": False}}
    media = MediaFileUpload(str(srt_path), mimetype="application/octet-stream")
    return _service().captions().insert(part="snippet", body=body, media_body=media).execute()


def find_playlist(title: str) -> str | None:
    """The id of the channel's playlist with exactly this title (looks at
    the first four pages of 50), or None."""
    service = _service()
    token = None
    for _ in range(_MAX_PLAYLIST_PAGES):
        params = {"part": "snippet", "mine": True, "maxResults": 50}
        if token:
            params["pageToken"] = token
        page = service.playlists().list(**params).execute()
        for item in page.get("items", []):
            if (item.get("snippet") or {}).get("title") == title:
                return item["id"]
        token = page.get("nextPageToken")
        if not token:
            break
    return None


def create_playlist(title: str, description: str, privacy: str = "public") -> str:
    """Make a playlist; returns its id."""
    body = {"snippet": {"title": title, "description": description}, "status": {"privacyStatus": privacy}}
    return _service().playlists().insert(part="snippet,status", body=body).execute()["id"]


def playlist_has_video(playlist_id: str, video_id: str) -> bool:
    items = _service().playlistItems().list(
        part="id", playlistId=playlist_id, videoId=video_id, maxResults=1).execute().get("items", [])
    return bool(items)


def add_to_playlist(playlist_id: str, video_id: str) -> str:
    """Put a video in a playlist; returns the new playlist item's id."""
    body = {"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
    return _service().playlistItems().insert(part="snippet", body=body).execute().get("id", "")


def playlist_item_id(playlist_id: str, video_id: str) -> str | None:
    """The id of the playlist item holding this video in this playlist, or None."""
    items = _service().playlistItems().list(
        part="id", playlistId=playlist_id, videoId=video_id, maxResults=1).execute().get("items", [])
    return items[0]["id"] if items else None


def remove_from_playlist(playlist_item: str) -> None:
    """Take a video out of a playlist (playlistItems.delete, 50 quota units)."""
    _service().playlistItems().delete(id=playlist_item).execute()
