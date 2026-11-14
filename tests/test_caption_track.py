"""Caption tracks through the queue (publish/caption_track.py): synthetic data only."""

import importlib
import json
import re
import zipfile
from types import SimpleNamespace

import pytest

from ingest.store import job_dir
from publish import caption_track, extras, optional, quota
from publish import youtube as yt

JOB = "jobcap1"
INTRO, OFFSET, OUTRO, DURATION = 5.0, 20.0, 6.0, 120.0
REEL_END = INTRO + 3 + 2.5
REMOVED = "ummremovedword"


def _lines():
    out = []
    # highlights reel (after the 5 s intro), then the meeting after the title card
    for i, text in enumerate(["Welcome to the planning review today.", "We shipped the first build."]):
        out.append({"start": INTRO + i * 3, "end": INTRO + i * 3 + 2.5, "speaker": "Alice Example",
                    "text": text, "section": "highlights"})
    for i in range(12):
        s = OFFSET + i * 4
        out.append({"start": s, "end": s + 3.5, "speaker": "Bob Example" if i % 2 else "Alice Example",
                    "text": f"This is sentence number {i} about the roadmap and the next milestone.",
                    "section": "meeting"})
    return out


def _write(folder, lines=None):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "transcript_clean.json").write_text(json.dumps({
        "intro_s": INTRO, "outro_s": OUTRO, "cleaned_starts_at_s": OFFSET, "lines": lines or _lines()}))
    (folder / "manifest.json").write_text(json.dumps({"final": {
        "duration_s": DURATION, "intro_s": INTRO, "outro_s": OUTRO, "cleaned_starts_at_s": OFFSET}}))


def _cues(srt):
    found = re.findall(r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)\n(.+)", srt)
    rows = []
    for m in found:
        a = [int(x) for x in m[:8]]
        rows.append((a[0] * 3600 + a[1] * 60 + a[2] + a[3] / 1000, a[4] * 3600 + a[5] * 60 + a[6] + a[7] / 1000, m[8]))
    return rows


def test_srt_is_clean_and_inside_the_speech():
    _write(job_dir(JOB))
    srt = caption_track.full_srt(JOB)
    cues = _cues(srt)
    assert cues
    assert REMOVED not in srt
    assert "Alice" not in srt and "Bob" not in srt and "Example" not in srt
    for start, end, text in cues:
        assert start >= INTRO and end <= DURATION - OUTRO
        assert len(text) <= 42
        assert not (end > REEL_END + 0.01 and start < OFFSET)  # nothing on the title card
    assert [c[0] for c in cues] == sorted(c[0] for c in cues)
    assert all(cues[i][1] <= cues[i + 1][0] + 1e-6 for i in range(len(cues) - 1))


def test_words_on_intro_and_outro_are_dropped():
    lines = _lines() + [
        {"start": 1.0, "end": 3.0, "speaker": "x", "text": "intro words", "section": "highlights"},
        {"start": DURATION - 4, "end": DURATION - 1, "speaker": "x", "text": "outro words", "section": "meeting"}]
    _write(job_dir(JOB), lines)
    srt = caption_track.full_srt(JOB)
    assert "intro words" not in srt and "outro words" not in srt


def test_clamped_reel_line_stays_off_the_title_card():
    lines = [
        {"start": 14.6, "end": 15.0, "speaker": "x", "section": "highlights",
         "text": "and then we decided to move the whole launch"},
        {"start": OFFSET + 1, "end": OFFSET + 3, "speaker": "x", "section": "meeting", "text": "Fine."}]
    _write(job_dir(JOB), lines)
    cues = _cues(caption_track.full_srt(JOB))
    assert cues
    assert not [c for c in cues if OFFSET > c[0] >= 15.0 or (c[0] < 15.0 and c[1] > 15.0 + 0.01)]
    assert cues[0][0] == pytest.approx(14.6)


def test_last_meeting_line_is_cut_at_the_outro_not_dropped():
    outro_start = DURATION - OUTRO
    lines = _lines() + [{"start": outro_start - 0.5, "end": outro_start, "speaker": "x", "section": "meeting",
                         "text": "thanks everyone see you all next week bye"}]
    _write(job_dir(JOB), lines)
    cues = _cues(caption_track.full_srt(JOB))
    assert cues[-1][1] <= outro_start + 1e-6
    assert any(c[0] >= outro_start - 0.5 - 1e-6 for c in cues)


def test_removed_speech_is_not_there():
    # the kept transcript never has removed words; the SRT carries only what it holds
    _write(job_dir(JOB))
    assert REMOVED not in caption_track.full_srt(JOB)
    with_removed = _lines()
    _write(job_dir(JOB), [row for row in with_removed if REMOVED not in row["text"]])
    assert REMOVED not in caption_track.full_srt(JOB)


