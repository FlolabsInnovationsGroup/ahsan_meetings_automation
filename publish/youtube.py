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
from datetime import UTC, datetime
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger
from core.models import YoutubePrivacy

logger = get_logger(__name__)

_API = "https://www.googleapis.com/auth/"
# What uploading needs. Read-only is used to show which channel is connected so
# nobody uploads to the wrong one, and to check a new upload came out as asked.
BASE_SCOPES = [_API + "youtube.upload", _API + "youtube.readonly"]
# What the extras need: caption tracks, playlists and changing videos after
# upload (force-ssl), and view analytics. Never the full "youtube" scope,
# youtubepartner or the money analytics scope.
WIDER_SCOPES = [_API + "youtube.force-ssl", _API + "yt-analytics.readonly"]
SCOPES = BASE_SCOPES + WIDER_SCOPES

# Which permission each optional feature needs, and how to say it to the owner.
FEATURE_SCOPES = {
    "thumbnails": _API + "youtube.upload",
    "translations": _API + "youtube.upload",
    "captions": _API + "youtube.force-ssl",
    "playlists": _API + "youtube.force-ssl",
    "edit": _API + "youtube.force-ssl",
    "analytics": _API + "yt-analytics.readonly",
}
FEATURE_WORDS = {
    "thumbnails": "thumbnails",
    "translations": "translated titles",
    "captions": "caption tracks",
    "playlists": "playlists",
    "edit": "changing videos after upload",
    "analytics": "view analytics",
}
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
    granted = sorted(granted_scopes()) if connected else []
    missing = [f for f, scope in FEATURE_SCOPES.items() if scope not in granted] if connected else []
    reconnect_for = []
    for feature in ("captions", "playlists", "edit", "analytics"):
        if feature in missing:
            reconnect_for.append(FEATURE_WORDS[feature])
    return {
        "configured": is_configured(), "connected": connected, "channel_title": channel,
        "granted": granted, "missing": missing,
        "features": {f: f not in missing and connected for f in ("captions", "playlists", "edit", "analytics")},
        "reconnect_for": reconnect_for,
    }


