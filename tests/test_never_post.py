"""The never-post list (autopilot/never_post.py): Operations, Project
Management, Legal, HR and 1-to-1 meetings are never processed or posted by the
autopilot, at whatever stage it finds out."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from api.jobs import job_store
from autopilot import never_post
from autopilot.engine import OLD_GONE_ERROR, iso
from autopilot.store import Meeting
from core.models import JobStatus
from ingest import zoom
from ledger import files, prefs
from ledger.uploads import get_ledger
from publish import settings
from tests.test_autopilot import PM, T0, World, _meeting

START = T0 - timedelta(hours=2)


@pytest.fixture
def world(monkeypatch):
    # these tests find a posted video by the meeting name its title starts with
    settings.save({"title_style": "meeting_date"})
    return World(monkeypatch)


# ------------------------------------------------------------------ what it recognises


@pytest.mark.parametrize("title", [
    "Operations team meeting",
    "OPERATIONS",
    "monday operation meeting",
    "Operations: Team Chat - A & B",
    "Project Management Team meeting",
    "project  management sync",
    "Ethics & Legal Team meeting",
    "legal team",
    "Someone and HR Team",
    "Someone: FloLabs HR Meeting",
    "Someone: 1 to 1 meeting",
    "Someone 1-to-1",
    "1:1 with someone",
    "one on one",
])
def test_the_built_in_list_stops_these(title):
    assert never_post.match(title) is not None


@pytest.mark.parametrize("title", [
    # the team meetings on the main Zoom account, as they are really called
    "Automation Team Meeting", "Data Analytics", "Design Marketing & Data Analytics", "Design Team",
    "FloBrain Weekly Backend Meeting", "GTM & Influencers Team meeting", "Humanoid Team Meeting",
    "Internet of Things Meeting", "MoodChanger Meeting", "Multi-modal perception team meeting",
    "Robotics and Dev Meeting", "Robotics Team meeting", "Space Team meeting", "Technical Meeting",
    "Urgent Frontend Team Meeting", "Weekly FrontEnd Team meeting",
    # whole words only
    "Cooperation meeting", "Operational metrics review", "Paralegal sync", "Three hours of demos", "Sprint 11",
    "",
])
def test_the_built_in_list_lets_these_through(title):
    assert never_post.match(title) is None


def test_a_renamed_repeating_meeting_is_known_by_its_zoom_id():
    assert "84116996599" in never_post.match("Monday catch-up", 84116996599)
    assert never_post.match("Monday catch-up", 84116996598) is None
    assert never_post.match("Monday catch-up", 123, ["123 456 789"]) is None
    assert never_post.match("Monday catch-up", 123456789, ["123 456 789"]) is not None


def test_the_saved_list_replaces_the_built_in_one_and_an_empty_one_counts():
    assert never_post.set_entries(["  Board   meeting ", "", "board meeting", "BOARD-MEETING", "841 1699 6599"]) == [
        "Board meeting", "841 1699 6599"]
    assert never_post.entries() == ["Board meeting", "841 1699 6599"]
    assert never_post.match("Operations team meeting") is None
    assert never_post.match("The board meeting") is not None
    assert never_post.set_entries([]) == [] and never_post.entries() == []


@pytest.mark.parametrize("entry", ["&&", "x" * 101])
def test_an_entry_that_could_never_match_is_refused(entry):
    with pytest.raises(ValueError):
        never_post.set_entries(["operations", entry])
    assert never_post.entries() == list(never_post.DEFAULT_ENTRIES)  # nothing saved


@pytest.mark.parametrize("damaged", ["operations", [1, 2], None])
def test_a_damaged_saved_list_falls_back_to_the_built_in_one(damaged):
    files.write_json(files.state_dir() / prefs.FILE, {"never_post": damaged})
    assert never_post.entries() == list(never_post.DEFAULT_ENTRIES)


def test_saving_the_retention_setting_keeps_the_list():
    never_post.set_entries(["board"])
    prefs.set_retention_hours(12)
    assert never_post.entries() == ["board"]


# ------------------------------------------------------------------ the autopilot


def test_a_listed_meeting_is_skipped_without_asking_zoom_and_the_others_go_ahead(world):
    world.zoom.meetings["ops"] = _meeting("ops", START, topic="Operations team meeting")
    world.zoom.meetings["team"] = _meeting("team", START, topic="Weekly FrontEnd Team meeting")
    world.step()
    ops = world.meeting("ops")
    assert ops.state == "skipped" and ops.error.startswith('Never posted: its title has "operations"')
    assert world.submitted == [("team", False)] and ("get", "ops") not in world.zoom.calls


def test_the_zoom_id_stops_a_meeting_someone_renamed(world):
    renamed = _meeting("ops", START, topic="Monday catch-up")
    renamed["id"] = 84116996599
    world.zoom.meetings["ops"] = renamed
    world.step()
    assert world.meeting("ops").state == "skipped" and "84116996599" in world.meeting("ops").error
    assert world.submitted == [] and ("get", "ops") not in world.zoom.calls  # the list's own row gave the ID away


def test_a_waiting_meeting_renamed_onto_the_list_in_zoom_is_stopped(world):
    world.zoom.meetings["m1"] = _meeting("m1", START, transcript=False)
    world.step()
    assert world.meeting("m1").state == "waiting_transcript"
    world.zoom.meetings["m1"] = _meeting("m1", START, transcript=False, topic="HR review")
    world.step(minutes=30)
    assert world.meeting("m1").state == "skipped" and world.submitted == []


def test_the_list_holds_while_the_autopilot_is_paused(world):
    world.zoom.meetings["m1"] = _meeting("m1", START, transcript=False)
    world.step()
    world.engine.set_paused(True)
    never_post.set_entries(["weekly sync"])
    world.step(minutes=1)
    assert world.meeting("m1").state == "skipped"


def test_a_job_already_running_may_finish_but_nothing_of_it_is_posted(world):
    world.zoom.meetings["m1"] = _meeting("m1", START)
    world.step()
    job_id = world.meeting("m1").job_id
    never_post.set_entries(["weekly sync"])
    world.step(minutes=1)
    assert world.meeting("m1").state == "skipped"
    world.finish(job_id, "m1")
    for minutes in (1, 30, 60):
        world.step(minutes=minutes)
    assert world.meeting("m1").state == "skipped" and world.channel.videos == {}


def test_a_meeting_waiting_for_its_next_posting_round_is_stopped(world):
    world.zoom.meetings["m1"] = _meeting("m1", START)
    world.step()
    job_id = world.meeting("m1").job_id
    world.finish(job_id, "m1")
    with world.engine.store.editing() as state:
        state.meetings["m1"].state, state.meetings["m1"].next_try_at = "posting", iso(T0 + timedelta(hours=1))
    never_post.set_entries(["weekly sync"])
    world.step(minutes=1)  # its next round is an hour away: it leaves the queue now, not when that comes
    assert world.meeting("m1").state == "skipped"
    world.step(minutes=60)
    assert world.meeting("m1").state == "skipped" and world.channel.videos == {}


def test_the_last_look_before_posting_also_reads_what_the_job_knows(world):
    """Its own record says "Weekly Sync", but the job was cut as the Operations meeting."""
    world.zoom.meetings["m1"] = _meeting("m1", START)
    world.step()
    job = world.finish(world.meeting("m1").job_id, "m1")
    job.zoom_meeting = {"topic": "Operations team meeting", "meeting_id": 84116996599}
    job_store.update(job)
    world.step(minutes=1)
    assert world.meeting("m1").state == "skipped" and world.channel.videos == {}
    assert world.meeting("m1").error.startswith("Never posted: ")


def test_a_meeting_the_old_rule_skipped_as_deleted_stays_off_if_it_is_listed(world):
    with world.engine.store.editing() as state:
        state.meetings["ops"] = Meeting(uuid="ops", host_email=PM, topic="Operations team meeting",
                                        start_time=iso(START), state="skipped", error=OLD_GONE_ERROR)
    world.zoom.meetings["ops"] = _meeting("ops", START, topic="Operations team meeting")
    world.step()
    assert world.meeting("ops").state == "skipped" and world.submitted == []
    assert world.meeting("ops").error.startswith("Never posted: ")


def test_only_taking_it_off_the_list_lets_it_be_posted(world):
    world.zoom.meetings["ops"] = _meeting("ops", START, topic="Operations team meeting")
    world.step()
    with pytest.raises(ValueError, match="Take it off the list first"):
        world.engine.retry("ops")
    assert world.meeting("ops").state == "skipped"
    never_post.set_entries(["legal"])
    world.step(minutes=1)
    assert world.meeting("ops").state == "skipped" and world.submitted == []  # never comes back by itself
    world.engine.retry("ops")  # "Post it after all"
    world.step(minutes=1)
    assert world.submitted == [("ops", False)]


# ------------------------------------------------------------------ all the way to the channel

TEAM_MEETINGS = {"Space Team meeting": 81929595102, "Design Team": 87096814480,
                 "FloBrain Weekly Backend Meeting": 89952328054}
PRIVATE_MEETINGS = {"Operations team meeting": 84116996599, "Project Management Team meeting": 85138528372,
                    "Ethics & Legal Team meeting": 89716953476, "Someone and HR Team": 82348993968,
                    "Someone: 1 to 1 meeting": 84506002536}


def _add_meetings(world, meetings):
    for i, (topic, meeting_id) in enumerate(meetings.items()):
        meeting = _meeting(topic, START - timedelta(minutes=i), topic=topic)  # the uuid is the title, for the checks
        meeting["id"] = meeting_id
        world.zoom.meetings[topic] = meeting


def _run_until_quiet(world):
    """The autopilot's steps, with every job it starts finishing at once, cut
    under its meeting's title as a real job is."""
    for _ in range(30):
        world.step(minutes=1)
        for m in world.engine.store.read().meetings.values():
            if m.state == "processing" and job_store.get(m.job_id).status != JobStatus.DONE:
                job = world.finish(m.job_id, m.uuid)
                job.title, job.zoom_meeting = m.topic, {"topic": m.topic, "meeting_id": m.meeting_id}
                job_store.update(job)


