"""The posted list and the upload journal: nothing is ever posted twice, across
crashes at any step, restarts, retries and other jobs of the same meeting."""

import json
import threading
from contextlib import contextmanager

import pytest

from core.models import JobRecord, JobStatus
from ledger import files, uploads
from ledger.uploads import AlreadyPosted, InFlight, Ledger, NeedsCheck, get_ledger
from publish import once
from tests.fake_channel import CRASH_POINTS, Crash, FakeChannel, TransientError


def _job(job_id="j1", uuid="abc==") -> JobRecord:
    return JobRecord(job_id=job_id, status=JobStatus.DONE, title="Weekly Sync", meeting_date="2026-10-05",
                     zoom_meeting_uuid=uuid)


def _restart() -> Ledger:
    """A new process: the ledger is read back from its files."""
    uploads._registry.clear()
    return get_ledger()


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"x" * 1000)

    @contextmanager
    def opener():
        yield path

    return opener


def _post(item, opener, by="autopilot"):
    return once.upload_once(item, opener, title="A title", description="words", privacy="public", by=by)


# ------------------------------------------------------------------ the files


def test_jsonl_survives_a_line_cut_short_by_a_crash(tmp_path, monkeypatch):
    path = files.state_dir() / "x.jsonl"
    files.append_jsonl(path, {"at": "1", "n": 1})
    with path.open("ab") as fh:
        fh.write(b'{"id":"half","n"')  # the power went mid-write
    files.append_jsonl(path, {"at": "2", "n": 2})
    assert [r["n"] for r in files._parse_jsonl(path)] == [1, 2]


def test_a_record_missing_from_the_main_file_comes_back_from_the_backup():
    path = files.state_dir() / "x.jsonl"
    files.append_jsonl(path, {"at": "1", "n": 1})
    files.append_jsonl(path, {"at": "2", "n": 2})
    path.write_text(path.read_text().splitlines()[0] + "\n")  # the second line is lost
    assert [r["n"] for r in files.read_jsonl(path)] == [1, 2]
    assert len(path.read_text().splitlines()) == 2  # and is written back


def test_a_lost_main_file_is_restored_from_the_backup():
    path = files.state_dir() / "x.jsonl"
    files.append_jsonl(path, {"at": "1", "n": 1})
    path.unlink()
    assert [r["n"] for r in files.read_jsonl(path)] == [1]


def test_json_falls_back_to_the_backup_when_damaged():
    path = files.state_dir() / "x.json"
    files.write_json(path, {"a": 1})
    path.write_text("{not json")
    assert files.read_json(path, None) == {"a": 1}
    assert files.read_json(files.state_dir() / "missing.json", {"d": 0}) == {"d": 0}


def test_every_file_lives_outside_the_folders_that_get_cleaned():
    state = files.state_dir()
    for cleaned in ("jobs", "videos", "transcripts"):
        assert cleaned not in state.parts and cleaned not in files.backup_dir().parts


# ------------------------------------------------------------------ identity


def test_the_full_video_is_one_per_meeting_whatever_the_job():
    assert uploads.full_item(_job("a")).key == uploads.full_item(_job("b")).key == "zoom:abc==|full"
    upload_job = JobRecord(job_id="u1")
    assert uploads.full_item(upload_job).key == "job:u1|full"