def granted_scopes() -> set[str]:
    """The permissions the connected token really has: what Google granted
    (stored when connecting), else the token's own `scopes` (a token from
    before this was stored), else the original upload and read pair."""
    try:
        data = json.loads(_token_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    return set(_scope_list(data.get("granted_scopes") or data.get("scopes")) or BASE_SCOPES)


def has_scope(scope: str) -> bool:
    return scope in granted_scopes()


def _scope_list(value) -> list[str]:
    """Scopes as a sorted list, from a list or a space-separated string."""
    if not value:
        return []
    if isinstance(value, str):
        value = value.split()
    return sorted({str(s) for s in value if s})


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
        include_granted_scopes="true",  # a reconnect adds to what was approved before
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

    # Google lets the person untick boxes, so read what was really granted
    raw = getattr(creds, "granted_scopes", None)
    if not raw:
        try:
            raw = flow.oauth2session.token.get("scope")
        except (AttributeError, TypeError):
            raw = None
    granted = _scope_list(raw)
    if _API + "youtube.upload" not in granted:
        raise ValueError("Google didn't grant the upload permission; press Connect again and leave every box ticked")
    not_granted = [s.rsplit("/", 1)[-1] for s in WIDER_SCOPES if s not in granted]
    if not_granted:
        logger.warning("YouTube connected without: %s (those extras stay off until reconnecting)",
                       ", ".join(not_granted))

    channel_title = _channel_title(creds)
    data = json.loads(creds.to_json())
    data["channel_title"] = channel_title
    data["granted_scopes"] = granted
    _write_secret(_token_path(), json.dumps(data))
    logger.info("YouTube connected to channel %r", channel_title)
    try:  # follow-ups that waited for a permission can run now
        from publish import extras

        extras.wake()
    except Exception as exc:  # noqa: BLE001 - connecting must not fail because of the queue
        logger.warning("could not wake the follow-up queue: %s", exc)
    return return_to


def _count(units: int, kind: str) -> None:
    """Count quota units of an essential call. Counting must never break the call."""
    try:
        from publish import quota

        quota.spend(units, kind, essential=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not count %d YouTube quota unit(s) for %s: %s", units, kind, exc)


def _count_upload_call() -> None:
    try:
        from publish import quota

        quota.spend_upload_call()
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not count an upload call: %s", exc)


# One refresh-and-save at a time: the upload and the follow-up worker both load the
# token, and two refreshes writing the same file at once made one of them fail.
_TOKEN_LOCK = threading.RLock()


def _write_secret(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # a name of its own per write, so two writers never share one temporary file
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        with _TOKEN_LOCK:
            os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _load_credentials():
    with _TOKEN_LOCK:  # the file is re-read inside, so a second caller gets the token the first just wrote
        return _load_credentials_locked()


def _load_credentials_locked():
    path = _token_path()
    if not path.exists():
        raise YoutubeNotConnectedError("No YouTube channel connected yet.")
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    data = json.loads(path.read_text(encoding="utf-8"))
    # The scopes this token was granted, never the module's wider list: google-auth
    # sends them on every refresh, and asking an old token for a permission it
    # never got would make Google refuse, and uploads stop.
    own = _scope_list(data.get("granted_scopes") or data.get("scopes")) or list(BASE_SCOPES)
    creds = Credentials.from_authorized_user_info(data, own)
    if not creds.valid:
        creds.refresh(Request())
        refreshed = json.loads(creds.to_json())
        refreshed["channel_title"] = data.get("channel_title")
        refreshed["granted_scopes"] = own
        _write_secret(path, json.dumps(refreshed))
    return creds


def _channel_title(creds) -> str | None:
    from googleapiclient.discovery import build

    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    _count(1, "channels.list")
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
    publish_at: datetime | None = None,
    notify: bool = True,
    category_id: str | None = None,
    language: str | None = None,
    recording_date: str | None = None,
    localizations: dict | None = None,
) -> str:
    """Resumable upload to the connected channel. Returns the YouTube video id.

    A new upload first asks YouTube for an upload link and hands it to
    `on_session` before any byte is sent; the caller records it, because one
    link can create at most one video (ledger/uploads.py). With `session_uri`
    the upload carries on through that link from `start_byte` instead (the
    title and description were fixed when the link was made).

    With `publish_at` the video is uploaded private and YouTube makes it
    public at that time (see publish/schedule.py). `notify` says whether
    subscribers are told when it goes public."""
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    creds = _load_credentials()
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    def make_request(with_localizations: dict | None):
        body = insert_body(
            title, description, privacy, tags=tags, publish_at=publish_at, category_id=category_id,
            language=language, recording_date=recording_date, localizations=with_localizations,
        )
        parts = ["snippet", "status"]
        if "recordingDetails" in body:
            parts.append("recordingDetails")
        if "localizations" in body:
            parts.append("localizations")
        media = MediaFileUpload(str(path), mimetype="video/*", chunksize=CHUNK_SIZE, resumable=True)
        return service.videos().insert(part=",".join(parts), body=body, media_body=media,
                                       notifySubscribers=notify), "localizations" in body

    request, sent_localizations = make_request(localizations)
    if session_uri:
        request.resumable_uri = session_uri
        request.resumable_progress = start_byte
    else:
        _count_upload_call()
        try:
            request.resumable_uri = _start_session(request)
        except HttpError as exc:
            if not sent_localizations or getattr(exc.resp, "status", None) != 400:
                raise
            # YouTube refused the request with the translations in it. No upload
            # link exists yet and nothing is recorded before one does, so ask
            # once more without them.
            logger.warning("YouTube refused the upload request with translations; retrying without them")
            request, _ = make_request(None)
            _count_upload_call()
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


def insert_body(
    title: str,
    description: str,
    privacy: YoutubePrivacy,
    *,
    tags: "list[str] | tuple[str, ...]" = (),
    publish_at: datetime | None = None,
    category_id: str | None = None,
    language: str | None = None,
    recording_date: str | None = None,
    localizations: dict | None = None,
) -> dict:
    """The videos.insert request body. A scheduled video (`publish_at`) is
    always private: YouTube only schedules a private video that was never
    public, and flips it to public itself at that time."""
    snippet: dict = {"title": title, "description": description}
    if tags:
        snippet["tags"] = list(tags)
    if category_id:
        snippet["categoryId"] = str(category_id)
    if language:
        snippet["defaultLanguage"] = language
        snippet["defaultAudioLanguage"] = language
    status: dict = {
        "privacyStatus": "private" if publish_at is not None else privacy,
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": False,
    }
    if publish_at is not None:
        status["publishAt"] = publish_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    body: dict = {"snippet": snippet, "status": status}
    if recording_date:
        body["recordingDetails"] = {"recordingDate": f"{recording_date}T00:00:00Z"}
    if localizations and language:  # YouTube needs snippet.defaultLanguage to accept translations
        body["localizations"] = {lang: dict(text) for lang, text in localizations.items()}
    return body


def read_status(video_id: str) -> dict:
    """What YouTube says about a video's visibility: {"privacyStatus": ...,
    "publishAt": ... or None}. One quota unit. Used to check a new upload
    came out as asked."""
    from googleapiclient.discovery import build

    service = build("youtube", "v3", credentials=_load_credentials(), cache_discovery=False)
    _count(1, "videos.list")
    items = service.videos().list(part="status", id=video_id).execute().get("items", [])
    status = (items[0].get("status") if items else None) or {}
    return {"privacyStatus": status.get("privacyStatus"), "publishAt": status.get("publishAt")}


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
    _count(1, "channels.list")
    channels = service.channels().list(part="contentDetails", mine=True).execute().get("items", [])
    if not channels:
        return None
    playlist = channels[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    token = None
    for _ in range(max_pages):
        params = {"part": "contentDetails", "playlistId": playlist, "maxResults": 50}
        if token:
            params["pageToken"] = token
        _count(1, "playlistItems.list")
        page = service.playlistItems().list(**params).execute()
        ids = [item["contentDetails"]["videoId"] for item in page.get("items", [])]
        if ids:
            _count(1, "videos.list")
            videos = service.videos().list(part="snippet", id=",".join(ids)).execute()
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
