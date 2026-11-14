"""The never-post list stops a post at the upload step, on every route:
the Publish button, the autopilot's plan, the upload loop and upload_once."""

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from autopilot import never_post
from ledger import uploads
from ledger.uploads import get_ledger
from publish import once
from publish import run as publish_run
from tests.fake_channel import FakeChannel
from tests.test_publish import _connect_fake_channel, _finished_job, _reread


@pytest.fixture
def client():
    return TestClient(api_main.app)


def _retitle(job, title, zoom_meeting=None):
    job.title = title
    job.zoom_meeting = zoom_meeting
    api_main.job_store.update(job)
    return job


def test_manual_publish_of_a_listed_title_is_refused_and_nothing_goes_up(client):
    _connect_fake_channel()
    channel = FakeChannel()
    job = _retitle(_finished_job(), "Operations team meeting")
    with pytest.MonkeyPatch.context() as mp:
        channel.install(mp)
        res = client.post(f"/jobs/{job.job_id}/publish", json={"title": "Ops", "shorts": [{"index": 1}]})
    assert res.status_code == 403
    assert "can't be posted" in res.json()["detail"] and "never-post list" in res.json()["detail"]
    assert channel.videos == {} and channel.calls == []
    reread = _reread(job.job_id)
    assert reread.youtube_status is None and reread.youtube_shorts == {}


def test_a_listed_zoom_id_is_refused_even_with_a_harmless_title(client):
    _connect_fake_channel()
    job = _retitle(_finished_job(), "Robotics weekly sync", {"topic": "Robotics weekly sync", "meeting_id": 84116996599})
    res = client.post(f"/jobs/{job.job_id}/publish", json={"title": "x"})
    assert res.status_code == 403
    assert "84116996599" in res.json()["detail"]


def test_the_zoom_topic_counts_even_when_the_job_title_was_renamed():
    job = _retitle(_finished_job(), "Robotics weekly sync", {"topic": "HR check-in", "id": 1})
    assert never_post.match_job(job) is not None
    assert never_post.match_job(_retitle(job, "Robotics weekly sync", None)) is None


def test_a_harmless_meeting_still_uploads(monkeypatch, client):
    _connect_fake_channel()
    channel = FakeChannel().install(monkeypatch)
    job = _retitle(_finished_job(with_shorts=False), "Robotics weekly sync", {"meeting_id": "123456789"})
    res = client.post(f"/jobs/{job.job_id}/publish", json={"title": "Robotics weekly sync"})
    assert res.status_code == 200
    assert len(channel.videos) == 1
    assert _reread(job.job_id).youtube_status == "published"


def test_the_list_changing_after_the_plan_stops_every_upload(monkeypatch):
    _connect_fake_channel()
    channel = FakeChannel().install(monkeypatch)
    job = _retitle(_finished_job(), "Robotics weekly sync")
    plan = publish_run.build_plan(job, title="Full", shorts=[(1, None, None), (2, None, None)])
    publish_run.prepare(job, plan)
    never_post.set_entries(["robotics"])  # someone adds it while the job waits
    stopped = publish_run.publish_job(job, api_main.job_store.update, plan)
    assert channel.videos == {} and channel.calls == []
    assert stopped and "never-post list" in stopped
    reread = _reread(job.job_id)
    assert reread.youtube_status == "failed" and "Not uploaded:" in reread.youtube_error
    assert all(s.status == "failed" and "never-post list" in s.error for s in reread.youtube_shorts.values())
    assert not get_ledger().needing_check()


def test_the_autopilot_gets_no_plan_for_a_listed_job():
    _connect_fake_channel()
    job = _retitle(_finished_job(), "Project Management Team meeting")
    assert publish_run.plan_everything(job, "private", "autopilot") is None
    ok = _retitle(_finished_job("pub-2"), "Robotics weekly sync")
    assert publish_run.plan_everything(ok, "private", "autopilot") is not None


def test_publish_options_tell_the_page_about_a_listed_job(client):
    _connect_fake_channel()
    job = _retitle(_finished_job(), "Legal review")
    body = client.get(f"/jobs/{job.job_id}/publish").json()
    assert body["never_post"] and "never-post list" in body["never_post"]
    ok = _retitle(_finished_job("pub-2"), "Robotics weekly sync")
    assert client.get(f"/jobs/{ok.job_id}/publish").json()["never_post"] is None


def test_upload_once_refuses_a_listed_item_and_leaves_the_journal_clean(monkeypatch):
    channel = FakeChannel().install(monkeypatch)
    job = _retitle(_finished_job(with_shorts=False), "1:1 with Sam")
    item = uploads.full_item(job)
    opened = []
    with pytest.raises(once.NeverPost, match="Not uploaded"):
        once.upload_once(item, lambda: opened.append(1), title="t", description="d", privacy="private", by="person")
    assert opened == [] and channel.calls == []
    ledger = get_ledger()
    assert ledger.get(item.key) is None
    assert ledger.blocker(item) is None
    assert not ledger.needing_check()
