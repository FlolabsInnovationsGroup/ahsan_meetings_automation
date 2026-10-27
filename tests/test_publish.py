import json
import re
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from api.jobs import JobStore
from api.queue import reconcile_on_startup
from bundle.package import write_bundle
from core.config import get_settings
from core.models import (
    TERMINAL_STATUSES,
    ArtifactInfo,
    JobRecord,
    JobStatus,
    RenderManifest,
    RenderPiece,
    YoutubeUpload,
)
from ledger import uploads
from publish import delete_job_files, describe, media
from publish import run as publish_run
from publish import youtube as yt

FINAL = "Final_WeeklySync_2026-10-05_Youtube.mp4"
FPS = "25"
OFFSET = 60.0  # the cleaned meeting starts a minute into the final video

# source 0-40 s kept (plays 0-40 s into the meeting), 40-100 s cut, 100-200 s kept (plays 40-140 s)
PIECES = [RenderPiece(src_start_frame=0, src_end_frame=1000, out_start_frame=0),
          RenderPiece(src_start_frame=2500, src_end_frame=5000, out_start_frame=1000)]
SHORTS = [
    # (number, source start, source end, title, hook)
    (1, 110.0, 150.0, "Why we moved to Postgres", "Three reasons the old database had to go."),
    (2, 30.0, 70.0, "The rollout plan", "How the migration ships in two steps."),
    (3, 45.0, 95.0, "A cut-only aside", ""),  # entirely inside the stretch the cleanup removed
]


def _finished_job(job_id="pub-1", *, in_zip=False, with_shorts=True) -> JobRecord:
    """A finished job like deliver() leaves it: final video, shorts, manifest.json,
    render manifest, chapters, and the source + transcript outside the job folder.
    With `in_zip` the individual files are gone and only bundle.zip holds them
    (SERVE_INDIVIDUAL_ARTIFACTS=false)."""
    settings = get_settings()
    source = settings.videos_dir / f"{job_id}.mp4"
    source.write_bytes(b"source")
    vtt = settings.transcripts_dir / f"{job_id}.vtt"
    vtt.write_text("WEBVTT")
    job_dir = settings.jobs_dir / job_id
    (job_dir / "shorts").mkdir(parents=True, exist_ok=True)

    artifacts = {"final.mp4": ArtifactInfo(path=FINAL, mandatory=True)}
    (job_dir / FINAL).write_bytes(b"full-video")
    rows = []
    if with_shorts:
        for number, start, end, title, hook in SHORTS:
            name = f"shorts/short_{number:02d}.mp4"
            (job_dir / name).write_bytes(f"short-{number}".encode())
            artifacts[name] = ArtifactInfo(path=name)
            rows.append({"file": name, "source_start": start, "source_end": end, "title": title, "hook": hook})
    chapters = "00:00 Highlights\n02:00 The plan\n05:00 Questions\n"
    (job_dir / "chapters.txt").write_text(chapters, encoding="utf-8")
    artifacts["chapters.txt"] = ArtifactInfo(path="chapters.txt", kind="text")
    (job_dir / "chapters.json").write_text(json.dumps({"entries": [
        {"t": 0, "title": "Highlights"}, {"t": 120, "title": "The plan"}, {"t": 300, "title": "Questions"}]}))
    (job_dir / "manifest.json").write_text(json.dumps({"job_id": job_id, "shorts": rows}))
    render = RenderManifest(kind="cleaned", fps=FPS, width=1920, height=1080, pieces=PIECES)
    (job_dir / "render_manifest.json").write_text(render.model_dump_json())

    job = JobRecord(
        job_id=job_id,
        status=JobStatus.DONE,
        title="Weekly Sync",
        meeting_date="2026-10-05",
        source_path=str(source),
        native_transcript_path=str(vtt),
        output_video_path=str(job_dir / FINAL),
        render_manifest_path=str(job_dir / "render_manifest.json"),
        chapters_path=str(job_dir / "chapters.txt"),
        final_offset_s=OFFSET,
        artifacts=artifacts,
    )
    if in_zip:
        bundle = write_bundle(job_dir, artifacts, job_dir / "bundle.zip", extra=["manifest.json"])
        job.bundle_path = str(bundle)
        for name, info in artifacts.items():
            (job_dir / info.path).unlink()
            info.on_disk = False
        job.output_video_path = job.chapters_path = None
    api_main.job_store.create(job)
    return job


def _connect_fake_channel(title="FloLabs"):
    path = get_settings().storage_dir / "youtube_token.json"
    path.write_text(json.dumps({"refresh_token": "x", "channel_title": title}))


class FakeYoutube:
    """Stands in for yt.upload_video: records every upload and hands out ids.
    A failure listed in `fail` (by title) happens before YouTube made an
    upload link, so nothing of it is on the channel."""

    def __init__(self, monkeypatch, fail=None):
        self.uploads = []
        self.fail = fail or {}
        monkeypatch.setattr(yt, "upload_video", self)

    def __call__(self, path, title, description, privacy, on_progress=None, *, tags=(), session_uri=None,
                 start_byte=0, on_session=None):
        self.uploads.append({"bytes": Path(path).read_bytes(), "title": title, "description": description,
                             "privacy": privacy, "tags": list(tags)})
        if title in self.fail:
            raise self.fail[title]
        if on_session is not None:
            on_session(f"https://upload.example/session/{len(self.uploads)}")
        on_progress(50)
        return f"id{len(self.uploads)}"

    @property
    def order(self):
        return [u["bytes"] for u in self.uploads]


