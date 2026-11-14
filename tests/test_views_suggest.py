"""What does best (analytics/suggest.py) and GET /views/suggestions. Synthetic data only."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from analytics import db, suggest
from costs.db import local_day
from publish import series
from publish import settings as publishing_settings


def _ago(n: int) -> str:
    return (date.fromisoformat(local_day()) - timedelta(days=n)).isoformat()


def _video(vid, *, team="Alpha Team", kind="full", ago=60, d7=None, d28=None, job=None, short_style=None,
           thumb_arm=None, title_style=None, privacy="public"):
    """A video that went public `ago` days ago, with snapshots at day 7 / 28 when given."""
    with db.connect() as con:
        db.upsert_video(con, vid, ours=1, kind=kind, title=f"Title {vid}", series_key=team.lower(),
                        series_label=team, published_at=f"{_ago(ago)}T12:00:00+00:00", privacy=privacy,
                        job_id=job, short_style=short_style, thumb_arm=thumb_arm, title_style=title_style)
        if d7 is not None:
            db.upsert_snapshot(con, vid, _ago(ago - 7), d7, 0, 0)
        if d28 is not None:
            db.upsert_snapshot(con, vid, _ago(ago - 28), d28, 0, 0)


def _team(name, base, n=8, kind="full"):
    for i in range(n):
        _video(f"{name}-{kind}-{i}", team=name, kind=kind, ago=60 - i, d7=base + i, d28=base * 2 + i)


@pytest.fixture
def rankings_on():
    publishing_settings.save({"views_rankings": True})


@pytest.fixture
def client():
    from api import main

    return TestClient(main.app)


def test_off_gives_an_empty_answer_with_status_200(client):
    _team("Alpha Team", 100)
    assert suggest.suggestions() == {}
    res = client.get("/views/suggestions")
    assert res.status_code == 200
    assert res.json() == {}


def test_route_answers_when_on(client, rankings_on):
    res = client.get("/views/suggestions")
    assert res.status_code == 200
    assert res.json()["enabled"] is True
    assert res.json()["teams"] == []


def test_minimum_counts_suppress_small_groups(rankings_on):
    _team("Alpha Team", 100, n=7)  # one short of 8
    _team("Beta Team", 50, n=8)
    out = suggest.suggestions()
    labels = [t["label"] for t in out["teams"]]
    assert labels == ["Beta Team"]


def test_four_weeks_of_data_are_needed(rankings_on):
    for i in range(8):
        _video(f"young-{i}", ago=20, d7=100 + i)  # all public only 20 days ago
    assert suggest.suggestions()["teams"] == []
    assert suggest.suggestions()["formats"] == []


def test_day7_snapshot_nearest_is_picked(rankings_on):
    for i in range(8):
        _video(f"v{i}", ago=60)
        with db.connect() as con:
            db.upsert_snapshot(con, f"v{i}", _ago(60 - 3), 10, 0, 0)   # day 3: too early, but within 3 days of 7? no, 4 off
            db.upsert_snapshot(con, f"v{i}", _ago(60 - 6), 500, 0, 0)  # day 6: one day off
            db.upsert_snapshot(con, f"v{i}", _ago(60 - 9), 900, 0, 0)  # day 9: two days off
            db.upsert_snapshot(con, f"v{i}", _ago(60 - 28), 3000, 0, 0)
    team = suggest.suggestions()["teams"][0]
    assert team["d7_median"] == 500
    assert team["d28_median"] == 3000 and team["d28_videos"] == 8


def test_day7_ignores_snapshots_far_from_the_target_and_uses_engaged_views(rankings_on):
    for i in range(8):
        _video(f"far{i}", team="Far Team", ago=60)
        with db.connect() as con:
            db.upsert_snapshot(con, f"far{i}", _ago(60 - 1), 5, 0, 0)  # day 1: 6 days off, too far
    assert suggest.suggestions()["teams"] == []
    for i in range(8):
        _video(f"e{i}", team="Engaged Team", ago=60, d7=1000)
        with db.connect() as con:
            for d in range(8):  # engaged views 10 a day for days 0..7 = 80
                con.execute("INSERT INTO yta_daily (video_id, day, engaged_views, views) VALUES (?,?,?,?)",
                            (f"e{i}", _ago(60 - d), 10, 99))
    team = next(t for t in suggest.suggestions()["teams"] if t["label"] == "Engaged Team")
    assert team["d7_median"] == 80


def test_watch_time_data_that_starts_after_the_video_went_public_does_not_replace_the_snapshot(rankings_on):
    # views were 1000 at day 7; the analytics pull only began at day 5, so its sum (60) would be wrong
    for i in range(8):
        _video(f"late{i}", team="Late Team", ago=60, d7=1000)
        with db.connect() as con:
            for d in range(5, 8):
                con.execute("INSERT INTO yta_daily (video_id, day, engaged_views, views) VALUES (?,?,?,?)",
                            (f"late{i}", _ago(60 - d), 20, 99))
    team = next(t for t in suggest.suggestions()["teams"] if t["label"] == "Late Team")
    assert team["d7_median"] == 1000
    # one video that was already public when the data began keeps the report's number
    pub = date.fromisoformat(_ago(60))
    today = date.fromisoformat(local_day())
    snap = [{"day": _ago(53), "views": 1000}]
    full = [{"day": _ago(60 - d), "engaged_views": 20} for d in range(8)]
    partial = [{"day": _ago(60 - d), "engaged_views": 20} for d in range(5, 8)]
    assert suggest._view_number(pub, 7, snap, full, today, yta_start=_ago(60)) == 160
    assert suggest._view_number(pub, 7, snap, partial, today, yta_start=_ago(55)) == 1000


def test_ranks_and_team_sentences(rankings_on):
    _team("Top Team", 900)
    _team("Mid Team", 500)
    _team("Low Team", 100)
    out = suggest.suggestions()
    ranks = {t["label"]: t["rank"] for t in out["teams"]}
    assert ranks == {"Top Team": "top", "Mid Team": "middle", "Low Team": "bottom"}
    text = out["suggestions"]
    assert any(s.startswith("Low Team: day-7 median of 104 views over 8 videos is in the bottom third; consider Archive")
               for s in text)
    assert any(s.startswith("Top Team: day-7 median of 904 views over 8 videos is in the top third; "
                            "consider a 3rd Short or more topic cuts") for s in text)
    assert not any(s.startswith("Mid Team") for s in text)


def test_two_teams_get_no_thirds(rankings_on):
    _team("Top Team", 900)
    _team("Low Team", 100)
    out = suggest.suggestions()
    assert [t["rank"] for t in out["teams"]] == [None, None]
    assert out["suggestions"] == []


def test_ab_pairs_need_enough_videos_in_both_arms(rankings_on):
    for i in range(8):
        _video(f"c{i}", kind="short", ago=60 - i, d7=100 + i, job=f"j{i}", short_style="classic")
    for i in range(7):  # only 7 new ones
        _video(f"n{i}", kind="short", ago=60 - i, d7=300 + i, job=f"j{i}", short_style="new")
    out = suggest.suggestions()
    assert out["short_styles"] == []
    assert not any("New Shorts style" in s for s in out["suggestions"])
    _video("n7", kind="short", ago=53, d7=307, job="j7", short_style="new")
    out = suggest.suggestions()
    assert not any(s.startswith("Thumbnails") for s in out["suggestions"])
    assert "New Shorts style: day-7 median 304 vs 104 for classic over 8 pairs" in out["suggestions"]
    assert {s["arm"]: s["pairs"] for s in out["short_styles"]} == {"classic": 8, "new": 8}


def test_thumbnail_and_title_style_arms(rankings_on):
    for i in range(8):
        _video(f"a{i}", ago=60 - i, d7=100, thumb_arm="auto", title_style="topic_first")
        _video(f"b{i}", ago=60 - i, d7=200, thumb_arm="custom", title_style="topic_first")
    out = suggest.suggestions()
    assert [a["arm"] for a in out["thumbnail_arms"]] == ["auto", "custom"]
    assert out["title_styles"] == []  # only one title style has videos
    assert "Thumbnails, day-7 medians: auto 100 over 8 videos, custom 200 over 8 videos" in out["suggestions"]


def test_unlisted_videos_are_left_out(rankings_on):
    for i in range(8):
        _video(f"u{i}", team="Archive Team", ago=60, d7=5, privacy="unlisted")
    assert suggest.suggestions()["teams"] == []


def test_nothing_is_written(rankings_on, client):
    series.set_row("alpha-team", label="Alpha Team", tier="public")
    _team("Alpha Team", 100)
    _team("Beta Team", 300)
    _team("Gamma Team", 500)
    watched = [series._path(), db.db_path().with_suffix(".sqlite-wal")]
    from core.config import get_settings

    watched += [p for p in (get_settings().storage_dir / "state").glob("*.json")]
    before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in watched if p.exists()}
    series_text = series._path().read_bytes()
    assert suggest.suggestions()["teams"]
    assert client.get("/views/suggestions").status_code == 200
    after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in watched if p.exists()}
    assert after == before
    assert series._path().read_bytes() == series_text
    assert publishing_settings.current().views_rankings is True
