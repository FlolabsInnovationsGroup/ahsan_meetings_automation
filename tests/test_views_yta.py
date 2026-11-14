"""The watch-time pull (analytics/yta.py) with a fake analytics service."""

import json
from datetime import date as _date
from datetime import timedelta
from types import SimpleNamespace

import httplib2
from googleapiclient.errors import HttpError

from analytics import db, yta
from publish import optional, quota, youtube

SCOPE = yta.SCOPE


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


def _http_error(status, reason, message="boom"):
    body = json.dumps({"error": {"message": message, "errors": [{"reason": reason}]}}).encode()
    return HttpError(httplib2.Response({"status": str(status)}), body)


class FakeAnalytics:
    """reports().query() with canned rows per day."""

    def __init__(self, by_day=None, error=None, reject_engaged=False):
        self.by_day = by_day or {}
        self.error = error
        self.reject_engaged = reject_engaged
        self.queries: list[dict] = []

    def reports(self):
        return SimpleNamespace(query=self._query)

    def _query(self, **kw):
        def run():
            self.queries.append(kw)
            if self.error:
                raise self.error
            if self.reject_engaged and "engagedViews" in kw["metrics"]:
                raise _http_error(400, "badRequest", "Unknown metric engagedViews")
            rows = self.by_day.get(kw["startDate"], [])
            if "engagedViews" not in kw["metrics"]:
                rows = [[r[0], *r[2:]] for r in rows]
            return {"rows": rows}
        return _Req(run)


def _day(ago):
    """ago=0 is the newest day the pull reads: Pacific today minus 2."""
    return (_date.fromisoformat(quota.day_of()) - timedelta(days=2 + ago)).isoformat()


def _know(*ids):
    with db.connect() as con:
        for vid in ids:
            db.upsert_video(con, vid, ours=1, kind="full")


def _grant(monkeypatch, granted=True):
    monkeypatch.setattr(youtube, "has_scope", lambda s: granted if s == SCOPE else True)


def _all(table="yta_daily"):
    with db.connect() as con:
        return [dict(r) for r in con.execute(f"SELECT * FROM {table} ORDER BY day, video_id").fetchall()]


def test_a_missing_scope_makes_no_call_and_says_reconnect(monkeypatch):
    _grant(monkeypatch, False)
    fake = FakeAnalytics()
    result = yta.pull_daily(service=fake)
    assert fake.queries == [] and not result["ok"]
    assert "Reconnect" in result["status"] and "Reconnect" in yta.last_status()


def test_switched_off_makes_no_call(monkeypatch):
    from dataclasses import replace

    from publish import settings as publishing_settings

    _grant(monkeypatch)
    patched = replace(publishing_settings.current(), views_watch_time=False)
    monkeypatch.setattr(publishing_settings, "current", lambda: patched)
    fake = FakeAnalytics()
    assert yta.pull_daily(service=fake)["ran"] is False
    assert fake.queries == []


def test_the_window_ends_two_pacific_days_back_and_asks_one_day_at_a_time(monkeypatch):
    _grant(monkeypatch)
    _know("v1")
    fake = FakeAnalytics({_day(0): [["v1", 5, 6, 12.5, 40.0, 55.0, 1]]})
    result = yta.pull_daily(service=fake)
    assert result["ok"] and result["calls"] == 5
    assert [q["startDate"] for q in fake.queries] == [_day(4), _day(3), _day(2), _day(1), _day(0)]
    assert fake.queries[-1]["startDate"] == (_date.fromisoformat(quota.day_of()) - timedelta(days=2)).isoformat()
    assert all(q["startDate"] == q["endDate"] and q["ids"] == "channel==MINE" and q["dimensions"] == "video"
               and q["sort"] == "-views" and q["maxResults"] == 200 for q in fake.queries)
    assert "engagedViews" in fake.queries[0]["metrics"]
    rows = _all()
    assert len(rows) == 1 and rows[0]["engaged_views"] == 5 and rows[0]["minutes_watched"] == 12.5
    assert quota.today()["by_kind"]["analytics"] == 5


