"""Import meetings straight from the company Zoom account.

Uses a Server-to-Server OAuth app (ZOOM_ACCOUNT_ID / ZOOM_CLIENT_ID /
ZOOM_CLIENT_SECRET). Needed scopes: cloud_recording:read:list_user_recordings:admin
(list) and cloud_recording:read:recording:admin (one meeting's files).

Zoom's own transcript (or, failing that, its closed captions) comes along
with the video, so these jobs never need WhisperX.
"""

import threading
import time
import urllib.parse
import uuid
from datetime import date, timedelta
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger

logger = get_logger(__name__)

API = "https://api.zoom.us/v2"
TOKEN_URL = "https://zoom.us/oauth/token"

# Which MP4 to cut when Zoom recorded several layouts, best first.
MP4_PREFERENCE = [
    "shared_screen_with_speaker_view",
    "shared_screen_with_gallery_view",
    "active_speaker",
    "gallery_view",
    "speaker_view",
    "shared_screen",
]
# Zoom's post-meeting audio transcript first; live closed captions as fallback.
TRANSCRIPT_TYPES = ["TRANSCRIPT", "CC"]

_token_lock = threading.Lock()
_token: dict = {"value": None, "expires": 0.0}


class ZoomError(RuntimeError):
    pass


def is_configured() -> bool:
    s = get_settings()
    return bool(s.zoom_account_id and s.zoom_client_id and s.zoom_client_secret)


def _client():
    import httpx

    return httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True)


def _access_token() -> str:
    with _token_lock:
        if _token["value"] and time.time() < _token["expires"] - 60:
            return _token["value"]
        s = get_settings()
        with _client() as client:
            resp = client.post(
                TOKEN_URL,
                params={"grant_type": "account_credentials", "account_id": s.zoom_account_id},
                auth=(s.zoom_client_id, s.zoom_client_secret),
            )
        data = resp.json() if resp.content else {}
        if resp.status_code != 200 or "access_token" not in data:
            raise ZoomError(f"Zoom sign-in failed ({resp.status_code}): {data.get('reason') or data}")
        _token["value"] = data["access_token"]
        _token["expires"] = time.time() + float(data.get("expires_in", 3600))
        return _token["value"]


def _api_get(path: str, params: dict | None = None) -> dict:
    with _client() as client:
        resp = client.get(f"{API}/{path}", params=params, headers={"Authorization": f"Bearer {_access_token()}"})
    data = resp.json() if resp.content else {}
    if resp.status_code != 200:
        raise ZoomError(f"Zoom API {path} failed ({resp.status_code}): {data.get('message') or data}")
    return data


def _meeting_path_id(meeting_uuid: str) -> str:
    """Zoom requires double URL-encoding for UUIDs that start with / or contain //."""
    once = urllib.parse.quote(meeting_uuid, safe="")
    if meeting_uuid.startswith("/") or "//" in meeting_uuid:
        return urllib.parse.quote(once, safe="")
    return once


def _pick_mp4(files: list[dict]) -> dict | None:
    mp4s = [f for f in files if f.get("file_type") == "MP4" and f.get("status", "completed") == "completed"]
    if not mp4s:
        return None
    rank = {kind: i for i, kind in enumerate(MP4_PREFERENCE)}
    return min(mp4s, key=lambda f: rank.get(f.get("recording_type"), len(rank)))


def _pick_transcript(files: list[dict]) -> dict | None:
    for kind in TRANSCRIPT_TYPES:
        for f in files:
            if f.get("file_type") == kind and f.get("status", "completed") == "completed":
                return f
    return None


def summarize(meeting: dict) -> dict:
    """What the UI shows for one recorded meeting."""
    files = meeting.get("recording_files", [])
    mp4 = _pick_mp4(files)
    transcript = _pick_transcript(files)
    processing = any(f.get("status") == "processing" for f in files) or not files or not any(
        f.get("file_type") for f in files
    )
    return {
        "uuid": meeting.get("uuid"),
        "topic": meeting.get("topic") or "Untitled meeting",
        "start_time": meeting.get("start_time"),
        "duration_min": meeting.get("duration"),
        "has_video": mp4 is not None,
        "transcript_type": transcript.get("file_type") if transcript else None,
        "size_mb": round((mp4 or {}).get("file_size", 0) / 1_048_576),
        "processing": processing,
        "ready": mp4 is not None and transcript is not None,
    }


def list_recordings(days: int = 30) -> list[dict]:
    """Recorded meetings of the Zoom user, newest first.

    Zoom answers at most one month per request, so longer ranges are walked
    in 30-day windows.
    """
    user = get_settings().zoom_user
    end = date.today()
    start = end - timedelta(days=max(1, min(days, 180)))
    meetings: list[dict] = []
    window_end = end
    while window_end > start:
        window_start = max(start, window_end - timedelta(days=30))
        token = ""
        while True:
            params = {"from": window_start.isoformat(), "to": window_end.isoformat(), "page_size": 300}
            if token:
                params["next_page_token"] = token
            data = _api_get(f"users/{urllib.parse.quote(user, safe='@')}/recordings", params)
            meetings.extend(data.get("meetings", []))
            token = data.get("next_page_token") or ""
            if not token:
                break
        window_end = window_start - timedelta(days=1)

    seen: set[str] = set()
    unique = []
    for m in meetings:
        if m.get("uuid") in seen:
            continue
        seen.add(m.get("uuid"))
        unique.append(summarize(m))
    unique.sort(key=lambda m: m["start_time"] or "", reverse=True)
    return unique


def get_meeting(meeting_uuid: str) -> dict:
    return _api_get(f"meetings/{_meeting_path_id(meeting_uuid)}/recordings")


def _download(url: str, dest: Path) -> None:
    import httpx

    tmp = dest.with_suffix(dest.suffix + ".part")
    timeout = httpx.Timeout(120.0, connect=15.0)
    # access_token as a query parameter: an Authorization header would be
    # forwarded to the storage host Zoom redirects to, which rejects it.
    with (
        httpx.Client(timeout=timeout, follow_redirects=True) as client,
        client.stream("GET", url, params={"access_token": _access_token()}) as resp,
    ):
        if resp.status_code != 200:
            raise ZoomError(f"Zoom download failed ({resp.status_code})")
        with tmp.open("wb") as out:
            for chunk in resp.iter_bytes(1024 * 1024):
                out.write(chunk)
    tmp.replace(dest)


def download_meeting(meeting_uuid: str) -> tuple[Path, Path, dict]:
    """Download the best MP4 and the transcript. Returns (video, transcript, summary)."""
    meeting = get_meeting(meeting_uuid)
    files = meeting.get("recording_files", [])
    mp4 = _pick_mp4(files)
    transcript = _pick_transcript(files)
    if mp4 is None:
        raise ZoomError("This meeting has no finished MP4 recording yet.")
    if transcript is None:
        raise ZoomError("Zoom has no transcript for this meeting yet. Try again later, or upload the files.")

    settings = get_settings()
    stem = uuid.uuid4().hex
    video_path = settings.videos_dir / f"{stem}.mp4"
    transcript_path = settings.transcripts_dir / f"{stem}.vtt"
    _download(transcript["download_url"], transcript_path)
    _download(mp4["download_url"], video_path)
    return video_path, transcript_path, summarize(meeting)
