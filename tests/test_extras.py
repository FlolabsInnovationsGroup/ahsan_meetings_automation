"""The follow-up queue (publish/extras.py): saved tasks that run after an
upload, with fake builders and runners and no network."""

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from autopilot import never_post
from ledger import files
from ledger.uploads import Item, get_ledger
from publish import extras, optional, youtube
from publish import settings as publishing_settings


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(extras, "_builders", {})
    monkeypatch.setattr(extras, "_runners", {})
    monkeypatch.setattr(extras, "_loaded", True)  # no real feature modules
    monkeypatch.setattr(youtube, "youtube_status", lambda: {"connected": True})


def _posted(n: int = 1, kind: str = "full", meeting: str = "Space team meeting"):
    """A published video in the ledger; returns (item, upload)."""
    item = Item(f"job:{n}|{kind}", f"job:{n}", kind, f"job{n}", meeting, topic=meeting, meeting_id="111222333")
    ledger = get_ledger()
    ledger.begin(item, title="t", privacy="public", by="test")
    return item, ledger.published(item.key, f"video{n:06d}", by="test")


def _enqueue(item, upload, kind="thumbnail", **fields):
    def builder(job, it, posted, *, video_kind, index, cfg):
        return extras.Task(kind=kind, **fields)

    extras.register(kind, builder, extras._runners.get(kind) or (lambda task: optional.Outcome(optional.DONE)))
    extras.enqueue_for_upload(SimpleNamespace(id=item.job_id), item, upload, video_kind=item.kind)


def _only() -> extras.Task:
    [task] = extras.all_tasks()
    return task


def test_a_builder_that_raises_cannot_fail_the_enqueue():
    item, upload = _posted()

    def boom(*a, **k):
        raise RuntimeError("builder bug")

    extras.register("thumbnail", boom, lambda task: optional.Outcome(optional.DONE))
    extras.register("captions", lambda *a, **k: extras.Task(kind="captions"), lambda t: optional.Outcome(optional.DONE))
    extras.enqueue_for_upload(SimpleNamespace(id="job1"), item, upload, video_kind="full")  # returns normally
    assert [t.kind for t in extras.all_tasks()] == ["captions"]  # the others still got in


def test_the_task_is_filled_in_from_the_upload():
    item, upload = _posted()
    _enqueue(item, upload)
    task = _only()
    assert (task.item_key, task.video_id, task.job_id) == (item.key, upload.video_id, item.job_id)
    assert (task.meeting, task.topic, task.meeting_id) == ("Space team meeting", "Space team meeting", "111222333")
    assert task.state == "waiting" and task.video_kind == "full"


def test_the_same_follow_up_is_not_queued_twice():
    item, upload = _posted()
    _enqueue(item, upload)
    extras.enqueue_for_upload(SimpleNamespace(id="job1"), item, upload, video_kind="full")
    assert len(extras.all_tasks()) == 1


def test_a_due_task_runs_and_is_done():
    item, upload = _posted()
    seen = []
    extras.register("thumbnail", lambda *a, **k: extras.Task(kind="thumbnail"),
                    lambda t: seen.append(t.video_id) or optional.Outcome(optional.DONE))
    extras.enqueue_for_upload(SimpleNamespace(id="job1"), item, upload, video_kind="full")
    assert extras.run_due() == 1
    assert seen == [upload.video_id]
    assert _only().state == "done"


def test_a_meeting_added_to_the_never_post_list_after_upload_is_skipped():
    item, upload = _posted()
    ran = []
    extras.register("thumbnail", lambda *a, **k: extras.Task(kind="thumbnail"),
                    lambda t: ran.append(1) or optional.Outcome(optional.DONE))
    extras.enqueue_for_upload(SimpleNamespace(id="job1"), item, upload, video_kind="full")
    never_post.set_entries(["space team"])
    extras.run_due()
    assert ran == []
    task = _only()
    assert task.state == "skipped" and task.reason == "never-post list"


