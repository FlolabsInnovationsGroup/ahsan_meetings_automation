"""The Shorts style choice (classic / new / alternate), the new-look render
with its fallbacks, and the fields that carry the style into shorts.json and
manifest.json. Synthetic lavfi sources and made-up words only."""

import dataclasses
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

import outputs as outputs_module
import pipeline as pipeline_module
import publish.settings as pub_settings
import slice.ffmpeg_wrapper as fw
import slice.pipeline as slice_pipeline
from core.config import get_settings
from core.models import JobStatus, ShortClip, Word
from slice.pipeline import pick_style
from tests.test_pipeline import _make_job, _scored_meeting

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


# ----------------------------------------------------------------- pick_style


def test_pick_style_honours_classic_and_new():
    for job_id in ("a", "b", "c"):
        assert pick_style("classic", job_id, 1) == "classic"
        assert pick_style("new", job_id, 2) == "new"


def test_alternate_gives_one_of_each_for_shorts_one_and_two():
    for n in range(300):
        job_id = f"job-{n:04d}-synthetic"
        assert {pick_style("alternate", job_id, 1), pick_style("alternate", job_id, 2)} == {"new", "classic"}


def test_alternate_is_deterministic_and_varies_between_jobs():
    assert [pick_style("alternate", "same-job", i) for i in (1, 2, 3, 4)] == \
           [pick_style("alternate", "same-job", i) for i in (1, 2, 3, 4)]
    firsts = {pick_style("alternate", f"job-{n}", 1) for n in range(40)}
    assert firsts == {"new", "classic"}  # not always the same style first


# --------------------------------------------------------------------- render


def _source(path: Path, seconds: int = 4) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=25:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ac", "1", str(path)],
        check=True)


@pytest.fixture
def synthetic(tmp_path):
    from slice.profile import probe_media

    src = tmp_path / "src.mp4"
    _source(src)
    media = probe_media(src)
    flac = tmp_path / "audio.flac"
    fw.extract_audio_flac(src, flac, media.audio_channels, media.duration)
    return src, flac, media


CLIP = ShortClip(index=1, moment_id=0, start=0.5, end=3.5, score=9, title="A made up title",
                 screen_hook="Made up hook text")
WORDS = [Word(word="Alpha beta gamma delta epsilon zeta eta theta iota.", start=0.6, end=3.2, speaker="A")]


def _render(tmp_path, synthetic, **kw):
    from slice.fonts import find_font

    src, flac, media = synthetic
    return slice_pipeline.render_short_clip(
        src, flac, CLIP, media, WORDS, tmp_path / "shorts" / "short_01.mp4", tmp_path / "shorts" / "short_01.srt",
        tmp_path / "w", find_font(), width=216, height=384, **kw)


