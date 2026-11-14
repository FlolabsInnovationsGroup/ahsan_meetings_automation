"""The page's follow-up routes and the permission fields it reads (EP3):
/youtube/status, /publishing/quota, /publishing/extras and the retry route,
plus a syntax check of the page script. Synthetic data only."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from publish import youtube

PAGE = Path(__file__).resolve().parent.parent / "web" / "app.html"


@pytest.fixture
def client():
    return TestClient(api_main.app)


def test_status_without_a_token_has_empty_permission_fields(client):
    body = client.get("/youtube/status").json()
    assert body["connected"] is False
    assert body["granted"] == [] and body["missing"] == []


def test_status_with_an_upload_only_token_lists_what_is_missing(client, tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({
        "channel_title": "Test channel",
        "granted_scopes": list(youtube.BASE_SCOPES),
    }), encoding="utf-8")
    monkeypatch.setattr(youtube, "_token_path", lambda: token)
    body = client.get("/youtube/status").json()
    assert body["connected"] is True
    assert "captions" in body["missing"] and "analytics" in body["missing"]
    assert body["granted"] and body["features"]["captions"] is False
    assert body["reconnect_for"]


def test_quota_answers_with_an_empty_state(client):
    body = client.get("/publishing/quota").json()
    assert body["used"] == 0
    assert body["remaining"] == body["daily_units"]
    assert body["reserve_units"] >= 0 and body["resets_at"]


def test_extras_answers_with_an_empty_state(client):
    assert client.get("/publishing/extras").json() == {"videos": []}


def test_retry_of_an_unknown_follow_up_is_404(client):
    assert client.post("/publishing/extras/no-such-id/retry").status_code == 404


def _script() -> str:
    html = PAGE.read_text(encoding="utf-8")
    return "\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.DOTALL))


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_script_parses(tmp_path):
    js = tmp_path / "page.js"
    js.write_text(_script(), encoding="utf-8")
    done = subprocess.run(["node", "--check", str(js)], check=False, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_the_page_has_no_duplicate_ids():
    html = PAGE.read_text(encoding="utf-8")
    body = re.sub(r"<script.*?</script>", "", html, flags=re.DOTALL)
    ids = re.findall(r'\sid="([^"]+)"', body)
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})


def test_the_page_shows_follow_up_text_only_as_text():
    script = _script()
    start = script.index("function addFollowUpLines")
    block = script[start:start + 1200]
    assert "innerHTML" not in block and "textContent" in script[script.index("function sideEl"):][:200]
