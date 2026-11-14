"""Topic cuts inside the job: when deliver._topic_cuts runs, what its manifest
rows hold, that a failed render never fails the job, and render_topic_cut's
frame-exact build (cold open + card + topic + outro). Synthetic lavfi sources
only."""

import json
import shutil
import subprocess
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config import get_settings
from core.models import (
    Chapter,
    EditDecisionList,
    EDLRange,
    JobRecord,
    JobStatus,
    Moment,
    RenderManifest,
    RenderPiece,
    Word,
)
from core.timeline import rate_str

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
FPS = Fraction(30)


def _make_source(path, seconds=30):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
        check=True)


def _edl(ranges, total, fade=0):
    return EditDecisionList(
        ranges=[EDLRange(start=float(Fraction(a) / FPS), end=float(Fraction(b) / FPS), start_frame=a, end_frame=b)
                for a, b in ranges],
        source_duration=float(Fraction(total) / FPS), fps=rate_str(FPS), fade_frames=fade, total_frames=total)


def _settings(monkeypatch, **changes):
    from publish import settings as ps

    cfg = replace(ps.current(), **changes)
    monkeypatch.setattr(ps, "current", lambda: cfg)


def _moment():
    return Moment(id=0, start=2.0, end=20.0, first_index=1, last_index=1, score=9, raw_score=9.0,
                  category="insight", title="Why the sensors drift", hook="Watch the drift", screen_hook="DRIFT",
                  stands_alone=8.0)


def _cut(index=0, start_frame=60, end_frame=700, cleaned=(10.0, 30.0), open_span=(2.0, 5.0)):
    return SimpleNamespace(
        index=index, cleaned_start_s=cleaned[0], cleaned_end_s=cleaned[1], src_start_frame=start_frame,
        src_end_frame=end_frame, chapter_titles=["Drift"], best=_moment(), score_tuple=(9.0, 8.0, 0.5, -1, -20.0),
        open_span=open_span, title="Why the sensors drift", hook="Watch the drift", thumb_src_s=3.0)


# ------------------------------------------------------------ _topic_cuts (mocked)


def _fixture(tmp_path, monkeypatch, *, tier="public", deploying=False, **settings):
    """A job folder, a minimal DeliverInputs / RenderOutputs and the patches that
    stand in for the team list, the deploy flag and the picker."""
    import deliver
    import outputs

    _settings(monkeypatch, topic_cuts=True, topic_cuts_per_meeting=1, **settings)
    monkeypatch.setattr("publish.series.tier_for_job", lambda job: tier)
    monkeypatch.setattr("autopilot.engine.deploying", lambda: deploying)
    monkeypatch.setattr("publish.describe.series_label", lambda job: "FloLabs Sensors")
    picked = []
    monkeypatch.setattr("decide.topics.pick_cuts", lambda *a, **k: picked.append(1) or [_cut()])
    job_dir = get_settings().jobs_dir / "topic-job"
    job_dir.mkdir(parents=True, exist_ok=True)
    words = [Word(word="Sensors", start=2.0, end=3.0, speaker="A"), Word(word="drift.", start=3.0, end=4.0, speaker="A"),
             Word(word="outside", start=29.0, end=29.5, speaker="A")]
    inp = deliver.DeliverInputs(render=SimpleNamespace(job_dir=job_dir, words=words), moments=[_moment()], segments=[])
    manifest = RenderManifest(kind="full", fps="30", width=640, height=360,
                              pieces=[RenderPiece(src_start_frame=0, src_end_frame=900, out_start_frame=0)])
    out = SimpleNamespace(cleaned=manifest, loudness_gain_db=2.5, full_hd=True)
    job = JobRecord(job_id="topic-job", status=JobStatus.REPORTING, title="Sensors meeting")
    calls = []

    def fake_render(rinp, cut, title, subtitle, out_path, work_dir, font, bold_font, gain_db=None, hd=False,
                    on_progress=None):
        calls.append({"title": title, "subtitle": subtitle, "gain_db": gain_db, "hd": hd})
        Path(out_path).write_bytes(b"x")
        return outputs.TopicRender(path=Path(out_path), frames=900, duration_s=30.0, open_s=3.0, card_s=3.0,
                                   body_s=21.0, outro_s=3.0, notes=["outro skipped: none"])

    monkeypatch.setattr("outputs.render_topic_cut", fake_render)
    return deliver, job, inp, out, calls, picked