def _new_look(**changes):
    return dataclasses.replace(pub_settings.looks().shorts, hd=False, loudness=True, **changes)


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_new_style_writes_an_exact_short_and_records_how(tmp_path, synthetic):
    result = _render(tmp_path, synthetic, style="new", look=_new_look(), recording_type="active_speaker",
                     job_id="j", index=1)
    h = result.header
    assert (h.width, h.height, h.video_frames) == (216, 384, 75)
    assert result.style == "new" and result.layout_kind in ("screen", "speaker", "gallery", "fit")
    assert result.gain_db is not None and -10 <= result.gain_db <= 12
    assert result.hook_text == "Made up hook text" and result.look["loudness"] is True
    assert result.analysis_s >= 0
    srt = (tmp_path / "shorts" / "short_01.srt").read_text(encoding="utf-8")
    assert "Alpha" in srt and "Made up hook text" not in srt  # the .srt stays the plain captions
    assert not (tmp_path / "w").exists()


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_a_layout_failure_falls_back_to_fit_with_a_note(tmp_path, synthetic, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("analysis exploded")

    monkeypatch.setattr(slice_pipeline.layout_mod, "analyse", boom)
    result = _render(tmp_path, synthetic, style="new", look=_new_look(), job_id="j", index=1)
    assert result.style == "new" and result.layout_kind == "fit"
    assert any("layout analysis failed" in n for n in result.notes)
    assert result.header.video_frames == 75


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_a_measurement_failure_means_no_gain_and_a_note(tmp_path, synthetic, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("meter exploded")

    monkeypatch.setattr(slice_pipeline, "measure_lufs", boom)
    result = _render(tmp_path, synthetic, style="new", look=_new_look(), job_id="j", index=1)
    assert result.style == "new" and result.gain_db is None
    assert any("loudness measurement failed" in n for n in result.notes)


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_a_fixed_layout_setting_skips_the_analysis(tmp_path, synthetic, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not analyse")

    monkeypatch.setattr(slice_pipeline.layout_mod, "analyse", boom)
    result = _render(tmp_path, synthetic, style="new", look=_new_look(layout="fit"), job_id="j", index=1)
    assert result.layout_kind == "fit" and not result.notes


def _spy_commands(monkeypatch):
    """Every ffmpeg command line render_short builds, with its filter graph text."""
    seen = []
    real = fw.run_checked

    def spy(cmd, *a, **k):
        if "-map" in cmd:
            graph = next((Path(c).read_text(encoding="utf-8") for c in cmd if str(c).endswith("short_graph.txt")),
                         "")
            seen.append((list(cmd), graph))
        return real(cmd, *a, **k)

    monkeypatch.setattr(fw, "run_checked", spy)
    return seen


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_classic_is_todays_render_whatever_the_new_arguments(tmp_path, synthetic, monkeypatch):
    seen = _spy_commands(monkeypatch)
    plain = _render(tmp_path, synthetic)  # exactly the old call
    again = _render(tmp_path, synthetic, style="classic", look=_new_look(), recording_type="active_speaker",
                    job_id="j", index=2)
    assert len(seen) == 2 and seen[0] == seen[1]
    cmd, graph = seen[0]
    assert "alimiter" not in graph and "crop=" in graph and "boxblur" in graph
    assert plain.style == again.style == "classic" and plain.gain_db is None and plain.hook_text == ""
    # the new style without a look is also today's render
    seen.clear()
    _render(tmp_path, synthetic, style="new", look=None)
    assert seen[0] == (cmd, graph)


# ---------------------------------------------------- recording type, job id


def test_render_inputs_carry_the_recording_type_and_job_id(monkeypatch):
    calls, got = [], []
    _scored_meeting(monkeypatch, calls)
    fake = outputs_module.render_short_clip

    def spy(*a, **kw):
        got.append(kw)
        return fake(*a, **kw)

    monkeypatch.setattr(outputs_module, "render_short_clip", spy)
    job = _make_job("job-rt", native_transcript_path="t.vtt", zoom_meeting={"recording_type": "shared_screen"})
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DONE, job.error
    assert got and all(k["recording_type"] == "shared_screen" and k["job_id"] == "job-rt" for k in got)
    assert [k["index"] for k in got] == list(range(1, len(got) + 1))
    assert {k["style"] for k in got} <= {"classic", "new"}
    if len(got) > 1:  # the default is alternate
        assert {k["style"] for k in got[:2]} == {"classic", "new"}

    got.clear()
    upload = _make_job("job-up", native_transcript_path="t.vtt")  # an upload: no Zoom info
    pipeline_module.run_pipeline(upload, lambda j: None)
    assert got and all(k["recording_type"] is None for k in got)


# ------------------------------------------------------------ deliver fields


def test_shorts_json_and_manifest_carry_style_layout_and_gain(monkeypatch):
    calls = []
    _scored_meeting(monkeypatch, calls)
    fake = outputs_module.render_short_clip

    def styled(*a, **kw):
        result = fake(*a, **kw)
        if kw["index"] == 1:
            result.style, result.layout_kind, result.gain_db, result.hook_text = "new", "screen", -3.5, "Hook!"
        return result

    monkeypatch.setattr(outputs_module, "render_short_clip", styled)
    job = _make_job("job-rows", native_transcript_path="t.vtt")
    pipeline_module.run_pipeline(job, lambda j: None)
    assert job.status == JobStatus.DONE, job.error
    with zipfile.ZipFile(job.bundle_path) as zf:
        shorts_json = json.loads(zf.read("shorts/shorts.json"))
        manifest = json.loads(zf.read("manifest.json"))
    first, second = shorts_json[0], shorts_json[1]
    assert (first["style"], first["layout"], first["gain_db"], first["screen_hook_shown"]) == \
           ("new", "screen", -3.5, "Hook!")
    assert (second["style"], second["screen_hook_shown"]) == ("classic", "") and second["gain_db"] is None
    row = manifest["shorts"][0]
    assert (row["style"], row["layout"], row["gain_db"], row["screen_hook_shown"]) == ("new", "screen", -3.5, "Hook!")
    assert manifest["shorts"][1]["style"] == "classic"
    assert set(manifest["render_look"]) == {"shorts", "full"}
    assert manifest["render_look"]["shorts"]["style"] == pub_settings.looks().shorts.style
    assert "render_shorts_analysis" in job.stage_timings


def test_an_old_manifest_without_style_still_loads_as_classic():
    from publish.describe import load_shorts

    job = _make_job("job-old-manifest")
    job_dir = get_settings().jobs_dir / job.job_id
    job_dir.mkdir(parents=True)
    row = {"file": "shorts/short_01.mp4", "source_start": 1.0, "source_end": 40.0, "title": "T", "hook": "H",
           "text": "words"}
    (job_dir / "manifest.json").write_text(json.dumps({"shorts": [row]}), encoding="utf-8")
    shorts = load_shorts(job)
    assert len(shorts) == 1 and shorts[0].style == "classic"
