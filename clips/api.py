"""The clips web page and its JSON API.

Run with: uvicorn clips.api:app

Other clips modules are imported inside the functions that use them, so this
file loads (and its tests run) even when a module is missing or replaced.
Tests swap the Store and Runner through `get_store` and `get_runner`.
"""

from __future__ import annotations

import hmac
import importlib
import secrets
import threading
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, StrictBool
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from clips.config import clips_settings
from clips.models import Clip, ClipStatus, Copy, Platform, RightsBasis, Source

API_KEY_HEADER = "X-API-Key"
API_KEY_COOKIE = "clips_api_key"
# The page, the health check, the login call and Google's return trip need no key.
# The callback only acts on a one-time state handed out by /youtube/connect (which needs the key).
PUBLIC_PATHS = frozenset({"/", "/health", "/api/login", "/youtube/callback", "/favicon.ico"})
# With no key set, only the page and the health check answer unless the page is addressed as localhost.
OPEN_PATHS = frozenset({"/", "/health", "/favicon.ico"})
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
INDEX_FILE = Path(__file__).parent / "web" / "index.html"
EDITABLE = (ClipStatus.DRAFT, ClipStatus.APPROVED)


# --------------------------------------------------------------------- wiring

_lock = threading.RLock()  # get_runner calls get_store while holding it
_store: Any = None
_runner: Any = None


def _mod(name: str) -> Any:
    """Load a clips module at call time (tests may put a fake in sys.modules)."""
    return importlib.import_module(f"clips.{name}")


def get_store() -> Any:
    """The one Store of this process. Tests replace this function."""
    global _store
    with _lock:
        if _store is None:
            _store = _mod("store").Store()
        return _store


def get_runner() -> Any:
    """The one Runner of this process. Tests replace this function."""
    global _runner
    with _lock:
        if _runner is None:
            _runner = _mod("runner").Runner(get_store())
        return _runner


# ----------------------------------------------------------------------- auth


def _local_only() -> bool:
    """True when the configured public address is this machine (an SSH tunnel or a local run)."""
    return (urlparse(clips_settings().public_base_url).hostname or "") in LOCAL_HOSTS


def _gate(scope: Scope) -> tuple[int, str] | None:
    """None to let the request in, or (status, message) to turn it away."""
    token = clips_settings().api_token
    conn = HTTPConnection(scope)
    if not token:
        if _local_only() or conn.url.path in OPEN_PATHS:
            return None
        return 503, "set CLIPS_API_TOKEN: with no key the API is only served when CLIPS_PUBLIC_BASE_URL is localhost"
    if conn.url.path in PUBLIC_PATHS:
        return None
    supplied = conn.headers.get(API_KEY_HEADER) or unquote(conn.cookies.get(API_KEY_COOKIE) or "")
    if hmac.compare_digest(supplied.encode(), token.encode()):
        return None
    return 401, f"missing or wrong {API_KEY_HEADER}"


