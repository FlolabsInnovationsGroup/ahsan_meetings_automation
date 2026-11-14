import itertools
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from clips.config import ClipsSettings
from clips.slots import allowance_on, next_slots

NY = ZoneInfo("America/New_York")


def make(**kw) -> ClipsSettings:
    base = {"timezone": "UTC", "lead_minutes": 30, "min_gap_minutes": 180, "_env_file": None}
    base.update(kw)
    return ClipsSettings(**base)


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=UTC)


def test_allowance_before_and_after_first_post():
    s = make(posts_per_day=1, ramp_per_week=1, max_posts_per_day=6)
    first = date(2026, 11, 1)
    assert allowance_on(date(2026, 11, 1), None, s) == 1
    assert allowance_on(date(2026, 10, 30), first, s) == 1
    assert allowance_on(date(2026, 11, 7), first, s) == 1
    assert allowance_on(date(2026, 11, 8), first, s) == 2
    assert allowance_on(date(2026, 11, 22), first, s) == 4


def test_allowance_never_passes_the_max():
    s = make(posts_per_day=1, ramp_per_week=2, max_posts_per_day=6)
    assert allowance_on(date(2027, 6, 1), date(2026, 11, 1), s) == 6


def test_allowance_base_above_max_is_capped():
    s = make(posts_per_day=10, max_posts_per_day=4)
    assert allowance_on(date(2026, 11, 1), None, s) == 4


def test_first_slot_is_after_the_lead_on_a_quarter_hour():
    s = make()
    [slot] = next_slots(utc(2026, 11, 14, 10, 7), [], s, None)
    assert slot == utc(2026, 11, 14, 10, 45)


def test_exact_quarter_is_kept():
    s = make(lead_minutes=30)
    [slot] = next_slots(utc(2026, 11, 14, 10, 15), [], s, None)
    assert slot == utc(2026, 11, 14, 10, 45)


def test_gap_from_booked_times_both_sides():
    s = make(posts_per_day=6, min_gap_minutes=180, lead_minutes=0)
    booked = [utc(2026, 11, 14, 12, 0)]
    slots = next_slots(utc(2026, 11, 14, 10, 0), booked, s, None, count=2)
    for slot in slots:
        assert abs(slot - booked[0]) >= timedelta(minutes=180)
    assert slots[1] - slots[0] >= timedelta(minutes=180)


def test_slots_are_apart_from_each_other():
    s = make(posts_per_day=6, min_gap_minutes=180)
    slots = next_slots(utc(2026, 11, 14, 0, 0), [], s, None, count=4)
    assert len(slots) == 4
    for a, b in itertools.pairwise(slots):
        assert b - a >= timedelta(minutes=180)


def test_full_day_moves_to_the_next_day():
    s = make(posts_per_day=1)
    booked = [utc(2026, 11, 14, 15, 0)]
    [slot] = next_slots(utc(2026, 11, 14, 10, 0), booked, s, None)
    assert slot.date() == date(2026, 11, 15)
    assert slot == utc(2026, 11, 15, 0, 0)


def test_window_hours_are_respected():
    s = make(window_start_hour=9, window_end_hour=21, posts_per_day=3)
    slots = next_slots(utc(2026, 11, 14, 3, 0), [], s, None, count=3)
    assert slots == [utc(2026, 11, 14, 9, 0), utc(2026, 11, 14, 12, 0), utc(2026, 11, 14, 15, 0)]


def test_after_the_window_goes_to_tomorrow():
    s = make(window_start_hour=9, window_end_hour=21)
    [slot] = next_slots(utc(2026, 11, 14, 22, 0), [], s, None)
    assert slot == utc(2026, 11, 15, 9, 0)


def test_window_end_is_exclusive():
    s = make(window_start_hour=9, window_end_hour=12, posts_per_day=24, max_posts_per_day=24, min_gap_minutes=15)
    slots = next_slots(utc(2026, 11, 14, 8, 0), [], s, None, count=20)
    day = [x for x in slots if x.date() == date(2026, 11, 14)]
    assert max(day) == utc(2026, 11, 14, 11, 45)


def test_local_day_counts_in_the_zone():
    s = make(timezone="America/New_York", posts_per_day=1)
    # 02:00 UTC on the 15th is 21:00 on the 14th in New York: that local day is full
    booked = [utc(2026, 11, 15, 2, 0)]
    [slot] = next_slots(utc(2026, 11, 14, 14, 0), booked, s, None)
    assert slot.astimezone(NY).date() == date(2026, 11, 15)


