"""The team (series) registry (publish/series.py). Synthetic names only."""

import threading

import pytest

from api.jobs import job_store
from autopilot import never_post
from autopilot.store import Meeting, store
from core.models import JobRecord
from ledger import files
from publish import series

PL = "PLabcdefghij12345"


def _plant_never(*words):
    never_post.set_entries(list(words))


def _seed_empty():
    series.seed_once()


@pytest.fixture(autouse=True)
def _fresh_store():
    store._state = None
    yield
    store._state = None


def _add_meeting(uuid, topic, meeting_id=None):
    with store.editing() as state:
        state.meetings[uuid] = Meeting(uuid=uuid, topic=topic, meeting_id=meeting_id,
                                       start_time="2026-01-05T10:00:00Z")


def test_series_key():
    assert series.series_key("Internet of Things Meeting (2026-11-03)") == "internet of things"
    assert series.series_key("Team Meeting") == ""
    assert series.series_key("") == ""
    assert series.series_key(None) == ""


def test_aliases_resolve_and_tiers():
    _seed_empty()
    series.set_row("gadget lab", label="Gadget Lab", tier="public", aliases=["gadgets"])
    series.set_row("archive club", tier="archive")
    assert series.lookup("Gadgets Weekly (2026-02-01)").key == "gadget lab"
    assert series.lookup_key("gadgets").key == "gadget lab"
    assert series.tier_of("Gadget Lab Meeting") == "public"
    assert series.tier_of("Gadgets sync") == "public"
    assert series.tier_of("Archive Club") == "archive"
    assert series.tier_of("Brand New Group") == "unknown"
    assert series.tier_of("Team Meeting") == "unknown"
    _plant_never("zorblat")
    assert series.tier_of("Zorblat Review") == "never"


def test_tier_for_job_uses_zoom_topic_first():
    _seed_empty()
    series.set_row("gadget lab", tier="public")
    job = JobRecord(job_id="j1", title="Something else", zoom_meeting={"topic": "Gadget Lab Meeting"})
    assert series.tier_for_job(job) == "public"
    assert series.tier_for_job(JobRecord(job_id="j2", title="Gadget Lab")) == "public"
    assert series.tier_for_job(JobRecord(job_id="j3", title="Team Meeting")) == "unknown"


def test_set_row_refuses_public_for_never_post():
    _seed_empty()
    _plant_never("zorblat")
    with pytest.raises(ValueError, match="never-post always wins"):
        series.set_row("zorblat club", tier="public")
    with pytest.raises(ValueError, match="never-post always wins"):
        series.set_row("zorblat club", tier="archive")
    assert series.set_row("zorblat club", tier="never").tier == "never"
    # an example topic that matches counts too
    series.set_row("plain group", tier="public")
    series.note_seen("Plain Group Meeting")
    _plant_never("zorblat", "meeting")
    with pytest.raises(ValueError, match="never-post always wins"):
        series.set_row("plain group", tier="archive")


def test_validation():
    _seed_empty()
    with pytest.raises(ValueError):
        series.set_row("alpha", label="Bad <b>")
    with pytest.raises(ValueError):
        series.set_row("alpha", playlists={"full": "nope"})
    with pytest.raises(ValueError):
        series.set_row("alpha", playlists={"other": PL})
    with pytest.raises(ValueError):
        series.set_row("alpha", hashtags=["#One", "#Two", "#Three"])
    with pytest.raises(ValueError):
        series.set_row("alpha", hashtags=["NoHash"])
    with pytest.raises(ValueError):
        series.set_row("alpha", hashtags=["#" + "a" * 30])
    with pytest.raises(ValueError):
        series.set_row("alpha", colour="red")
    row = series.set_row("alpha", hashtags=["#AlphaTeam", "#Ai2"], playlists={"full": PL})
    assert row.hashtags == ["#AlphaTeam", "#Ai2"] and row.playlists["full"] == PL
    assert series.set_playlist("alpha", "shorts", PL).playlists["shorts"] == PL
    assert series.set_row("alpha", playlists={"full": ""}).playlists["full"] == ""
    with pytest.raises(ValueError):
        series.set_playlist("alpha", "cuts", "bad id")


def test_note_seen_never_creates_a_tier_and_keeps_three_examples():
    _seed_empty()
    for n in range(5):
        series.note_seen(f"Delta Group Meeting (2026-02-0{n + 1})")
    row = series.lookup("Delta Group")
    assert row.tier is None and row.seen == 5 and len(row.examples) == 3
    assert row.examples[-1].endswith("(2026-02-05)")
    assert series.tier_of("Delta Group") == "unknown"
    assert series.note_seen("Team Meeting") is None