def test_video_ids_are_read_from_every_kind_of_link():
    for url in ("https://youtu.be/dQw4w9WgXcQ", "https://youtube.com/shorts/dQw4w9WgXcQ",
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3", "dQw4w9WgXcQ"):
        assert uploads.video_id_from_url(url) == "dQw4w9WgXcQ"
    assert uploads.video_id_from_url("https://example.com/nothing") is None


# ------------------------------------------------------------------ the happy path


def test_a_posted_video_is_on_the_list_with_its_meeting_and_link(monkeypatch, video):
    channel = FakeChannel().install(monkeypatch)
    item = uploads.short_item(_job(), 2, 30.0, 70.0)
    posted = _post(item, video)
    assert posted.url == f"https://youtube.com/shorts/{posted.video_id}"
    line = files.read_jsonl(files.state_dir() / uploads.POSTED)[0]
    assert {k: line[k] for k in ("meeting", "meeting_date", "kind", "short", "title", "url", "by")} == {
        "meeting": "Weekly Sync", "meeting_date": "2026-10-05", "kind": "short", "short": 2,
        "title": "A title", "url": posted.url, "by": "autopilot"}
    assert channel.videos[posted.video_id]["tags"] == [uploads.marker_for(item.key)]
    # the clean list has exactly one line per video
    assert len(files.read_jsonl(files.state_dir() / uploads.POSTED)) == 1


def test_a_posted_video_is_refused_again_even_after_a_restart(monkeypatch, video):
    FakeChannel().install(monkeypatch)
    item = uploads.full_item(_job())
    _post(item, video)
    _restart()
    with pytest.raises(AlreadyPosted):
        _post(uploads.full_item(_job("another-job")), video)  # same meeting, another processing run


def test_a_meetings_shorts_come_from_one_run_only(monkeypatch, video):
    FakeChannel().install(monkeypatch)
    _post(uploads.short_item(_job("first"), 1, 0, 30), video)
    # the first run's other shorts still go up
    _post(uploads.short_item(_job("first"), 2, 100, 130), video)
    # a re-run of the meeting posts none, whatever clips it picked
    with pytest.raises(AlreadyPosted, match="another processing run"):
        _post(uploads.short_item(_job("second"), 3, 500, 530), video)
    # but another meeting's shorts are untouched
    _post(uploads.short_item(_job("other", uuid="zzz"), 1, 0, 30), video)


def test_a_failed_short_of_another_run_does_not_block(monkeypatch, video):
    channel = FakeChannel(crash_at="before_link", raise_with=TransientError).install(monkeypatch)
    with pytest.raises(TransientError):
        _post(uploads.short_item(_job("first"), 1, 0, 30), video)
    _post(uploads.short_item(_job("second"), 1, 0, 30), video)
    assert len(channel.videos) == 1


def test_the_meeting_state_drives_the_green_button(monkeypatch, video):
    FakeChannel().install(monkeypatch)
    ledger = get_ledger()
    assert ledger.meeting_state("zoom:abc==") is None
    _post(uploads.short_item(_job(), 1, 0, 30), video)
    assert ledger.meeting_state("zoom:abc==") == "partly"
    _post(uploads.full_item(_job()), video)
    assert ledger.meeting_state("zoom:abc==") == "posted"


# ------------------------------------------------------------------ crashes


@pytest.mark.parametrize("point", CRASH_POINTS)
@pytest.mark.parametrize("links_expire", [False, True])
def test_a_crash_at_any_step_never_makes_a_second_video(monkeypatch, video, point, links_expire):
    """Die at each step of an upload, restart (optionally YouTube forgets the
    upload links meanwhile), publish again: never two videos. When nobody can
    tell whether it went up, it waits for a person instead of guessing."""
    channel = FakeChannel(crash_at=point).install(monkeypatch)
    item = uploads.full_item(_job())
    with pytest.raises(Crash):
        _post(item, video)
    _restart()
    if links_expire:
        channel.forget_links()

    try:
        posted = _post(item, video)
    except NeedsCheck:
        # only when bytes were sent through a link YouTube forgot and no video exists
        assert point in ("after_link", "mid_upload") and links_expire
        assert channel.videos == {}
        assert get_ledger().get(item.key).state == "unsure"
        return
    assert len(channel.videos) == 1
    assert posted.video_id in channel.videos
    # and a third try is refused
    with pytest.raises(AlreadyPosted):
        _post(item, video)
    assert len(channel.videos) == 1


@pytest.mark.parametrize("point", CRASH_POINTS)
def test_a_crash_resumes_through_the_same_link(monkeypatch, video, point):
    channel = FakeChannel(crash_at=point).install(monkeypatch)
    item = uploads.short_item(_job(), 1, 0, 30)
    with pytest.raises(Crash):
        _post(item, video)
    _restart()
    _post(item, video)
    if point == "before_link":
        assert channel.calls == ["new_link", "new_link"]  # nothing was sent: a fresh upload is safe
    elif point == "after_last_byte":
        assert channel.calls[-1] == "status"  # YouTube had it all: just recorded
    else:
        assert channel.calls[1:] == ["status", f"resume@{500 if point == 'mid_upload' else 0}"]
    assert len(channel.sessions) == 1 or point == "before_link"


def test_a_failed_upload_in_the_same_process_resumes_too(monkeypatch, video):
    channel = FakeChannel(crash_at="mid_upload", raise_with=TransientError).install(monkeypatch)
    item = uploads.full_item(_job())
    with pytest.raises(TransientError):
        _post(item, video)
    assert get_ledger().get(item.key).state == "interrupted"
    _post(item, video)
    assert len(channel.videos) == 1 and len(channel.sessions) == 1


def test_when_youtube_cant_be_asked_the_link_is_kept_for_later(monkeypatch, video):
    channel = FakeChannel(crash_at="after_link").install(monkeypatch)
    item = uploads.full_item(_job())
    with pytest.raises(Crash):
        _post(item, video)
    _restart()
    channel.status_error = TransientError("network down")
    with pytest.raises(TransientError):
        _post(item, video)
    assert get_ledger().get(item.key).state == "interrupted"
    _post(item, video)
    assert len(channel.videos) == 1 and len(channel.sessions) == 1


def test_a_person_settles_an_unsure_upload(monkeypatch, video):
    channel = FakeChannel(crash_at="mid_upload").install(monkeypatch)
    item = uploads.full_item(_job())
    with pytest.raises(Crash):
        _post(item, video)
    _restart()
    channel.forget_links()
    with pytest.raises(NeedsCheck):
        _post(item, video)
    with pytest.raises(NeedsCheck):  # stays waiting, also after another restart
        _restart()
        _post(item, video)
    ledger = get_ledger()
    with pytest.raises(ValueError):
        ledger.resolve(item.key, on_youtube=True, url="not a link")
    # "not there": a new upload is allowed
    ledger.resolve(item.key, on_youtube=False)
    _post(item, video)
    assert len(channel.videos) == 1


def test_a_person_can_say_it_is_on_youtube(monkeypatch, video):
    channel = FakeChannel(crash_at="mid_upload").install(monkeypatch)
    item = uploads.short_item(_job(), 1, 0, 30)
    with pytest.raises(Crash):
        _post(item, video)
    _restart()
    channel.forget_links()
    with pytest.raises(NeedsCheck):
        _post(item, video)
    get_ledger().resolve(item.key, on_youtube=True, url="https://youtube.com/shorts/dQw4w9WgXcQ")
    _restart()
    assert get_ledger().get(item.key).url == "https://youtube.com/shorts/dQw4w9WgXcQ"
    assert get_ledger().meeting_state("zoom:abc==") == "partly"
    with pytest.raises(AlreadyPosted):
        _post(item, video)


def test_a_published_line_missing_from_the_list_is_written_back(monkeypatch, video):
    FakeChannel().install(monkeypatch)
    item = uploads.full_item(_job())
    _post(item, video)
    posted = files.state_dir() / uploads.POSTED
    posted.unlink()
    (files.backup_dir() / uploads.POSTED).unlink()
    _restart()
    lines = files.read_jsonl(posted)
    assert [line["key"] for line in lines] == [item.key]


def test_the_list_alone_is_enough_to_refuse(monkeypatch, video):
    """Even with the journal gone, a video on the list is never posted again."""
    FakeChannel().install(monkeypatch)
    item = uploads.full_item(_job())
    _post(item, video)
    for folder in (files.state_dir(), files.backup_dir()):
        (folder / uploads.JOURNAL).unlink()
    _restart()
    with pytest.raises(AlreadyPosted):
        _post(item, video)


def test_a_journal_write_that_fails_stops_the_upload_before_any_byte(monkeypatch, video):
    channel = FakeChannel().install(monkeypatch)
    item = uploads.full_item(_job())
    ledger = get_ledger()

    def broken(key, uri):
        raise OSError("disk full")

    monkeypatch.setattr(ledger, "session_started", broken)
    with pytest.raises(OSError):
        _post(item, video)
    # the link was made, but no byte went through it and no video exists
    assert channel.videos == {}
    assert all(s.received == 0 for s in channel.sessions.values())


# ------------------------------------------------------------------ racing


def test_two_threads_never_upload_the_same_video(monkeypatch, video):
    channel = FakeChannel().install(monkeypatch)
    started, release = threading.Event(), threading.Event()
    real = channel.upload_video

    def slow(*args, **kwargs):
        started.set()
        release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr("publish.youtube.upload_video", slow)
    item = uploads.full_item(_job())
    results = []

    def first():
        results.append(_post(item, video))

    thread = threading.Thread(target=first)
    thread.start()
    assert started.wait(5)
    with pytest.raises(InFlight):
        _post(uploads.full_item(_job("other-job")), video)
    release.set()
    thread.join(5)
    assert len(channel.videos) == 1 and len(results) == 1


def test_many_threads_racing_make_one_video(monkeypatch, video):
    channel = FakeChannel().install(monkeypatch)
    item = uploads.full_item(_job())
    barrier = threading.Barrier(8)
    outcomes = []

    def worker():
        barrier.wait()
        try:
            _post(item, video)
            outcomes.append("posted")
        except (InFlight, AlreadyPosted):
            outcomes.append("refused")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert outcomes.count("posted") == 1 and len(channel.videos) == 1


def test_the_journal_is_plain_readable_text(monkeypatch, video):
    FakeChannel().install(monkeypatch)
    _post(uploads.full_item(_job()), video)
    events = [json.loads(line)["event"]
              for line in (files.state_dir() / uploads.JOURNAL).read_text(encoding="utf-8").splitlines()]
    assert events == ["claim", "session", "published"]



# ------------------------------------------------------------------ review round 1


def test_another_job_never_finishes_a_cut_off_upload_with_its_own_file(monkeypatch, video):
    """The full video is one per meeting, but its upload link belongs to the
    job whose file it started with: a re-run of the meeting must not send
    its own file through that link."""
    channel = FakeChannel(crash_at="mid_upload").install(monkeypatch)
    with pytest.raises(Crash):
        _post(uploads.full_item(_job("first")), video)
    _restart()
    with pytest.raises(NeedsCheck, match="first"):
        _post(uploads.full_item(_job("second")), video)
    assert channel.calls == ["new_link"]  # nothing sent for the second job
    # the cut-off upload is in the list a person can settle
    [cut] = get_ledger().needing_check()
    assert cut.state == "interrupted" and cut.job_id == "first"
    # the first job finishes it through its link
    _post(uploads.full_item(_job("first")), video)
    assert len(channel.videos) == 1 and len(channel.sessions) == 1


def test_videos_published_before_the_list_existed_are_imported(monkeypatch, video):
    from core.models import YoutubeUpload
    from publish.run import import_published_jobs

    old = _job("old")
    old.youtube_status, old.youtube_url = "published", "https://youtu.be/dQw4w9WgXcQ"
    old.youtube_shorts = {1: YoutubeUpload(status="published", title="A short",
                                           url="https://youtube.com/shorts/aaaaaaaaaaa")}
    assert import_published_jobs([old, None]) == 2
    assert import_published_jobs([old]) == 0  # once
    FakeChannel().install(monkeypatch)
    with pytest.raises(AlreadyPosted):
        _post(uploads.full_item(_job("new")), video)
    with pytest.raises(AlreadyPosted):
        _post(uploads.short_item(_job("new"), 1, 0, 30), video)
    assert get_ledger().meeting_state("zoom:abc==") == "posted"


def test_a_claim_restored_from_the_backup_keeps_its_upload_link(monkeypatch, video):
    """Restoring a lost line keeps the written order: a claim and its upload
    link share their second, and a link replayed before its claim was lost."""
    channel = FakeChannel(crash_at="mid_upload").install(monkeypatch)
    item = uploads.full_item(_job())
    with pytest.raises(Crash):
        _post(item, video)
    journal = files.state_dir() / uploads.JOURNAL
    lines = journal.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["event"] == "claim"
    journal.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")  # the claim is lost
    _restart()
    assert get_ledger().get(item.key).session_uri  # still known: resumed, not sent again
    _post(item, video)
    assert len(channel.sessions) == 1 and len(channel.videos) == 1


def test_the_publish_panel_says_where_a_cut_off_upload_belongs(monkeypatch, video):
    from publish.run import blocked_text

    FakeChannel(crash_at="mid_upload").install(monkeypatch)
    with pytest.raises(Crash):
        _post(uploads.full_item(_job("first")), video)
    _restart()
    item = uploads.full_item(_job("second"))
    text = blocked_text(get_ledger().blocker(item), item, "The full video")
    assert "cut off mid-upload in job first" in text
