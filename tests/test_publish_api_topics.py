"""The HTTP side of topic cuts: POST /jobs/{id}/publish with topics, the artifact route, the
posted list and the release plan labels. A fake YouTube; synthetic data only."""

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from autopilot import never_post
from publish import run as publish_run
from tests.fake_channel import FakeChannel
from tests.test_publish import _connect_fake_channel, _reread
from tests.test_topic_release import _cut_job


@pytest.fixture(autouse=True)
def _no_follow_ups(monkeypatch):
    monkeypatch.setattr(publish_run, "_queue_extras", lambda *args, **kwargs: None)


@pytest.fixture
def channel(monkeypatch):
    _connect_fake_channel()
    return FakeChannel().install(monkeypatch)


@pytest.fixture
def client():
    return TestClient(api_main.app)


def test_the_api_accepts_topics_and_posts_the_cut(client, channel):
    job = _cut_job(with_shorts=False)
    res = client.post(f"/jobs/{job.job_id}/publish", json={"privacy": "private", "topics": [{"index": 1}]})
    assert res.status_code == 200, res.text
    after = _reread(job.job_id)
    assert after.youtube_topics[1].status == "published"
    assert channel.uploaded[0]["title"] == "Moving the session store to Redis | FloLabs IoT"


def test_a_typed_title_and_description_are_used(client, channel):
    job = _cut_job(with_shorts=False)
    res = client.post(f"/jobs/{job.job_id}/publish", json={
        "privacy": "private", "topics": [{"index": 1, "title": "My cut", "description": "My words."}]})
    assert res.status_code == 200
    assert channel.uploaded[0]["title"] == "My cut" and channel.uploaded[0]["description"] == "My words."


def test_the_title_length_is_checked(client, channel):
    job = _cut_job()
    too_long = client.post(f"/jobs/{job.job_id}/publish", json={"topics": [{"index": 1, "title": "x" * 101}]})
    assert too_long.status_code == 422
    angle = client.post(f"/jobs/{job.job_id}/publish", json={"topics": [{"index": 1, "title": "a <b>"}]})
    assert angle.status_code == 400
    too_many = client.post(f"/jobs/{job.job_id}/publish", json={"topics": [{"index": n} for n in (1, 2, 3, 4)]})
    assert too_many.status_code == 422
    assert channel.videos == {}


def test_an_unknown_cut_is_404_and_nothing_to_publish_is_400(client, channel):
    job = _cut_job()
    assert client.post(f"/jobs/{job.job_id}/publish", json={"topics": [{"index": 9}]}).status_code == 404
    assert client.post(f"/jobs/{job.job_id}/publish", json={}).status_code == 400


def test_the_never_post_403_comes_through(client, channel):
    job = _cut_job()
    never_post.set_entries(["iot"])
    res = client.post(f"/jobs/{job.job_id}/publish", json={"topics": [{"index": 1}]})
    assert res.status_code == 403 and "never-post" in res.json()["detail"]
    assert channel.videos == {}
    assert _reread(job.job_id).youtube_topics == {}


def test_a_never_team_is_refused_with_403(client, channel):
    job = _cut_job(tier="never")
    res = client.post(f"/jobs/{job.job_id}/publish", json={"topics": [{"index": 1}]})
    assert res.status_code == 403


def test_a_cut_already_on_youtube_is_409(client, channel):
    job = _cut_job(with_shorts=False)
    body = {"privacy": "private", "topics": [{"index": 1}]}
    assert client.post(f"/jobs/{job.job_id}/publish", json=body).status_code == 200
    again = client.post(f"/jobs/{job.job_id}/publish", json=body)
    assert again.status_code == 409 and "already on YouTube" in again.json()["detail"]


def test_the_cut_file_is_served_as_an_artifact(client):
    job = _cut_job()
    res = client.get(f"/jobs/{job.job_id}/artifacts/topic/topic_01.mp4")
    assert res.status_code == 200 and res.headers["content-type"] == "video/mp4"
    assert res.content == b"cut-topic/topic_01.mp4"
    assert client.get(f"/jobs/{job.job_id}/artifacts/topic/topic_02.mp4").status_code == 404


def test_the_publish_panel_data_lists_the_cuts(client):
    job = _cut_job()
    options = client.get(f"/jobs/{job.job_id}/publish").json()
    (row,) = options["topics"]
    assert row["index"] == 1 and row["ok"] and row["available"] and row["front_door"]
    assert row["title"].endswith("| FloLabs IoT") and "Full meeting" not in row["description"]
    assert options["topic_cuts"]["on"] is True


def test_the_posted_list_carries_the_topic_kind(client, channel):
    job = _cut_job(with_shorts=False)
    client.post(f"/jobs/{job.job_id}/publish", json={"title": "Full video", "privacy": "private",
                                                     "topics": [{"index": 1}]})
    body = client.get("/posted").json()
    (meeting,) = body["meetings"]
    assert meeting["full"]["title"] == "Full video" and meeting["shorts"] == []
    (cut,) = meeting["topics"]
    assert cut["topic"] == 1 and cut["title"].startswith("Moving the session store")
    from ledger.uploads import get_ledger

    assert {u.kind for u in get_ledger().posted()} == {"full", "topic"}


def test_the_release_plan_labels_a_cut_as_a_topic_cut(client, channel):
    job = _cut_job(with_shorts=False)
    res = client.post(f"/jobs/{job.job_id}/publish", json={"title": "Full video", "privacy": "public",
                                                           "topics": [{"index": 1}]})
    assert res.status_code == 200
    rows = client.get("/publishing/schedule").json()["upcoming"]
    assert [(r["kind"], r["kind_label"]) for r in rows] == [("topic", "Topic cut"), ("full", "Full video")]
    assert rows[0]["notify"] is True and rows[1]["notify"] is False  # the cut is the front door


def test_publish_without_topics_still_works(client, channel):
    job = _cut_job(with_shorts=False)
    res = client.post(f"/jobs/{job.job_id}/publish", json={"title": "Full video", "privacy": "private"})
    assert res.status_code == 200 and len(channel.videos) == 1


def test_the_page_has_the_topic_cut_blocks_and_uses_text_only(client):
    html = client.get("/app").text
    for marker in ('id="topicsGrid"', 'addPublishRow(`topic:${t.index}`', "body.topics.push", "Topic cut",
                   "youtube_topics", "goes public first", "Goes public first and tells subscribers"):
        assert marker in html, marker
    start = html.index("function loadTopicTitles")
    block = html[start:html.index("function clock(", start)]
    assert "innerHTML" not in block