def test_window_is_in_local_time():
    s = make(timezone="America/New_York", window_start_hour=9, window_end_hour=21)
    [slot] = next_slots(utc(2026, 11, 14, 6, 0), [], s, None)  # 01:00 in New York
    assert slot.astimezone(NY).hour == 9


def test_dst_spring_forward_day():
    # 2026-03-08: New York clocks jump from 02:00 to 03:00
    s = make(timezone="America/New_York", window_start_hour=0, window_end_hour=24, posts_per_day=24,
             max_posts_per_day=24, min_gap_minutes=60, lead_minutes=0)
    slots = next_slots(utc(2026, 3, 8, 4, 0), [], s, None, count=30)
    day = [x for x in slots if x.astimezone(NY).date() == date(2026, 3, 8)]
    assert len(day) == 23
    assert all(b - a == timedelta(hours=1) for a, b in itertools.pairwise(day))


def test_dst_fall_back_day():
    s = make(timezone="America/New_York", posts_per_day=24, max_posts_per_day=24, min_gap_minutes=60,
             lead_minutes=0)
    slots = next_slots(utc(2026, 11, 1, 4, 0), [], s, None, count=30)
    day = [x for x in slots if x.astimezone(NY).date() == date(2026, 11, 1)]
    assert len(day) == 24  # the allowance caps the 25-hour day
    assert all(b - a == timedelta(hours=1) for a, b in itertools.pairwise(day))


def test_hourly_settings_give_one_slot_an_hour():
    s = make(posts_per_day=24, max_posts_per_day=24, min_gap_minutes=60, lead_minutes=0)
    slots = next_slots(utc(2026, 11, 14, 0, 0), [], s, None, count=24)
    assert [x.hour for x in slots] == list(range(24))


def test_default_pace_one_a_day_for_a_week():
    s = make()
    booked: list[datetime] = []
    now = utc(2026, 11, 14, 9, 0)
    for _ in range(5):
        [slot] = next_slots(now, booked, s, None)
        booked.append(slot)
    assert len({b.date() for b in booked}) == 5


def test_ramp_allows_two_a_day_in_week_two():
    s = make(posts_per_day=1, ramp_per_week=1)
    first = date(2026, 11, 1)
    slots = next_slots(utc(2026, 11, 8, 0, 0), [], s, first, count=2)
    assert len(slots) == 2 and slots[0].date() == slots[1].date()


def test_zero_allowance_finds_nothing():
    s = make(posts_per_day=0, ramp_per_week=0)
    assert next_slots(utc(2026, 11, 14, 0, 0), [], s, None) == []


def test_looks_thirty_days_ahead_only():
    s = make(posts_per_day=1)
    booked = [utc(2026, 11, 14, 0, 0) + timedelta(days=d) for d in range(32)]
    assert next_slots(utc(2026, 11, 14, 0, 0), booked, s, None) == []


def test_naive_times_are_read_as_utc():
    s = make()
    [slot] = next_slots(datetime(2026, 11, 14, 10, 0), [datetime(2026, 11, 14, 11, 0)], s, None)
    assert slot.tzinfo is not None


def test_bad_zone_falls_back_to_utc():
    s = make(timezone="Nowhere/Land")
    assert next_slots(utc(2026, 11, 14, 10, 0), [], s, None)


@pytest.mark.parametrize("count", [1, 3])
def test_count_is_honoured(count):
    s = make(posts_per_day=6)
    assert len(next_slots(utc(2026, 11, 14, 0, 0), [], s, None, count=count)) == count


def test_default_ramp_follows_the_research_not_a_straight_line():
    s = make(max_posts_per_day=6)
    first = date(2026, 11, 1)
    week = lambda n: allowance_on(first + timedelta(days=7 * n), first, s)
    assert [week(n) for n in range(10)] == [1, 2, 2, 3, 3, 3, 3, 4, 4, 4]


def test_steps_are_a_setting_and_capped_by_the_max():
    s = make(ramp_steps="1,5,9", max_posts_per_day=6)
    first = date(2026, 11, 1)
    assert [allowance_on(first + timedelta(days=7 * n), first, s) for n in range(4)] == [1, 5, 6, 6]


def test_unreadable_steps_fall_back_to_posts_per_day():
    s = make(ramp_steps="x,,", posts_per_day=2)
    assert allowance_on(date(2026, 11, 20), date(2026, 11, 1), s) == 2