CHAPTERS = [Chapter(start=0.0, title="Intro"), Chapter(start=12.0, title="Drift"), Chapter(start=25.0, title="Wrap")]


def test_a_public_team_gets_a_cut_with_the_full_videos_look(tmp_path, monkeypatch):
    deliver, job, inp, out, calls, _ = _fixture(tmp_path, monkeypatch)
    rows = deliver._topic_cuts(job, inp, out, CHAPTERS, [])
    assert len(rows) == 1
    assert calls == [{"title": "Why the sensors drift", "subtitle": "FloLabs Sensors", "gain_db": 2.5, "hd": True}]
    assert job.artifacts["topic/topic_01.mp4"].status == "ok"
    assert job.artifacts["topic/topic_01.mp4"].duration_s == 30.0
    assert "render_topic" in job.stage_timings
    assert any("outro skipped" in w for w in job.warnings) and job.status == JobStatus.REPORTING


def test_the_card_never_shows_a_never_post_word_or_a_name(tmp_path, monkeypatch):
    from autopilot import never_post

    never_post.set_entries(["acquisition"])
    deliver, job, inp, out, calls, _ = _fixture(tmp_path, monkeypatch)
    cut = _cut()
    cut.title = "Acquisition talks change the sensors drift plan"
    monkeypatch.setattr("decide.topics.pick_cuts", lambda *a, **k: [cut])
    (row,) = deliver._topic_cuts(job, inp, out, CHAPTERS, [])
    # the moment title holds a listed word: the card falls back to the first chapter title
    assert calls[-1]["title"] == "Drift" and row["card_title"] == "Drift"
    assert row["title"] == cut.title  # the row keeps the re-rank title; the publish checks look at it separately
    cut.chapter_titles = ["Acquisition notes"]
    deliver._topic_cuts(job, inp, out, CHAPTERS, [])
    assert calls[-1]["title"] == ""  # nothing fits: a card with only the team name
    cut.title = "Sensors drift"
    cut.chapter_titles = ["Drift"]
    deliver._topic_cuts(job, inp, out, CHAPTERS, [])
    assert calls[-1]["title"] == "Sensors drift"


def test_the_card_does_not_name_a_speaker_or_say_what_the_cut_did_not(tmp_path, monkeypatch):
    deliver, job, inp, out, calls, _ = _fixture(tmp_path, monkeypatch)
    (get_settings().jobs_dir / "topic-job" / "segments.json").write_text(
        json.dumps([{"start": 2.0, "end": 4.0, "speaker": "Rahul Mehta", "text": "Sensors drift."}]), encoding="utf-8")
    cut = _cut()
    cut.title = "Rahul explains the sensors drift"
    monkeypatch.setattr("decide.topics.pick_cuts", lambda *a, **k: [cut])
    deliver._topic_cuts(job, inp, out, CHAPTERS, [])
    assert calls[-1]["title"] == "Drift"  # names a speaker: the chapter title instead
    cut.title = "Budget approval for the warehouse fleet"  # none of it was said in the cut
    cut.chapter_titles = []
    deliver._topic_cuts(job, inp, out, CHAPTERS, [])
    assert calls[-1]["title"] == ""


def test_the_manifest_row_has_the_agreed_shape(tmp_path, monkeypatch):
    deliver, job, inp, out, _, _ = _fixture(tmp_path, monkeypatch)
    (row,) = deliver._topic_cuts(job, inp, out, CHAPTERS, [])
    assert set(row) == {"file", "source_start", "source_end", "cleaned_start", "cleaned_end", "duration_s", "open_s",
                        "card_s", "title", "card_title", "hook", "screen_hook", "chapters", "best_score", "stands_alone",
                        "category", "thumb_src_s", "text"}
    assert row["file"] == "topic/topic_01.mp4"
    assert (row["source_start"], row["source_end"]) == (2.0, pytest.approx(700 / 30, abs=1e-3))
    assert (row["cleaned_start"], row["cleaned_end"]) == (10.0, 30.0)
    assert (row["duration_s"], row["open_s"], row["card_s"]) == (30.0, 3.0, 3.0)
    assert (row["title"], row["hook"], row["screen_hook"]) == ("Why the sensors drift", "Watch the drift", "DRIFT")
    # only the chapters inside the cut, with times from the start of the topic
    assert row["chapters"] == [{"t": 2.0, "title": "Drift"}, {"t": 15.0, "title": "Wrap"}]
    assert (row["best_score"], row["stands_alone"], row["category"]) == (9.0, 8.0, "insight")
    assert row["thumb_src_s"] == 3.0
    assert row["text"] == "Sensors drift."  # the cut's own words, not the one after its end
    json.dumps(row)