def test_a_zoom_id_on_the_never_post_list_skips_the_task():
    item, upload = _posted()
    _enqueue(item, upload)
    never_post.set_entries(["111 222 333"])
    extras.run_due()
    assert _only().state == "skipped"


def test_a_ledger_row_that_is_no_longer_published_is_skipped():
    item = Item("job:9|full", "job:9", "full", "job9", "Space team meeting")
    ledger = get_ledger()
    ledger.begin(item, title="t", privacy="public", by="test")
    ledger.stopped(item.key, "network")  # the upload did not finish: not published
    _enqueue(item, SimpleNamespace(video_id="video000009"))
    extras.run_due()
    assert _only().state == "skipped"
    assert extras.tasks_for_page()[0]["text"].startswith("Thumbnail skipped")


def test_a_task_for_another_video_id_is_skipped():
    item, upload = _posted()
    _enqueue(item, upload, video_id="someoneelse1")
    extras.run_due()
    assert _only().state == "skipped"


def test_needs_reconnect_stays_and_runs_after_wake(monkeypatch):
    item, upload = _posted()
    answers = [optional.Outcome(optional.NEEDS_RECONNECT, reason="Reconnect to turn on caption tracks"),
               optional.Outcome(optional.DONE)]
    calls = []
    extras.register("captions", lambda *a, **k: extras.Task(kind="captions"),
                    lambda t: calls.append(1) or answers.pop(0))
    extras.enqueue_for_upload(SimpleNamespace(id="job1"), item, upload, video_kind="full")
    extras.run_due()
    assert _only().state == "needs_reconnect"
    extras.run_due()  # still waiting for the reconnect: not called again
    assert len(calls) == 1
    # wake() makes it due and runs it in a thread; run the same step directly here
    monkeypatch.setattr(threading, "Thread", lambda target, **k: SimpleNamespace(start=target))
    extras.wake()
    assert _only().state == "done" and len(calls) == 2


