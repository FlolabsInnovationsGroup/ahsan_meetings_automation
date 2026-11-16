"""The thumbnail stage of deliver(): clean base frames kept for the
upload-time follow-up. Synthetic lavfi sources only."""

import json
import shutil
import subprocess
import zipfile
from dataclasses import replace
from fractions import Fraction

import pytest

from core.config import get_settings
from core.models import (
    EditDecisionList,
    EDLRange,
    JobRecord,
    JobStatus,
    Moment,
    ShortClip,
    Word,
)
from core.timeline import rate_str

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not installed"
)


def _make_source(path, seconds=20):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
        check=True)


def _edl(total):
    fps = Fraction(30)
    return EditDecisionList(
        ranges=[EDLRange(start=0.0, end=float(Fraction(total) / fps), start_frame=0, end_frame=total)],
        source_duration=float(Fraction(total) / fps), fps=rate_str(fps), fade_frames=0, total_frames=total)


def _settings(monkeypatch, **changes):
    from publish import settings as ps

    cfg = replace(ps.current(), **changes)
    monkeypatch.setattr(ps, "current", lambda: cfg)


def _run(tmp_path):
    from deliver import DeliverInputs, deliver
    from outputs import RenderInputs
    from slice.profile import probe_media

    src = tmp_path / "meeting.mp4"
    _make_source(src)
    job = JobRecord(job_id="thumb-job", status=JobStatus.SLICING, title="Weekly sync")
    job_dir = get_settings().jobs_dir / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    words = [Word(word="Opening remarks.", start=0.5, end=2.0, speaker="A"),
             Word(word="The demo works well.", start=6.0, end=8.0, speaker="B")]
    render = RenderInputs(
        job_dir=job_dir, source=src, media=probe_media(src), edl=_edl(600), removed=[], reel_edl=_edl(600),
        shorts=[ShortClip(index=1, moment_id=0, start=5.0, end=15.0, score=9, title="The demo", hook="Watch this")],
        words=words, title="Weekly sync")
    moments = [Moment(id=0, start=5.0, end=15.0, first_index=1, last_index=1, score=9, title="The demo")]
    deliver(job, lambda j: None, DeliverInputs(render=render, reel=moments))
    return job, job_dir


def test_stage_writes_base_frames_keeps_them_after_cleanup_and_bundles_them(tmp_path, monkeypatch):
    _settings(monkeypatch, thumbnail_mode="test", thumbnail_faces=True)
    monkeypatch.setenv("SERVE_INDIVIDUAL_ARTIFACTS", "false")
    get_settings.cache_clear()
    job, job_dir = _run(tmp_path)
    assert job.status == JobStatus.DONE, job.error
    assert not [w for w in job.warnings if "thumbnails" in w]
    rows = json.loads((job_dir / "thumbnails" / "thumbs.json").read_text(encoding="utf-8"))
    assert {r["video"] for r in rows} == {"full", "short"}
    files = [r["file"] for r in rows if r["file"]]
    assert "thumbnails/base_short_01.jpg" in files
    assert any(f.startswith("thumbnails/base_full_") for f in files)
    for f in files:
        assert (job_dir / f).stat().st_size > 1000
    # shorts were cleaned up, thumbnails were not
    assert not (job_dir / "shorts").exists()
    with zipfile.ZipFile(job_dir / "bundle.zip") as z:
        names = set(z.namelist())
    assert "thumbnails/thumbs.json" in names
    assert set(files) <= names
    assert job.artifacts["thumbnails/base_short_01.jpg"].kind == "image"
    assert job.artifacts["thumbnails/base_short_01.jpg"].on_disk


def test_a_failure_in_the_stage_is_only_a_warning(tmp_path, monkeypatch):
    from publish import thumbframes

    _settings(monkeypatch, thumbnail_mode="test")

    def boom(*a, **k):
        raise RuntimeError("no keyframes")

    monkeypatch.setattr(thumbframes, "score_keyframes", boom)
    job, job_dir = _run(tmp_path)
    assert job.status == JobStatus.DONE, job.error
    assert any(w.startswith("thumbnails skipped:") and "no keyframes" in w for w in job.warnings)
    assert not (job_dir / "thumbnails" / "thumbs.json").exists()


def test_stage_is_skipped_when_thumbnails_and_short_thumbnails_are_off(tmp_path, monkeypatch):
    _settings(monkeypatch, thumbnail_mode="off", thumbnail_shorts=False)
    job, job_dir = _run(tmp_path)
    assert job.status == JobStatus.DONE, job.error
    assert not (job_dir / "thumbnails").exists()
    assert "thumbnails" not in job.stage_timings
    assert not [w for w in job.warnings if "thumbnails" in w]


def test_a_shared_screen_pick_uses_the_slide_box_and_anything_else_keeps_its_crop(monkeypatch):
    from types import SimpleNamespace

    import deliver
    from publish import thumbframes as tf

    seen = []
    monkeypatch.setattr(tf, "slide_crop", lambda *a, **k: seen.append(a) or (250, 100, 800, 450))
    share = SimpleNamespace(kind=tf.KIND_SHARE, t=30.0, crop=(0, 70, 1280, 650))
    assert deliver._slide_or(tf, "s.mp4", share, 1280, 720, "shared_screen") == ((250, 100, 800, 450), True)
    camera = SimpleNamespace(kind=tf.KIND_CAMERA, t=30.0, crop=None)
    assert deliver._slide_or(tf, "s.mp4", camera, 1280, 720, None) == (None, False)
    assert len(seen) == 1
    monkeypatch.setattr(tf, "slide_crop", lambda *a, **k: None)  # no reliable slide box: today's framing
    assert deliver._slide_or(tf, "s.mp4", share, 1280, 720, None) == ((0, 70, 1280, 650), False)
