"""The cost meter: recording, the report's arithmetic, the upload sync and the API."""

import json
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
import costs
from core.config import get_settings
from core.models import JobRecord, JobStatus
from costs import db


def _job(job_id="j1", calls=2, prompt=1_000_000, out=100_000, timings=None, title="Weekly sync"):
    return JobRecord(
        job_id=job_id, status=JobStatus.DONE, title=title,
        llm_usage={"calls": calls, "prompt_tokens": prompt, "output_tokens": out,
                   "cache_read_tokens": 0, "cache_write_tokens": 0},
        stage_timings={"decide": 60.0, "render": 120.0} if timings is None else timings,
        updated_at=datetime(2026, 11, 10, 8, 0, tzinfo=UTC),
    )


def _rows(kind=None):
    with db.connect() as con:
        sql = "SELECT * FROM events" + (" WHERE kind=?" if kind else "")
        return [dict(r) for r in con.execute(sql, (kind,) if kind else ())]


@pytest.fixture(autouse=True)
def _sonnet(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    get_settings.cache_clear()


def test_database_lives_in_storage_dir_and_is_seeded():
    costs.record_job(_job())
    assert db.db_path() == get_settings().storage_dir / "state" / "costs.sqlite"
    assert db.db_path().exists()
    config = costs.read_config()
    sonnet = next(p for p in config["prices"] if p["model"] == "claude-sonnet-5-5")
    assert sonnet["cache_read"] == 0.10
    assert [f["usd_per_month"] for f in config["fixed_costs"]] == [70.08]
    assert config["settings"]["youtube_uploads_per_day"] == 100


def test_recording_a_job_twice_updates_one_row():
    costs.record_job(_job(calls=2, prompt=1_000_000, out=100_000))
    ai = _rows("ai")
    assert len(ai) == 1
    assert ai[0]["usd"] == pytest.approx(2.0 + 1.0)  # 1M in at 2.00, 100K out at 10.00
    assert ai[0]["model"] == "claude-sonnet-5-5" and ai[0]["provider"] == "anthropic"
    assert ai[0]["day"] == "2026-11-10"
    # chapters ran after rendering: the usage grew
    costs.record_job(_job(calls=3, prompt=2_000_000, out=100_000))
    ai = _rows("ai")
    assert len(ai) == 1 and ai[0]["calls"] == 3 and ai[0]["usd"] == pytest.approx(5.0)
    compute = _rows("compute")
    assert len(compute) == 1 and compute[0]["seconds"] == 180 and compute[0]["usd"] == 0


def test_a_job_with_nothing_to_record_adds_no_rows():
    costs.record_job(_job(calls=0, timings={}))
    assert _rows() == []


def test_nested_stage_timings_are_not_counted_twice():
    costs.record_job(_job(timings={"transcribing": 100.0, "building_edl": 50.0, "silence": 20.0,
                                   "rendering": 400.0, "render_cleaned": 250.0, "render_shorts": 150.0}))
    assert _rows("compute")[0]["seconds"] == 550  # not 970


def test_empty_recordings_are_not_meetings():
    costs.record_job(_job("real", prompt=1_000_000, out=0))
    costs.record_job(_job("empty", calls=0, timings={"transcribing": 30.0}))
    r = costs.report(7, today=date(2026, 11, 14))
    assert r["meetings"]["count"] == 1
    assert r["compute_hours"] == round((180 + 30) / 3600, 2)


def test_unknown_price_gives_null_usd_and_is_listed(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    get_settings.cache_clear()
    costs.record_job(_job())
    row = _rows("ai")[0]
    assert row["usd"] is None and row["model"] == get_settings().gemini_model
    r = costs.report(30, today=date(2026, 11, 14))
    assert r["unpriced_models"] == [get_settings().gemini_model]
    assert r["totals"]["ai_usd"] == 0
    # adding the price fixes the past
    costs.write_config(prices=[{"model": "gemini-", "input": 1, "output": 2, "cache_read": 0, "cache_write": 0}])
    assert _rows("ai")[0]["usd"] == pytest.approx(1.2)
    assert costs.report(30, today=date(2026, 11, 14))["unpriced_models"] == []


def test_report_math():
    today = date(2026, 11, 14)
    costs.record_job(_job("a", prompt=1_000_000, out=100_000))  # $3.00 on 2026-11-10
    with db.connect() as con:
        db.upsert_event(con, kind="ai", key="ai:b", at="2026-11-12T00:00:00+00:00", day="2026-11-12", job_id="b",
                        provider="anthropic", model="claude-sonnet-5-5", calls=1, input_tokens=500_000,
                        output_tokens=0, usd=1.0)
        db.upsert_event(con, kind="tool", key="tool:x", at="2026-11-12T00:00:00+00:00", day="2026-11-12", usd=0.5)
        db.upsert_event(con, kind="upload", key="upload:1", at="2026-11-12T01:00:00+00:00", day="2026-11-12",
                        units=1, usd=0.0, note="short")
        db.upsert_event(con, kind="upload", key="upload:2", at="2026-11-12T02:00:00+00:00", day="2026-11-12",
                        units=1, usd=0.0, note="full")
        db.upsert_event(con, kind="upload", key="upload:3", at="2026-11-05T02:00:00+00:00", day="2026-11-05",
                        units=1, usd=0.0, note="short")
    r = costs.report(7, today=today)  # 2026-11-08 .. 2026-11-14
    assert (r["start"], r["end"]) == ("2026-11-08", "2026-11-14")
    assert len(r["per_day"]) == 7
    by_day = {d["day"]: d for d in r["per_day"]}
    assert by_day["2026-11-09"] == {"day": "2026-11-09", "usd": 0.0, "uploads": 0}
    assert by_day["2026-11-10"]["usd"] == 3.0
    assert by_day["2026-11-12"]["usd"] == 1.5 and by_day["2026-11-12"]["uploads"] == 2
    t = r["totals"]
    assert t["ai_usd"] == 4.0 and t["tools_usd"] == 0.5
    assert t["fixed_usd"] == pytest.approx(70.08 * 7 / 30.4375, abs=1e-3)
    assert t["total_usd"] == pytest.approx(4.5 + 70.08 * 7 / 30.4375, abs=1e-3)
    assert r["uploads"]["count"] == 2 and r["uploads"]["shorts"] == 1 and r["uploads"]["full"] == 1
    assert r["uploads"]["busiest_day"] == "2026-11-12" and r["uploads"]["limit_per_day"] == 100
    assert r["compute_hours"] == 0.05
    assert r["meetings"]["count"] == 2  # a and b
    assert r["meetings"]["avg_usd"] == 2.25
    assert r["meetings"]["avg_usd_with_fixed"] == pytest.approx((4.5 + t["fixed_usd"]) / 2, abs=1e-3)
    assert r["projection"]["variable_usd"] == pytest.approx(4.5 / 7 * 30, abs=0.01)
    assert r["projection"]["month_usd"] == pytest.approx(4.5 / 7 * 30 + 70.08, abs=0.01)
    assert r["by_model"][0]["model"] == "claude-sonnet-5-5" and r["by_model"][0]["usd"] == 4.0
    # a wider range reaches the older upload; one day sees only today
    assert costs.report(30, today=today)["uploads"]["count"] == 3
    one = costs.report(1, today=today)
    assert len(one["per_day"]) == 1 and one["meetings"]["count"] == 0 and one["meetings"]["avg_usd"] is None


def test_report_rejects_bad_days():
    for bad in (0, 3651, -1):
        with pytest.raises(ValueError):
            costs.report(bad)


def test_upload_sync_reads_posted_list_without_duplicates():
    state = get_settings().storage_dir / "state"
    state.mkdir(parents=True)
    lines = [
        {"posted_at": "2026-11-10T20:00:00+00:00", "meeting": "Sync", "kind": "full", "video_id": "AAA", "job_id": "j1", "key": "k1"},
        {"posted_at": "2026-11-10T20:05:00+00:00", "meeting": "Sync", "kind": "short", "video_id": "BBB", "job_id": "j1", "key": "k2"},
    ]
    (state / "posted.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n", encoding="utf-8")
    assert costs.sync_uploads() == 2
    assert costs.sync_uploads() == 0
    rows = _rows("upload")
    assert sorted(r["key"] for r in rows) == ["upload:AAA", "upload:BBB"]
    assert all(r["units"] == 1 and r["usd"] == 0 for r in rows)
    # 20:00 UTC is already the next day in Karachi (UTC+5)
    assert rows[0]["day"] == "2026-11-11"
    # a new line appears later
    with (state / "posted.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"posted_at": "2026-11-11T01:00:00+00:00", "kind": "short", "video_id": "CCC", "key": "k3"}) + "\n")
    assert costs.sync_uploads() == 1


def test_a_broken_job_never_raises():
    class Broken:
        job_id = "x"
        llm_usage = None
        stage_timings = None

        @property
        def updated_at(self):
            raise RuntimeError("boom")

    costs.record_job(Broken())  # must not raise


def test_api_round_trip_and_validation():
    client = TestClient(api_main.app)
    r = client.get("/costs?days=7")
    assert r.status_code == 200 and r.json()["days"] == 7 and len(r.json()["per_day"]) == 7
    assert client.get("/costs?days=0").status_code == 400
    assert client.get("/costs?days=9999").status_code == 400

    cfg = client.get("/costs/settings").json()
    assert {"prices", "fixed_costs", "settings"} <= set(cfg)
    prices = cfg["prices"] + [{"model": "my-model", "input": 1, "output": 2, "cache_read": 0.5, "cache_write": 1}]
    fixed = [{"name": "Server", "usd_per_month": 10, "active": True}, {"name": "Domain", "usd_per_month": 1.5}]
    r = client.post("/costs/settings", json={"prices": prices, "fixed_costs": fixed})
    assert r.status_code == 200
    saved = client.get("/costs/settings").json()
    assert any(p["model"] == "my-model" for p in saved["prices"])
    assert [f["name"] for f in saved["fixed_costs"]] == ["Server", "Domain"]
    assert client.get("/costs?days=30").json()["projection"]["fixed_usd"] == 11.5

    def bad(payload):
        res = client.post("/costs/settings", json=payload)
        assert res.status_code == 400 and res.json()["detail"]

    bad({"fixed_costs": [{"name": "x", "usd_per_month": -1}]})
    bad({"fixed_costs": [{"name": "", "usd_per_month": 1}]})
    bad({"fixed_costs": [{"name": "n" * 101, "usd_per_month": 1}]})
    bad({"fixed_costs": [{"name": "x", "usd_per_month": "lots"}]})
    bad({"fixed_costs": [{"name": f"c{i}", "usd_per_month": 1} for i in range(51)]})
    bad({"prices": [{"model": "m", "input": 1, "output": 1, "cache_read": -1, "cache_write": 0}]})
    bad({"prices": [{"model": "m", "input": 1, "output": 1, "cache_read": 0, "cache_write": 0}] * 2})
    bad({"settings": {"youtube_uploads_per_day": -5}})
    # nothing was changed by the refused posts
    assert [f["name"] for f in client.get("/costs/settings").json()["fixed_costs"]] == ["Server", "Domain"]


def test_projection_averages_over_days_with_data():
    """A meter that started 2 days ago must not spread its spend over the whole range."""
    today = date(2026, 11, 14)
    with db.connect() as con:
        for d in ("2026-11-13", "2026-11-14"):
            db.upsert_event(con, kind="ai", key=f"ai:{d}", at=f"{d}T00:00:00+00:00", day=d, job_id=d,
                            provider="anthropic", model="claude-sonnet-5-5", calls=1, input_tokens=0,
                            output_tokens=0, usd=3.0)
    for days in (30, 90):
        r = costs.report(days, today=today)
        assert r["projection"]["based_on_days"] == 2
        assert r["projection"]["variable_usd"] == pytest.approx(90.0, abs=0.01)
        assert r["meetings"]["avg_usd_with_fixed"] == pytest.approx((6.0 + 70.08 * 2 / 30.4375) / 2, abs=1e-3)
    assert costs.report(1, today=today)["projection"]["based_on_days"] == 1
