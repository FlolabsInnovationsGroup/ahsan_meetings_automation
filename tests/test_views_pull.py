"""The views database and the daily read-only statistics pull (analytics/)."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httplib2
import pytest
from googleapiclient.errors import HttpError

from analytics import db, pull, record
from ledger import files
from ledger.uploads import Item, get_ledger
from publish import quota


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeService:
    """videos().list / channels().list / playlistItems().list with canned answers."""

    def __init__(self, stats=None, uploads=(), error=None):
        self.stats = stats or {}
        self.uploads = list(uploads)
        self.error = error
        self.video_calls: list[list[str]] = []
        self.channel_calls = 0
        self.playlist_calls = 0

    def videos(self):
        return SimpleNamespace(list=self._videos_list)

    def channels(self):
        return SimpleNamespace(list=self._channels_list)

    def playlistItems(self):
        return SimpleNamespace(list=self._playlist_list)

    def _videos_list(self, part, id):
        def run():
            if self.error:
                raise self.error
            ids = id.split(",")
            self.video_calls.append(ids)
            return {"items": [self._video(i) for i in ids if i in self.stats]}
        return _Req(run)

    def _video(self, vid):
        views = self.stats[vid]
        return {"id": vid, "snippet": {"title": f"Title {vid}", "publishedAt": "2026-11-01T10:00:00Z"},
                "status": {"privacyStatus": "public"}, "contentDetails": {"duration": "PT1M30S"},
                "statistics": {"viewCount": str(views), "likeCount": "2", "commentCount": "1"}}

    def _channels_list(self, part, mine):
        def run():
            self.channel_calls += 1
            return {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUx"}}}]}
        return _Req(run)

    def _playlist_list(self, part, playlistId, maxResults, pageToken=None):
        def run():
            self.playlist_calls += 1
            return {"items": [{"contentDetails": {"videoId": v}} for v in self.uploads]}
        return _Req(run)


def _post(n: int, kind="full") -> str:
    """Put a fake posted video in the ledger; returns its id."""
    vid = f"vid{n:08d}"
    item = Item(key=f"k{n}", source=f"src{n}", kind=kind, job_id=f"job{n}", meeting="Robot Sync Meeting")
    get_ledger().begin(item, title=f"Title {n}", privacy="public", by="test")
    get_ledger().published(item.key, vid, by="test")
    return vid


def _channel_file(unknown=(), age_days=0):
    when = datetime.now(UTC) - timedelta(days=age_days)
    files.write_json(files.state_dir() / "channel_check.json", {
        "checked_at": when.isoformat(timespec="seconds"), "counts": {}, "listed": [],
        "unknown": [{"id": i, "title": "x", "privacy": "public"} for i in unknown], "truncated": False})


def _settings(monkeypatch, **changes):
    from publish import settings as publishing_settings

    real = publishing_settings.current()
    patched = publishing_settings.replace(real, **changes) if hasattr(publishing_settings, "replace") else None
    if patched is None:
        from dataclasses import replace

        patched = replace(real, **changes)
    monkeypatch.setattr(publishing_settings, "current", lambda: patched)


def _count(table):
    with db.connect() as con:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_ids_go_in_batches_of_50_and_units_are_counted():
    ids = [_post(n) for n in range(120)]
    _channel_file()
    svc = FakeService({i: 5 for i in ids})
    result = pull.pull_stats(svc)
    assert result["ok"] and result["videos"] == 120
    assert [len(c) for c in svc.video_calls] == [50, 50, 20]
    assert result["units"] == 3 and result["calls"] == 3
    assert quota.today()["by_kind"]["views"] == 3
    last = db.last_pull()
    assert last["ok"] == 1 and last["units"] == 3 and last["videos"] == 120


def test_one_snapshot_a_day_replaced_on_a_second_pull():
    vid = _post(1)
    _channel_file()
    pull.pull_stats(FakeService({vid: 5}))
    pull.pull_stats(FakeService({vid: 9}))
    assert _count("snapshots") == 1
    with db.connect() as con:
        assert con.execute("SELECT views FROM snapshots").fetchone()[0] == 9
    row = db.get_video(vid)
    assert row["ours"] == 1 and row["kind"] == "full" and row["duration_s"] == 90.0 and row["privacy"] == "public"


def test_other_videos_follow_the_setting(monkeypatch):
    mine = _post(1)
    _channel_file(unknown=["other0000001"])
    stats = {mine: 3, "other0000001": 7}

    _settings(monkeypatch, views_include_other=False)
    svc = FakeService(stats)
    pull.pull_stats(svc)
    assert svc.video_calls == [[mine]]
    assert db.get_video("other0000001") is None

    _settings(monkeypatch, views_include_other=True)
    svc = FakeService(stats)
    pull.pull_stats(svc)
    assert set(svc.video_calls[0]) == {mine, "other0000001"}
    other = db.get_video("other0000001")
    assert other["ours"] == 0 and other["kind"] == "other"
    assert db.get_video(mine)["ours"] == 1


def test_an_old_channel_file_makes_the_pull_read_the_uploads_playlist(monkeypatch):
    mine = _post(1)
    _channel_file(age_days=3)
    svc = FakeService({mine: 1, "fromplaylist": 4}, uploads=["fromplaylist"])
    result = pull.pull_stats(svc)
    assert svc.channel_calls == 1 and svc.playlist_calls == 1
    assert result["units"] == 3  # channels.list + one page + one videos.list
    assert db.get_video("fromplaylist")["kind"] == "other"

    _channel_file(age_days=0)  # a fresh file: no listing calls
    svc = FakeService({mine: 1})
    pull.pull_stats(svc)
    assert svc.channel_calls == 0 and svc.playlist_calls == 0


def test_a_video_missing_twice_is_deleted():
    vid = _post(1)
    _channel_file()
    pull.pull_stats(FakeService({vid: 5}))
    assert db.get_video(vid) is not None
    pull.pull_stats(FakeService({}))  # YouTube no longer knows it
    assert db.get_video(vid)["missing_count"] == 1
    pull.pull_stats(FakeService({vid: 6}))  # came back: the count resets
    assert db.get_video(vid)["missing_count"] == 0
    pull.pull_stats(FakeService({}))
    pull.pull_stats(FakeService({}))
    assert db.get_video(vid) is None
    assert _count("snapshots") == 0


def test_no_successful_pull_for_30_days_deletes_everything():
    vid = _post(1)
    _channel_file()
    pull.pull_stats(FakeService({vid: 5}))
    assert _count("videos") == 1
    assert pull.purge_if_stale(True) is False
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat(timespec="seconds")
    db.set_meta("last_ok_pull", old)
    assert pull.purge_if_stale(True) is True
    assert _count("videos") == 0 and _count("snapshots") == 0


def test_disconnected_for_30_days_deletes_everything():
    vid = _post(1)
    _channel_file()
    pull.pull_stats(FakeService({vid: 5}))
    pull.purge_if_stale(True)  # connected now
    assert pull.purge_if_stale(False) is False  # just disconnected
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat(timespec="seconds")
    db.set_meta("last_connected", old)
    db.set_meta("last_ok_pull", datetime.now(UTC).isoformat(timespec="seconds"))
    assert pull.purge_if_stale(False) is True
    assert _count("videos") == 0


def test_record_upload_writes_the_arms(monkeypatch):
    _settings(monkeypatch, title_style="meeting_date")
    item = SimpleNamespace(key="k", source="src", kind="short", job_id="j1", meeting="Robot Sync Meeting",
                           topic="Robot Sync Meeting (2026-11-03)", style="new", layout="slide",
                           stands_alone=8.5, screen_hook="Big claim")
    upload = SimpleNamespace(video_id="abcdefghijk", title="A title", posted_at="2026-11-03T10:00:00+00:00",
                             publish_at=None, privacy="unlisted")
    record.record_upload(item, upload, None)
    row = db.get_video("abcdefghijk")
    assert row["ours"] == 1 and row["kind"] == "short" and row["short_style"] == "new"
    assert row["title_style"] == "meeting_date" and row["layout"] == "slide"
    assert row["stands_alone"] == 8.5 and row["screen_hook"] == "Big claim"
    assert row["series_key"] == "robot" and row["privacy"] == "unlisted" and row["job_id"] == "j1"


def test_record_upload_swallows_errors(monkeypatch):
    def boom():
        raise RuntimeError("disk gone")

    monkeypatch.setattr(db, "connect", boom)
    record.record_upload(SimpleNamespace(kind="full"), SimpleNamespace(video_id="abcdefghijk"), None)
    record.set_arm("abcdefghijk", "thumb", "B")  # neither raises


def test_set_arm_stores_the_thumbnail_arm_and_ignores_unknown_features():
    vid = _post(1)
    _channel_file()
    pull.pull_stats(FakeService({vid: 5}))
    record.set_arm(vid, "thumb", "B")
    assert db.get_video(vid)["thumb_arm"] == "B"
    record.set_arm(vid, "nonsense", "B")  # logged, no change, no error
    assert db.get_video(vid)["thumb_arm"] == "B"


@pytest.fixture
def api_main(monkeypatch):
    from api import main

    monkeypatch.setattr(main, "_views_pull_tried", 0.0)
    return main


def _trigger_setup(monkeypatch, main, connected=True):
    runs = []
    monkeypatch.setattr(main.youtube, "youtube_status", lambda: {"connected": connected})
    monkeypatch.setattr(pull, "pull_stats", lambda service=None: runs.append(1) or {"ok": True})
    return runs


def test_the_daily_trigger_runs_at_most_once_a_day(monkeypatch, api_main):
    runs = _trigger_setup(monkeypatch, api_main)
    api_main._daily_views_pull(sync=True)
    api_main._daily_views_pull(sync=True)
    assert runs == [1]
    # a restart (the in-memory guard is gone) still sees today's pulls row
    db.add_pull(kind="views", calls=1, units=1, videos=1, ok=True)
    monkeypatch.setattr(api_main, "_views_pull_tried", 0.0)
    api_main._daily_views_pull(sync=True)
    assert runs == [1]
    old = datetime.now(UTC) - timedelta(hours=30)
    with db.connect() as con:
        con.execute("UPDATE pulls SET at=?", (old.isoformat(timespec="seconds"),))
    monkeypatch.setattr(api_main, "_views_pull_tried", 0.0)
    api_main._daily_views_pull(sync=True)
    assert runs == [1, 1]


def test_the_daily_trigger_stays_quiet_when_off_or_disconnected(monkeypatch, api_main):
    runs = _trigger_setup(monkeypatch, api_main, connected=False)
    api_main._daily_views_pull(sync=True)
    assert runs == []
    runs = _trigger_setup(monkeypatch, api_main, connected=True)
    _settings(monkeypatch, views_pull=False)
    api_main._daily_views_pull(sync=True)
    assert runs == []


def test_the_purge_still_runs_when_the_pull_is_off(monkeypatch, api_main):
    vid = _post(1)
    _channel_file()
    pull.pull_stats(FakeService({vid: 5}))
    db.set_meta("last_ok_pull", (datetime.now(UTC) - timedelta(days=31)).isoformat(timespec="seconds"))
    runs = _trigger_setup(monkeypatch, api_main, connected=True)
    _settings(monkeypatch, views_pull=False)
    api_main._daily_views_pull(sync=True)
    assert runs == [] and _count("videos") == 0


def test_a_vanished_other_video_is_purged_after_two_pulls(monkeypatch):
    mine = _post(1)
    _channel_file(unknown=["other0000001"])
    _settings(monkeypatch, views_include_other=True)
    stats = {mine: 3, "other0000001": 7}
    pull.pull_stats(FakeService(stats))
    assert db.get_video("other0000001") is not None
    _channel_file(unknown=[])  # the channel scan no longer lists it, and YouTube has dropped it
    gone = {mine: 3}
    pull.pull_stats(FakeService(gone))
    assert db.get_video("other0000001")["missing_count"] == 1
    pull.pull_stats(FakeService(gone))
    assert db.get_video("other0000001") is None
    assert db.get_video(mine) is not None


def test_other_rows_are_deleted_when_the_setting_is_turned_off(monkeypatch):
    mine = _post(1)
    _channel_file(unknown=["other0000001"])
    _settings(monkeypatch, views_include_other=True)
    pull.pull_stats(FakeService({mine: 3, "other0000001": 7}))
    assert db.get_video("other0000001") is not None
    _settings(monkeypatch, views_include_other=False)
    pull.pull_stats(FakeService({mine: 3}))
    assert db.get_video("other0000001") is None and db.get_video(mine) is not None


def test_a_quota_error_is_logged_without_a_retry_loop(monkeypatch, api_main):
    vid = _post(1)
    _channel_file()
    body = json.dumps({"error": {"code": 403, "message": "quota", "errors": [{"reason": "quotaExceeded"}]}})
    err = HttpError(httplib2.Response({"status": 403}), body.encode())
    svc = FakeService({vid: 5}, error=err)
    result = pull.pull_stats(svc)
    assert not result["ok"] and "quota" in result["error"].lower()
    last = db.last_pull()
    assert last["ok"] == 0 and last["error"]
    assert quota.today()["exhausted_until"] is not None
    # and a pull while the quota is gone calls nothing
    quiet = FakeService({vid: 5})
    assert not pull.pull_stats(quiet)["ok"] and quiet.video_calls == []
    # the sweep does not try again the same day
    monkeypatch.setattr(api_main.youtube, "youtube_status", lambda: {"connected": True})
    calls = []
    monkeypatch.setattr(pull, "pull_stats", lambda service=None: calls.append(1))
    monkeypatch.setattr(api_main, "_views_pull_tried", 0.0)
    api_main._daily_views_pull(sync=True)
    assert calls == []
