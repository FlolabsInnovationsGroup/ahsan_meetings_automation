"""publish/optional.call: every Google answer maps to one outcome, and nothing escapes."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from publish import optional, quota
from publish import youtube as yt


class FakeHttpError(Exception):
    """Shaped like googleapiclient.errors.HttpError: .resp.status and a JSON .content."""

    def __init__(self, status, reason="", message="boom"):
        super().__init__(f"<HttpError {status} {reason}>")
        self.resp = SimpleNamespace(status=status)
        self.content = json.dumps({"error": {
            "code": status, "message": message, "errors": [{"reason": reason}] if reason else [],
        }}).encode()


class RefreshError(Exception):
    pass


class HttpLib2Error(Exception):
    """Named like httplib2.HttpLib2Error (not an OSError)."""


class ServerNotFoundError(HttpLib2Error):
    """Named like httplib2.ServerNotFoundError."""


class TransportError(Exception):
    """Named like google.auth.exceptions.TransportError (not an OSError)."""


@pytest.fixture(autouse=True)
def _granted_everything(monkeypatch):
    monkeypatch.setattr(yt, "granted_scopes", lambda: set(yt.SCOPES))
    monkeypatch.setattr(quota, "_limits", lambda: (10000, 2000))


def _raiser(exc):
    def fn():
        raise exc
    return fn


@pytest.mark.parametrize("exc, status", [
    (FakeHttpError(409, "captionExists"), "done"),
    (FakeHttpError(409, "playlistItemDuplicate"), "done"),
    (FakeHttpError(404, "videoNotFound"), "skipped"),
    (FakeHttpError(404, "playlistNotFound"), "skipped"),
    (FakeHttpError(400, "invalidValue", "Bad title"), "skipped"),
    (FakeHttpError(403, "forbidden"), "skipped"),
    (FakeHttpError(403, "insufficientPermissions"), "needs_reconnect"),
    (FakeHttpError(403, "ACCESS_TOKEN_SCOPE_INSUFFICIENT"), "needs_reconnect"),
    (FakeHttpError(401, "authError"), "stop_all"),
    (RefreshError("invalid_grant: Token has been expired or revoked."), "stop_all"),
    (FakeHttpError(403, "uploadRateLimitExceeded"), "retry"),
    (FakeHttpError(403, "rateLimitExceeded"), "retry"),
    (FakeHttpError(429), "retry"),
    (FakeHttpError(500, "backendError"), "retry"),
    (FakeHttpError(503), "retry"),
    (ConnectionError("reset"), "retry"),
    (TimeoutError("slow"), "retry"),
    (ServerNotFoundError("Unable to find the server"), "retry"),
    (TransportError("cannot reach the token endpoint"), "retry"),
    (ValueError("a bug in the call"), "skipped"),
])
def test_google_answers_map_to_the_right_status(exc, status):
    outcome = optional.call("captions", 50, _raiser(exc))
    assert outcome.status == status
    if status == "retry":
        assert outcome.retry_at is not None
    assert outcome.reason


def test_quota_exceeded_stops_everything_optional_until_midnight():
    outcome = optional.call("captions", 50, _raiser(FakeHttpError(403, "quotaExceeded")))
    assert outcome.status == "stop_all" and outcome.retry_at is not None
    assert quota.today()["exhausted_until"] is not None
    ran = []
    again = optional.call("playlists", 1, lambda: ran.append(1))
    assert again.status == "retry" and ran == []  # not even called


def test_the_forbidden_reason_is_the_callers_words():
    outcome = optional.call("captions", 50, _raiser(FakeHttpError(403, "forbidden")),
                            forbidden_reason="This channel cannot add captions to this video")
    assert outcome.status == "skipped" and outcome.reason == "This channel cannot add captions to this video"


def test_the_back_off_grows_with_the_attempt():
    gaps = []
    for attempt in (0, 1, 2, 7):
        out = optional.call("captions", 1, _raiser(FakeHttpError(500)), attempt=attempt)
        gaps.append(round((out.retry_at - datetime.now(UTC)).total_seconds() / 3600))
    assert gaps == [1, 6, 24, 24]


def test_a_missing_scope_makes_no_call_and_spends_nothing(monkeypatch):
    monkeypatch.setattr(yt, "granted_scopes", lambda: set(yt.BASE_SCOPES))
    ran = []
    outcome = optional.call("captions", 50, lambda: ran.append(1))
    assert outcome.status == "needs_reconnect" and ran == []
    assert outcome.reason == "Reconnect to turn on caption tracks"
    assert quota.today()["used"] == 0
    # a feature the old token can do still runs
    assert optional.call("thumbnails", 50, lambda: "ok").status == "done"


def test_the_reserve_stops_an_optional_call_without_calling_it():
    quota.spend(7990, "videos.list")
    ran = []
    outcome = optional.call("playlists", 50, lambda: ran.append(1))
    assert outcome.status == "retry" and ran == []
    assert outcome.reason == "daily quota reserve reached"
    assert outcome.retry_at == quota.next_reset()
    assert quota.today()["used"] == 7990


def test_units_are_spent_once_on_success_and_on_failure():
    assert optional.call("captions", 400, lambda: {"id": "c1"}).value == {"id": "c1"}
    assert quota.today()["used"] == 400
    optional.call("captions", 400, _raiser(FakeHttpError(500)))
    assert quota.today()["used"] == 800
    assert quota.today()["by_kind"]["captions"] == 800


def test_nothing_escapes_but_keyboard_interrupt_and_system_exit(monkeypatch):
    assert optional.call("captions", 1, _raiser(RuntimeError("x"))).status == "skipped"
    with pytest.raises(KeyboardInterrupt):
        optional.call("captions", 1, _raiser(KeyboardInterrupt()))
    with pytest.raises(SystemExit):
        optional.call("captions", 1, _raiser(SystemExit(1)))

    # a broken quota file does not escape either
    def broken(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(quota, "may_spend", broken)
    assert optional.call("captions", 1, lambda: 1).status == "skipped"


def test_read_status_and_find_by_marker_record_their_units(monkeypatch):
    pytest.importorskip("googleapiclient")
    pages = [{"items": [{"contentDetails": {"videoId": "v1"}}], "nextPageToken": "p2"},
             {"items": [{"contentDetails": {"videoId": "v2"}}]}]

    class Exec:
        def __init__(self, value):
            self.value = value

        def execute(self):
            return self.value

    class Service:
        def channels(self):
            return SimpleNamespace(list=lambda **k: Exec(
                {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UP"}}}]}))

        def playlistItems(self):
            return SimpleNamespace(list=lambda **k: Exec(pages.pop(0)))

        def videos(self):
            return SimpleNamespace(list=lambda **k: Exec({"items": [{"id": k["id"], "status": {
                "privacyStatus": "private"}, "snippet": {"tags": ["other"]}}]}))

    from googleapiclient import discovery

    monkeypatch.setattr(discovery, "build", lambda *a, **k: Service())
    monkeypatch.setattr(yt, "_load_credentials", lambda: object())
    assert yt.find_by_marker("not-there") is None
    # 1 channels.list + 2 playlist pages + 2 videos.list
    assert quota.today()["used"] == 5
    assert yt.read_status("v1")["privacyStatus"] == "private"
    assert quota.today()["used"] == 6
    assert quota.today()["by_kind"]["videos.list"] == 3