def test_text_is_capped_at_2000_characters(tmp_path, monkeypatch):
    deliver, job, inp, out, _, _ = _fixture(tmp_path, monkeypatch)
    inp.render.words = [Word(word="word" * 20, start=3.0 + i * 0.01, end=3.005 + i * 0.01, speaker="A")
                        for i in range(200)]
    (row,) = deliver._topic_cuts(job, inp, out, CHAPTERS, [])
    assert len(row["text"]) == 2000


@pytest.mark.parametrize("tier", ["archive", "unknown", "never"])
def test_skipped_for_archive_unknown_and_never_teams(tmp_path, monkeypatch, tier):
    deliver, job, inp, out, calls, picked = _fixture(tmp_path, monkeypatch, tier=tier)
    assert deliver._topic_cuts(job, inp, out, CHAPTERS, []) == []
    assert not calls and not picked and not job.artifacts and not job.warnings


@pytest.mark.parametrize("changes", [{"topic_cuts": False}, {"topic_cuts_per_meeting": 0}])
def test_skipped_when_the_setting_is_off(tmp_path, monkeypatch, changes):
    deliver, job, inp, out, calls, picked = _fixture(tmp_path, monkeypatch)
    _settings(monkeypatch, **changes)
    assert deliver._topic_cuts(job, inp, out, CHAPTERS, []) == []
    assert not calls and not picked


def test_skipped_without_moments(tmp_path, monkeypatch):
    deliver, job, inp, out, calls, picked = _fixture(tmp_path, monkeypatch)
    inp.moments = []
    assert deliver._topic_cuts(job, inp, out, CHAPTERS, []) == []
    assert not calls and not picked


def test_a_waiting_deploy_skips_the_cut_with_a_warning(tmp_path, monkeypatch):
    deliver, job, inp, out, calls, _ = _fixture(tmp_path, monkeypatch, deploying=True)
    assert deliver._topic_cuts(job, inp, out, CHAPTERS, []) == []
    assert not calls
    assert job.artifacts["topic/topic_01.mp4"].status == "skipped"
    assert any("topic cut 1 skipped" in w and "update" in w for w in job.warnings)


def test_low_disk_skips_the_cut_with_a_warning(tmp_path, monkeypatch):
    deliver, job, inp, out, calls, _ = _fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("MIN_FREE_DISK_BYTES", str(3 * 1024**3))
    get_settings.cache_clear()
    cut_s = 20.0
    need = int(cut_s * 2.5e6 / 8 * 1.3) + 3 * 1024**3
    monkeypatch.setattr("deliver.shutil.disk_usage", lambda p: SimpleNamespace(total=need * 9, used=0, free=need - 1))
    assert deliver._topic_cuts(job, inp, out, CHAPTERS, []) == []
    assert not calls and job.artifacts["topic/topic_01.mp4"].status == "skipped"
    assert any("topic cut 1 skipped" in w and "GiB" in w for w in job.warnings)
    # one byte more and it renders
    monkeypatch.setattr("deliver.shutil.disk_usage", lambda p: SimpleNamespace(total=need * 9, used=0, free=need))
    assert len(deliver._topic_cuts(job, inp, out, CHAPTERS, [])) == 1


def test_a_render_error_is_a_failed_artifact_not_a_failed_job(tmp_path, monkeypatch):
    deliver, job, inp, out, _, _ = _fixture(tmp_path, monkeypatch)

    def boom(*a, **k):
        (get_settings().jobs_dir / "topic-job" / "topic" / "topic_01.mp4").write_bytes(b"half")
        raise RuntimeError("ffmpeg exploded\nsecond line")

    monkeypatch.setattr("outputs.render_topic_cut", boom)
    assert deliver._topic_cuts(job, inp, out, CHAPTERS, []) == []
    art = job.artifacts["topic/topic_01.mp4"]
    assert art.status == "failed" and art.reason == "ffmpeg exploded"
    assert any("topic cut 1 failed" in w for w in job.warnings)
    assert not (inp.render.job_dir / "topic" / "topic_01.mp4").exists()  # no half-written file left


