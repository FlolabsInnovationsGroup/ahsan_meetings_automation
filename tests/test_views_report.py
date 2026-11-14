"""The Views report (analytics/report.py) and its two routes (GET /views, POST /views/pull)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from analytics import db, report
from costs.db import local_day


def _ago(n: int) -> str:
    return (date.fromisoformat(local_day()) - timedelta(days=n)).isoformat()


def _stamp(n: int) -> str:
    return f"{_ago(n)}T12:00:00+00:00"


def _video(vid, kind="full", team="Alpha Team", published=30, short_style=None, privacy="public"):
    with db.connect() as con:
        db.upsert_video(con, vid, ours=1, kind=kind, title=f"Title {vid}", series_key=team.lower(),
                        series_label=team, published_at=_stamp(published), privacy=privacy,
                        short_style=short_style)


def _snap(vid, ago, views, likes=0, comments=0):
    with db.connect() as con:
        db.upsert_snapshot(con, vid, _ago(ago), views, likes, comments)


def _row(r, vid):
    return next(v for v in r["videos"] if v["video_id"] == vid)


def test_window_delta_uses_the_snapshot_at_or_before_the_start():
    _video("a1")
    _snap("a1", 40, 100, 5, 1)
    _snap("a1", 9, 150, 6, 1)     # gap: nothing between day 40 and day 9
    _snap("a1", 7, 180, 8, 2)     # exactly at the 7-day start
    _snap("a1", 0, 200, 9, 4)
    r7 = _row(report.report(7), "a1")
    assert (r7["views"], r7["likes"], r7["comments"], r7["total_views"]) == (20, 1, 2, 200)
    assert r7["partial_since"] is None
    r30 = _row(report.report(30), "a1")
    assert r30["views"] == 100 and r30["partial_since"] is None      # 200 - 100 (day 40)


def test_a_video_public_inside_the_window_counts_its_whole_total():
    _video("b1", published=3)
    _snap("b1", 3, 40, 2, 0)
    _snap("b1", 0, 90, 4, 1)
    row = _row(report.report(7), "b1")
    assert (row["views"], row["likes"], row["comments"]) == (90, 4, 1)
    assert row["partial_since"] is None


def test_first_seen_after_the_start_is_partial():
    _video("c1", published=60)
    _snap("c1", 5, 300)
    _snap("c1", 0, 340)
    row = _row(report.report(30), "c1")
    assert row["views"] == 40 and row["partial_since"] == _ago(5)
    assert row["total_views"] == 340


def test_a_video_with_one_snapshot_gains_nothing_and_no_snapshot_means_no_data():
    _video("d1", published=60)
    _snap("d1", 0, 500)
    _video("d2", published=60)
    r = report.report(7)
    assert _row(r, "d1")["views"] == 0 and _row(r, "d1")["partial_since"] == _ago(0)
    assert _row(r, "d2")["has_data"] is False


def test_format_team_and_style_sums_are_raw_and_have_no_ratios():
    _video("f1", kind="full", team="Alpha Team", published=1)
    _snap("f1", 0, 100, 1, 1)
    _video("s1", kind="short", team="Alpha Team", published=1, short_style="new")
    _snap("s1", 0, 10, 2, 0)
    _video("s2", kind="short", team="Beta Team", published=1, short_style="classic")
    _snap("s2", 0, 5, 1, 0)
    _video("s3", kind="short", team="Beta Team", published=1, short_style="new")
    _snap("s3", 0, 7, 0, 0)
    fmt = report.report(30, "format")
    groups = {g["label"]: g for g in fmt["groups"]}
    assert groups["short"]["videos"] == 3 and groups["short"]["views"] == 22
    assert groups["full"]["views"] == 100 and groups["topic"]["videos"] == 0
    team = {g["label"]: g for g in report.report(30, "team")["groups"]}
    assert team["Alpha Team"]["views"] == 110 and team["Beta Team"]["videos"] == 2
    styles = {g["label"]: g for g in fmt["short_styles"]}
    assert styles["new"]["videos"] == 2 and styles["new"]["views"] == 17 and styles["classic"]["views"] == 5
    for g in fmt["groups"]:
        assert set(g) == {"label", "videos", "views", "likes", "comments", "total_views"}


def test_pull_times_and_the_days_range_are_checked():
    assert report.report(7)["first_pull_at"] is None
    db.add_pull(kind="views", calls=1, units=1, videos=1, ok=True)
    r = report.report(7)
    assert r["first_pull_at"] and r["last_pull_at"]
    for bad in (0, -1, 3651, "7", True, 1.5):
        with pytest.raises(ValueError):
            report.report(bad)
    with pytest.raises(ValueError):
        report.report(7, "rank")
    assert report.report(3650)["range"]["days"] == 3650


def test_watch_time_sums_for_the_window_only():
    _video("w1", published=30)
    _snap("w1", 0, 10)
    with db.connect() as con:
        for ago, ev, mins in ((1, 5, 10.0), (2, 3, 6.0), (20, 99, 99.0)):
            con.execute("INSERT INTO yta_daily VALUES (?,?,?,?,?,?,?,?)", ("w1", _ago(ago), ev, ev, mins, 60.0, 50.0, 1))
    r = report.report(7)
    assert r["watch"] == {"engaged_views": 8, "minutes_watched": 16.0, "subs_gained": 2}
    assert _row(r, "w1")["watch"]["avg_view_duration_s"] == 60.0
    assert report.report(30)["watch"]["engaged_views"] == 107


def test_no_watch_block_before_the_reconnect():
    _video("n1")
    _snap("n1", 0, 3)
    assert report.report(7)["watch"] is None


@pytest.fixture
def client():
    from api import main

    return TestClient(main.app)


def test_views_route_validates_days_and_by(client):
    assert client.get("/views?days=7&by=video").status_code == 200
    assert client.get("/views?days=0").status_code == 400
    assert client.get("/views?days=3651").status_code == 400
    assert client.get("/views?days=7&by=best").status_code == 400
    body = client.get("/views?days=7&by=team").json()
    assert body["by"] == "team" and "watch_status" in body


def test_pull_now_is_refused_within_ten_minutes(client, monkeypatch):
    from analytics import pull
    from publish import youtube

    monkeypatch.setattr(youtube, "youtube_status", lambda: {"connected": True})
    calls = []
    monkeypatch.setattr(pull, "pull_stats", lambda: calls.append(1) or {"ok": True})
    assert client.post("/views/pull").status_code == 200
    assert calls == [1]
    db.add_pull(kind="views", calls=1, units=1, videos=1, ok=True)
    assert client.post("/views/pull").status_code == 429
    assert calls == [1]
    with db.connect() as con:
        con.execute("UPDATE pulls SET at=?", ((datetime.now(UTC) - timedelta(minutes=11)).isoformat(timespec="seconds"),))
    assert client.post("/views/pull").status_code == 200


def test_pull_now_needs_a_connection(client, monkeypatch):
    from publish import youtube

    monkeypatch.setattr(youtube, "youtube_status", lambda: {"connected": False})
    assert client.post("/views/pull").status_code == 400