def test_seed_once():
    _add_meeting("u1", "Gadget Lab Meeting (2026-01-05)")
    _add_meeting("u2", "Gadget Lab Meeting (2026-01-12)")
    _add_meeting("u3", "Zorblat Meeting")
    _plant_never("zorblat")
    job_store.create(JobRecord(job_id="jobone", title="Widget Crew Weekly"))
    job_store.create(JobRecord(job_id="jobtwo", title="Team Meeting"))
    series.seed_once()
    by_key = {r.key: r for r in series.rows()}
    assert set(by_key) == {"gadget lab", "zorblat", "widget crew"}
    assert by_key["gadget lab"].tier == "public" and by_key["gadget lab"].seen == 2
    assert by_key["zorblat"].tier == "never"
    assert by_key["widget crew"].tier == "public"
    # runs once: a deleted row is not added back, a new meeting is not seeded
    assert series.delete_row("widget crew")
    _add_meeting("u4", "Late Group")
    series.seed_once()
    assert {r.key for r in series.rows()} == {"gadget lab", "zorblat"}
    assert series.tier_of("Late Group") == "unknown"


def test_seed_keeps_a_team_public_when_only_one_meeting_is_listed_by_zoom_id():
    # the never-post list holds the ID of one recurring meeting; another meeting with the
    # same team name was posted until now and must keep posting
    _plant_never("111 2223 3344")
    _add_meeting("u1", "Gadget Review", meeting_id="11122233344")
    _add_meeting("u2", "Gadget Review", meeting_id="99988877766")
    series.seed_once()
    assert series.lookup("Gadget Review").tier == "public"
    assert series.tier_of("Gadget Review", "99988877766") == "public"
    assert series.tier_of("Gadget Review", "11122233344") == "never"  # never-post stays the guard


def test_seed_marks_never_when_every_title_is_caught_by_words():
    _plant_never("zorblat")
    _add_meeting("u1", "Zorblat Meeting")
    _add_meeting("u2", "Zorblat Meeting (2026-01-12)")
    series.seed_once()
    assert series.lookup("Zorblat Meeting").tier == "never"


def test_flolabs_prefix_stripped_from_label():
    _add_meeting("u1", "FloLabs Gadget Lab Meeting")
    series.seed_once()
    assert series.lookup_key("flolabs gadget lab").label == "Gadget Lab"


def test_concurrent_changes_lose_nothing():
    _seed_empty()
    errors = []

    def seen(n):
        try:
            for _ in range(5):
                series.note_seen(f"Group {chr(97 + n)} Meeting")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def create(n):
        try:
            series.set_row(f"other {chr(97 + n)}", label=f"Other {n}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=seen, args=(n,)) for n in range(6)]
    threads += [threading.Thread(target=create, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    by_key = {r.key: r for r in series.rows()}
    assert all(by_key[f"group {chr(97 + n)}"].seen == 5 for n in range(6))
    assert all(f"other {chr(97 + n)}" in by_key for n in range(6))


def test_damaged_file_is_not_overwritten():
    path = files.state_dir() / series.FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    assert series.tier_of("Gadget Lab") == "unknown"
    assert series.lookup("Gadget Lab") is None
    assert series.rows() == []
    assert series.note_seen("Gadget Lab") is None
    with pytest.raises(ValueError):
        series.set_row("gadget lab", tier="public")
    assert path.read_text(encoding="utf-8") == "{ this is not json"


def test_colour_index_is_stable():
    first = series.colour_index("gadget lab")
    assert 0 <= first <= 5
    assert series.colour_index("gadget lab") == first == series.colour_index("Gadget  Lab")
    assert {series.colour_index(f"team {n}") for n in range(50)} == set(range(6))


def test_merge_keeps_counts():
    _seed_empty()
    for _ in range(2):
        series.note_seen("Gadget Lab")
    for _ in range(3):
        series.note_seen("Gadgets")
    series.set_row("gadget lab", tier="public")
    series.set_row("gadgets", playlists={"full": PL})
    row = series.merge("gadgets", "gadget lab")
    assert row.seen == 5 and "gadgets" in row.aliases and row.playlists["full"] == PL
    assert series.lookup_key("gadgets").key == "gadget lab"
    assert [r.key for r in series.rows()] == ["gadget lab"]
    with pytest.raises(ValueError):
        series.merge("gadget lab", "gadget lab")


def test_held_keys():
    _seed_empty()
    series.set_row("gadget lab", tier="public")
    _plant_never("zorblat")
    held = series.held_keys(["Gadget Lab", "Brand New Group", {"topic": "Zorblat Sync"}, "Team Meeting"])
    assert held == {"brand new group", "zorblat"}


def test_set_row_on_an_alias_changes_the_owning_team():
    _seed_empty()
    series.set_row("gadget lab", label="Gadget Lab", aliases=["gadgets"])
    row = series.set_row("gadgets", tier="public")
    assert row.key == "gadget lab" and row.tier == "public"
    assert [r.key for r in series.rows()] == ["gadget lab"]
    assert series.tier_of("Gadgets Weekly") == "public"


def test_seeding_reads_other_stores_without_the_series_lock(monkeypatch):
    held = []
    real = series._gather_sources

    def spy():
        held.append(series._lock._is_owned())
        return real()

    monkeypatch.setattr(series, "_gather_sources", spy)
    series.rows()
    assert held == [False]
