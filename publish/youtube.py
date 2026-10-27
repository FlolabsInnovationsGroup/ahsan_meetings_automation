"""The company YouTube channel: connecting it and uploading one video.

One-time setup: an admin opens /youtube/connect, signs in with the Google
account that manages the channel and approves access. The resulting refresh
token is stored on the server (storage_dir/youtube_token.json, mode 600) and
every later upload uses it, so nobody else needs channel credentials. Only
the upload permission is requested (and read access to the channel's name).

What gets uploaded, in which order, and with which words is publish/run.py.

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
from core.models import YoutubePrivacy

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


def cancel_connect(state: str) -> str:
    """Forget a sign-in the admin turned down at Google. Returns where to send
    the browser back to (the page they started from)."""
    with _pending_lock:
        pending = _pending.pop(state, None)
    return pending[2] if pending is not None else "/app"


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
    *,
    tags: "list[str] | tuple[str, ...]" = (),
    session_uri: str | None = None,
    start_byte: int = 0,
    on_session: "callable[[str], None] | None" = None,
) -> str:
    """Resumable upload to the connected channel. Returns the YouTube video id.

    A new upload first asks YouTube for an upload link and hands it to
    `on_session` before any byte is sent; the caller records it, because one
    link can create at most one video (ledger/uploads.py). With `session_uri`
    the upload carries on through that link from `start_byte` instead (the
    title and description were fixed when the link was made)."""
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    creds = _load_credentials()
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    snippet = {"title": title, "description": description}
    if tags:
        snippet["tags"] = list(tags)
    body = {
        "snippet": snippet,
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(path), mimetype="video/*", chunksize=CHUNK_SIZE, resumable=True)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)
    if session_uri:
        request.resumable_uri = session_uri
        request.resumable_progress = start_byte
    else:
        request.resumable_uri = _start_session(request)
        if on_session is not None:
            on_session(request.resumable_uri)

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


def _start_session(request) -> str:
    """Ask YouTube for the upload link (the first step of a resumable upload,
    as googleapiclient's next_chunk does it, done here so the link can be
    recorded before any byte is sent). Making a link creates no video, so
    asking again after an error is safe."""
    from googleapiclient.errors import HttpError

    headers = dict(request.headers)
    headers["X-Upload-Content-Type"] = request.resumable.mimetype()
    headers["X-Upload-Content-Length"] = str(request.resumable.size())
    headers["content-length"] = str(request.body_size)
    retries = 0
    while True:
        try:
            resp, content = request.http.request(
                request.uri, method=request.method, body=request.body, headers=headers
            )
        except (ConnectionError, TimeoutError, OSError):
            if retries >= MAX_RETRIES:
                raise
            retries += 1
            time.sleep(min(2**retries, 60))
            continue
        if resp.status == 200 and "location" in resp:
            return resp["location"]
        if resp.status in _RETRIABLE_STATUS and retries < MAX_RETRIES:
            retries += 1
            time.sleep(min(2**retries, 60))
            continue
        raise HttpError(resp, content, uri=request.uri)


def session_status(session_uri: str, size: int) -> tuple[str, "str | int | None"]:
    """Where an upload link stands: ("done", video_id) when YouTube has the
    whole video, ("partial", next_byte) when it has part of it, ("gone",
    None) when YouTube no longer knows the link (it expired). Raises on a
    network error or any other answer: try again later."""
    from googleapiclient.errors import HttpError

    http = _authorized_http(_load_credentials())
    resp, content = http.request(
        session_uri, "PUT", headers={"Content-Range": f"bytes */{size}", "Content-Length": "0"}
    )
    if resp.status in (200, 201):
        return "done", json.loads(content)["id"]
    if resp.status == 308:
        received = resp.get("range")  # "bytes=0-1234"; none = nothing received yet
        return "partial", int(received.rsplit("-", 1)[1]) + 1 if received else 0
    if resp.status in (404, 410):
        return "gone", None
    raise HttpError(resp, content, uri=session_uri)


def find_by_marker(marker: str, max_pages: int = 4) -> str | None:
    """The id of the channel's video tagged `marker` among its latest uploads
    (50 per page), or None. Tags are only shown to the channel's owner, which
    the connected account is (read access is part of the permission asked)."""
    from googleapiclient.discovery import build

    service = build("youtube", "v3", credentials=_load_credentials(), cache_discovery=False)
    channels = service.channels().list(part="contentDetails", mine=True).execute().get("items", [])
    if not channels:
        return None
    playlist = channels[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    token = None
    for _ in range(max_pages):
        params = {"part": "contentDetails", "playlistId": playlist, "maxResults": 50}
        if token:
            params["pageToken"] = token
        page = service.playlistItems().list(**params).execute()
        ids = [item["contentDetails"]["videoId"] for item in page.get("items", [])]
        if ids:
            videos = service.videos().list(part="snippet", id=",".join(ids), maxResults=50).execute()
            for video in videos.get("items", []):
                if marker in (video.get("snippet", {}).get("tags") or []):
                    return video["id"]
        token = page.get("nextPageToken")
        if not token:
            break
    return None


def _authorized_http(creds):
    import google_auth_httplib2
    from googleapiclient.http import build_http

    return google_auth_httplib2.AuthorizedHttp(creds, http=build_http())


def friendly_error(exc: Exception) -> str:
    text = str(exc)
    if "quotaExceeded" in text or "uploadLimitExceeded" in text:
        return "YouTube upload limit reached for today. Try again tomorrow."
    if "invalid_grant" in text:
        return "The YouTube connection expired or was revoked. Reconnect the channel and publish again."
    if isinstance(exc, YoutubeNotConnectedError):
        return str(exc)
    return f"Upload failed: {text[:300]}"


def is_account_wide_error(exc: Exception) -> bool:
    """The channel or project itself refused (not connected, token revoked,
    quota or upload limit, no permission): every further upload in the same
    run would fail the same way, so the run stops instead of trying them."""
    if isinstance(exc, YoutubeNotConnectedError) or type(exc).__name__ == "RefreshError":
        return True
    if getattr(getattr(exc, "resp", None), "status", None) in (401, 403):
        return True
    text = str(exc)
    return any(marker in text for marker in ("quotaExceeded", "uploadLimitExceeded", "invalid_grant"))