class ApiKeyMiddleware:
    """Check the key before routing, so unknown paths do not leak either."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        refused = None if scope["type"] not in ("http", "websocket") else _gate(scope)
        if refused is None:
            await self.app(scope, receive, send)
            return
        response = JSONResponse(status_code=refused[0], content={"detail": refused[1]})
        await response(scope, receive, send)


app = FastAPI(title="Clips", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(ApiKeyMiddleware)


class LoginBody(BaseModel):
    token: str = ""


@app.post("/api/login")
def login(body: LoginBody, response: Response) -> dict:
    """Set the key cookie (so <video> tags work without headers) when the token is right."""
    token = clips_settings().api_token
    if token and not hmac.compare_digest(body.token.encode(), token.encode()):
        raise HTTPException(401, "wrong key")
    if token:
        response.set_cookie(API_KEY_COOKIE, quote(token), httponly=True, samesite="strict", max_age=30 * 86400)
    return {"ok": True}


# ---------------------------------------------------------------------- pages


@app.get("/")
def index() -> FileResponse:
    return FileResponse(INDEX_FILE, media_type="text/html", headers={"Cache-Control": "no-store"})


@app.get("/health")
def health() -> dict:
    return {"ok": True}


# --------------------------------------------------------------------- helpers


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _safe(fn, default=None):
    """Run a helper that may fail (module missing, not connected) and fall back."""
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _source_json(source: Source) -> dict:
    data = source.model_dump(mode="json")
    verdict = _safe(lambda: _mod("rights").check_source(source))
    if verdict is None:
        data["can_clip"], data["reasons"] = False, ["the rights check is not available"]
    else:
        data["can_clip"], data["reasons"] = verdict.ok, list(verdict.reasons)
    data["download_note"] = ""
    if source.platform is Platform.YOUTUBE and not clips_settings().youtube_downloads:
        data["download_note"] = (
            "YouTube downloads are off (CLIPS_YOUTUBE_DOWNLOADS=false), so this source is not listed. "
            "Use a watched folder, or switch the setting on if you have decided to allow downloads."
        )
    return data


def _clip_json(clip: Clip, store: Any) -> dict:
    data = clip.model_dump(mode="json")
    data["media_url"] = f"/media/{clip.id}" if clip.file else ""
    data["thumb_url"] = f"/thumb/{clip.id}" if clip.thumb else ""
    source = _safe(lambda: store.get_source(clip.source_id))
    data["source_name"] = source.name if source else clip.source_id
    data["problems"] = []
    if clip.text is not None:
        pkg = _safe(lambda: _mod("package"))
        if pkg is not None:
            data["problems"] = _safe(
                lambda: pkg.check_copy(clip.text, kind=clip.kind, credit=clip.credit), []
            )
    return data


def _get_clip(store: Any, clip_id: str) -> Clip:
    clip = store.get_clip(clip_id)
    if clip is None:
        raise HTTPException(404, "no such clip")
    return clip


def _problem(status: int, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status, {"error": message, **extra})


def _rights_error(exc: Exception) -> HTTPException:
    reasons = getattr(exc, "reasons", None) or [str(exc)]
    return _problem(400, str(exc), reasons=list(reasons))


# ---------------------------------------------------------------------- status


def _isolation_problem() -> str:
    """Why this process is not working on the clips folder alone, or empty when it is."""
    try:
        _mod("guard").assert_isolated()
    except Exception as exc:  # noqa: BLE001 - GuardError, or the guard missing: either way, not safe
        return str(exc) or type(exc).__name__
    return ""


def _require_isolated() -> None:
    problem = _isolation_problem()
    if problem:
        raise _problem(503, f"not available: {problem}")


def _allowance_and_slots(store: Any) -> dict:
    now = datetime.now(UTC)
    settings = clips_settings()
    slots = _mod("slots")
    first = store.first_post_date()
    out: dict = {"allowed_today": slots.allowance_on(now.date(), first, settings)}
    out["next_slots"] = [_iso(s) for s in slots.next_slots(now, store.booked_times(), settings, first, count=3)]
    return out


@app.get("/api/status")
def status() -> dict:
    store = get_store()
    data: dict = _safe(lambda: dict(get_runner().status()), {}) or {}
    guard = _safe(lambda: _mod("guard"))
    problem = _isolation_problem()
    # Off the clips folder the sign-in token on disk would be the company's: never read it.
    channel = None if problem or "channel" in data else _safe(lambda: _mod("uploader").connected_channel())
    extras = {
        "isolated": not problem,
        "isolation_problem": problem,
        "channel": {"id": channel[0], "title": channel[1]} if channel else None,
        "kill_switch": bool(_safe(lambda: guard.kill_switch_on(), False)),
        "breaker_open": bool(_safe(lambda: guard.breaker_open(store), False)),
        "tick_running": _tick_state["running"],
        "auto_approve": bool(_safe(lambda: get_runner().auto_approve_on(), False)),
        **(_safe(lambda: _allowance_and_slots(store), {}) or {}),
    }
    for key, value in extras.items():
        data.setdefault(key, value)
    return data


# --------------------------------------------------------------------- sources


class SourceBody(BaseModel):
    name: str = Field(min_length=1)
    platform: Platform
    rights: RightsBasis = RightsBasis.NONE
    enabled: bool = False
    url: str = ""
    channel_id: str = ""
    folder: str = ""
    evidence_url: str = ""
    evidence_note: str = ""
    evidence_date: str = ""
    credit: str = ""
    skip_music: bool = True
    avoid_spoilers: bool = True
    notes: str = ""


@app.get("/api/sources")
def list_sources() -> list[dict]:
    return [_source_json(s) for s in get_store().list_sources()]


@app.post("/api/sources")
def add_source(body: SourceBody) -> dict:
    fields = body.model_dump(exclude={"name", "platform", "rights", "enabled"})
    try:
        source = _mod("sources").add_source(
            get_store(), name=body.name, platform=body.platform, rights=body.rights,
            enabled=body.enabled, **fields,
        )
    except ValueError as exc:  # RightsError is a ValueError
        raise _rights_error(exc) from exc
    return _source_json(source)


def _source_action(source_id: str, action: str) -> dict:
    store = get_store()
    if store.get_source(source_id) is None:
        raise HTTPException(404, "no such source")
    try:
        return _source_json(getattr(_mod("sources"), action)(store, source_id))
    except KeyError as exc:
        raise HTTPException(404, "no such source") from exc
    except ValueError as exc:
        raise _rights_error(exc) from exc


@app.post("/api/sources/{source_id}/enable")
def enable_source(source_id: str) -> dict:
    return _source_action(source_id, "enable_source")


@app.post("/api/sources/{source_id}/disable")
def disable_source(source_id: str) -> dict:
    return _source_action(source_id, "disable_source")


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: str) -> dict:
    if not get_store().delete_source(source_id):
        raise HTTPException(404, "no such source")
    return {"ok": True}


# ----------------------------------------------------------------------- clips


@app.get("/api/clips")
def list_clips(status: str = "", source_id: str = "", limit: int = 200) -> list[dict]:
    store = get_store()
    try:
        wanted = [ClipStatus(s.strip()) for s in status.split(",") if s.strip()] or None
    except ValueError as exc:
        raise _problem(400, f"unknown status: {status}") from exc
    clips = store.list_clips(wanted, source_id or None, max(1, min(limit, 500)))
    return [_clip_json(c, store) for c in clips]


@app.get("/api/clips/{clip_id}")
def get_clip(clip_id: str) -> dict:
    store = get_store()
    return _clip_json(_get_clip(store, clip_id), store)


class ClipEdit(BaseModel):
    title: str | None = None
    description: str | None = None
    hashtags: list[str] | None = None


@app.patch("/api/clips/{clip_id}")
def edit_clip(clip_id: str, body: ClipEdit) -> dict:
    store = get_store()
    clip = _get_clip(store, clip_id)
    if clip.status not in EDITABLE:
        raise _problem(409, f"a {clip.status.value} clip cannot be edited")
    if clip.text is None:
        raise _problem(409, "this clip has no text yet")
    changes = body.model_dump(exclude_none=True)
    text = Copy(**{**clip.text.model_dump(), **changes})
    problems = _mod("package").check_copy(text, kind=clip.kind, credit=clip.credit)
    if problems:
        raise _problem(422, "the text has problems", problems=list(problems))
    return _clip_json(store.set_clip_status(clip_id, clip.status, text=text), store)


class RejectBody(BaseModel):
    reason: str = ""


def _review(clip_id: str, action: str, *args: Any) -> dict:
    store = get_store()
    _get_clip(store, clip_id)
    try:
        clip = getattr(get_runner(), action)(clip_id, *args)
    except KeyError as exc:
        raise HTTPException(404, "no such clip") from exc
    except ValueError as exc:
        raise _problem(409, str(exc)) from exc
    return _clip_json(clip, store)


@app.post("/api/clips/{clip_id}/approve")
def approve_clip(clip_id: str) -> dict:
    return _review(clip_id, "approve")


@app.post("/api/clips/{clip_id}/release")
def release_clip(clip_id: str) -> dict:
    """Put a held clip back in the queue (it gets a new slot) once the rights checks pass again."""
    return _review(clip_id, "release")


@app.post("/api/clips/{clip_id}/reject")
def reject_clip(clip_id: str, body: RejectBody | None = None) -> dict:
    return _review(clip_id, "reject", (body.reason if body else "").strip())


# ----------------------------------------------------------------------- media


def _render_file(clip_id: str, field: str) -> Path:
    """The clip's rendered file, only when it really lives inside the renders folder."""
    clip = _get_clip(get_store(), clip_id)
    name = getattr(clip, field)
    if not name:
        raise HTTPException(404, "no file")
    root = clips_settings().renders_dir.resolve()
    try:
        path = Path(name).resolve()
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "no file") from exc
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(404, "no file")
    return path


