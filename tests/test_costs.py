"""The AI cost meter: recording, the report's arithmetic and the API."""

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
    assert set(config) == {"prices"}


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
    assert _rows("compute") == []  # only AI usage is recorded


def test_a_job_with_nothing_to_record_adds_no_rows():
    costs.record_job(_job(calls=0))
    assert _rows() == []


def test_a_job_without_ai_calls_is_not_a_meeting():
    costs.record_job(_job("real", prompt=1_000_000, out=0))
    costs.record_job(_job("empty", calls=0))
    r = costs.report(7, today=date(2026, 11, 14))
    assert r["meetings"]["count"] == 1


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
        # old non-AI rows in an existing database are ignored by the report
        db.upsert_event(con, kind="tool", key="tool:x", at="2026-11-12T00:00:00+00:00", day="2026-11-12", usd=0.5)
        db.upsert_event(con, kind="upload", key="upload:1", at="2026-11-12T01:00:00+00:00", day="2026-11-12",
                        units=1, usd=0.0, note="short")
        db.upsert_event(con, kind="compute", key="compute:a", at="2026-11-10T01:00:00+00:00", day="2026-11-10",
                        job_id="zzz", seconds=180.0, usd=0.0)
    r = costs.report(7, today=today)  # 2026-11-08 .. 2026-11-14
    assert r["range"] == {"start": "2026-11-08", "end": "2026-11-14", "days": 7}
    assert len(r["per_day"]) == 7
    by_day = {d["day"]: d for d in r["per_day"]}
    assert by_day["2026-11-09"] == {"day": "2026-11-09", "usd": 0.0, "calls": 0}
    assert by_day["2026-11-10"]["usd"] == 3.0 and by_day["2026-11-10"]["calls"] == 2
    assert by_day["2026-11-12"]["usd"] == 1.0
    t = r["totals"]
    assert t["ai_usd"] == 4.0 and t["total_usd"] == 4.0
    assert t["calls"] == 3 and t["input_tokens"] == 1_500_000 and t["output_tokens"] == 100_000
    assert r["meetings"]["count"] == 2  # a and b
    assert r["meetings"]["avg_usd"] == 2.0
    assert r["projection"] == {"based_on_days": 5, "month_usd": pytest.approx(4.0 / 5 * 30, abs=0.01)}
    assert r["by_model"][0]["model"] == "claude-sonnet-5-5" and r["by_model"][0]["usd"] == 4.0
    for gone in ("fixed_usd", "tools_usd"):
        assert gone not in t
    assert not {"uploads", "compute_hours", "ai", "days", "start", "end"} & set(r)
    one = costs.report(1, today=today)
    assert len(one["per_day"]) == 1 and one["meetings"]["count"] == 0 and one["meetings"]["avg_usd"] is None


def test_report_rejects_bad_days():
    for bad in (0, 3651, -1):
        with pytest.raises(ValueError):
            costs.report(bad)


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
    assert r.status_code == 200 and r.json()["range"]["days"] == 7 and len(r.json()["per_day"]) == 7
    assert client.get("/costs?days=0").status_code == 400
    assert client.get("/costs?days=9999").status_code == 400

    cfg = client.get("/costs/settings").json()
    assert set(cfg) == {"prices"}
    prices = cfg["prices"] + [{"model": "my-model", "input": 1, "output": 2, "cache_read": 0.5, "cache_write": 1}]
    r = client.post("/costs/settings", json={"prices": prices})
    assert r.status_code == 200 and set(r.json()) == {"prices"}
    saved = client.get("/costs/settings").json()
    assert any(p["model"] == "my-model" for p in saved["prices"])

    def bad(payload):
        res = client.post("/costs/settings", json=payload)
        assert res.status_code == 400 and res.json()["detail"]

    bad({"prices": [{"model": "m", "input": 1, "output": 1, "cache_read": -1, "cache_write": 0}]})
    bad({"prices": [{"model": "m", "input": 1, "output": 1, "cache_read": 0, "cache_write": 0}] * 2})
    bad({"prices": [{"model": "", "input": 1, "output": 1, "cache_read": 0, "cache_write": 0}]})
    bad({"prices": [{"model": "m", "input": "lots", "output": 1, "cache_read": 0, "cache_write": 0}]})
    bad({"prices": [{"model": f"m{i}", "input": 1, "output": 1, "cache_read": 0, "cache_write": 0} for i in range(51)]})
    # nothing was changed by the refused posts
    assert client.get("/costs/settings").json() == saved


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
        assert r["projection"]["month_usd"] == pytest.approx(90.0, abs=0.01)
    assert costs.report(1, today=today)["projection"]["based_on_days"] == 1
