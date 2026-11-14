"""The "Check the channel" scan: leaks of never-post meetings, and videos the
app didn't upload."""

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from autopilot import never_post
from ledger import uploads
from ledger.uploads import get_ledger
from publish import channel_check


class _Call:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeService:
    """The three YouTube calls the scan makes, over two pages of uploads."""

    def __init__(self, videos):
        self.videos_by_id = {v["id"]: v for v in videos}
        self.pages = [[v["id"] for v in videos[:2]], [v["id"] for v in videos[2:]]]
        self.asked = []

    def channels(self):
        return self

    def playlistItems(self):
        return self

    def videos(self):
        return self

    def list(self, part, **kw):
        if part == "contentDetails" and "mine" in kw:
            return _Call(lambda: {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
        if part == "contentDetails":
            index = int(kw.get("pageToken", "0"))
            self.asked.append(index)
            page = {"items": [{"contentDetails": {"videoId": i}} for i in self.pages[index]]}
            if index + 1 < len(self.pages):
                page["nextPageToken"] = str(index + 1)
            return _Call(lambda: page)
        assert part == "snippet,status" and len(kw["id"].split(",")) <= 50
        found = [self.videos_by_id[i] for i in kw["id"].split(",")]
        return _Call(lambda: {"items": [
            {"id": v["id"], "snippet": {"title": v["title"], "publishedAt": "2026-11-01T10:00:00Z"},
             "status": {"privacyStatus": v["privacy"], **({"publishAt": v["at"]} if v.get("at") else {})}}
            for v in found]})


def _video(video_id, title, privacy="public", at=None):
    return {"id": video_id, "title": title, "privacy": privacy, "at": at}


def _ours(video_id, title="Our own video"):
    item = uploads.Item(f"job:{video_id}|full", f"job:{video_id}", "full", video_id, "x")
    get_ledger().begin(item, title=title, privacy="public", by="test")
    get_ledger().published(item.key, video_id, by="test")


def test_the_scan_pages_through_the_channel_and_finds_each_kind():
    never_post.set_entries(["operations"])
    _ours("A" * 11)
    _ours("B" * 11)
    service = FakeService([
        _video("A" * 11, "Space Team meeting"),
        _video("L" * 11, "Operations team meeting", "public"),  # a leak
        _video("S" * 11, "Operations team meeting 2", "private", at="2026-12-01T10:00:00Z"),  # a scheduled leak
        _video("B" * 11, "Operations notes", "private"),  # private, not scheduled: safe
        _video("U" * 11, "Someone's upload", "unlisted"),  # not ours
    ])
    result = channel_check.check_channel(service)
    assert service.asked == [0, 1]
    assert result["counts"] == {"total": 5, "public": 2, "private": 2, "unlisted": 1, "scheduled": 1}
    assert [v["id"] for v in result["listed"]] == ["L" * 11, "S" * 11]
    assert all("operations" in v["reason"].lower() for v in result["listed"])
    assert result["listed"][0]["studio_url"] == f"https://studio.youtube.com/video/{'L' * 11}/edit"
    assert [v["id"] for v in result["unknown"]] == ["L" * 11, "S" * 11, "U" * 11]
    assert result["checked_at"]
    assert channel_check.last_result() == result


def test_a_short_is_flagged_by_the_meeting_it_came_from_not_only_its_title():
    never_post.set_entries(["operations"])
    item = uploads.Item("zoom:u1|short|j1|0", "zoom:u1", "short", "j1", "Operations team meeting", None, 0)
    get_ledger().begin(item, title="Why we moved the robot test to Friday", privacy="public", by="test")
    get_ledger().published(item.key, "H" * 11, by="test")
    service = FakeService([_video("H" * 11, "Why we moved the robot test to Friday")])
    result = channel_check.check_channel(service)
    assert [v["id"] for v in result["listed"]] == ["H" * 11]
    assert "operations" in result["listed"][0]["reason"].lower()


def test_before_any_scan_there_is_no_result():
    assert channel_check.last_result() == {"checked_at": None}


@pytest.fixture
def client():
    return TestClient(api_main.app)


def test_the_route_says_when_no_channel_is_connected(client):
    res = client.post("/channel/check")
    assert res.status_code == 409 and "No YouTube channel connected yet." in res.json()["detail"]
    assert client.get("/channel/check").json() == {"checked_at": None}


def test_the_route_saves_and_returns_the_result(client, monkeypatch):
    service = FakeService([_video("U" * 11, "Someone's upload")])
    service.pages = [["U" * 11]]
    monkeypatch.setattr(channel_check, "_build_service", lambda: service)
    res = client.post("/channel/check")
    assert res.status_code == 200 and res.json()["counts"]["total"] == 1
    assert client.get("/channel/check").json() == res.json()


def test_a_youtube_error_is_answered_in_plain_words(client, monkeypatch):
    def broken():
        raise RuntimeError("quotaExceeded")

    monkeypatch.setattr(channel_check, "_build_service", broken)
    res = client.post("/channel/check")
    # a read-only scan: worded for reading, not "Upload failed"
    assert res.status_code == 502
    assert res.json()["detail"] == "Couldn't read the channel: YouTube's daily limit is reached. Try again tomorrow."


def test_other_errors_say_what_could_not_be_read(client, monkeypatch):
    def broken():
        raise RuntimeError("connection reset")

    monkeypatch.setattr(channel_check, "_build_service", broken)
    detail = client.post("/channel/check").json()["detail"]
    assert detail == "Couldn't read the channel: connection reset. Try again later."
    assert "Upload" not in detail


def test_an_unlisted_video_from_a_never_post_meeting_is_a_leak():
    never_post.set_entries(["operations"])
    service = FakeService([
        _video("X" * 11, "Operations team meeting", "unlisted"),
        _video("Y" * 11, "Space Team", "unlisted"),
    ])
    result = channel_check.check_channel(service)
    assert [v["id"] for v in result["listed"]] == ["X" * 11]


def test_a_zoom_id_entry_flags_a_renamed_meeting_through_its_job(monkeypatch):
    never_post.set_entries(["84116996599"])
    item = uploads.Item("zoom:u1|full", "zoom:u1", "full", "j1", "Weekly sync")
    get_ledger().begin(item, title="Why we moved to Postgres | FloLabs", privacy="public", by="test")
    get_ledger().published(item.key, "H" * 11, by="test")

    class _Job:
        title = "Weekly sync"
        zoom_meeting = None

        def __init__(self):
            self.zoom_meeting = {"topic": "Weekly sync", "meeting_id": "84116996599"}

    from api import jobs
    monkeypatch.setattr(jobs.job_store, "get", lambda job_id: _Job())
    service = FakeService([_video("H" * 11, "Why we moved to Postgres | FloLabs")])
    result = channel_check.check_channel(service)
    assert [v["id"] for v in result["listed"]] == ["H" * 11]
    assert "84116996599" in result["listed"][0]["reason"]