def test_a_task_waiting_for_the_reconnect_outlives_the_retry_window(monkeypatch):
    item, upload = _posted()
    source = files.state_dir() / "extras"
    source.mkdir(parents=True, exist_ok=True)
    saved = source / "captions.srt"
    saved.write_bytes(b"1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    calls = []
    extras.register("captions", lambda *a, **k: extras.Task(kind="captions", file=str(saved)),
                    lambda t: calls.append(Path(t.file).exists()) or optional.Outcome(optional.DONE))
    extras.enqueue_for_upload(SimpleNamespace(id="job1"), item, upload, video_kind="full")
    task = _only()
    days = publishing_settings.current().extras_retry_days
    extras._update(task.id, state="needs_reconnect", reason="Reconnect to turn on caption tracks",
                   created_at=extras._iso(datetime.now(UTC) - timedelta(days=days + 1)))
    extras.run_due()  # the 5-minute sweep: must not give it up or delete its file
    assert _only().state == "needs_reconnect" and saved.exists()
    monkeypatch.setattr(threading, "Thread", lambda target, **k: SimpleNamespace(start=target))
    extras.wake()
    assert _only().state == "done" and calls == [True]


def test_stop_all_halts_the_rest_of_the_run():
    first, up1 = _posted(1)
    second, up2 = _posted(2)
    calls = []

    def runner(task):
        calls.append(task.item_key)
        return optional.Outcome(optional.STOP_ALL, reason="Reconnect the channel")

    extras.register("thumbnail", lambda job, it, posted, **k: extras.Task(kind="thumbnail"), runner)
    for item, upload in ((first, up1), (second, up2)):
        extras.enqueue_for_upload(SimpleNamespace(id=item.job_id), item, upload, video_kind="full")
    extras.run_due()
    assert len(calls) == 1
    assert [t.state for t in extras.all_tasks()] == ["waiting", "waiting"]


def test_the_run_stops_when_the_channel_is_not_connected(monkeypatch):
    item, upload = _posted()
    _enqueue(item, upload)
    monkeypatch.setattr(youtube, "youtube_status", lambda: {"connected": False})
    assert extras.run_due() == 0
    assert _only().state == "waiting"


def test_the_run_stops_when_the_quota_is_used_up():
    from publish import quota

    item, upload = _posted()
    _enqueue(item, upload)
    quota.mark_exhausted()
    assert extras.run_due() == 0
    assert _only().state == "waiting"


def test_full_videos_go_before_shorts():
    short, up_s = _posted(1, "short")
    full, up_f = _posted(2, "full")
    order = []
    extras.register("thumbnail", lambda job, it, posted, *, video_kind, **k: extras.Task(kind="thumbnail",
                                                                                         video_kind=video_kind),
                    lambda t: order.append(t.video_kind) or optional.Outcome(optional.DONE))
    extras.enqueue_for_upload(SimpleNamespace(id="a"), short, up_s, video_kind="short")
    extras.enqueue_for_upload(SimpleNamespace(id="b"), full, up_f, video_kind="full")
    extras.run_due()
    assert order == ["full", "short"]


def test_video_kind_comes_from_the_upload_when_the_builder_leaves_it_blank():
    item, upload = _posted(1, "short")
    extras.register("thumbnail", lambda job, it, posted, **k: extras.Task(kind="thumbnail"),
                    lambda t: optional.Outcome(optional.DONE))
    extras.enqueue_for_upload(SimpleNamespace(id=item.job_id), item, upload, video_kind="short")
    assert _only().video_kind == "short"


def test_stop_all_without_a_time_backs_off_instead_of_retrying_every_round():
    item, upload = _posted()
    calls = []

    def runner(task):
        calls.append(1)
        return optional.Outcome(optional.STOP_ALL, reason="Reconnect the channel")

    extras.register("thumbnail", lambda job, it, posted, **k: extras.Task(kind="thumbnail"), runner)
    extras.enqueue_for_upload(SimpleNamespace(id=item.job_id), item, upload, video_kind="full")
    extras.run_due()
    task = _only()
    assert task.attempts == 1 and task.state == "waiting"
    extras.run_due()  # not due again yet
    assert len(calls) == 1


def test_a_blocked_kind_is_left_alone():
    item, upload = _posted()
    _enqueue(item, upload)
    extras.block_kind("thumbnail", 6)
    assert extras.run_due() == 0
    assert _only().state == "waiting"


def test_retries_back_off_then_give_up_after_the_retry_window(monkeypatch):
    item, upload = _posted()
    when = datetime.now(UTC) + timedelta(hours=1)
    runs = []

    def runner(task):
        runs.append(task.attempts)
        return optional.Outcome(optional.RETRY, reason="YouTube is busy", retry_at=when)

    extras.register("thumbnail", lambda *a, **k: extras.Task(kind="thumbnail", file=None), runner)
    extras.enqueue_for_upload(SimpleNamespace(id="job1"), item, upload, video_kind="full")
    extras.run_due()
    task = _only()
    assert task.state == "waiting" and task.attempts == 1
    assert datetime.fromisoformat(task.not_before) > datetime.now(UTC)
    extras.run_due()  # not due yet
    assert runs == [0]
    # later, and past the window: the retry gives up instead of waiting again
    days = publishing_settings.current().extras_retry_days
    extras._update(task.id, not_before=extras._iso(datetime.now(UTC)),
                   created_at=extras._iso(datetime.now(UTC) - timedelta(days=days, minutes=1)))
    extras.run_due()
    assert _only().state == "given_up"
    assert runs == [0]  # given up before another call


def test_a_runner_that_raises_is_retried_not_lost():
    item, upload = _posted()

    def runner(task):
        raise RuntimeError("bug in the call")

    extras.register("thumbnail", lambda *a, **k: extras.Task(kind="thumbnail"), runner)
    extras.enqueue_for_upload(SimpleNamespace(id="job1"), item, upload, video_kind="full")
    extras.run_due()
    task = _only()
    assert task.state == "waiting" and task.attempts == 1


def test_a_skipped_task_can_be_retried():
    item, upload = _posted()
    extras.register("thumbnail", lambda *a, **k: extras.Task(kind="thumbnail"),
                    lambda t: optional.Outcome(optional.SKIPPED, reason="YouTube refused the picture"))
    extras.enqueue_for_upload(SimpleNamespace(id="job1"), item, upload, video_kind="full")
    extras.run_due()
    task = _only()
    assert task.state == "skipped"
    assert extras.retry(task.id) is True
    assert _only().state == "waiting"
    assert extras.retry("nope") is False


def test_two_concurrent_runs_process_each_task_once():
    item, upload = _posted()
    count = []
    gate = threading.Event()

    def runner(task):
        count.append(task.id)
        gate.wait(2)
        return optional.Outcome(optional.DONE)

    extras.register("thumbnail", lambda *a, **k: extras.Task(kind="thumbnail"), runner)
    extras.enqueue_for_upload(SimpleNamespace(id="job1"), item, upload, video_kind="full")
    results = []
    first = threading.Thread(target=lambda: results.append(extras.run_due()))
    first.start()
    deadline = time.time() + 2
    while not count and time.time() < deadline:
        time.sleep(0.01)
    assert extras.run_due() == 0  # a second call returns at once
    gate.set()
    first.join(3)
    assert results == [1] and len(count) == 1


def test_task_files_survive_deleting_the_job_folder_and_go_when_done(tmp_path):
    item, upload = _posted()
    job_dir = tmp_path / "jobs" / "job1"
    job_dir.mkdir(parents=True)
    source = job_dir / "thumb.jpg"
    source.write_bytes(b"jpg")

    def builder(job, it, posted, **k):
        task = extras.Task(kind="thumbnail")
        task.file = extras.stash_file(task.id, source, "jpg")
        return task

    extras.register("thumbnail", builder, lambda t: optional.Outcome(optional.DONE))
    extras.enqueue_for_upload(SimpleNamespace(id="job1"), item, upload, video_kind="full")
    source.unlink()
    job_dir.rmdir()
    task = _only()
    assert task.file and task.file.startswith(str(files.state_dir() / "extras"))
    assert Path(task.file).read_bytes() == b"jpg"
    extras.run_due()
    assert not Path(task.file).exists()  # done: the file is removed


def test_old_finished_records_are_pruned():
    item, upload = _posted()
    _enqueue(item, upload)
    task = _only()
    extras._update(task.id, state="done", created_at=extras._iso(datetime.now(UTC) - timedelta(days=61)))
    extras.run_due()
    assert extras.all_tasks() == []


def test_the_page_lines_are_plain_english():
    item, upload = _posted()
    _enqueue(item, upload)
    task = _only()
    [row] = extras.tasks_for_page()
    assert row["text"] == "Thumbnail waiting"
    extras._update(task.id, state="done")
    assert extras.tasks_for_page()[0]["text"] == "Thumbnail set"
    extras._update(task.id, state="needs_reconnect")
    assert extras.tasks_for_page()[0]["text"] == "Thumbnail waiting for the reconnect"
    extras._update(task.id, state="skipped", reason="never-post list")
    assert extras.tasks_for_page()[0]["text"] == "Thumbnail skipped: never-post list"


def test_the_api_lists_tasks_and_retry_resets_them():
    item, upload = _posted()
    extras.register("thumbnail", lambda *a, **k: extras.Task(kind="thumbnail"),
                    lambda t: optional.Outcome(optional.SKIPPED, reason="refused"))
    extras.enqueue_for_upload(SimpleNamespace(id="job1"), item, upload, video_kind="full")
    extras.run_due()
    client = TestClient(api_main.app)
    listed = client.get("/publishing/extras").json()["videos"]
    [task] = listed[0]["tasks"]
    assert task["state"] == "skipped" and task["can_retry"] is True
    assert client.post(f"/publishing/extras/{task['id']}/retry").status_code == 200
    assert _only().state == "waiting"
    assert client.post("/publishing/extras/unknown/retry").status_code == 404
