"""The YouTube permissions: an old token keeps working, a reconnect stores what
Google really granted, and the page can tell which extras need a reconnect."""

import json
import time
from types import SimpleNamespace

import pytest

from core.config import get_settings
from publish import youtube as yt

API = "https://www.googleapis.com/auth/"
UPLOAD, READ = API + "youtube.upload", API + "youtube.readonly"
SSL, ANALYTICS = API + "youtube.force-ssl", API + "yt-analytics.readonly"


def _token_path():
    return get_settings().storage_dir / "youtube_token.json"


def _write_token(**extra):
    data = {
        "token": "t", "refresh_token": "r", "client_id": "cid", "client_secret": "sec",
        "token_uri": "https://oauth2.googleapis.com/token", "channel_title": "Test Channel", **extra,
    }
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_an_old_token_loads_with_only_the_scopes_it_was_granted():
    pytest.importorskip("google.oauth2")
    _write_token(scopes=[UPLOAD, READ], expiry="2999-01-01T00:00:00Z")
    creds = yt._load_credentials()
    # google-auth sends these on every refresh: they must not include scopes the token never had
    assert sorted(creds.scopes) == sorted([UPLOAD, READ])
    assert SSL not in creds.scopes and ANALYTICS not in creds.scopes


def test_a_token_with_no_scope_field_falls_back_to_upload_and_read():
    pytest.importorskip("google.oauth2")
    _write_token(expiry="2999-01-01T00:00:00Z")
    assert sorted(yt._load_credentials().scopes) == sorted(yt.BASE_SCOPES)


def test_granted_scopes_win_over_the_scopes_field():
    pytest.importorskip("google.oauth2")
    _write_token(scopes=[UPLOAD, READ, SSL], granted_scopes=[UPLOAD, READ], expiry="2999-01-01T00:00:00Z")
    assert sorted(yt._load_credentials().scopes) == sorted([UPLOAD, READ])


def test_the_refresh_rewrite_keeps_granted_scopes_and_the_channel_title(monkeypatch):
    pytest.importorskip("google.oauth2")
    from google.oauth2.credentials import Credentials

    path = _write_token(scopes=[UPLOAD, READ], granted_scopes=[UPLOAD, READ])  # no expiry: needs a refresh
    seen = {}

    def fake_refresh(self, request):
        seen["scopes"] = list(self.scopes)
        self.token = "new-token"

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    yt._load_credentials()
    assert sorted(seen["scopes"]) == sorted([UPLOAD, READ])
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["channel_title"] == "Test Channel"
    assert sorted(saved["granted_scopes"]) == sorted([UPLOAD, READ])
    assert saved["token"] == "new-token"


class _FakeFlow:
    def __init__(self, scopes, as_string=False):
        raw = " ".join(scopes) if as_string else list(scopes)
        self.credentials = SimpleNamespace(
            refresh_token="r", granted_scopes=None,
            to_json=lambda: json.dumps({"token": "t", "refresh_token": "r"}),
        )
        self.oauth2session = SimpleNamespace(token={"scope": raw})

    def fetch_token(self, code):
        pass


def _connect(monkeypatch, scopes, as_string=False):
    pytest.importorskip("google_auth_oauthlib")
    from google_auth_oauthlib.flow import Flow

    monkeypatch.setattr(Flow, "from_client_config", classmethod(lambda cls, *a, **k: _FakeFlow(scopes, as_string)))
    monkeypatch.setattr(yt, "_channel_title", lambda creds: "Test Channel")
    monkeypatch.setattr(yt, "is_configured", lambda: True)
    yt._pending["state1"] = (time.time(), None, "/app")
    return yt.finish_connect("state1", "code")


def test_connecting_without_the_wider_scopes_stores_what_was_granted(monkeypatch):
    assert _connect(monkeypatch, [UPLOAD, READ]) == "/app"
    data = json.loads(_token_path().read_text(encoding="utf-8"))
    assert data["granted_scopes"] == sorted([UPLOAD, READ])
    assert data["channel_title"] == "Test Channel"
    status = yt.youtube_status()
    assert set(status["missing"]) == {"captions", "playlists", "edit", "analytics"}
    assert status["reconnect_for"] == ["caption tracks", "playlists", "changing videos after upload",
                                       "view analytics"]
    assert status["features"] == {"captions": False, "playlists": False, "edit": False, "analytics": False}


def test_a_space_separated_scope_string_is_understood(monkeypatch):
    _connect(monkeypatch, [UPLOAD, READ, SSL, ANALYTICS], as_string=True)
    status = yt.youtube_status()
    assert status["granted"] == sorted([UPLOAD, READ, SSL, ANALYTICS])
    assert status["missing"] == [] and status["reconnect_for"] == []
    assert all(status["features"].values())


def test_a_grant_without_upload_is_refused(monkeypatch):
    with pytest.raises(ValueError, match="upload permission"):
        _connect(monkeypatch, [READ, SSL])
    assert not _token_path().exists()


def test_has_scope_and_the_feature_map():
    assert yt.granted_scopes() == set() and not yt.has_scope(UPLOAD)  # nothing connected
    _write_token(scopes=[UPLOAD, READ])
    assert yt.has_scope(UPLOAD) and not yt.has_scope(SSL)
    assert yt.granted_scopes() == {UPLOAD, READ}
    _write_token(granted_scopes=[UPLOAD, READ, SSL])
    assert yt.has_scope(SSL) and not yt.has_scope(ANALYTICS)
    assert yt.FEATURE_SCOPES == {
        "thumbnails": UPLOAD, "translations": UPLOAD, "captions": SSL, "playlists": SSL,
        "edit": SSL, "analytics": ANALYTICS,
    }
    # thumbnails and translations work before any reconnect
    _write_token(scopes=[UPLOAD, READ])
    status = yt.youtube_status()
    assert "thumbnails" not in status["missing"] and "translations" not in status["missing"]


def test_a_token_with_no_scope_field_counts_as_upload_and_read():
    _write_token()
    assert yt.granted_scopes() == set(yt.BASE_SCOPES)


def test_two_threads_saving_the_token_at_once_do_not_fail(tmp_path):
    """The upload and the follow-up worker can both refresh and save the token."""
    import threading

    path = tmp_path / "token.json"
    errors = []

    def save(n):
        try:
            for i in range(150):
                yt._write_secret(path, json.dumps({"n": n * 1000 + i}))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=save, args=(n,)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert path.exists() and sorted(p.name for p in tmp_path.glob("token*")) == ["token.json"]
