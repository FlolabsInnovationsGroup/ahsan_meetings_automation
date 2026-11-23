"""The end card: a new-style Short is speech + card long, frame and sample exact;
off means no card. Synthetic source, made-up words."""

import dataclasses
import shutil
import subprocess
from fractions import Fraction

import pytest

import publish.settings as pub_settings
import slice.pipeline as slice_pipeline
from core.models import ShortClip, Word
from core.timeline import samples_at_frame
from slice.captions import ass_document_new
from slice.endcard import render_end_card

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
CLIP = ShortClip(index=1, moment_id=0, start=1.0, end=7.0, score=9, title="A made up title",
                 screen_hook="Made up hook")
WORDS = [Word(word="Alpha beta gamma delta.", start=1.2, end=2.4, speaker="A")]


@pytest.fixture
def synthetic(tmp_path):
    from slice.ffmpeg_wrapper import extract_audio_flac
    from slice.profile import probe_media

    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=9",
                    "-f", "lavfi", "-i", "aevalsrc=exprs='0.3*sin(2*PI*440*t)*not(between(t,3,5.5))':s=48000:d=9",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-ac", "1", str(src)], check=True)
    media = probe_media(src)
    flac = tmp_path / "audio.flac"
    extract_audio_flac(src, flac, media.audio_channels, media.duration)
    return src, flac, media


def _render(tmp_path, synthetic, silences=None, **look_changes):
    from slice.fonts import find_font

    src, flac, media = synthetic
    look = dataclasses.replace(pub_settings.looks().shorts, hd=False, loudness=False, layout="fit",
                               **{"trim_pauses": False, **look_changes})
    return slice_pipeline.render_short_clip(
        src, flac, CLIP, media, WORDS, tmp_path / "s" / "short_01.mp4", tmp_path / "s" / "short_01.srt",
        tmp_path / "w", find_font(), width=216, height=384, style="new", look=look, job_id="j", index=1,
        card_title="Weekly sync: a made up topic", silences=silences)


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_the_short_is_speech_plus_card_frame_and_sample_exact(tmp_path, synthetic):
    media = synthetic[2]
    result = _render(tmp_path, synthetic, end_card=True, end_card_seconds=4.5)
    card = round(4.5 * float(media.fps))
    assert result.header.video_frames == 150 + card and result.frames == 150 + card
    want = samples_at_frame(150, media.fps) + samples_at_frame(card, media.fps)
    assert result.header.audio_duration == pytest.approx(want / 48000, abs=0.03)
    assert result.duration_s == pytest.approx(float(Fraction(150 + card) / media.fps))


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_with_the_card_off_there_is_no_card(tmp_path, synthetic):
    result = _render(tmp_path, synthetic, end_card=False)
    assert result.header.video_frames == 150


def test_the_end_line_moves_onto_the_card():
    look = dataclasses.replace(pub_settings.looks().shorts, hook_seconds=2.5)
    doc = ass_document_new([], hook="", title="Topic", end_line="Full meeting on our channel", duration_s=20.0,
                           look=look, font_family="Montserrat", width=1080, height=1920, card_s=4.5)
    end = next(ln for ln in doc.splitlines() if "Full meeting" in ln)
    assert end.startswith("Dialogue: 1,0:00:15.50,0:00:20.00")


def test_the_card_draws_with_and_without_a_picture(tmp_path):
    from PIL import Image

    pic = tmp_path / "p.jpg"
    Image.new("RGB", (640, 360), (30, 90, 140)).save(pic)
    a = render_end_card(tmp_path / "a.png", "A topic", (216, 384))
    b = render_end_card(tmp_path / "b.png", "A topic", (216, 384), pic)
    assert Image.open(a).size == (216, 384) == Image.open(b).size
    assert Image.open(a).tobytes() != Image.open(b).tobytes()


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_the_card_follows_trimmed_pauses(tmp_path, synthetic):
    from slice.pipeline import kept_spans
    from slice.silence import SilenceMap

    media = synthetic[2]
    sil = SilenceMap(ranges=[(3.0, 5.5)], threshold_db=None, audio_s=9.0)
    spans = kept_spans(1.0, 7.0, sil.ranges, 0.6, fps=media.fps)
    assert len(spans) == 2
    kept = sum(b - a for a, b in spans)
    result = _render(tmp_path, synthetic, silences=sil, end_card=True, end_card_seconds=4.5, trim_pauses=True,
                     max_pause_s=0.6)
    card = round(4.5 * float(media.fps))
    assert result.header.video_frames == kept + card < 150 + card