@pytest.fixture
def client():
    return TestClient(api_main.app)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "id")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://cutter.example.com/")
    get_settings.cache_clear()


def _reread(job_id) -> JobRecord:
    return JobStore().get(job_id)  # from disk, as after a restart


# ------------------------------------------------------- connecting the channel


def test_status_reports_not_configured_and_not_connected():
    assert yt.youtube_status() == {"configured": False, "connected": False, "channel_title": None}


def test_status_reports_connected_channel(configured, client):
    _connect_fake_channel("FloLabs Channel")
    assert client.get("/youtube/status").json() == {
        "configured": True, "connected": True, "channel_title": "FloLabs Channel"}


def test_return_to_only_allows_local_paths():
    assert yt._safe_return_to("/app?job=abc") == "/app?job=abc"
    assert yt._safe_return_to("//evil.com") == "/"
    assert yt._safe_return_to("https://evil.com") == "/"
    assert yt._safe_return_to(None) == "/"


def test_connect_requires_configuration(client):
    assert client.get("/youtube/connect", follow_redirects=False).status_code == 409


def test_connect_redirects_to_google_with_offline_access(configured, client):
    pytest.importorskip("google_auth_oauthlib")
    res = client.get("/youtube/connect?return_to=/app%3Fjob%3Dabc", follow_redirects=False)
    assert res.status_code == 302
    location = res.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/auth")
    assert "access_type=offline" in location
    assert "redirect_uri=https%3A%2F%2Fcutter.example.com%2Fyoutube%2Fcallback" in location
    assert "youtube.upload" in location


def test_the_permission_asked_for_stays_upload_and_read_only():
    assert yt.SCOPES == ["https://www.googleapis.com/auth/youtube.upload",
                         "https://www.googleapis.com/auth/youtube.readonly"]


def test_callback_rejects_unknown_state(configured, client):
    assert client.get("/youtube/callback?state=nope&code=x", follow_redirects=False).status_code == 400


