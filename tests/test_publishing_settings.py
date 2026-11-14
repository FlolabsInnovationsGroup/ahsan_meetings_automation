"""The side panel's Publishing choices: GET /publishing/settings and POST."""

import json

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from ledger import files
from publish import settings as publishing


@pytest.fixture
def client():
    return TestClient(api_main.app)


def test_get_returns_settings_and_defaults(client):
    body = client.get("/publishing/settings").json()
    assert body["settings"] == publishing.as_dict(publishing.DEFAULTS)
    assert body["defaults"] == publishing.as_dict(publishing.DEFAULTS)


def test_post_saves_and_get_returns_the_new_values(client):
    res = client.post("/publishing/settings", json={"max_per_day": 3, "hashtags": False, "links_text": "https://flolabs.com"})
    assert res.status_code == 200
    assert res.json()["settings"]["max_per_day"] == 3
    got = client.get("/publishing/settings").json()
    assert got["settings"]["max_per_day"] == 3
    assert got["settings"]["hashtags"] is False
    assert got["settings"]["links_text"] == "https://flolabs.com"
    assert got["defaults"]["max_per_day"] == publishing.DEFAULTS.max_per_day


def test_bad_value_is_400_with_a_message_and_saves_nothing(client):
    # one good value and one bad: neither is kept
    res = client.post("/publishing/settings", json={"max_per_day": 3, "review_hours": 500})
    assert res.status_code == 400
    assert "review_hours" in res.json()["detail"]
    assert client.get("/publishing/settings").json()["settings"]["max_per_day"] == publishing.DEFAULTS.max_per_day


def test_unknown_field_is_400(client):
    res = client.post("/publishing/settings", json={"post_everything": True})
    assert res.status_code == 400
    assert "post_everything" in res.json()["detail"]


def test_damaged_saved_value_falls_back_to_its_default(client):
    files.write_json(files.state_dir() / "prefs.json", {"publishing": {"max_per_day": "lots", "hashtags": False}})
    got = client.get("/publishing/settings").json()["settings"]
    assert got["max_per_day"] == publishing.DEFAULTS.max_per_day
    assert got["hashtags"] is False  # the good value next to it is kept
    assert json.dumps(got)