@app.get("/media/{clip_id}")
def media(clip_id: str) -> FileResponse:
    return FileResponse(_render_file(clip_id, "file"), media_type="video/mp4")


@app.get("/thumb/{clip_id}")
def thumb(clip_id: str) -> FileResponse:
    return FileResponse(_render_file(clip_id, "thumb"))


# ----------------------------------------------------------- safety controls


class KillBody(BaseModel):
    reason: str = ""


@app.post("/api/kill")
def kill(body: KillBody | None = None) -> dict:
    reason = (body.reason if body else "").strip() or "stopped from the page"
    report = _mod("guard").set_kill_switch(True, reason) or {}
    get_store().add_event("kill", f"kill switch on: {reason}")
    out: dict = {"kill_switch": True}
    if report.get("pulled_back") or report.get("failed") or report.get("skipped"):
        out["pull_back"] = {
            "pulled_back": list(report.get("pulled_back") or []),
            "failed": list(report.get("failed") or []),
            "skipped": report.get("skipped") or "",
        }
    return out


@app.post("/api/resume")
def resume() -> dict:
    _mod("guard").set_kill_switch(False, "")
    get_store().add_event("resume", "kill switch off")
    return {"kill_switch": False}


@app.post("/api/breaker/reset")
def breaker_reset() -> dict:
    store = get_store()
    _mod("guard").reset_breaker(store)
    store.add_event("breaker", "breaker reset by a person")
    return {"breaker_open": False}