def _on_the_channel(world, topic) -> int:
    return sum(1 for video in world.channel.videos.values() if video["title"].startswith(topic))


def test_only_the_team_meetings_reach_the_channel(world):
    _add_meetings(world, {**PRIVATE_MEETINGS, **TEAM_MEETINGS})
    _run_until_quiet(world)
    for topic in TEAM_MEETINGS:
        assert world.meeting(topic).state == "posted" and _on_the_channel(world, topic) == 1
    for topic in PRIVATE_MEETINGS:
        assert world.meeting(topic).state == "skipped" and world.meeting(topic).error.startswith("Never posted: ")
        assert get_ledger().meeting_state(f"zoom:{topic}") != "posted"
    assert {uuid for uuid, _ in world.submitted} == set(TEAM_MEETINGS)  # nothing was even processed
    # three shorts and the full video of each team meeting, and not one video more
    assert len(world.channel.videos) == 4 * len(TEAM_MEETINGS)


def test_without_the_list_those_meetings_would_have_gone_up(world):
    """The check above means something: the same run with an empty list posts them."""
    never_post.set_entries([])
    _add_meetings(world, {**PRIVATE_MEETINGS, **TEAM_MEETINGS})
    _run_until_quiet(world)
    for topic in {**PRIVATE_MEETINGS, **TEAM_MEETINGS}:
        assert world.meeting(topic).state == "posted" and _on_the_channel(world, topic) == 1


