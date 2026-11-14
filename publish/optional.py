"""The one door for every optional YouTube call (thumbnails, caption tracks,
playlists, edits after upload, analytics).

An optional call must never hurt an upload or crash a worker, so call() runs
it behind these checks and never raises:

1. the permission the feature needs was not granted -> needs_reconnect, and
   nothing is called;
2. the day's quota reserve is reached (or Google said the quota is gone) ->
   retry after the next Pacific midnight, and nothing is called;
3. the call runs and its units are counted, even when it fails.

Google's answers map to one of: done, retry (with a back-off by attempt),
skipped (do not try again), needs_reconnect, stop_all (stop every optional
call: the connection is gone or the quota is used up).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from core.logging import get_logger
from publish import quota, youtube

logger = get_logger(__name__)

DONE = "done"
RETRY = "retry"
SKIPPED = "skipped"
NEEDS_RECONNECT = "needs_reconnect"
STOP_ALL = "stop_all"

# Back-off by attempt (0 = the first failure): 1 hour, 6 hours, then a day.
BACKOFF_HOURS = (1, 6, 24)

_RATE_REASONS = {"uploadRateLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded", "backendError",
                 "internalError"}
_SCOPE_REASONS = {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}
_DUPLICATE_REASONS = {"captionExists", "duplicate", "playlistItemDuplicate", "videoAlreadyInPlaylist"}
_NOT_FOUND_REASONS = {"videoNotFound", "playlistNotFound", "notFound", "captionNotFound"}
_NETWORK_CLASS_NAMES = {"HttpLib2Error", "TransportError"}


@dataclass(frozen=True)
class Outcome:
    status: str
    value: Any = None
    reason: str = ""
    retry_at: datetime | None = None


def reconnect_words(feature: str) -> str:
    return youtube.FEATURE_WORDS.get(feature, feature)


def _backoff(attempt: int) -> datetime:
    hours = BACKOFF_HOURS[min(max(int(attempt), 0), len(BACKOFF_HOURS) - 1)]
    return datetime.now(UTC) + timedelta(hours=hours)


def _error_parts(exc: BaseException) -> tuple[int | None, str, str]:
    """(HTTP status, Google's reason, its message) of an HttpError, from the
    JSON in its body; (None, "", "") for anything else."""
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    reason = message = ""
    content = getattr(exc, "content", None)
    if content:
        try:
            if isinstance(content, bytes | bytearray):
                content = bytes(content).decode("utf-8", "replace")
            error = (json.loads(content) or {}).get("error") or {}
            if isinstance(error, dict):
                message = str(error.get("message") or "")
                errors = error.get("errors") or []
                if errors and isinstance(errors[0], dict):
                    reason = str(errors[0].get("reason") or "")
                if not reason:
                    reason = str(error.get("status") or "")
        except (ValueError, AttributeError, TypeError):
            pass
    return status, reason, message


def _map_error(exc: BaseException, attempt: int, forbidden_reason: str) -> Outcome:
    if type(exc).__name__ == "RefreshError":
        return Outcome(STOP_ALL, reason="Reconnect the channel")
    if isinstance(exc, youtube.YoutubeNotConnectedError):
        return Outcome(STOP_ALL, reason="Reconnect the channel")
    status, reason, message = _error_parts(exc)
    text = str(exc)
    if "invalid_grant" in text or status == 401:
        return Outcome(STOP_ALL, reason="Reconnect the channel")
    if status is None:
        # no HTTP answer: the network, a timeout, or a bug in the call itself
        # httplib2 (DNS failures) and google-auth (token refresh cannot reach Google) are
        # not OSErrors; matched by class name down the MRO, like RefreshError above
        if isinstance(exc, ConnectionError | TimeoutError | OSError) or any(
                c.__name__ in _NETWORK_CLASS_NAMES for c in type(exc).__mro__):
            return Outcome(RETRY, reason="Network problem; trying again later", retry_at=_backoff(attempt))
        return Outcome(SKIPPED, reason=f"The call failed: {text[:200]}")
    if reason in _SCOPE_REASONS:
        return Outcome(NEEDS_RECONNECT, reason="Google says the permission is missing; reconnect the channel")
    if status == 403 and reason in ("quotaExceeded", "dailyLimitExceeded"):
        until = quota.mark_exhausted()
        return Outcome(STOP_ALL, reason="YouTube's daily quota is used up", retry_at=until)
    if status == 403 and reason in _RATE_REASONS:
        return Outcome(RETRY, reason="YouTube asked us to slow down", retry_at=_backoff(attempt))
    if status == 403:
        return Outcome(SKIPPED, reason=forbidden_reason or "YouTube refused this (forbidden)")
    if status == 429 or reason in _RATE_REASONS or status >= 500:
        return Outcome(RETRY, reason="YouTube is busy or slow; trying again later", retry_at=_backoff(attempt))
    if status == 409 or reason in _DUPLICATE_REASONS:
        return Outcome(DONE, reason="Already there")
    if status == 404 or reason in _NOT_FOUND_REASONS:
        return Outcome(SKIPPED, reason="The video or playlist is gone")
    if status == 400:
        return Outcome(SKIPPED, reason=(message or reason or "YouTube refused the request")[:200])
    return Outcome(SKIPPED, reason=(message or reason or text)[:200])


def call(feature: str, units: int, fn, *, attempt: int = 0, forbidden_reason: str = "") -> Outcome:
    """Run `fn()` as an optional call of `feature` costing `units` quota.
    `attempt` is how many times it has failed before (picks the back-off);
    `forbidden_reason` is what a plain 403 means for this feature. Never
    raises, except KeyboardInterrupt and SystemExit."""
    try:
        scope = youtube.FEATURE_SCOPES.get(feature)
        if scope is not None and not youtube.has_scope(scope):
            return Outcome(NEEDS_RECONNECT, reason=f"Reconnect to turn on {reconnect_words(feature)}")
        if not quota.may_spend(units):
            return Outcome(RETRY, reason="daily quota reserve reached", retry_at=quota.next_reset())
        try:
            value = fn()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # noqa: BLE001 - mapped below; nothing escapes
            quota.spend(units, feature)
            outcome = _map_error(exc, attempt, forbidden_reason)
            logger.warning("optional %s call: %s (%s)", feature, outcome.status, outcome.reason)
            return outcome
        quota.spend(units, feature)
        return Outcome(DONE, value=value)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001 - the checks themselves must not take a worker down
        logger.warning("optional %s call could not run: %s", feature, exc)
        return Outcome(SKIPPED, reason=f"The call could not run: {str(exc)[:200]}")