class AutoApproveBody(BaseModel):
    on: StrictBool  # a real true or false, never the text "yes"


@app.post("/api/auto-approve")
def auto_approve(body: AutoApproveBody) -> dict:
    """Switch Auto-approve Shorts on or off. Only on the clips folder, like the other changing routes."""
    _require_isolated()
    return {"auto_approve": bool(get_runner().set_auto_approve(body.on))}


# ------------------------------------------------------------------------ tick

_tick_state: dict = {"running": False, "last": None, "error": ""}
_tick_lock = threading.Lock()


def _run_tick(runner: Any) -> None:
    try:
        _tick_state["last"], _tick_state["error"] = runner.tick(), ""
    except Exception as exc:  # noqa: BLE001 - shown in the status, never crashes the thread silently
        _tick_state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        with _tick_lock:
            _tick_state["running"] = False


@app.post("/api/tick", status_code=202)
def tick() -> dict:
    _require_isolated()  # the pass fetches, calls the model and uploads: only on the clips folder
    runner = get_runner()
    with _tick_lock:
        if _tick_state["running"]:
            raise _problem(409, "a tick is already running")
        _tick_state["running"] = True
    threading.Thread(target=_run_tick, args=(runner,), daemon=True, name="clips-tick").start()
    return {"started": True}


@app.get("/api/tick")
def tick_state() -> dict:
    return dict(_tick_state)


@app.get("/api/events")
def events(limit: int = 100, kind: str = "") -> list[dict]:
    rows = get_store().list_events(max(1, min(limit, 500)), kind or None)
    return [{**r, "at": _iso(r["at"]) if isinstance(r.get("at"), datetime) else r.get("at")} for r in rows]


# --------------------------------------------------------------- YouTube sign-in

_pending_states: set[str] = set()


def _plain_page(title: str, message: str, status_code: int = 200) -> HTMLResponse:
    body = (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title><body style='font:16px system-ui;max-width:32rem;margin:3rem auto;padding:0 1rem'>"
        f"<h1>{escape(title)}</h1><p>{escape(message)}</p><p><a href='/'>Back to the clips page</a></p>"
    )
    return HTMLResponse(body, status_code=status_code)


@app.get("/youtube/connect")
def youtube_connect() -> RedirectResponse:
    _require_isolated()
    state = secrets.token_urlsafe(16)
    try:
        url = _mod("uploader").auth_url(state)
    except Exception as exc:
        raise _problem(503, f"cannot start sign-in: {exc}") from exc
    _pending_states.add(state)
    return RedirectResponse(url, status_code=302)


@app.get("/youtube/callback")
def youtube_callback(state: str = "", code: str = "", error: str = "") -> HTMLResponse:
    problem = _isolation_problem()
    if problem:
        return _plain_page("Not available", f"{problem} Nothing was connected.", 503)
    if state not in _pending_states:
        return _plain_page("Sign-in refused", "This sign-in was not started from here.", 400)
    _pending_states.discard(state)
    if error or not code:
        return _plain_page("Sign-in cancelled", f"Google did not connect a channel ({error or 'no code'}).", 400)
    try:
        channel_id, title = _mod("uploader").finish_auth(code)
        _mod("guard").check_channel(channel_id)
    except Exception as exc:  # noqa: BLE001 - GuardError or a Google error: say why, in plain words
        return _plain_page("Channel refused", f"{exc} Nothing will be uploaded to it.", 400)
    return _plain_page("Channel connected", f"Connected: {title} ({channel_id}).")
