"""Zoom's webhook: "a recording (or its transcript) is ready".

It only wakes the autopilot up; the next step asks Zoom's API about the
meeting itself, so nothing in a webhook body is trusted beyond its signature.
Zoom's rules (developers.zoom.us, Webhooks):
- every call is signed: x-zm-signature = "v0=" + HMAC-SHA256 of
  "v0:{x-zm-request-timestamp}:{raw body}" keyed with the app's secret token;
- endpoint.url_validation must be answered with the plain token and its
  HMAC-SHA256 (hex) under the same secret;
- the answer must come within 3 seconds, and Zoom retries a failed call only
  three times, so the timed checks stay the backbone.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from core.config import get_settings

# Zoom's timestamp is in milliseconds; a call older than this is refused (a replay)
MAX_AGE_S = 300
MAX_BODY = 256 * 1024
RECORDING_EVENTS = frozenset({"recording.completed", "recording.transcript_completed"})


class WebhookRefused(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def _sign(secret: str, message: str) -> str:
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def verify(body: bytes, signature: str | None, timestamp: str | None, now: float | None = None) -> dict:
    """The event, once its signature and age check out; raises WebhookRefused."""
    secret = get_settings().zoom_webhook_secret_token
    if not secret:
        raise WebhookRefused(503, "ZOOM_WEBHOOK_SECRET_TOKEN is not set on this server")
    if len(body) > MAX_BODY:
        raise WebhookRefused(413, "too large")
    if not signature or not timestamp:
        raise WebhookRefused(401, "unsigned")
    expected = "v0=" + _sign(secret, f"v0:{timestamp}:{body.decode('utf-8', errors='replace')}")
    if not hmac.compare_digest(signature.encode(), expected.encode()):
        raise WebhookRefused(401, "bad signature")
    try:
        sent = float(timestamp)
    except ValueError as exc:
        raise WebhookRefused(401, "bad timestamp") from exc
    sent = sent / 1000 if sent > 1e11 else sent  # Zoom sends milliseconds
    if abs((now if now is not None else time.time()) - sent) > MAX_AGE_S:
        raise WebhookRefused(401, "too old")
    try:
        event = json.loads(body)
    except ValueError as exc:
        raise WebhookRefused(400, "not JSON") from exc
    if not isinstance(event, dict):
        raise WebhookRefused(400, "not an event")
    return event


def url_validation(event: dict) -> dict:
    plain = str((event.get("payload") or {}).get("plainToken") or "")
    return {"plainToken": plain, "encryptedToken": _sign(get_settings().zoom_webhook_secret_token, plain)}


def recording_uuid(event: dict) -> str | None:
    """The meeting instance a recording event is about, else None."""
    if event.get("event") not in RECORDING_EVENTS:
        return None
    uuid = ((event.get("payload") or {}).get("object") or {}).get("uuid")
    return uuid if isinstance(uuid, str) and 0 < len(uuid) <= 200 else None
