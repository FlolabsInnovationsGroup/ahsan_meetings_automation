"""Publish finished cuts to the company YouTube channel.

One-time setup: an admin opens /youtube/connect, signs in with the Google
account that manages the channel and approves access. The resulting refresh
token is stored on the server (storage_dir/youtube_token.json, mode 600) and
every later upload uses it, so nobody else needs channel credentials.

Google libraries are imported lazily so the rest of the service and the test
suite run without them.
"""

import json
import os
import secrets
import threading
import time
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger
from core.models import JobRecord, YoutubePrivacy

from .cleanup import delete_job_files

logger = get_logger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    # Read-only, used only to show which channel is connected so nobody
    # uploads to the wrong one.
    "https://www.googleapis.com/auth/youtube.readonly",
]
# Google may return scopes in a different order/form than requested.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

CHUNK_SIZE = 8 * 1024 * 1024
MAX_RETRIES = 5
_RETRIABLE_STATUS = {500, 502, 503, 504}
_PENDING_TTL_SECONDS = 600

_pending_lock = threading.Lock()
# state -> (created_at, code_verifier, return_to)
_pending: dict[str, tuple[float, str | None, str]] = {}


class YoutubeNotConnectedError(RuntimeError):
    pass


def _token_path() -> Path:
    return get_settings().storage_dir / "youtube_token.json"


def _redirect_uri() -> str:
    return get_settings().public_base_url.rstrip("/") + "/youtube/callback"


def _client_config() -> dict:
    settings = get_settings()
    return {
        "web": {
            "client_id": settings.youtube_client_id,
            "client_secret": settings.youtube_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def is_configured() -> bool:
    s = get_settings()
    return bool(s.youtube_client_id and s.youtube_client_secret and s.public_base_url)


def youtube_status() -> dict:
    """What the UI needs: is publishing set up, and to which channel."""
    path = _token_path()
    connected = path.exists()
    channel = None
    if connected:
        try:
            channel = json.loads(path.read_text(encoding="utf-8")).get("channel_title")
        except (OSError, ValueError):
            connected = False
    return {"configured": is_configured(), "connected": connected, "channel_title": channel}


def disconnect() -> None:
    _token_path().unlink(missing_ok=True)


def _safe_return_to(value: str | None) -> str:
    # Only same-site paths, never "//evil.com" or absolute URLs.
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def start_connect(return_to: str | None = None) -> str:
    """Return the Google consent URL to send the admin's browser to."""
    if not is_configured():
        raise YoutubeNotConnectedError(
            "YouTube publishing is not configured: set YOUTUBE_CLIENT_ID, "
            "YOUTUBE_CLIENT_SECRET and PUBLIC_BASE_URL in .env"
        )
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=_redirect_uri())
    state = secrets.token_urlsafe(24)
    url, _ = flow.authorization_url(
        access_type="offline",  # we need a refresh token
        prompt="consent",  # always return a refresh token, also on reconnect
        state=state,
    )
    now = time.time()
    with _pending_lock:
        for key in [k for k, v in _pending.items() if now - v[0] > _PENDING_TTL_SECONDS]:
            del _pending[key]
        _pending[state] = (now, flow.code_verifier, _safe_return_to(return_to))
    return url


def finish_connect(state: str, code: str) -> str:
    """Exchange the authorization code, store the token. Returns the return path."""
    with _pending_lock:
        pending = _pending.pop(state, None)
    if pending is None or time.time() - pending[0] > _PENDING_TTL_SECONDS:
        raise ValueError("This sign-in link expired or was already used. Start again from the page.")
    _, code_verifier, return_to = pending

    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        _client_config(), scopes=SCOPES, redirect_uri=_redirect_uri(), code_verifier=code_verifier
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    if not creds.refresh_token:
        raise ValueError("Google did not return a refresh token. Remove the app's access in the Google account and try again.")

    channel_title = _channel_title(creds)
    data = json.loads(creds.to_json())
    data["channel_title"] = channel_title
    _write_secret(_token_path(), json.dumps(data))
    logger.info("YouTube connected to channel %r", channel_title)
    return return_to


def _write_secret(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _load_credentials():
    path = _token_path()
    if not path.exists():
        raise YoutubeNotConnectedError("No YouTube channel connected yet.")
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    data = json.loads(path.read_text(encoding="utf-8"))
    creds = Credentials.from_authorized_user_info(data, SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        refreshed = json.loads(creds.to_json())
        refreshed["channel_title"] = data.get("channel_title")
        _write_secret(path, json.dumps(refreshed))
    return creds


def _channel_title(creds) -> str | None:
    from googleapiclient.discovery import build

    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = service.channels().list(part="snippet", mine=True).execute().get("items", [])
    return items[0]["snippet"]["title"] if items else None


def upload_video(
    path: Path,
    title: str,
    description: str,
    privacy: YoutubePrivacy,
    on_progress: "callable[[int], None] | None" = None,
) -> str:
    """Resumable upload to the connected channel. Returns the YouTube video id."""
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    creds = _load_credentials()
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {
        "snippet": {"title": title, "description": description},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(path), mimetype="video/*", chunksize=CHUNK_SIZE, resumable=True)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    retries = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            retries = 0
            if status and on_progress:
                on_progress(int(status.progress() * 100))
        except HttpError as exc:
            if exc.resp.status not in _RETRIABLE_STATUS or retries >= MAX_RETRIES:
                raise
            retries += 1
            time.sleep(min(2**retries, 60))
        except (ConnectionError, TimeoutError, OSError):
            if retries >= MAX_RETRIES:
                raise
            retries += 1
            time.sleep(min(2**retries, 60))
    return response["id"]


def publish_job(
    job: JobRecord,
    update: "callable[[JobRecord], None]",
    title: str,
    description: str,
    privacy: YoutubePrivacy,
    delete_after: bool,
) -> None:
    """Background task: upload the job's output, then optionally delete its files."""
    try:
        video_id = upload_video(
            Path(job.output_video_path),
            title,
            description,
            privacy,
            on_progress=lambda pct: _set_progress(job, update, pct),
        )
    except Exception as exc:  # reported to the user via the job record
        logger.exception("job %s: YouTube upload failed", job.job_id)
        job.youtube_status = "failed"
        job.youtube_error = _friendly_error(exc)
        update(job)
        return

    job.youtube_video_id = video_id
    job.youtube_url = f"https://youtu.be/{video_id}"
    job.youtube_status = "published"
    job.youtube_progress = 100
    job.youtube_error = None
    if delete_after:
        delete_job_files(job)
    update(job)


def _set_progress(job: JobRecord, update, pct: int) -> None:
    job.youtube_progress = max(0, min(99, pct))
    update(job)


def _friendly_error(exc: Exception) -> str:
    text = str(exc)
    if "quotaExceeded" in text or "uploadLimitExceeded" in text:
        return "YouTube upload limit reached for today. Try again tomorrow."
    if "invalid_grant" in text:
        return "The YouTube connection expired or was revoked. Reconnect the channel and publish again."
    if isinstance(exc, YoutubeNotConnectedError):
        return str(exc)
    return f"Upload failed: {text[:300]}"
