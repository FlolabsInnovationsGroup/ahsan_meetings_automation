"""The Teams routes (GET /series, POST /series and its helpers) and the Teams
panel on the page. Synthetic team names only."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from autopilot import never_post
from autopilot.store import Meeting, store
from publish import series

PAGE = Path(__file__).resolve().parent.parent / "web" / "app.html"
HELD_ERROR = 'Held for a person: "Gadget Review" is a new team. Post this series from now on, or Skip.'


@pytest.fixture(autouse=True)
def _fresh_store():
    store._state = None
    series.seed_once()  # an empty list, seeded: later teams are added by hand
    yield
    store._state = None


@pytest.fixture
def client():
    return TestClient(api_main.app)


def _team(topic, tier=None):
    """A team the autopilot has seen (a new one has no tier)."""
    row = series.note_seen(topic)
    if tier:
        series.set_row(row.key, tier=tier)
    return row.key


def _held(uuid, topic, start="2026-01-05T10:00:00Z"):
    key = _team(topic)
    with store.editing() as state:
        state.meetings[uuid] = Meeting(uuid=uuid, topic=topic, start_time=start, state="needs_attention",
                                       series_key=key, series_hold=True, error=HELD_ERROR)
    return key


def _row(body, key):
    return next(r for r in body["rows"] if r["key"] == key)


def test_get_series_shape(client):
    gadget = _held("m1", "Gadget Review")
    gizmo = _team("Gizmo Sync", tier="public")
    body = client.get("/series").json()
    assert set(body) == {"mode", "seeded", "rows", "held"}
    assert body["mode"] == "allow_list" and body["seeded"] is True
    assert {r["key"] for r in body["rows"]} == {gadget, gizmo}
    row = _row(body, gadget)
    assert row["label"] == "Gadget Review" and row["tier"] is None
    assert row["never_post"] is False and row["meetings_waiting"] == 1
    assert set(row["playlists"]) == {"full", "cuts", "shorts"}
    assert _row(body, gizmo)["meetings_waiting"] == 0
    assert body["held"] == [{"key": gadget, "label": "Gadget Review",
                             "meetings": [{"uuid": "m1", "topic": "Gadget Review",
                                           "start_time": "2026-01-05T10:00:00Z"}]}]


def test_a_team_on_the_never_post_list_says_so(client):
    key = _team("Widget Council", tier="never")
    assert _row(client.get("/series").json(), key)["never_post"] is False
    never_post.set_entries(["widget council"])
    assert _row(client.get("/series").json(), key)["never_post"] is True


@pytest.mark.parametrize("change", [
    {"label": "Bad <b>"},
    {"label": ""},
    {"hashtags": ["nohash"]},
    {"hashtags": ["#a", "#b", "#c"]},
    {"tier": "sometimes"},
    {"playlists": {"full": "not a playlist"}},
])
def test_bad_values_are_refused_in_plain_words(client, change):
    key = _team("Gadget Review")
    before = client.get("/series").json()
    res = client.post("/series", json={"rows": [{"key": key, **change}]})
    assert res.status_code == 400
    assert isinstance(res.json()["detail"], str) and res.json()["detail"]
    assert client.get("/series").json() == before


def test_a_team_on_the_never_post_list_can_not_be_set_to_post(client):
    key = _team("Widget Council")
    never_post.set_entries(["widget council"])
    res = client.post("/series", json={"rows": [{"key": key, "tier": "public"}]})
    assert res.status_code == 400 and "never-post list" in res.json()["detail"]
    assert series.lookup_key(key).tier is None


def test_saving_one_row_leaves_the_others_alone(client):
    one = _team("Gadget Review", tier="public")
    two = _team("Gizmo Sync", tier="archive")
    series.set_row(two, hashtags=["#Gizmo"])
    body = client.post("/series", json={"rows": [{"key": one, "label": "Gadgets", "hashtags": ["#Gadget"]}]}).json()
    assert _row(body, one)["label"] == "Gadgets" and _row(body, one)["hashtags"] == ["#Gadget"]
    assert _row(body, one)["tier"] == "public"  # a field that was not sent stays
    assert _row(body, two)["tier"] == "archive" and _row(body, two)["hashtags"] == ["#Gizmo"]
    # a stale page that sends one row can't delete a team it has never heard of
    third = _team("Sprocket Talk", tier="public")
    client.post("/series", json={"rows": [{"key": two, "tier": "public"}]})
    assert series.lookup_key(third).tier == "public" and series.lookup_key(one).label == "Gadgets"


def test_allow_releases_the_held_meetings(client):
    key = _held("m1", "Gadget Review")
    _held("m2", "Gadget Review", start="2026-01-12T10:00:00Z")
    body = client.post("/series/allow", json={"key": key}).json()
    assert _row(body, key)["tier"] == "public" and body["held"] == []
    for uuid in ("m1", "m2"):
        m = store.read().meetings[uuid]
        assert not m.series_hold and m.state == "waiting_recording"


def test_never_stops_the_held_meetings(client):
    key = _held("m1", "Gadget Review")
    body = client.post("/series/never", json={"key": key}).json()
    assert _row(body, key)["tier"] == "never" and body["held"] == []
    m = store.read().meetings["m1"]
    assert m.state == "skipped" and not m.series_hold and "never" in (m.error or "").lower()


def test_allow_and_never_of_an_unknown_team_are_404(client):
    assert client.post("/series/allow", json={"key": "no such team"}).status_code == 404
    assert client.post("/series/never", json={"key": "no such team"}).status_code == 404
    assert client.get("/series").json()["rows"] == []  # nothing was created


def test_allow_of_a_never_post_team_is_refused(client):
    key = _held("m1", "Widget Council")
    never_post.set_entries(["widget council"])
    res = client.post("/series/allow", json={"key": key})
    assert res.status_code == 400 and "never-post list" in res.json()["detail"]
    assert store.read().meetings["m1"].series_hold is True


def test_merge_folds_one_spelling_into_the_other(client):
    keep = _team("Gadget Review", tier="public")
    other = _team("Gadgets Review Group")
    body = client.post("/series/merge", json={"alias": other, "into": keep}).json()
    assert [r["key"] for r in body["rows"]] == [keep]
    assert other in _row(body, keep)["aliases"] and _row(body, keep)["seen"] == 2
    assert client.post("/series/merge", json={"alias": keep, "into": keep}).status_code == 400
    assert client.post("/series/merge", json={"alias": "nobody", "into": keep}).status_code == 400


def test_the_autopilot_rows_carry_their_team(client):
    key = _held("m1", "Gadget Review")
    series.set_row(key, label="Gadget Club")
    row = client.get("/autopilot").json()["meetings"][0]
    assert row["series_key"] == key and row["series_label"] == "Gadget Club" and row["series_hold"] is True


def test_a_label_with_markup_comes_back_as_plain_data(client):
    key = _team("<script>alert(1)</script> Review")
    body = client.get("/series").json()
    row = _row(body, key)  # the raw title stays data in "examples"; the page shows it with textContent
    assert any("<script>" in e for e in row["examples"]) and "<" not in row["label"]
    assert client.get("/series").headers["content-type"].startswith("application/json")


# ------------------------------------------------------------------ the page


def _scripts() -> list[str]:
    html = PAGE.read_text(encoding="utf-8")
    return re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.DOTALL)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_every_page_script_parses(tmp_path):
    for number, text in enumerate(_scripts()):
        js = tmp_path / f"page{number}.js"
        js.write_text(text, encoding="utf-8")
        done = subprocess.run(["node", "--check", str(js)], check=False, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr


def test_the_page_has_one_of_each_id_and_a_teams_section():
    html = PAGE.read_text(encoding="utf-8")
    body = re.sub(r"<script.*?</script>", "", html, flags=re.DOTALL)
    ids = re.findall(r'\sid="([^"]+)"', body)
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})
    assert {"seriesSection", "seriesHeld", "seriesList", "seriesCount", "ytSeriesNote"} <= set(ids)
    assert body.index('id="neverPostSection"') < body.index('id="seriesSection"')


def test_the_teams_panel_builds_with_text_only():
    panel = next(s for s in _scripts() if 'getElementById("seriesSection")' in s)
    assert "innerHTML" not in panel and "insertAdjacentHTML" not in panel
    for words in ("Post this series from now on", "Never post this team", "Same team as",
                  "On the never-post list: never posted"):
        assert words in panel


# ------------------------------------------------------------------ Process and the tier card


class _FakeZoom:
    def get_meeting(self, uuid):
        return {"uuid": uuid, "id": 123456789, "topic": "Gadget Review", "recording_files": []}


def test_process_of_a_never_team_is_refused_with_403(client, monkeypatch):

    monkeypatch.setattr(api_main.zoom, "get_client", lambda: _FakeZoom())
    _team("Gadget Review", tier="never")
    res = client.post("/jobs/zoom", json={"meeting_uuid": "abc=="})
    assert res.status_code == 403 and "Never" in res.json()["detail"]


def test_picking_public_on_the_card_releases_the_held_meetings(client):
    key = _held("m1", "Gadget Review")
    body = client.post("/series", json={"rows": [{"key": key, "tier": "public"}]}).json()
    assert body["held"] == []
    m = store.read().meetings["m1"]
    assert not m.series_hold and m.state == "waiting_recording"


def test_picking_archive_on_the_card_releases_and_stays_archive(client):
    key = _held("m1", "Gadget Review")
    body = client.post("/series", json={"rows": [{"key": key, "tier": "archive"}]}).json()
    assert _row(body, key)["tier"] == "archive" and body["held"] == []
    assert not store.read().meetings["m1"].series_hold


def test_picking_never_on_the_card_skips_the_held_meetings(client):
    key = _held("m1", "Gadget Review")
    body = client.post("/series", json={"rows": [{"key": key, "tier": "never"}]}).json()
    assert body["held"] == []
    assert store.read().meetings["m1"].state == "skipped"


def test_merging_a_held_spelling_into_a_public_team_releases_it(client):
    keep = _team("Gadget Review", tier="public")
    other = _held("m1", "Gadgets Review Group")
    body = client.post("/series/merge", json={"alias": other, "into": keep}).json()
    assert body["held"] == []
    assert not store.read().meetings["m1"].series_hold
