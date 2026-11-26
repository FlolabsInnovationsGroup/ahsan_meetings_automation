"""Videos already scheduled on YouTube are brought forward into free slots
(publish/repack.py, schedule.hold_earlier), and release numbers saved under
old rules no longer hold the new ones back (publish/settings.py)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from core.config import get_settings
from ledger import files
from ledger.uploads import Item, get_ledger
from publish import optional, repack, schedule
from publish import settings as pub_settings
from publish import youtube as yt


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


NOW = at(17, 9)
EDIT = yt.FEATURE_SCOPES["edit"]


@pytest.fixture(autouse=True)
def _utc_team(monkeypatch):
    monkeypatch.setenv("LOCAL_TIMEZONE", "UTC")
    get_settings.cache_clear()


@pytest.fixture
def channel(monkeypatch):
    """A connected channel with the edit permission; YouTube's answers are recorded."""
    monkeypatch.setattr(yt, "granted_scopes", lambda: {yt.BASE_SCOPES[0], EDIT})
    calls = []

    def fake_set(video_id, when):
        calls.append((video_id, when))
        return True

    monkeypatch.setattr(repack, "youtube_set", fake_set)
    return calls


def _posted(key, kind, source, when, *, title="Moving the session store to Redis", meeting="Robotics and Dev",
            notify=False):
    """A video on YouTube with a time to go public, and its booking."""
    ledger = get_ledger()
    item = Item(key=key, source=source, kind=kind, job_id="", meeting=meeting)
    ledger.begin(item, title=title, privacy="private", by="autopilot", publish_at=schedule.rfc3339(when),
                 notify=notify)
    ledger.published(key, f"vid-{key}")
    ledger.end(key)
    schedule.keep(schedule.Booking(key, kind, source, when, notify, NOW, meeting), now=NOW)


def _times() -> dict:
    return {b.key: b.at for b in schedule.upcoming(NOW)}


# ------------------------------------------------------------- the settings


def test_release_numbers_saved_under_old_rules_give_way_to_todays_defaults():
    files.write_json(files.state_dir() / "prefs.json", {"publishing": {
        "review_hours": 12.0, "min_gap_hours": 2.0, "max_per_day": 6, "shorts_per_day": 3,
        "window_start_hour": 10, "window_end_hour": 22, "shorts_after_full_hours": 24.0, "shorts_per_meeting": 2,
        "hashtags_max": 5}})
    p = pub_settings.current()
    assert (p.review_hours, p.min_gap_hours, p.max_per_day) == (0.0, 1.0, 24)
    assert (p.window_start_hour, p.window_end_hour, p.shorts_after_full_hours) == (0, 24, 1.0)
    assert p.shorts_per_meeting == pub_settings.DEFAULTS.shorts_per_meeting
    assert p.hashtags_max == 5  # not a release number: kept


def test_a_save_keeps_only_what_differs_and_survives_a_rules_change():
    pub_settings.save({"min_gap_hours": 3, "hashtags_max": 5})
    stored = files.read_json(files.state_dir() / "prefs.json", {})["publishing"]
    assert stored == {"min_gap_hours": 3.0, "hashtags_max": 5, "_rules_version": pub_settings.RULES_VERSION}
    assert pub_settings.current().min_gap_hours == 3.0  # a choice made under today's rules stays


def test_the_defaults_are_one_hour_apart_and_up_to_24_a_day():
    d = pub_settings.DEFAULTS
    assert (d.min_gap_hours, d.max_per_day, d.window_start_hour, d.window_end_hour) == (1.0, 24, 0, 24)
    assert d.review_hours == 0.0 and d.shorts_after_full_hours == 1.0
    assert pub_settings.capacity_warning(d) is None


# ------------------------------------------------------------- the slot book


def test_hold_earlier_moves_a_far_booking_to_the_first_free_slot():
    schedule.keep(schedule.Booking("a", "full", "m1", at(20, 10), False, NOW), now=NOW)
    old, new = schedule.hold_earlier("a", lead_time=timedelta(minutes=30), now=NOW)
    assert old.at == at(20, 10) and new.at == at(17, 9, 30)
    assert _times() == {"a": at(17, 9, 30)}


def test_hold_earlier_keeps_an_hour_from_other_videos():
    schedule.keep(schedule.Booking("x", "full", "m0", at(17, 10), False, NOW), now=NOW)
    schedule.keep(schedule.Booking("a", "short", "m1", at(20, 10), False, NOW), now=NOW)
    _, new = schedule.hold_earlier("a", lead_time=timedelta(minutes=30), now=NOW)
    assert new.at == at(17, 11)


def test_hold_earlier_does_nothing_for_a_small_gain():
    schedule.keep(schedule.Booking("a", "full", "m1", at(17, 10), False, NOW), now=NOW)
    assert schedule.hold_earlier("a", lead_time=timedelta(minutes=30), now=NOW) is None


def test_a_notifying_video_moves_only_to_a_day_without_another_notification():
    schedule.keep(schedule.Booking("x", "topic", "m0", at(17, 12), True, NOW), now=NOW)
    schedule.keep(schedule.Booking("a", "full", "m1", at(20, 10), True, NOW), now=NOW)
    _, new = schedule.hold_earlier("a", lead_time=timedelta(minutes=30), now=NOW)
    assert new.at == at(18, 0)