def test_missing_file_gives_empty():
    assert caption_track.full_srt("nojob") == ""
    assert caption_track.short_srt("nojob", 1) == ""


def test_zip_fallback():
    folder = job_dir(JOB)
    _write(folder)
    srt_short = "1\n00:00:00,000 --> 00:00:01,000\nHello there\n"
    with zipfile.ZipFile(folder / "bundle.zip", "w") as zf:
        zf.write(folder / "transcript_clean.json", "transcript_clean.json")
        zf.write(folder / "manifest.json", "manifest.json")
        zf.writestr("shorts/short_02.srt", srt_short)
    (folder / "transcript_clean.json").unlink()
    (folder / "manifest.json").unlink()
    assert caption_track.full_srt(JOB).strip()
    assert caption_track.short_srt(JOB, 2) == srt_short


# ------------------------------------------------------------------ the queue


@pytest.fixture
def _env(monkeypatch):
    monkeypatch.setattr(yt, "granted_scopes", lambda: set(yt.SCOPES))
    monkeypatch.setattr(quota, "_limits", lambda: (10000, 2000))
    _write(job_dir(JOB))


def _task(video_id="vid1"):
    t = extras.Task(kind="captions", video_id=video_id)
    path = extras.files_dir() / f"{t.id}.srt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1\n00:00:06,000 --> 00:00:07,000\nHi\n")
    t.file = str(path)
    return t


def _patch_insert(monkeypatch, fn):
    from publish import youtube_extra

    monkeypatch.setattr(youtube_extra, "insert_caption", fn)


def test_missing_scope_needs_reconnect_without_a_call(_env, monkeypatch):
    monkeypatch.setattr(yt, "granted_scopes", lambda: {s for s in yt.SCOPES if "force-ssl" not in s})
    calls = []
    _patch_insert(monkeypatch, lambda *a, **k: calls.append(1))
    out = caption_track.run(_task())
    assert out.status == optional.NEEDS_RECONNECT and not calls


def test_caption_exists_is_done_and_units_spent(_env, monkeypatch):
    from tests.test_optional_calls import FakeHttpError

    def boom(*a, **k):
        raise FakeHttpError(409, "captionExists")

    _patch_insert(monkeypatch, boom)
    spent = []
    monkeypatch.setattr(quota, "spend", lambda units, feature: spent.append((units, feature)))
    out = caption_track.run(_task())
    assert out.status == optional.DONE
    assert spent == [(400, "captions")]


def test_success_passes_language_and_name(_env, monkeypatch):
    seen = {}
    _patch_insert(monkeypatch, lambda vid, path, language, name: seen.update(v=vid, lang=language, name=name) or {})
    assert caption_track.run(_task("abc")).status == optional.DONE
    assert seen["v"] == "abc" and seen["name"] == "English (FloLabs)" and seen["lang"]


CFG_ON = SimpleNamespace(captions_full=True, captions_shorts=False)


def _item(kind="full", index=None):
    return SimpleNamespace(key=f"{JOB}|{kind}", job_id=JOB, index=index)


def test_builder_skips_plain_private(_env):
    posted = SimpleNamespace(privacy="private", publish_at=None, video_id="v")
    task = caption_track.build(SimpleNamespace(id=JOB), _item(), posted, video_kind="full", cfg=CFG_ON)
    assert task.state == extras.SKIPPED and task.file is None


def test_builder_scheduled_private_gets_a_file(_env):
    posted = SimpleNamespace(privacy="private", publish_at="2030-01-01T00:00:00Z", video_id="v")
    task = caption_track.build(SimpleNamespace(id=JOB), _item(), posted, video_kind="full", cfg=CFG_ON)
    assert task.state == extras.WAITING and task.file.endswith(".srt")
    with open(task.file, encoding="utf-8") as f:
        assert _cues(f.read())


def test_builder_respects_switches_and_topic(_env):
    posted = SimpleNamespace(privacy="public", publish_at=None, video_id="v")
    off = SimpleNamespace(captions_full=False, captions_shorts=False)
    job = SimpleNamespace(id=JOB)
    assert caption_track.build(job, _item(), posted, video_kind="full", cfg=off) is None
    assert caption_track.build(job, _item("short", 1), posted, video_kind="short", cfg=CFG_ON) is None
    assert caption_track.build(job, _item("topic"), posted, video_kind="topic", cfg=CFG_ON) is None


def test_registered_with_the_queue(monkeypatch):
    monkeypatch.setattr(extras, "_builders", {})
    monkeypatch.setattr(extras, "_runners", {})
    importlib.reload(caption_track)
    assert extras._builders["captions"] is caption_track.build
    assert extras._runners["captions"] is caption_track.run