def test_a_corrected_day_replaces_its_rows_and_unknown_videos_are_dropped(monkeypatch):
    _grant(monkeypatch)
    _know("v1", "v2")
    first = FakeAnalytics({_day(0): [["v1", 5, 6, 10.0, 30.0, 50.0, 0], ["v2", 1, 1, 1.0, 5.0, 5.0, 0]]})
    yta.pull_daily(service=first)
    assert len(_all()) == 2
    later = FakeAnalytics({_day(0): [["v1", 8, 9, 14.0, 31.0, 51.0, 2], ["zzz", 9, 9, 9.0, 9.0, 9.0, 9]]})
    yta.pull_daily(service=later)
    rows = _all()
    assert [(r["video_id"], r["engaged_views"], r["subs_gained"]) for r in rows] == [("v1", 8, 2)]


def test_a_day_outside_the_window_is_left_alone(monkeypatch):
    _grant(monkeypatch)
    _know("v1")
    with db.connect() as con:
        con.execute("INSERT INTO yta_daily VALUES ('v1', ?, 1, 1, 1.0, 1.0, 1.0, 0)", (_day(10),))
    yta.pull_daily(service=FakeAnalytics())
    assert [r["day"] for r in _all()] == [_day(10)]


def test_api_not_enabled_gives_a_status_line_and_stops(monkeypatch):
    _grant(monkeypatch)
    fake = FakeAnalytics(error=_http_error(403, "accessNotConfigured", "API not used"))
    result = yta.pull_daily(service=fake)
    assert not result["ok"] and "not turned on" in result["status"]
    assert len(fake.queries) == 1
    assert db.last_pull()["ok"] == 0


def test_quota_stop_is_a_status_not_an_exception(monkeypatch):
    _grant(monkeypatch)
    fake = FakeAnalytics(error=_http_error(403, "quotaExceeded"))
    result = yta.pull_daily(service=fake)
    assert not result["ok"] and "quota" in result["status"].lower()
    assert quota.today()["exhausted_until"]


def test_engaged_views_rejected_falls_back_to_views(monkeypatch):
    _grant(monkeypatch)
    _know("v1")
    fake = FakeAnalytics({_day(0): [["v1", 5, 6, 10.0, 30.0, 50.0, 1]]}, reject_engaged=True)
    result = yta.pull_daily(service=fake)
    assert result["ok"] and "plain views" in result["status"]
    row = _all()[0]
    assert row["engaged_views"] == 6 and row["views"] == 6     # engaged = views in the fallback
    assert all("engagedViews" not in q["metrics"] for q in fake.queries[1:])


def test_a_generic_query_not_supported_error_still_falls_back_and_is_remembered(monkeypatch):
    _grant(monkeypatch)
    _know("v1")

    class Generic(FakeAnalytics):
        def _query(self, **kw):
            if "engagedViews" in kw["metrics"]:
                def run():
                    self.queries.append(kw)
                    raise _http_error(400, "badRequest", "The query is not supported.")
                return _Req(run)
            return super()._query(**kw)

    fake = Generic({_day(0): [["v1", 5, 6, 10.0, 30.0, 50.0, 1]]})
    result = yta.pull_daily(service=fake)
    assert result["ok"] and "plain views" in result["status"]
    later = Generic()
    yta.pull_daily(service=later)
    assert all("engagedViews" not in q["metrics"] for q in later.queries)


def test_an_unexpected_error_never_raises(monkeypatch):
    _grant(monkeypatch)
    monkeypatch.setattr(optional, "call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    result = yta.pull_daily(service=FakeAnalytics())
    assert not result["ok"] and "failed" in result["status"]


def test_the_stats_pull_calls_the_watch_time_pull(monkeypatch):
    from analytics import pull

    seen = []
    monkeypatch.setattr(yta, "pull_daily", lambda *a, **k: seen.append(1) or {"status": "ok"})
    monkeypatch.setattr(pull, "_wanted_ids", lambda run, other: [])
    monkeypatch.setattr(pull, "_store", lambda found, asked: None)
    result = pull.pull_stats(SimpleNamespace())
    assert result["ok"] and seen == [1] and result["watch_status"] == "ok"
