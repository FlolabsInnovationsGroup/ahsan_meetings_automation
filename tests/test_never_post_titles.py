"""What people really type as a meeting's title: every way of writing the
meetings that must stay off YouTube, and the real team meetings (and near
misses) that must keep going up."""

import pytest

from autopilot import never_post

MUST_STOP = [
    # Operations, as written
    "Operations team meeting", "Operations", "operations", "OPERATIONS TEAM MEETING", "Operation meeting",
    "monday operation meeting", "Weekly Operations Sync", "Operations: HubSpot Emails - A & B",
    "Operations call: templates | A & B", "operations meeting",
    # Operations, the way people shorten and decorate it
    "Ops", "ops", "OPS", "Ops sync", "Ops team meeting", "Weekly Ops", "Ops - weekly", "[Ops] planning", "Ops/Finance",
    "Team Ops catch-up",
    # punctuation, spacing and accents Zoom keeps as they were typed
    "operations-team", "Operations_Team", "(Operations) weekly", "Operations, weekly",
    "  Operations team meeting  ", "Operations team meeting", "Operations  team   meeting",
    "Opérations team meeting", "🚀 Operations team meeting 🚀", "Operations\tteam",
    # Project Management, as written
    "Project Management Team meeting", "Project Management", "project management", "PROJECT MANAGEMENT TEAM MEETING",
    "Project-Management sync", "Project_Management", "project  management weekly", "Weekly Project Management",
    "Project Manager sync", "Project Managers weekly",
    # Project Management, the way the team says it
    "PM", "pm", "PM team", "PM Team Meeting", "PM team meeting", "PM meeting", "pm sync", "Weekly PM", "PM weekly",
    "PM's weekly", "PM-team", "PM / Design", "PMO weekly",
    # Legal
    "Ethics & Legal Team meeting", "Ethics and Legal", "legal team", "Legal", "LEGAL review", "Legal-Team sync",
    # HR
    "HR", "hr", "HR sync", "HR Team", "Someone and HR Team", "Someone: FloLabs HR Meeting", "Someone : FloLabs HR Meeting",
    "HR-team", "Human Resources", "human resources weekly", "H.R. review",
    # 1-to-1
    "Someone: 1 to 1 meeting", "1 to 1", "1-to-1", "1 to 1 with someone", "Someone 1-to-1", "1:1", "1 : 1", "1:1 with someone",
    "1-1", "1 - 1", "1 on 1", "1-on-1", "1on1", "1to1", "One to One", "one-to-one", "One on One", "one-on-one with someone",
]

MUST_POST = [
    # every team meeting on the main Zoom account, as they are really called
    "Automation Team Meeting", "Data Analytics", "Design Marketing & Data Analytics", "Design Team",
    "FloBrain Weekly Backend Meeting", "GTM & Influencers Team meeting", "Humanoid Team Meeting",
    "Internet of Things Meeting", "MoodChanger Meeting", "Moodchanger Meeting", "Multi-modal perception team meeting",
    "Robotics and Dev Meeting", "Robotics Team meeting", "Space Team meeting", "Technical Meeting",
    "Urgent Frontend Team Meeting", "Weekly FrontEnd Team meeting",
    # other real titles that are about work, not people
    "Exceptional IoT meeting", "flobrain the road map", "FloLabs Innovation Group Website Audit", "strapi + railway",
    "The urgent all-IoT team meeting", "LinkedIn Mini Team", "youtube team meeting",
    # words that only look like the list's
    "Cooperation meeting", "Operational metrics review", "Reoperations", "Opscode demo", "Stops and starts",
    "DevOps sync", "Topsoil", "Drops", "Laptops", "Paralegal sync", "Legality check", "Illegal moves", "Three hours of demos",
    "Chrome bug", "Shrimp", "Hrithik demo", "PMF review", "npm upgrade", "pm2 setup", "RPM tuning", "Pmod board",
    "Project Hermes", "Product Management", "Management sync", "Project sync",
    # numbers that only look like a 1-to-1
    "Sprint 11", "Sprint 1", "Release 1.1.1", "Q1 2026", "10:15 demo", "2 on 2", "2-to-3 migration", "One team",
    "One Piece", "Version 1 to 2 upgrade", "Stage 1", "",
]


@pytest.mark.parametrize("title", MUST_STOP)
def test_these_never_go_up(title):
    assert never_post.match(title) is not None, title


@pytest.mark.parametrize("title", MUST_POST)
def test_these_keep_going_up(title):
    assert never_post.match(title) is None, f"{title!r} stopped: {never_post.match(title)}"


@pytest.mark.parametrize("title", ["Demo at 5 PM", "Dev Ops sync", "Co-operation with a partner"])
def test_known_price_of_being_careful(title):
    """Whole words also catch these. A meeting wrongly kept off YouTube shows
    why in the side panel and can be posted; a private meeting wrongly posted
    can't be taken back from whoever saw it."""
    assert never_post.match(title) is not None


@pytest.mark.parametrize("meeting_id, listed", [
    (84116996599, True), ("84116996599", True), (85138528372, True), (89716953476, True),
    (84116996598, False), (841169965990, False), (8411699659, False), (None, False), (0, False), ("", False),
])
def test_the_zoom_ids_match_exactly(meeting_id, listed):
    assert (never_post.match("A renamed meeting", meeting_id) is not None) is listed