def test_the_picker_failing_never_fails_the_job(tmp_path, monkeypatch):
    deliver, job, inp, out, _, _ = _fixture(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise ValueError("no pieces")

    monkeypatch.setattr("decide.topics.pick_cuts", boom)
    assert deliver._topic_cuts(job, inp, out, CHAPTERS, []) == []
    assert any("topic cuts skipped" in w for w in job.warnings)


def test_the_report_order_puts_topic_after_shorts():
    import deliver

    names = ["report.pdf", "topic/topic_01.mp4", "shorts/short_01.mp4", "final.mp4"]
    from core.models import ArtifactInfo

    ordered = deliver._ordered({n: ArtifactInfo(path=n) for n in names})
    assert list(ordered) == ["final.mp4", "shorts/short_01.mp4", "topic/topic_01.mp4", "report.pdf"]


# ------------------------------------------------------- deliver() end to end


def _deliver(tmp_path, monkeypatch, render=None):
    import deliver
    from outputs import RenderInputs
    from slice.profile import probe_media

    _settings(monkeypatch, topic_cuts=True, topic_cuts_per_meeting=1, thumbnail_mode="test", thumbnail_faces=True)
    monkeypatch.setattr("publish.series.tier_for_job", lambda job: "public")
    monkeypatch.setattr("autopilot.engine.deploying", lambda: False)
    monkeypatch.setattr("decide.topics.pick_cuts", lambda *a, **k: [_cut(start_frame=60, end_frame=540,
                                                                          cleaned=(2.0, 18.0))])
    if render is not None:
        monkeypatch.setattr("outputs.render_topic_cut", render)
    monkeypatch.setenv("SERVE_INDIVIDUAL_ARTIFACTS", "true")
    get_settings.cache_clear()
    src = tmp_path / "meeting.mp4"
    _make_source(src, seconds=20)
    job = JobRecord(job_id="topic-e2e", status=JobStatus.SLICING, title="Weekly sync")
    job_dir = get_settings().jobs_dir / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    words = [Word(word="Opening remarks.", start=0.5, end=2.0, speaker="A"),
             Word(word="The demo works well.", start=6.0, end=8.0, speaker="B")]
    edl = _edl([(0, 600)], 600)
    rinp = RenderInputs(job_dir=job_dir, source=src, media=probe_media(src), edl=edl, removed=[], reel_edl=None,
                        shorts=[], words=words, title="Weekly sync")
    moments = [Moment(id=0, start=5.0, end=15.0, first_index=1, last_index=1, score=9, title="The demo")]
    deliver.deliver(job, lambda j: None, deliver.DeliverInputs(render=rinp, reel=moments, moments=moments))
    return job, job_dir


@needs_ffmpeg
def test_the_job_renders_the_cut_lists_it_and_makes_its_thumbnail(tmp_path, monkeypatch):
    job, job_dir = _deliver(tmp_path, monkeypatch)
    assert job.status == JobStatus.DONE, job.error
    art = job.artifacts["topic/topic_01.mp4"]
    assert art.status == "ok" and art.bytes and art.duration_s
    assert (job_dir / "topic" / "topic_01.mp4").is_file()
    assert not (job_dir / "tmp" / "topic").exists()
    manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
    (row,) = manifest["topics"]
    assert row["file"] == "topic/topic_01.mp4" and row["title"] == "Why the sensors drift"
    assert "topic/topic_01.mp4" in manifest["artifacts"]
    assert job.stage_timings["render_topic"] > 0
    thumbs = json.loads((job_dir / "thumbnails" / "thumbs.json").read_text(encoding="utf-8"))
    topic_rows = [t for t in thumbs if t["video"] == "topic"]
    assert [t["index"] for t in topic_rows] == [1]
    if topic_rows[0]["file"]:
        assert (job_dir / "thumbnails" / "base_topic_01.jpg").is_file()
        assert "thumbnails/base_topic_01.jpg" in job.artifacts


@needs_ffmpeg
def test_a_render_exception_leaves_the_job_done(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("the topic render broke")

    job, job_dir = _deliver(tmp_path, monkeypatch, render=boom)
    assert job.status == JobStatus.DONE, job.error
    art = job.artifacts["topic/topic_01.mp4"]
    assert art.status == "failed" and "broke" in art.reason
    assert any("topic cut 1 failed" in w for w in job.warnings)
    assert json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))["topics"] == []
    assert job.artifacts["final.mp4"].status == "ok"


# ------------------------------------------------------- render_topic_cut (ffmpeg)


def _ffprobe_frames(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
         "stream=nb_read_frames", "-of", "csv=p=0", str(path)], check=True, capture_output=True, text=True).stdout
    return int(out.strip().split(",")[0])