# ------------------------------------------------------------- the round


def test_a_round_brings_a_meeting_forward_in_its_own_order(channel):
    _posted("full", "full", "zoom:m1", at(19, 12))
    _posted("s1", "short", "zoom:m1", at(20, 12))
    _posted("s2", "short", "zoom:m1", at(20, 14))
    result = repack.pull_forward(NOW)
    assert result["moved"] == 3
    times = _times()
    assert times["full"] == at(17, 9, 30)
    assert times["s1"] >= times["full"] + timedelta(hours=1)  # Shorts stay an hour behind the full video
    assert times["s2"] >= times["s1"] + timedelta(hours=1)
    assert [c[0] for c in channel] == ["vid-full", "vid-s1", "vid-s2"]
    assert get_ledger().get("full").publish_at == schedule.rfc3339(at(17, 9, 30))


def test_a_second_round_moves_nothing(channel):
    _posted("full", "full", "zoom:m1", at(19, 12))
    repack.pull_forward(NOW)
    channel.clear()
    assert repack.pull_forward(NOW)["moved"] == 0 and channel == []


def test_nothing_on_the_never_post_list_is_brought_forward(channel):
    _posted("ops", "full", "zoom:m9", at(19, 12), title="Operations weekly sync", meeting="Operations weekly sync")
    assert repack.pull_forward(NOW)["moved"] == 0
    assert _times()["ops"] == at(19, 12) and channel == []


def test_without_the_edit_permission_nothing_moves_and_the_page_says_reconnect(monkeypatch):
    monkeypatch.setattr(yt, "granted_scopes", lambda: set(yt.BASE_SCOPES))
    _posted("full", "full", "zoom:m1", at(19, 12))
    result = repack.pull_forward(NOW)
    assert result["moved"] == 0 and "Reconnect YouTube" in result["note"]
    assert _times()["full"] == at(19, 12)
    assert "Reconnect" in repack.last_round()["note"]


def test_when_youtube_says_no_the_old_time_is_put_back(monkeypatch, channel):
    monkeypatch.setattr(repack, "youtube_set", lambda video_id, when: False)  # already public, say
    _posted("full", "full", "zoom:m1", at(19, 12))
    assert repack.pull_forward(NOW)["moved"] == 0
    assert _times()["full"] == at(19, 12)
    assert get_ledger().get("full").publish_at == schedule.rfc3339(at(19, 12))


def test_a_quota_stop_puts_the_time_back_and_ends_the_round(monkeypatch, channel):
    monkeypatch.setattr(repack.optional, "call",
                        lambda *a, **k: optional.Outcome(optional.STOP_ALL, reason="YouTube quota used up"))
    _posted("full", "full", "zoom:m1", at(19, 12))
    _posted("other", "full", "zoom:m2", at(19, 15))
    result = repack.pull_forward(NOW)
    assert result == {"moved": 0, "note": "YouTube quota used up"}
    assert _times() == {"full": at(19, 12), "other": at(19, 15)}


def test_a_video_not_on_youtube_yet_is_left_to_its_upload(channel):
    schedule.keep(schedule.Booking("later", "full", "zoom:m1", at(19, 12), False, NOW), now=NOW)
    assert repack.pull_forward(NOW)["moved"] == 0 and _times()["later"] == at(19, 12)


def test_the_release_plan_page_carries_the_last_round(channel):
    client = TestClient(api_main.app)
    _posted("full", "full", "zoom:m1", datetime.now(UTC) + timedelta(days=3))
    repack.pull_forward()
    body = client.get("/publishing/schedule").json()
    assert body["pull_forward"]["moved"] == 1


def test_a_saved_thumbnail_test_mode_gives_way_to_ours_on_every_video():
    files.write_json(files.state_dir() / "prefs.json", {"publishing": {
        "thumbnail_mode": "test", "min_gap_hours": 3.0, "_rules_version": 3}})
    p = pub_settings.current()
    assert p.thumbnail_mode == "custom"
    assert p.min_gap_hours == 3.0  # chosen under the rules of version 3: kept
    pub_settings.save({"thumbnail_mode": "test"})
    assert pub_settings.current().thumbnail_mode == "test"  # a choice made now stays


# ------------------------------------------------------------- pulling back


def test_a_personal_meeting_room_is_never_a_team_meeting():
    from autopilot import never_post

    for title in ("Flo Labs's Zoom Meeting", "Ahsan Yasin’s Zoom Meeting", "Zoom Meeting", "Personal Meeting Room"):
        assert never_post.match(title) is not None, title
    assert never_post.match("Internet of Things Meeting") is None
    assert never_post.match("Robotics and Dev Meeting") is None


def test_a_posted_personal_room_video_is_made_private_and_its_slot_freed(monkeypatch, channel):
    made = []
    monkeypatch.setattr(repack, "_make_private", lambda vid: made.append(vid) or True)
    _posted("leak", "full", "zoom:p1", at(19, 12), title="Coach mode: stopwatch and attendance",
            meeting="Flo Labs's Zoom Meeting")
    _posted("ok", "full", "zoom:m1", at(19, 15))
    result = repack.pull_forward(NOW)
    assert made == ["vid-leak"] and result["pulled_back"] == 1
    assert "leak" not in _times()
    assert repack.pull_forward(NOW).get("pulled_back") is None  # done once, not every round