def test_a_finished_job_someone_started_by_hand_is_not_posted_by_the_autopilot(world):
    _add_meetings(world, {"Operations team meeting": 84116996599})
    job = world.finish("by-hand", "Operations team meeting")
    job.title = "Operations team meeting"
    job_store.update(job)
    world.manual["Operations team meeting"] = job
    _run_until_quiet(world)
    assert world.meeting("Operations team meeting").state == "skipped" and world.channel.videos == {}


# ------------------------------------------------------------------ the page's API


def test_the_page_gets_the_list_and_which_meetings_are_on_it(world):
    world.zoom.meetings["ops"] = _meeting("ops", START, topic="Operations team meeting")
    world.zoom.meetings["team"] = _meeting("team", START, topic="Space Team meeting")
    world.step()
    client = TestClient(api_main.app)
    body = client.get("/autopilot").json()
    assert body["never_post"] == list(never_post.DEFAULT_ENTRIES)
    assert {m["uuid"]: m["never_post"] for m in body["meetings"]} == {"ops": True, "team": False}
    refused = client.post("/autopilot/meetings/retry", json={"uuid": "ops"})
    assert refused.status_code == 409 and "Take it off the list first" in refused.json()["detail"]


def test_the_page_can_change_the_list(world):
    client = TestClient(api_main.app)
    saved = client.post("/autopilot/never-post", json={"entries": ["board", "", "83243308622"]})
    assert saved.status_code == 200 and saved.json()["never_post"] == ["board", "83243308622"]
    assert client.get("/autopilot").json()["never_post"] == ["board", "83243308622"]
    refused = client.post("/autopilot/never-post", json={"entries": ["&&"]})
    assert refused.status_code == 400 and "no letters or numbers" in refused.json()["detail"]
    assert never_post.entries() == ["board", "83243308622"]


def test_the_zoom_list_marks_meetings_on_the_list(monkeypatch):
    class Client:
        def list_recordings(self, host, start, end):
            return [{"uuid": "a", "topic": "Project Management Team meeting", "meeting_id": 85138528372},
                    {"uuid": "b", "topic": "Renamed", "meeting_id": 85138528372},
                    {"uuid": "c", "topic": "Design Team", "meeting_id": 87096814480}]

    monkeypatch.setattr(zoom, "get_client", lambda: Client())
    rows = TestClient(api_main.app).get(f"/zoom/recordings?host={PM}").json()["meetings"]
    assert [r["never_post"] for r in rows] == [True, True, False]


def test_a_cancelled_job_of_a_listed_meeting_is_not_tried_again(world):
    world.zoom.meetings["m1"] = _meeting("m1", START)
    world.step()
    job = job_store.get(world.meeting("m1").job_id)
    never_post.set_entries(["weekly sync"])
    job.status = JobStatus.CANCELLED
    job_store.update(job)
    for minutes in (1, 60, 600):
        world.step(minutes=minutes)
    assert world.meeting("m1").state == "skipped" and len(world.submitted) == 1