@needs_ffmpeg
def test_a_two_range_cut_with_a_cold_open_adds_up_exactly(tmp_path, monkeypatch):
    from core.config import get_settings as gs
    from outputs import RenderInputs, render_topic_cut
    from slice.fonts import find_font
    from slice.profile import probe_header, probe_media

    src = tmp_path / "meeting.mp4"
    _make_source(src, seconds=30)
    outro = tmp_path / "outro.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100:duration=2", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(outro)], check=True)
    media = probe_media(src)
    # source frames 0-300 and 420-780 kept; the cut spans 60-700: 240 + 280 = 520 topic frames
    edl = _edl([(0, 300), (420, 780)], 900)
    inp = RenderInputs(job_dir=tmp_path, source=src, media=media, edl=edl, removed=[], reel_edl=None, shorts=[],
                       words=[], title="Meeting", outro=outro)
    cut = _cut(start_frame=60, end_frame=700, open_span=(2.0, 5.0))  # a 3 s (90 frame) cold open
    work = tmp_path / "work"
    out_path = tmp_path / "topic_01.mp4"
    font = find_font()
    res = render_topic_cut(inp, cut, "Why the sensors drift", "FloLabs Sensors", out_path, work, font,
                           find_font(bold=True), gain_db=None, hd=False)
    assert not work.exists()  # everything temporary is gone
    card_frames = round(res.card_s * 30)
    if font is None:
        assert res.card_s == 0
    else:
        assert card_frames == round(gs().title_card_s * 30)
    outro_frames = round(res.outro_s * 30)
    assert outro_frames == 60  # 2 s at the meeting's 30 fps
    assert res.open_s == pytest.approx(3.0) and res.body_s == pytest.approx(520 / 30)
    expected = 90 + card_frames + 520 + outro_frames
    assert res.frames == expected
    header = probe_header(out_path)
    assert header.video_frames == expected == _ffprobe_frames(out_path)
    assert header.audio_duration == pytest.approx(expected / 30, abs=0.05)
    assert header.fps == "30/1" and (header.width, header.height) == (media.width, media.height)


@needs_ffmpeg
def test_the_cut_matches_the_full_videos_encoding(tmp_path):
    from outputs import RenderInputs, render_topic_cut
    from slice.ffmpeg_wrapper import extract_audio_flac
    from slice.fonts import find_font
    from slice.pipeline import render_track
    from slice.profile import assert_concat_compatible, probe_media

    src = tmp_path / "meeting.mp4"
    _make_source(src, seconds=30)
    media = probe_media(src)
    edl = _edl([(0, 900)], 900)
    inp = RenderInputs(job_dir=tmp_path, source=src, media=media, edl=edl, removed=[], reel_edl=None, shorts=[],
                       words=[], title="Meeting")
    out_path = tmp_path / "topic_01.mp4"
    res = render_topic_cut(inp, _cut(start_frame=30, end_frame=300, open_span=None), "Title", "FloLabs Sensors",
                           out_path, tmp_path / "work", find_font(), find_font(bold=True))
    assert res.open_s == 0 and res.outro_s == 0
    flac = tmp_path / "ref.flac"
    extract_audio_flac(src, flac, media.audio_channels, media.duration)
    ref = render_track(src, flac, _edl([(0, 90)], 900), media, tmp_path / "ref", "ref")
    try:
        assert_concat_compatible([ref.video, out_path])
    finally:
        ref.delete()


@needs_ffmpeg
def test_a_cut_without_cold_open_or_outro_is_the_card_plus_the_topic(tmp_path):
    from outputs import RenderInputs, render_topic_cut
    from slice.fonts import find_font
    from slice.profile import probe_header, probe_media

    src = tmp_path / "meeting.mp4"
    _make_source(src, seconds=30)
    media = probe_media(src)
    inp = RenderInputs(job_dir=tmp_path, source=src, media=media, edl=_edl([(0, 900)], 900), removed=[],
                       reel_edl=None, shorts=[], words=[], title="Meeting")
    res = render_topic_cut(inp, _cut(start_frame=30, end_frame=330, open_span=None), "Title", "FloLabs Sensors",
                           tmp_path / "t.mp4", tmp_path / "w", find_font(), find_font(bold=True))
    assert res.open_s == 0 and res.outro_s == 0
    assert probe_header(tmp_path / "t.mp4").video_frames == res.frames == round(res.card_s * 30) + 300
