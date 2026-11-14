"""The release plan's slot book (publish/schedule.py) and the request body it
feeds (publish/youtube.py insert_body), rule by rule with a fixed clock."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from core.config import get_settings
from publish import schedule
from publish import settings as pub_settings
from publish import youtube as yt


def at(day: int, hour: int, minute: int = 0, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _utc_team(monkeypatch):
    """Team time = UTC, so the window hours below are plain clock hours."""
    monkeypatch.setenv("LOCAL_TIMEZONE", "UTC")
    get_settings.cache_clear()


def zone(name: str, monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_TIMEZONE", name)
    get_settings.cache_clear()


NOW = at(16, 0)  # Monday 16 Nov, midnight


# ------------------------------------------------------------------ the rules


def test_the_first_slot_is_after_the_review_time_on_a_quarter_hour():
    # 12 hours of review: 00:00 + 12 h = 12:00, inside the 10-22 window
    assert schedule.book("a", "full", "m1", now=NOW).at == at(16, 12)


def test_a_time_between_quarter_hours_goes_up_to_the_next_one():
    assert schedule.book("a", "full", "m1", now=at(16, 0, 7)).at == at(16, 12, 15)


def test_a_slot_never_comes_before_not_before():
    got = schedule.book("a", "short", "m1", now=NOW, not_before=at(17, 15, 10))
    assert got.at == at(17, 15, 15)


def test_the_window_start_is_included_and_the_end_is_not():
    # review ends at 10:00 sharp: allowed
    pub_settings.save({"review_hours": 10})
    assert schedule.book("a", "full", "m1", now=NOW).at == at(16, 10)
    # 21:45 is the last quarter hour inside 10-22; 22:00 is outside
    pub_settings.save({"review_hours": 21.75})
    assert schedule.book("b", "full", "m2", now=NOW).at == at(16, 21, 45)
    pub_settings.save({"review_hours": 22})
    assert schedule.book("c", "full", "m3", now=NOW).at == at(17, 10)


def test_before_the_window_waits_for_it():
    pub_settings.save({"review_hours": 3})  # 03:00
    assert schedule.book("a", "full", "m1", now=NOW).at == at(16, 10)


def test_the_window_is_in_the_team_time_zone(monkeypatch):
    zone("Asia/Karachi", monkeypatch)  # UTC+5, no daylight saving
    pub_settings.save({"review_hours": 0})
    # 10:00 Karachi = 05:00 UTC
    assert schedule.book("a", "full", "m1", now=NOW).at == at(16, 5)
    # 22:00 Karachi = 17:00 UTC is outside: 16:45 UTC is the last slot
    pub_settings.save({"review_hours": 16.75})
    assert schedule.book("b", "full", "m2", now=NOW).at == at(16, 16, 45)


def test_the_window_follows_a_daylight_saving_change(monkeypatch):
    zone("America/New_York", monkeypatch)
    pub_settings.save({"review_hours": 18})
    # 30 Oct: still summer time (UTC-4): 14:00 UTC is 10:00 local, inside the window
    assert schedule.book("a", "full", "m1", now=at(29, 20, month=10)).at == at(30, 14, month=10)
    # 1 Nov, after the clocks went back (UTC-5): the same 14:00 UTC is 09:00, so wait for 15:00 UTC
    assert schedule.book("b", "full", "m2", now=at(31, 20, month=10)).at == at(1, 15)


def test_two_videos_are_at_least_the_gap_apart():
    first = schedule.book("a", "full", "m1", now=NOW)
    second = schedule.book("b", "short", "m1", now=NOW)
    assert second.at - first.at == timedelta(hours=2.5)
    pub_settings.save({"min_gap_hours": 0})
    assert schedule.book("c", "full", "m2", now=NOW).at == first.at  # no gap asked: the same quarter hour


def test_the_gap_applies_on_both_sides_of_a_booking():
    pub_settings.save({"review_hours": 0, "min_gap_hours": 3})
    schedule.book("mid", "full", "m1", now=NOW, not_before=at(16, 11))  # 11:00
    # 10:00 is only an hour before it, so the next free time is 14:00 (11:00 + 3 h)
    assert schedule.book("other", "short", "m2", now=NOW).at == at(16, 14)


def test_at_most_max_per_day_go_public_on_a_day():
    pub_settings.save({"review_hours": 0, "min_gap_hours": 1, "max_per_day": 3, "shorts_per_day": 3})
    got = [schedule.book(f"k{i}", "full", f"m{i}", now=NOW).at for i in range(4)]
    assert got[:3] == [at(16, 10), at(16, 11), at(16, 12)]
    assert got[3] == at(17, 10)  # the fourth waits for the next day


def test_the_day_counts_in_team_time_not_utc(monkeypatch):
    zone("Asia/Karachi", monkeypatch)
    pub_settings.save({"review_hours": 0, "min_gap_hours": 1, "max_per_day": 1, "shorts_per_day": 1,
                       "window_start_hour": 0, "window_end_hour": 24})
    first = schedule.book("a", "full", "m1", now=at(16, 18, 30))  # 23:30 local, 16 Nov
    assert first.at == at(16, 18, 30)
    # 19:30 UTC (an hour later, the gap) is 00:30 on 17 Nov in Karachi: a new day, so one more is allowed
    assert schedule.book("b", "full", "m2", now=at(16, 18, 30)).at == at(16, 19, 30)


def test_at_most_shorts_per_day_are_shorts():
    pub_settings.save({"review_hours": 0, "min_gap_hours": 1, "max_per_day": 4, "shorts_per_day": 2})
    shorts = [schedule.book(f"s{i}", "short", "m1", now=NOW).at for i in range(3)]
    assert shorts[:2] == [at(16, 10), at(16, 11)]
    assert shorts[2] == at(17, 10)  # the third Short waits for the next day
    # a full video still fits on the first day
    assert schedule.book("f", "full", "m1", now=NOW).at == at(16, 12)


def test_only_the_first_full_video_of_a_day_notifies():
    pub_settings.save({"review_hours": 0, "min_gap_hours": 1})
    short = schedule.book("s", "short", "m1", now=NOW)
    one = schedule.book("f1", "full", "m1", now=NOW)
    two = schedule.book("f2", "full", "m2", now=NOW)
    next_day = schedule.book("f3", "full", "m3", now=NOW, not_before=at(17, 10))
    assert (short.notify, one.notify, two.notify, next_day.notify) == (False, True, False, True)


def test_no_slot_within_max_days_ahead_is_none():
    pub_settings.save({"review_hours": 30, "max_days_ahead": 1})
    assert schedule.book("a", "full", "m1", now=NOW) is None
    assert schedule.upcoming(NOW) == []
    # and a full calendar: everything inside the days is taken
    pub_settings.save({"review_hours": 0, "max_per_day": 1, "shorts_per_day": 1, "max_days_ahead": 3})
    booked = [schedule.book(f"k{i}", "full", "m", now=NOW) for i in range(4)]
    assert [b is not None for b in booked] == [True, True, True, False]  # 16th, 17th, 18th; the 19th is too far


def test_a_booking_still_in_the_future_is_reused():
    first = schedule.book("a", "full", "m1", now=NOW)
    schedule.book("b", "short", "m1", now=NOW)
    again = schedule.book("a", "full", "m1", now=NOW, not_before=first.at)
    assert again == first  # a retry keeps a slot that still leaves the review time
    assert len(schedule.upcoming(NOW)) == 2


def test_a_booking_that_is_now_too_close_is_replaced():
    """A slot left over from a cut-off upload must not be reused once it is
    inside the review time: the video would go public with no review."""
    first = schedule.book("a", "full", "m1", now=NOW)
    near = first.at - timedelta(minutes=10)
    again = schedule.book("a", "full", "m1", now=near)
    assert again.at > first.at
    assert again.at >= near + timedelta(hours=12)


def test_a_booking_in_the_past_is_replaced():
    first = schedule.book("a", "full", "m1", now=NOW)
    later = schedule.book("a", "full", "m1", now=first.at + timedelta(hours=1))
    assert later.at > first.at
    assert len(schedule.upcoming(NOW)) == 1


def test_release_gives_the_slot_back():
    first = schedule.book("a", "full", "m1", now=NOW)
    assert schedule.release("a", now=NOW) is True
    assert schedule.release("a", now=NOW) is False  # nothing left to give back
    assert schedule.get("a") is None
    assert schedule.book("b", "full", "m2", now=NOW).at == first.at


def test_upcoming_is_sorted_and_leaves_out_what_has_happened():
    pub_settings.save({"review_hours": 0, "min_gap_hours": 1})
    schedule.book("b", "full", "m", now=NOW, not_before=at(16, 14))
    schedule.book("a", "full", "m", now=NOW)  # 10:00
    assert [b.key for b in schedule.upcoming(NOW)] == ["a", "b"]
    assert [b.key for b in schedule.upcoming(at(16, 12))] == ["b"]


def test_bookings_older_than_eight_days_are_dropped_on_the_next_write():
    schedule.book("old", "full", "m", now=NOW)
    assert schedule.get("old") is not None
    schedule.book("new", "full", "m2", now=NOW + timedelta(days=9))
    assert schedule.get("old") is None and schedule.get("new") is not None


def test_a_damaged_file_or_row_never_stops_booking():
    path = schedule._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"bookings": [{"nonsense": 1}, 5, {"key": "x", "kind": "full", "at": "not a time"}]}',
                    encoding="utf-8")
    assert schedule.book("a", "full", "m", now=NOW).at == at(16, 12)


def test_times_are_written_in_a_form_youtube_and_the_page_can_read():
    moment = datetime(2026, 11, 15, 14, 30, tzinfo=timezone(timedelta(hours=5)))
    assert schedule.rfc3339(moment) == "2026-11-15T09:30:00Z"
    assert schedule.team_time_text(at(15, 14, 30)) == "Nov 15, 2:30 PM"
    assert schedule.team_time_text(at(15, 0, 5)) == "Nov 15, 12:05 AM"


# ------------------------------------------------------------------ insert_body


def test_a_scheduled_upload_is_private_with_a_publish_time():
    when = datetime(2026, 11, 15, 14, 30, tzinfo=timezone(timedelta(hours=5)))
    body = yt.insert_body("T", "D", "public", tags=["hcabc"], publish_at=when, category_id="28", language="en",
                          recording_date="2026-11-10")
    assert body["snippet"] == {"title": "T", "description": "D", "tags": ["hcabc"], "categoryId": "28",
                               "defaultLanguage": "en", "defaultAudioLanguage": "en"}
    assert body["status"] == {"privacyStatus": "private", "publishAt": "2026-11-15T09:30:00Z",
                              "selfDeclaredMadeForKids": False, "containsSyntheticMedia": False}
    assert body["recordingDetails"] == {"recordingDate": "2026-11-10T00:00:00Z"}


def test_an_upload_without_a_schedule_keeps_the_privacy_asked():
    body = yt.insert_body("T", "D", "public", category_id="27", language="en-GB")
    assert body["status"] == {"privacyStatus": "public", "selfDeclaredMadeForKids": False,
                              "containsSyntheticMedia": False}
    assert body["snippet"]["defaultLanguage"] == body["snippet"]["defaultAudioLanguage"] == "en-GB"
    assert "recordingDetails" not in body and "tags" not in body["snippet"]


def test_nothing_is_sent_for_a_field_that_was_not_given():
    body = yt.insert_body("T", "D", "unlisted")
    assert body == {"snippet": {"title": "T", "description": "D"},
                    "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False,
                               "containsSyntheticMedia": False}}


def test_a_booking_keeps_the_meeting_name_and_old_rows_still_load():
    booked = schedule.book("a", "full", "zoom:uuid", now=NOW, meeting="Weekly Sync")
    assert booked.meeting == "Weekly Sync"
    assert schedule.get("a").meeting == "Weekly Sync"
    # a row written before the field existed has no meeting and still loads
    from ledger import files
    files.write_json(schedule._path(), {"bookings": [
        {"key": "old", "kind": "full", "source": "zoom:x", "at": at(17, 12).isoformat(), "notify": True}]})
    old = schedule.get("old")
    assert old is not None and old.meeting == ""