def test_callback_sends_a_refused_sign_in_back_to_the_page_it_started_from(configured, client):
    pytest.importorskip("google_auth_oauthlib")
    location = client.get("/youtube/connect?return_to=/app%3Fjob%3Dabc", follow_redirects=False).headers["location"]
    state = re.search(r"state=([^&]+)", location).group(1)
    res = client.get(f"/youtube/callback?state={state}&error=access_denied", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/app?job=abc&youtube_error=access_denied"


def test_privacy_page_and_oauth_callback_are_reachable_without_the_api_key(monkeypatch, client):
    monkeypatch.setenv("HC_API_TOKEN", "s3cret")
    get_settings.cache_clear()
    page = client.get("/privacy")
    assert page.status_code == 200 and "Privacy Policy" in page.text
    # the callback answers (400: no such sign-in), not 401
    assert client.get("/youtube/callback?state=nope&code=x").status_code == 400
    assert client.get("/youtube/status").status_code == 401
    assert client.get("/jobs/whatever/publish").status_code == 401


# ------------------------------------------------------------ where a short plays


def _manifest():
    return RenderManifest(kind="cleaned", fps=FPS, width=1920, height=1080, pieces=PIECES)


def test_clip_position_when_the_clip_starts_in_a_kept_stretch():
    # source 110 s is 10 s into the second kept stretch, which plays at 40 s of the meeting
    assert describe.clip_position(_manifest(), 110.0, 150.0, OFFSET) == 110  # 60 + 40 + 10
    assert describe.clip_position(_manifest(), 30.0, 70.0, OFFSET) == 90  # 60 + 30


def test_clip_position_when_the_start_was_cut_uses_the_first_kept_moment():
    # starts in the cut stretch (40-100 s) and reaches the kept stretch at 100 s
    assert describe.clip_position(_manifest(), 90.0, 130.0, OFFSET) == 100  # 60 + 40 + 0


def test_clip_position_is_none_when_nothing_of_the_clip_was_kept():
    assert describe.clip_position(_manifest(), 45.0, 95.0, OFFSET) is None


def test_clip_position_ignores_a_sliver_at_a_cut():
    # only 0.4 s of the clip (39.6-40) is kept before the cut, and nothing after it
    assert describe.clip_position(_manifest(), 39.6, 60.0, OFFSET) is None


def test_clip_position_counts_whole_seconds_down():
    pieces = [RenderPiece(src_start_frame=0, src_end_frame=2500, out_start_frame=0)]
    manifest = RenderManifest(kind="cleaned", fps=FPS, width=1, height=1, pieces=pieces)
    assert describe.clip_position(manifest, 12.99, 40.0, 5.5) == 18  # 5.5 + 12.99 = 18.49


def test_load_shorts_knows_where_each_plays():
    job = _finished_job()
    shorts = describe.load_shorts(job)
    assert [(s.index, s.at_s) for s in shorts] == [(1, 110), (2, 90), (3, None)]
    assert shorts[0].title == "Why we moved to Postgres"


def test_load_shorts_skips_a_short_that_failed_to_render():
    job = _finished_job()
    job.artifacts["shorts/short_02.mp4"].status = "failed"
    assert [s.index for s in describe.load_shorts(job)] == [1, 3]


def test_load_shorts_works_without_a_render_manifest():
    job = _finished_job()
    job.render_manifest_path = None
    assert [s.at_s for s in describe.load_shorts(job)] == [None, None, None]


def test_a_job_without_shorts_has_none():
    assert describe.load_shorts(_finished_job(with_shorts=False)) == []


# ------------------------------------------------------------------- the words


def test_short_description_says_where_the_clip_plays():
    job = _finished_job()
    info = describe.load_shorts(job)[0]
    text = describe.short_description(info, job)
    assert text == ("Three reasons the old database had to go.\n\n"
                    "From the full meeting: Weekly Sync (2026-10-05)\n"
                    "This clip starts at 01:50 in the full video.")


def test_short_description_links_the_moment_when_the_full_video_is_online():
    job = _finished_job()
    info = describe.load_shorts(job)[0]
    text = describe.short_description(info, job, "https://youtu.be/FULL")
    assert text.endswith("This clip starts at 01:50 in the full video.\nhttps://youtu.be/FULL?t=110")


def test_short_description_for_a_clip_that_is_not_in_the_cleaned_meeting():
    job = _finished_job()
    info = describe.load_shorts(job)[2]
    assert describe.short_description(info, job) == "From the full meeting: Weekly Sync (2026-10-05)"


def test_short_description_does_not_repeat_a_date_the_title_has():
    job = _finished_job()
    job.title = "All Tech Team Meeting | August 2, 2026"
    info = describe.load_shorts(job)[1]
    assert "(2026-10-05)" not in describe.short_description(info, job)


def test_titles_and_descriptions_never_carry_angle_brackets():
    assert describe.one_line("a <b>bold</b>\n title") == "a b bold /b title"
    assert describe.title_problem("a <b>") and describe.title_problem("") and describe.title_problem("x" * 101)
    assert describe.title_problem("fine") is None
    assert describe.description_problem("1 < 2")
    assert describe.description_problem("é" * 2501)  # 5002 bytes
    assert describe.description_problem("é" * 2500) is None


def test_full_title_adds_the_day_only_when_the_name_has_none():
    job = _finished_job()
    assert describe.full_title(job) == "Weekly Sync — 2026-10-05"
    job.title = "All Tech Team Meeting | August 2, 2026"
    assert describe.full_title(job) == "All Tech Team Meeting | August 2, 2026"
    job.title = "x" * 150
    assert len(describe.full_title(job)) == 100


def test_full_description_is_the_chapters_even_after_the_files_are_gone():
    job = _finished_job(in_zip=True)
    assert job.chapters_path is None
    assert describe.full_description(job) == "00:00 Highlights\n02:00 The plan\n05:00 Questions"


def test_shorts_block_keeps_the_chapters_first_and_never_starts_a_line_with_a_time():
    shorts = [(1, "One", "https://youtube.com/shorts/a"), (2, "Two", "https://youtube.com/shorts/b")]
    text, listed = describe.with_shorts_block("00:00 Start\n01:00 Next\n02:00 End", shorts)
    assert listed == [1, 2]
    assert text == ("00:00 Start\n01:00 Next\n02:00 End\n\n"
                    "Shorts from this meeting:\n"
                    "• One — https://youtube.com/shorts/a\n"
                    "• Two — https://youtube.com/shorts/b")
    assert describe.with_shorts_block("", shorts)[0].startswith("Shorts from this meeting:")
    assert describe.with_shorts_block("chapters", []) == ("chapters", [])


def test_shorts_block_lists_as_many_shorts_as_fit_in_5000_bytes():
    shorts = [(i, "t" * 100, f"https://youtube.com/shorts/{i:011d}") for i in range(1, 11)]
    text, listed = describe.with_shorts_block("c" * 4400, shorts)
    assert len(text.encode("utf-8")) <= 5000
    assert 0 < len(listed) < 10 and listed == list(range(1, len(listed) + 1))
    assert describe.with_shorts_block("c" * 4990, shorts) == ("c" * 4990, [])


# ----------------------------------------------------------------- the options


def test_publish_options_suggests_the_words_and_says_what_can_be_uploaded(configured, client):
    _connect_fake_channel()
    job = _finished_job()
    body = client.get(f"/jobs/{job.job_id}/publish").json()
    assert body["youtube"]["connected"] and not body["busy"] and not body["files_deleted"]
    assert body["full"] == {"available": True, "title": "Weekly Sync — 2026-10-05",
                            "description": "00:00 Highlights\n02:00 The plan\n05:00 Questions",
                            "status": None, "progress": 0, "url": None, "error": None, "blocked": None}
    first = body["shorts"][0]
    assert (first["index"], first["available"], first["title"], first["at"], first["at_s"]) == (
        1, True, "Why we moved to Postgres", "01:50", 110)
    assert "01:50 in the full video" in first["description"]
    assert body["shorts"][2]["at"] is None
    assert body["shorts_block"] == ""


def test_publish_options_for_a_job_that_is_not_done_is_refused(client):
    api_main.job_store.create(JobRecord(job_id="busy", status=JobStatus.SLICING, source_path="x.mp4"))
    assert client.get("/jobs/busy/publish").status_code == 409


def test_publish_options_sees_files_that_are_only_in_the_zip(client):
    job = _finished_job(in_zip=True)
    body = client.get(f"/jobs/{job.job_id}/publish").json()
    assert body["full"]["available"] and all(s["available"] for s in body["shorts"])


# --------------------------------------------------------- refusing a request


def test_publish_requires_connected_channel(client):
    job = _finished_job()
    assert client.post(f"/jobs/{job.job_id}/publish", json={"title": "Weekly sync"}).status_code == 409


def test_publish_rejects_unfinished_job(client):
    _connect_fake_channel()
    api_main.job_store.create(JobRecord(job_id="busy", status=JobStatus.SLICING, source_path="x.mp4"))
    assert client.post("/jobs/busy/publish", json={"title": "x"}).status_code == 409


def test_publish_rejects_angle_brackets_in_a_title(client):
    _connect_fake_channel()
    job = _finished_job()
    assert client.post(f"/jobs/{job.job_id}/publish", json={"title": "a <b>"}).status_code == 400
    res = client.post(f"/jobs/{job.job_id}/publish", json={"shorts": [{"index": 1, "title": "a <b>"}]})
    assert res.status_code == 400 and "short 1" in res.json()["detail"]
    assert client.post(f"/jobs/{job.job_id}/publish",
                       json={"title": "ok", "description": "1 < 2"}).status_code == 400


def test_publish_with_nothing_chosen_is_refused(client):
    _connect_fake_channel()
    job = _finished_job()
    assert client.post(f"/jobs/{job.job_id}/publish", json={}).status_code == 400
    assert client.post(f"/jobs/{job.job_id}/publish", json={"shorts": []}).status_code == 400


def test_publish_an_unknown_short_is_refused(client):
    _connect_fake_channel()
    job = _finished_job()
    assert client.post(f"/jobs/{job.job_id}/publish", json={"shorts": [{"index": 9}]}).status_code == 404


def test_nothing_is_marked_when_a_request_is_refused(client):
    _connect_fake_channel()
    job = _finished_job()
    client.post(f"/jobs/{job.job_id}/publish", json={"title": "ok", "shorts": [{"index": 9}]})
    reread = _reread(job.job_id)
    assert reread.youtube_status is None and reread.youtube_shorts == {}


# ---------------------------------------------------------- the full video alone


def test_publish_uploads_the_full_video_then_removes_the_files(monkeypatch, client):
    _connect_fake_channel()
    job = _finished_job(with_shorts=False)
    fake = FakeYoutube(monkeypatch)
    res = client.post(f"/jobs/{job.job_id}/publish",
                      json={"title": "Weekly sync", "description": "notes", "privacy": "unlisted"})
    assert res.status_code == 200
    # the hidden tag finds the video on the channel if its upload link is ever lost
    marker = uploads.marker_for(f"job:{job.job_id}|full")
    assert fake.uploads == [{"bytes": b"full-video", "title": "Weekly sync", "description": "notes",
                             "privacy": "unlisted", "tags": [marker]}]

    reread = _reread(job.job_id)
    assert reread.youtube_status == "published"
    assert reread.youtube_url == "https://youtu.be/id1"
    assert reread.youtube_progress == 100
    assert reread.files_deleted is True
    assert reread.output_video_path is None and reread.bundle_path is None
    assert all(not info.on_disk for info in reread.artifacts.values())
    settings = get_settings()
    assert not list(settings.videos_dir.iterdir())
    assert not list(settings.transcripts_dir.iterdir())
    assert [p.name for p in (settings.jobs_dir / job.job_id).iterdir()] == ["job.json"]
    # what is gone answers "not available", not an error
    assert client.get(f"/jobs/{job.job_id}/video").status_code == 404
    assert client.get(f"/jobs/{job.job_id}/bundle").status_code == 404


def test_the_suggested_words_are_used_when_the_request_leaves_them_out(monkeypatch, client):
    _connect_fake_channel()
    job = _finished_job(with_shorts=False)
    fake = FakeYoutube(monkeypatch)
    client.post(f"/jobs/{job.job_id}/publish", json={"title": "Weekly sync"})
    assert fake.uploads[0]["description"] == "00:00 Highlights\n02:00 The plan\n05:00 Questions"
    assert fake.uploads[0]["privacy"] == "private"


def test_publish_can_keep_the_files(monkeypatch, client):
    _connect_fake_channel()
    job = _finished_job(with_shorts=False)
    FakeYoutube(monkeypatch)
    client.post(f"/jobs/{job.job_id}/publish", json={"title": "t", "delete_after": False})
    reread = _reread(job.job_id)
    assert reread.youtube_status == "published" and reread.files_deleted is False
    assert (get_settings().jobs_dir / job.job_id / FINAL).exists()


def test_a_failed_upload_keeps_the_files_and_says_why(monkeypatch, client):
    _connect_fake_channel()
    job = _finished_job(with_shorts=False)
    FakeYoutube(monkeypatch, fail={"t": RuntimeError("quotaExceeded")})
    client.post(f"/jobs/{job.job_id}/publish", json={"title": "t"})
    reread = _reread(job.job_id)
    assert reread.youtube_status == "failed" and "limit" in reread.youtube_error
    assert reread.files_deleted is False
    assert (get_settings().jobs_dir / job.job_id / FINAL).exists()


def test_a_published_full_video_is_not_uploaded_twice(monkeypatch, client):
    _connect_fake_channel()
    job = _finished_job(with_shorts=False)
    fake = FakeYoutube(monkeypatch)
    client.post(f"/jobs/{job.job_id}/publish", json={"title": "t", "delete_after": False})
    again = client.post(f"/jobs/{job.job_id}/publish", json={"title": "t", "delete_after": False})
    assert again.status_code == 409 and "https://youtu.be/id1" in again.json()["detail"]
    assert len(fake.uploads) == 1


def test_a_failed_full_video_can_be_tried_again(monkeypatch, client):
    _connect_fake_channel()
    job = _finished_job(with_shorts=False)
    fake = FakeYoutube(monkeypatch, fail={"t": RuntimeError("boom")})
    client.post(f"/jobs/{job.job_id}/publish", json={"title": "t", "delete_after": False})
    fake.fail = {}
    assert client.post(f"/jobs/{job.job_id}/publish", json={"title": "t", "delete_after": False}).status_code == 200
    reread = _reread(job.job_id)
    assert reread.youtube_status == "published" and reread.youtube_error is None


# --------------------------------------------------- shorts first, then the full video


def test_shorts_go_up_first_then_the_full_video_which_lists_them(monkeypatch, client):
    _connect_fake_channel()
    job = _finished_job()
    fake = FakeYoutube(monkeypatch)
    res = client.post(f"/jobs/{job.job_id}/publish", json={
        "title": "Weekly sync", "description": "00:00 Start\n01:00 Next\n02:00 End", "privacy": "unlisted",
        "delete_after": False,
        # sent out of order: they still go up by number
        "shorts": [{"index": 2}, {"index": 1, "title": "Postgres, finally"}],
    })
    assert res.status_code == 200
    assert fake.order == [b"short-1", b"short-2", b"full-video"]
    assert {u["privacy"] for u in fake.uploads} == {"unlisted"}

    first, second, full = fake.uploads
    assert first["title"] == "Postgres, finally"
    assert "This clip starts at 01:50 in the full video." in first["description"]
    assert second["title"] == "The rollout plan"
    assert "This clip starts at 01:30 in the full video." in second["description"]
    # the full video lists both shorts after its own chapters
    assert full["description"] == (
        "00:00 Start\n01:00 Next\n02:00 End\n\n"
        "Shorts from this meeting:\n"
        "• Postgres, finally — https://youtube.com/shorts/id1\n"
        "• The rollout plan — https://youtube.com/shorts/id2")

    reread = _reread(job.job_id)
    assert reread.youtube_url == "https://youtu.be/id3"
    assert {n: (s.status, s.url, s.title) for n, s in reread.youtube_shorts.items()} == {
        1: ("published", "https://youtube.com/shorts/id1", "Postgres, finally"),
        2: ("published", "https://youtube.com/shorts/id2", "The rollout plan")}
    assert reread.youtube_listed_shorts == [1, 2]
    assert not reread.youtube_busy and reread.files_deleted is False


def test_the_files_are_removed_only_when_everything_is_published(monkeypatch, client):
    _connect_fake_channel()
    job = _finished_job()
    FakeYoutube(monkeypatch)
    # the full video and one of three shorts: the rest still needs the files
    client.post(f"/jobs/{job.job_id}/publish", json={"title": "t", "shorts": [{"index": 1}]})
    assert _reread(job.job_id).files_deleted is False
    assert (get_settings().jobs_dir / job.job_id / "shorts" / "short_02.mp4").exists()
    # the others: now everything is on the channel
    client.post(f"/jobs/{job.job_id}/publish", json={"shorts": [{"index": 2}, {"index": 3}]})
    reread = _reread(job.job_id)
    assert reread.files_deleted is True
    assert [p.name for p in (get_settings().jobs_dir / job.job_id).iterdir()] == ["job.json"]


def test_after_the_files_are_removed_the_page_still_has_every_link(monkeypatch, client):
    _connect_fake_channel()
    job = _finished_job()
    FakeYoutube(monkeypatch)
    client.post(f"/jobs/{job.job_id}/publish",
                json={"title": "t", "shorts": [{"index": 1}, {"index": 2}, {"index": 3}]})
    assert _reread(job.job_id).files_deleted is True
    options = client.get(f"/jobs/{job.job_id}/publish").json()
    assert options["files_deleted"] is True
    assert [(s["index"], s["status"], s["available"], s["url"]) for s in options["shorts"]] == [
        (1, "published", False, "https://youtube.com/shorts/id1"),
        (2, "published", False, "https://youtube.com/shorts/id2"),
        (3, "published", False, "https://youtube.com/shorts/id3")]
    assert options["shorts"][0]["title"] == "Why we moved to Postgres"
    assert options["full"]["status"] == "published" and options["full"]["available"] is False
    assert options["shorts_block"] == ""  # all three were in the full video's description


def test_shorts_only_leaves_the_full_video_alone(monkeypatch, client):
    _connect_fake_channel()
    job = _finished_job()
    fake = FakeYoutube(monkeypatch)
    res = client.post(f"/jobs/{job.job_id}/publish", json={"shorts": [{"index": 1}]})
    assert res.status_code == 200
    assert fake.order == [b"short-1"]
    reread = _reread(job.job_id)
    assert reread.youtube_status is None and reread.youtube_shorts[1].status == "published"
    assert reread.files_deleted is False


def test_the_full_video_posted_later_lists_the_shorts_posted_earlier(monkeypatch, client):
    _connect_fake_channel()
    job = _finished_job()
    fake = FakeYoutube(monkeypatch)
    client.post(f"/jobs/{job.job_id}/publish", json={"shorts": [{"index": 1}]})
    client.post(f"/jobs/{job.job_id}/publish", json={"title": "t", "description": "chapters", "delete_after": False})
    assert fake.uploads[1]["description"] == (
        "chapters\n\nShorts from this meeting:\n• Why we moved to Postgres — https://youtube.com/shorts/id1")
    assert _reread(job.job_id).youtube_listed_shorts == [1]


def test_a_short_posted_after_the_full_video_links_to_its_moment_and_offers_the_list(monkeypatch, client):
    _connect_fake_channel()
    job = _finished_job()
    fake = FakeYoutube(monkeypatch)
    client.post(f"/jobs/{job.job_id}/publish", json={"title": "t", "delete_after": False})
    options = client.get(f"/jobs/{job.job_id}/publish").json()
    # the suggestion now links to the video on the channel
    assert options["shorts"][0]["description"].endswith("in the full video.\nhttps://youtu.be/id1?t=110")
    assert options["shorts_block"] == ""

    client.post(f"/jobs/{job.job_id}/publish", json={"shorts": [{"index": 1}], "delete_after": False})
    assert fake.uploads[1]["description"].endswith("https://youtu.be/id1?t=110")
    # the full video can't list it any more: the page offers the list to paste
    options = client.get(f"/jobs/{job.job_id}/publish").json()
    assert options["shorts_block"] == (
        "Shorts from this meeting:\n• Why we moved to Postgres — https://youtube.com/shorts/id2")


def test_a_published_short_is_not_uploaded_twice(monkeypatch, client):
    _connect_fake_channel()
    job = _finished_job()
    fake = FakeYoutube(monkeypatch)
    client.post(f"/jobs/{job.job_id}/publish", json={"shorts": [{"index": 1}]})
    again = client.post(f"/jobs/{job.job_id}/publish", json={"shorts": [{"index": 1}, {"index": 2}]})
    assert again.status_code == 409 and "Short 1" in again.json()["detail"]
    assert len(fake.uploads) == 1


# ----------------------------------------------------------------- when it goes wrong


def test_one_short_failing_does_not_stop_the_others_or_the_full_video(monkeypatch, client):
    _connect_fake_channel()
    job = _finished_job()
    fake = FakeYoutube(monkeypatch, fail={"The rollout plan": RuntimeError("Bad Request: invalidTitle")})
    client.post(f"/jobs/{job.job_id}/publish", json={
        "title": "t", "description": "", "shorts": [{"index": 1}, {"index": 2}, {"index": 3, "title": "Aside"}]})
    assert [u["title"] for u in fake.uploads] == ["Why we moved to Postgres", "The rollout plan", "Aside", "t"]
    reread = _reread(job.job_id)
    assert reread.youtube_shorts[2].status == "failed" and "invalidTitle" in reread.youtube_shorts[2].error
    assert reread.youtube_shorts[1].status == reread.youtube_shorts[3].status == "published"
    assert reread.youtube_status == "published" and reread.youtube_listed_shorts == [1, 3]
    # the one that failed still needs the files
    assert reread.files_deleted is False
    # ...and can be tried again on its own
    fake.fail = {}
    assert client.post(f"/jobs/{job.job_id}/publish", json={"shorts": [{"index": 2}]}).status_code == 200
    assert _reread(job.job_id).youtube_shorts[2].status == "published"


def test_a_problem_with_the_whole_account_stops_the_run(monkeypatch, client):
    _connect_fake_channel()
    job = _finished_job()
    fake = FakeYoutube(monkeypatch, fail={"Why we moved to Postgres": RuntimeError("quotaExceeded")})
    client.post(f"/jobs/{job.job_id}/publish", json={"title": "t", "shorts": [{"index": 1}, {"index": 2}]})
    assert len(fake.uploads) == 1  # nothing else was tried
    reread = _reread(job.job_id)
    assert reread.youtube_shorts[1].error == "YouTube upload limit reached for today. Try again tomorrow."
    assert reread.youtube_shorts[2].status == "failed" and reread.youtube_shorts[2].error.startswith("Not uploaded:")
    assert reread.youtube_status == "failed" and reread.youtube_error.startswith("Not uploaded:")
    assert not reread.youtube_busy and reread.files_deleted is False


def test_an_unexpected_crash_does_not_leave_uploads_waiting(monkeypatch, client):
    _connect_fake_channel()
    job = _finished_job()
    FakeYoutube(monkeypatch)

    def explode(*args, **kwargs):
        raise ValueError("kaboom")

    monkeypatch.setattr(publish_run, "everything_published", explode)
    client.post(f"/jobs/{job.job_id}/publish", json={"title": "t", "shorts": [{"index": 1}]})
    reread = _reread(job.job_id)
    assert not reread.youtube_busy
    assert reread.youtube_status == "published"  # the uploads that did finish stay published


def test_an_upload_waiting_blocks_a_second_one_and_deleting_the_job(client):
    _connect_fake_channel()
    job = _finished_job()
    job.youtube_shorts[1] = YoutubeUpload(status="uploading")
    api_main.job_store.update(job)
    assert client.post(f"/jobs/{job.job_id}/publish", json={"title": "t"}).status_code == 409
    assert client.delete(f"/jobs/{job.job_id}").status_code == 409
    assert client.delete(f"/jobs/{job.job_id}/files").status_code == 409


def test_restart_marks_interrupted_uploads_failed_but_keeps_the_job_done():
    store = JobStore()
    job = JobRecord(job_id="up", status=JobStatus.DONE, source_path="x.mp4", youtube_status="uploading",
                    youtube_shorts={1: YoutubeUpload(status="published", video_id="a", url="u"),
                                    2: YoutubeUpload(status="uploading"), 3: YoutubeUpload(status="queued")})
    store.create(job)
    assert reconcile_on_startup(JobStore())["interrupted"] == 1
    reread = JobStore().get("up")
    assert reread.status == JobStatus.DONE
    assert reread.youtube_status == "failed" and "restart" in reread.youtube_error
    assert [s.status for s in reread.youtube_shorts.values()] == ["published", "failed", "failed"]
    assert not reread.youtube_busy


def test_restart_clears_the_video_that_was_unpacked_for_the_cut_off_upload():
    job = _finished_job(in_zip=True)
    job.youtube_shorts[1] = YoutubeUpload(status="uploading")
    api_main.job_store.update(job)
    work = get_settings().jobs_dir / job.job_id / "publish_tmp"
    work.mkdir()
    (work / "short_01.mp4").write_bytes(b"half")
    reconcile_on_startup(JobStore())
    assert not work.exists()
    assert (get_settings().jobs_dir / job.job_id / "bundle.zip").exists()


def test_disconnect_is_refused_while_an_upload_runs(client):
    _connect_fake_channel()
    job = _finished_job()
    job.youtube_shorts[1] = YoutubeUpload(status="uploading")
    api_main.job_store.update(job)
    assert client.post("/youtube/disconnect").status_code == 409
    assert yt.youtube_status()["connected"] is True
    job.youtube_shorts[1].status = "failed"
    api_main.job_store.update(job)
    assert client.post("/youtube/disconnect").json()["connected"] is False


def test_a_short_whose_manifest_row_is_unreadable_still_keeps_the_files(monkeypatch, client):
    _connect_fake_channel()
    job = _finished_job()
    job_dir = get_settings().jobs_dir / job.job_id
    manifest = json.loads((job_dir / "manifest.json").read_text())
    del manifest["shorts"][2]["source_start"]  # short 3's row can't be read any more
    (job_dir / "manifest.json").write_text(json.dumps(manifest))
    FakeYoutube(monkeypatch)
    client.post(f"/jobs/{job.job_id}/publish", json={"title": "t", "shorts": [{"index": 1}, {"index": 2}]})
    reread = _reread(job.job_id)
    assert reread.youtube_status == "published" and reread.files_deleted is False
    assert (job_dir / "shorts" / "short_03.mp4").exists()  # it is on no list, but it was rendered


def test_restart_recovery_gives_the_job_a_fresh_retention_clock(monkeypatch):
    monkeypatch.setenv("JOB_RETENTION_HOURS", "1")
    get_settings.cache_clear()
    from datetime import UTC, datetime, timedelta
    old = datetime.now(UTC) - timedelta(hours=5)
    store = JobStore()
    store.create(JobRecord(job_id="up", status=JobStatus.DONE, source_path="x.mp4", youtube_status="uploading",
                           created_at=old))
    assert reconcile_on_startup(JobStore()) == {"interrupted": 1, "deleted": 0}
    assert JobStore().get("up") is not None


# --------------------------------------------------------- files that are in the zip


def test_everything_can_be_published_from_the_zip_alone(monkeypatch, client):
    _connect_fake_channel()
    job = _finished_job(in_zip=True)
    job_dir = get_settings().jobs_dir / job.job_id
    assert not (job_dir / FINAL).exists() and (job_dir / "bundle.zip").exists()
    fake = FakeYoutube(monkeypatch)
    res = client.post(f"/jobs/{job.job_id}/publish", json={
        "title": "t", "delete_after": False, "shorts": [{"index": 1}, {"index": 2}]})
    assert res.status_code == 200
    assert fake.order == [b"short-1", b"short-2", b"full-video"]
    assert not (job_dir / "publish_tmp").exists()  # the unpacked copies are gone again
    assert _reread(job.job_id).youtube_status == "published"


def test_unpacking_needs_free_disk_space(monkeypatch, client):
    _connect_fake_channel()
    job = _finished_job(in_zip=True)
    fake = FakeYoutube(monkeypatch)

    class Usage:
        free = 1024

    monkeypatch.setattr(media.shutil, "disk_usage", lambda path: Usage)
    client.post(f"/jobs/{job.job_id}/publish", json={"shorts": [{"index": 1}]})
    assert fake.uploads == []
    reread = _reread(job.job_id)
    assert reread.youtube_shorts[1].status == "failed" and "disk space" in reread.youtube_shorts[1].error


def test_unpacking_keeps_the_room_a_new_job_needs_to_start(monkeypatch):
    monkeypatch.setenv("MIN_FREE_DISK_BYTES", str(6 * 1024**3))
    get_settings.cache_clear()
    job = _finished_job(in_zip=True)

    class Usage:
        free = 5 * 1024**3  # plenty for the file, not for the floor a new job needs

    monkeypatch.setattr(media.shutil, "disk_usage", lambda path: Usage)
    with pytest.raises(media.MediaMissing, match="disk space"), media.open_media(job, "shorts/short_01.mp4"):
        pass


def test_open_media_clears_what_a_cut_off_run_left_behind():
    job = _finished_job(in_zip=True)
    work = get_settings().jobs_dir / job.job_id / "publish_tmp"
    work.mkdir()
    (work / "left-over.mp4").write_bytes(b"x")
    with media.open_media(job, "shorts/short_01.mp4"):
        assert [p.name for p in work.iterdir()] == ["short_01.mp4"]
    assert not work.exists()


def test_open_media_prefers_the_file_on_disk_and_cleans_up_what_it_unpacks():
    job = _finished_job(in_zip=True)
    with media.open_media(job, "shorts/short_01.mp4") as path:
        assert path.read_bytes() == b"short-1" and path.parent.name == "publish_tmp"
    assert not path.exists() and not path.parent.exists()
    with pytest.raises(media.MediaMissing), media.open_media(job, "shorts/short_09.mp4"):
        pass


def test_a_missing_file_is_refused_not_started(client):
    _connect_fake_channel()
    job = _finished_job()
    (get_settings().jobs_dir / job.job_id / "shorts" / "short_01.mp4").unlink()
    assert client.post(f"/jobs/{job.job_id}/publish", json={"shorts": [{"index": 1}]}).status_code == 410
    options = client.get(f"/jobs/{job.job_id}/publish").json()
    assert [s["available"] for s in options["shorts"]] == [False, True, True]


# --------------------------------------------------------------- removing the files


def test_delete_files_endpoint_keeps_the_record(client):
    job = _finished_job()
    res = client.delete(f"/jobs/{job.job_id}/files")
    assert res.status_code == 200 and res.json()["files_deleted"] is True
    assert client.get(f"/jobs/{job.job_id}").status_code == 200
    assert client.get(f"/jobs/{job.job_id}/video").status_code == 404
    assert client.get(f"/jobs/{job.job_id}/selection").status_code in (404, 409)
    # nothing left to publish
    _connect_fake_channel()
    assert client.post(f"/jobs/{job.job_id}/publish", json={"title": "t"}).status_code == 409


def test_delete_files_refuses_a_job_that_is_still_running(client):
    api_main.job_store.create(JobRecord(job_id="running", status=JobStatus.DECIDING, source_path="x.mp4"))
    assert client.delete("/jobs/running/files").status_code == 409


def test_a_keep_marker_protects_the_files_from_deleting(client):
    job = _finished_job()
    (get_settings().jobs_dir / job.job_id / ".keep").write_text("")
    assert client.delete(f"/jobs/{job.job_id}/files").status_code == 409
    assert delete_job_files(job) is False and job.files_deleted is False
    assert (get_settings().jobs_dir / job.job_id / FINAL).exists()


def test_delete_job_files_never_touches_paths_outside_storage(tmp_path):
    outside = tmp_path / "precious.mp4"
    outside.write_bytes(b"keep me")
    job = JobRecord(job_id="evil", status=JobStatus.DONE, source_path=str(outside))
    delete_job_files(job)
    assert outside.exists()


# ------------------------------------------------------------------ the deploy script


def test_the_deploy_script_waits_for_every_running_status():
    script = (Path(__file__).resolve().parent.parent / "deploy" / "auto-update.sh").read_text(encoding="utf-8")
    listed = re.search(r"finished = \{([^}]*)\}", script).group(1)
    assert {s.strip().strip('"') for s in listed.split(",")} == {s.value for s in TERMINAL_STATUSES}
    # ...and for an upload of the full video or of any short
    assert "youtube_shorts" in script and '"queued", "uploading"' in script


def test_the_container_healthcheck_asks_for_a_route_that_exists():
    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text(encoding="utf-8")
    path = re.search(r"urlopen\('http://127\.0\.0\.1:8000(/[a-z]*)'", dockerfile).group(1)
    assert path in {route.path for route in api_main.app.routes}


def test_the_job_files_zip_member_names_match_the_artifacts():
    job = _finished_job(in_zip=True)
    with zipfile.ZipFile(job.bundle_path) as zf:
        names = set(zf.namelist())
    assert {FINAL, "shorts/short_01.mp4", "manifest.json"} <= names
