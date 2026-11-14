"""The app's count of YouTube's daily quota (publish/quota.py)."""

import json
import threading
from datetime import UTC, datetime, timedelta

from ledger import files
from publish import quota


def _at(monkeypatch, when: datetime):
    monkeypatch.setattr(quota, "_now", lambda: when)


def test_the_day_is_the_los_angeles_date():
    # in winter the day turns at 08:00 UTC
    assert quota.day_of(datetime(2026, 12, 10, 7, 59, tzinfo=UTC)) == "2026-12-09"
    assert quota.day_of(datetime(2026, 12, 10, 8, 0, tzinfo=UTC)) == "2026-12-10"
    # in summer (daylight saving, UTC-7) it turns at 07:00 UTC
    assert quota.day_of(datetime(2026, 7, 10, 6, 59, tzinfo=UTC)) == "2026-07-09"
    assert quota.day_of(datetime(2026, 7, 10, 7, 0, tzinfo=UTC)) == "2026-07-10"


def test_the_counter_starts_over_when_the_pacific_day_rolls_over(monkeypatch):
    _at(monkeypatch, datetime(2026, 7, 10, 6, 0, tzinfo=UTC))
    quota.spend(100, "videos.list")
    assert quota.today()["used"] == 100
    _at(monkeypatch, datetime(2026, 7, 10, 7, 1, tzinfo=UTC))
    assert quota.today()["used"] == 0
    quota.spend(5, "videos.list")
    assert quota.today()["used"] == 5 and quota.today()["day"] == "2026-07-10"


def test_next_reset_is_the_next_los_angeles_midnight_across_daylight_saving():
    # winter: midnight PST = 08:00 UTC
    assert quota.next_reset(datetime(2026, 12, 10, 12, 0, tzinfo=UTC)) == datetime(2026, 12, 11, 8, 0, tzinfo=UTC)
    # summer: midnight PDT = 07:00 UTC
    assert quota.next_reset(datetime(2026, 7, 10, 12, 0, tzinfo=UTC)) == datetime(2026, 7, 11, 7, 0, tzinfo=UTC)
    # clocks go forward on 8 March 2026: that day has 23 hours, and the next one starts at 07:00 UTC
    assert quota.next_reset(datetime(2026, 3, 8, 12, 0, tzinfo=UTC)) == datetime(2026, 3, 9, 7, 0, tzinfo=UTC)
    # they go back on 1 November 2026: a 25 hour day that ends at 08:00 UTC the next morning
    assert quota.next_reset(datetime(2026, 11, 1, 12, 0, tzinfo=UTC)) == datetime(2026, 11, 2, 8, 0, tzinfo=UTC)


def test_the_reserve_blocks_optional_calls_but_not_essential_ones(monkeypatch):
    monkeypatch.setattr(quota, "_limits", lambda: (10000, 2000))
    quota.spend(7990, "videos.list")
    assert quota.may_spend(10)  # 8000 is exactly the line
    assert not quota.may_spend(11)
    assert quota.may_spend(11, essential=True)
    assert quota.may_spend(5000, essential=True)
    assert quota.remaining() == 2010
    assert quota.today()["remaining_for_extras"] == 10


def test_a_damaged_file_starts_today_at_zero():
    path = files.state_dir() / "quota.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert quota.today()["used"] == 0
    path.write_text(json.dumps(["a", "list"]), encoding="utf-8")
    assert quota.today()["used"] == 0
    path.write_text(json.dumps({"day": quota.day_of(), "used": "lots", "by_kind": 3}), encoding="utf-8")
    assert quota.today()["used"] == 0
    quota.spend(3, "x")
    assert quota.today()["used"] == 3


def test_two_threads_spending_lose_no_units():
    def work():
        for _ in range(25):
            quota.spend(2, "videos.list")

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert quota.today()["used"] == 4 * 25 * 2
    assert quota.today()["by_kind"]["videos.list"] == 200


def test_exhausted_blocks_optional_calls_until_pacific_midnight(monkeypatch):
    monkeypatch.setattr(quota, "_limits", lambda: (10000, 2000))
    noon = datetime(2026, 12, 10, 20, 0, tzinfo=UTC)  # noon in Los Angeles
    _at(monkeypatch, noon)
    until = quota.mark_exhausted()
    assert until == datetime(2026, 12, 11, 8, 0, tzinfo=UTC)
    assert not quota.may_spend(1)
    assert quota.may_spend(1, essential=True)
    assert quota.today()["exhausted_until"] is not None
    _at(monkeypatch, noon + timedelta(hours=11))  # 23:00 in Los Angeles: still blocked
    assert not quota.may_spend(1)
    _at(monkeypatch, until + timedelta(minutes=1))  # a new day
    assert quota.may_spend(1)
    assert quota.today()["exhausted_until"] is None and quota.today()["used"] == 0


def test_upload_calls_have_their_own_count():
    quota.spend_upload_call()
    quota.spend_upload_call()
    today = quota.today()
    assert today["upload_calls"] == 2 and today["used"] == 0


def test_the_page_summary_carries_the_limits_and_the_reset_time():
    summary = quota.today()
    assert summary["daily_units"] >= 1000
    assert summary["reserve_units"] < summary["daily_units"]
    assert summary["resets_at"]
