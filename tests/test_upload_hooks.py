"""The upload path's hooks: follow-up queue, views record, Short style and
translations. None of them may change what is posted, or stop an upload."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

import api.main as api_main
from analytics import record
from ledger import uploads
from ledger.uploads import AlreadyPosted, get_ledger
from publish import extras, once, translate
from publish import run as publish_run
from publish import settings as pub_settings
from publish import youtube as yt
from tests.fake_channel import FakeChannel
from tests.test_publish import _connect_fake_channel, _finished_job, _reread

SHORTS = [(1, None, None), (2, None, None)]
_REAL_STARTER = publish_run._start_extras_worker  # conftest turns the worker off for every other test


@pytest.fixture
def channel(monkeypatch):
    _connect_fake_channel()
    return FakeChannel().install(monkeypatch)


@pytest.fixture
def hooks(monkeypatch):
    """Record what the upload path hands to the queue, the worker and the views database."""
    seen = {"enqueued": [], "recorded": [], "workers": [], "ran": threading.Event()}

    def enqueue(job, item, posted, *, video_kind, index=None, cfg=None):
        seen["enqueued"].append((item.key, posted.video_id, video_kind, index))

    def run_due(now=None):
        seen["workers"].append(threading.current_thread().name)
        seen["ran"].set()
        return 0

    def record_upload(item, upload, job=None):
        seen["recorded"].append((item, upload))

    monkeypatch.setattr(extras, "enqueue_for_upload", enqueue)
    monkeypatch.setattr(extras, "run_due", run_due)
    monkeypatch.setattr(publish_run, "_start_extras_worker", _REAL_STARTER)
    monkeypatch.setattr(record, "record_upload", record_upload)
    return seen


def _run(job, privacy="public", shorts=SHORTS):
    plan = publish_run.build_plan(job, title="Full video", shorts=shorts, privacy=privacy)
    publish_run.prepare(job, plan)
    return publish_run.publish_job(job, api_main.job_store.update, plan)


def _style_the_shorts(job, styles):
    path = Path(job.output_video_path).parent / "manifest.json"
    data = json.loads(path.read_text())
    for row, style in zip(data["shorts"], styles, strict=False):
        row["style"], row["layout"] = style, "fit"
    path.write_text(json.dumps(data))


# ------------------------------------------------------------------ queue, views, style


def test_a_scheduled_run_queues_follow_ups_keeps_the_style_and_records_the_views(channel, hooks):
    job = _finished_job()
    _style_the_shorts(job, ["new", "classic"])
    assert _run(job) is None
    assert len(channel.uploaded) == 3
    kinds = sorted((kind, index or 0) for _, _, kind, index in hooks["enqueued"])
    assert kinds == [("full", 0), ("short", 1), ("short", 2)]
    assert hooks["ran"].wait(5) and set(hooks["workers"]) == {"extras"}
    assert len(hooks["recorded"]) == 3
    lines = [json.loads(x) for x in get_ledger().posted_path.read_text().splitlines()]
    styles = {line["short"]: (line.get("style"), line.get("layout")) for line in lines if line["kind"] == "short"}
    assert styles == {1: ("new", "fit"), 2: ("classic", "fit")}
    assert {item.style for item, _ in hooks["recorded"]} == {None, "new", "classic"}


def test_the_real_follow_up_queue_takes_the_uploads(channel, monkeypatch):
    monkeypatch.setattr(extras, "run_due", lambda now=None: 0)
    monkeypatch.setattr(publish_run, "_start_extras_worker", _REAL_STARTER)
    job = _finished_job()
    assert _run(job) is None
    assert isinstance(extras.all_tasks(), list)


def test_a_failing_queue_or_views_record_leaves_the_uploads_published(channel, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("broken")

    monkeypatch.setattr(extras, "enqueue_for_upload", boom)
    monkeypatch.setattr(record, "record_upload", boom)
    job = _finished_job()
    assert _run(job) is None
    after = _reread(job.job_id)
    assert after.youtube_status == "published"
    assert {s.status for s in after.youtube_shorts.values()} == {"published"}
    assert len(channel.uploaded) == 3
    assert len(get_ledger().posted()) == 3


def test_an_old_posted_line_without_a_style_loads(channel):
    job = _finished_job()
    assert _run(job) is None
    lines = [json.loads(x) for x in get_ledger().posted_path.read_text().splitlines()]
    for line in lines:
        line.pop("style", None)
        line.pop("layout", None)
    upload = uploads._from_posted(lines[0])
    assert upload.style is None and upload.layout is None and upload.state == "published"


def test_a_different_style_is_still_already_posted(channel):
    job = _finished_job()
    _style_the_shorts(job, ["classic", "classic"])
    assert _run(job) is None
    info = {i.index: i for i in publish_run.describe.load_shorts(job)}[1]
    first = publish_run._short_item(job, info)
    again = publish_run._short_item(job, type(info)(**{**info.__dict__, "style": "new"}))
    assert first.key == again.key and first.style != again.style
    with pytest.raises(AlreadyPosted):
        get_ledger().begin(again, title="x", privacy="public", by="person")


# ------------------------------------------------------------------ translations


class Recorder:
    """Stands in for yt.upload_video, noting the translations each upload carried."""

    def __init__(self, monkeypatch, channel):
        self.channel, self.localizations = channel, []
        monkeypatch.setattr(yt, "upload_video", self)

    def __call__(self, *args, localizations=None, **kwargs):
        self.localizations.append(localizations)
        return self.channel.upload_video(*args, **kwargs)


TRANSLATED = {"es": {"title": "Hola", "description": "Descripcion"}}


def test_translations_off_changes_nothing(channel, monkeypatch):
    called = []
    monkeypatch.setattr(translate, "localizations", lambda *a, **k: called.append(1) or TRANSLATED)
    seen = Recorder(monkeypatch, channel)
    assert _run(_finished_job(), privacy="private") is None
    assert seen.localizations == [None, None, None] and not called


def test_translations_on_go_up_with_the_video(channel, monkeypatch):
    pub_settings.save({"translations": True})
    asked = []

    def fake(title, description, langs, **kwargs):
        asked.append((title, kwargs["cache_key"], kwargs["timeout_s"]))
        return TRANSLATED

    monkeypatch.setattr(translate, "localizations", fake)
    seen = Recorder(monkeypatch, channel)
    assert _run(_finished_job(), privacy="private") is None
    assert seen.localizations == [TRANSLATED] * 3
    assert len(asked) == 3 and all(timeout == 90 for _, _, timeout in asked)


def test_a_caller_error_uploads_without_translations(channel, monkeypatch):
    pub_settings.save({"translations": True})

    def broken(*args, **kwargs):
        raise RuntimeError("no model")

    monkeypatch.setattr(translate, "localizations", broken)
    seen = Recorder(monkeypatch, channel)
    assert _run(_finished_job(), privacy="private") is None
    assert seen.localizations == [None, None, None] and len(channel.uploaded) == 3


def test_a_timeout_uploads_without_translations(channel, monkeypatch):
    pub_settings.save({"translations": True})
    monkeypatch.setattr(translate, "localizations", lambda *a, **k: {})  # what a timeout gives
    seen = Recorder(monkeypatch, channel)
    assert _run(_finished_job(), privacy="private") is None
    assert seen.localizations == [None, None, None]


def test_a_resumed_upload_never_recomputes_translations(channel, monkeypatch):
    Recorder(monkeypatch, channel)
    job = _finished_job()
    item = uploads.full_item(job)
    calls = []
    work = Path(job.output_video_path)

    @contextmanager
    def opened():
        yield work

    def words():
        calls.append(1)
        return TRANSLATED

    channel.crash_at, channel.raise_with = "mid_upload", RuntimeError
    with pytest.raises(RuntimeError):
        once.upload_once(item, opened, title="Full video", description="d", privacy="private", by="person",
                         language="en", localizations=words)
    assert calls == [1]
    once.upload_once(item, opened, title="Full video", description="d", privacy="private", by="person",
                     language="en", localizations=words)
    assert calls == [1]  # the second attempt resumed the first one's link
    assert any(call.startswith("resume@") for call in channel.calls)


# ------------------------------------------------------------------ the request itself


def test_insert_body_carries_translations_only_with_a_language():
    assert "localizations" not in yt.insert_body("T", "D", "private")
    assert yt.insert_body("T", "D", "private", language="en", localizations={}) == yt.insert_body(
        "T", "D", "private", language="en")
    assert "localizations" not in yt.insert_body("T", "D", "private", localizations=TRANSLATED)
    body = yt.insert_body("T", "D", "private", language="en", localizations=TRANSLATED)
    assert body["localizations"] == TRANSLATED


class _Resp:
    def __init__(self, status):
        self.status = status
        self.reason = "Bad Request"


def _fake_service(monkeypatch, sent):
    from googleapiclient import discovery

    class Request:
        def __init__(self, body):
            self.body = body
            self.resumable_uri = None

        def next_chunk(self):
            return None, {"id": "vid1"}

    class Videos:
        def insert(self, part, body, media_body, notifySubscribers):
            sent.append((part, body))
            return Request(body)

    class Service:
        def videos(self):
            return Videos()

    monkeypatch.setattr(discovery, "build", lambda *a, **k: Service())
    monkeypatch.setattr(yt, "_load_credentials", lambda: object())
    monkeypatch.setattr(yt, "_count_upload_call", lambda: None)


def test_a_400_at_session_start_is_retried_once_without_translations(monkeypatch, tmp_path):
    from googleapiclient.errors import HttpError

    sent = []
    _fake_service(monkeypatch, sent)

    def start(request):
        if "localizations" in request.body:
            raise HttpError(_Resp(400), b"bad")
        return "https://upload.example/s"

    monkeypatch.setattr(yt, "_start_session", start)
    media = tmp_path / "v.mp4"
    media.write_bytes(b"x")
    links = []
    video = yt.upload_video(media, "T", "D", "private", language="en", localizations=TRANSLATED,
                            on_session=links.append)
    assert video == "vid1" and links == ["https://upload.example/s"]
    assert len(sent) == 2
    assert "localizations" in sent[0][1] and sent[0][0].endswith("localizations")
    assert "localizations" not in sent[1][1] and "localizations" not in sent[1][0]
    assert sent[1][1] == yt.insert_body("T", "D", "private", language="en")


def test_a_400_without_translations_is_not_retried(monkeypatch, tmp_path):
    from googleapiclient.errors import HttpError

    _fake_service(monkeypatch, [])
    attempts = []

    def start(request):
        attempts.append(1)
        raise HttpError(_Resp(400), b"bad")

    monkeypatch.setattr(yt, "_start_session", start)
    media = tmp_path / "v.mp4"
    media.write_bytes(b"x")
    with pytest.raises(HttpError):
        yt.upload_video(media, "T", "D", "private", language="en")
    assert attempts == [1]
