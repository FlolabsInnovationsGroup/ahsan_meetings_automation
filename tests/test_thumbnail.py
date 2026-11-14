"""Thumbnails (publish/thumbnail.py): the label, the chip, the test arms, the
picture and the runner. Synthetic data only; no network."""

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config import get_settings
from ledger import files
from ledger.uploads import Item, get_ledger
from publish import extras, optional, quota, series, thumbnail, youtube
from publish import settings as publishing_settings
from publish import youtube_extra as ye

needs_ffmpeg = pytest.mark.skipif(shutil.which(get_settings().ffmpeg_bin) is None, reason="ffmpeg not installed")


class FakeHttpError(Exception):
    def __init__(self, status, reason=""):
        super().__init__(f"<HttpError {status}>")
        self.resp = SimpleNamespace(status=status)
        self.content = json.dumps({"error": {"code": status, "message": "x",
                                             "errors": [{"reason": reason}] if reason else []}}).encode()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(extras, "_builders", {})
    monkeypatch.setattr(extras, "_runners", {})
    monkeypatch.setattr(extras, "_loaded", True)
    monkeypatch.setattr(youtube, "granted_scopes", lambda: set(youtube.SCOPES))
    monkeypatch.setattr(youtube, "youtube_status", lambda: {"connected": True})
    monkeypatch.setattr(quota, "_limits", lambda: (10000, 2000))


def _cfg(**changes):
    return publishing_settings.current().model_copy(update=changes) if hasattr(
        publishing_settings.current(), "model_copy") else SimpleNamespace(**{**vars(publishing_settings.current()), **changes})


def _job(tmp_path, job_id="job1", shorts=None, highlights=None, speakers=()):
    folder = get_settings().jobs_dir / job_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps({"shorts": shorts or [], "highlights": highlights or []}))
    (folder / "segments.json").write_text(json.dumps([{"speaker": s, "text": "x"} for s in speakers]))
    return SimpleNamespace(job_id=job_id, id=job_id, title="Space team meeting", artifacts={}, final_offset_s=0,
                           render_manifest_path="")


def _short_row(n=1, hook="Caching cuts load time", text="the caching change cut the load time a lot"):
    return {"file": f"shorts/short_{n:02d}.mp4", "source_start": 10.0, "source_end": 40.0, "title": "t", "hook": "h",
            "screen_hook": hook, "text": text}


# ------------------------------------------------------------------ the label


def test_the_label_is_two_to_five_words(tmp_path):
    job = _job(tmp_path, shorts=[_short_row(1, "Caching cuts load time")])
    assert thumbnail.label_for(job, "short", {"index": 1}) == "Caching cuts load time"
    for hook in ("Caching", "one two three four five six", ""):
        job = _job(tmp_path, shorts=[_short_row(1, hook, "caching one two three four five six")])
        assert thumbnail.label_for(job, "short", {"index": 1}) is None


def test_a_label_with_a_name_an_unsaid_number_or_a_never_post_word_is_dropped(tmp_path):
    job = _job(tmp_path, shorts=[_short_row(1, "Maria fixed the cache", "we fixed the cache today")],
               speakers=["Maria Lopez"])
    assert thumbnail.label_for(job, "short", {"index": 1}) is None
    job = _job(tmp_path, shorts=[_short_row(1, "Cut costs by 80 percent", "we cut costs by half")])
    assert thumbnail.label_for(job, "short", {"index": 1}) is None
    job = _job(tmp_path, shorts=[_short_row(1, "Investor update this week", "the investor update this week")])
    assert thumbnail.label_for(job, "short", {"index": 1}) is None
    job = _job(tmp_path, shorts=[_short_row(1, "Angle <b> brackets here", "angle brackets here")])
    assert thumbnail.label_for(job, "short", {"index": 1}) is None


def test_the_label_is_off_when_the_setting_is_off(tmp_path):
    job = _job(tmp_path, shorts=[_short_row(1)])
    assert thumbnail.label_for(job, "short", {"index": 1}, _cfg(thumbnail_label=False)) is None


def test_a_full_video_without_a_hook_uses_the_first_words_of_its_title(tmp_path):
    job = _job(tmp_path)
    label = thumbnail.label_for(job, "full", "Caching cuts load time for every page | FloLabs Space")
    assert label == "Caching cuts load time for"


def test_a_topic_uses_its_hook(tmp_path):
    job = _job(tmp_path)
    label = thumbnail.label_for(job, "topic", {"hook": "Moving to a new queue", "text": "moving to a new queue now"})
    assert label == "Moving to a new queue"


# ------------------------------------------------------------------- the chip


def test_the_chip_uses_the_registry_label_and_a_stable_colour(tmp_path):
    job = _job(tmp_path)
    item = Item("k", "s", "full", "job1", "Space team meeting", topic="Space Weekly (2026-01-02)")
    label, colour = thumbnail.team_chip(job, item)
    assert label == "Space"
    assert colour in thumbnail.PALETTE
    assert thumbnail.team_chip(job, item) == (label, colour)
    other = Item("k2", "s2", "full", "job2", "x", topic="Space Weekly (2026-02-09)")
    assert thumbnail.team_chip(job, other)[1] == colour
    assert thumbnail.PALETTE[series.colour_index(series.series_key("Space Weekly"))] == colour


# ------------------------------------------------------------------- the arms


def _posted(n, topic="Space Weekly", kind="full"):
    item = Item(f"job:{n}|{kind}", f"job:{n}", kind, f"job{n}", topic, topic=topic)
    get_ledger().begin(item, title="t", privacy="public", by="test")
    upload = get_ledger().published(item.key, f"vid{n:08d}", by="test")
    return item, upload


def _arm(n, topic="Space Weekly", kind="full", **cfg):
    item, upload = _posted(n, topic, kind)
    return thumbnail.arm(SimpleNamespace(job_id=f"job{n}"), kind, item, upload, _cfg(**cfg))


def test_under_test_the_arm_alternates_per_team_and_is_written_down(monkeypatch):
    seen = []
    from analytics import record

    monkeypatch.setattr(record, "set_arm", lambda *a: seen.append(a))
    arms = [_arm(i, "Space Weekly", thumbnail_mode="test") for i in range(1, 5)]
    assert arms == ["custom", "auto", "custom", "auto"]
    assert _arm(10, "Robotics Weekly", thumbnail_mode="test") == "custom"  # its own count
    rows = files.read_jsonl(files.state_dir() / "experiments.jsonl")
    assert [r["arm"] for r in rows if r["series"] == series.series_key("Space Weekly")] == arms
    assert rows[0]["feature"] == "thumbnail" and rows[0]["video_id"] == "vid00000001"
    assert ("vid00000002", "thumbnail", "auto") in seen


def test_the_arm_of_a_video_never_flips_on_a_second_call():
    item, upload = _posted(1)
    job = SimpleNamespace(job_id="job1")
    first = thumbnail.arm(job, "full", item, upload, _cfg(thumbnail_mode="test"))
    _posted(2)
    assert thumbnail.arm(job, "full", item, upload, _cfg(thumbnail_mode="test")) == first


def test_custom_and_off_modes_and_shorts():
    assert _arm(1, thumbnail_mode="custom") == "custom"
    assert _arm(2, thumbnail_mode="custom") == "custom"
    assert _arm(3, thumbnail_mode="off") == "auto"
    item, upload = _posted(4, kind="short")
    job = SimpleNamespace(job_id="job4")
    assert thumbnail.arm(job, "short", item, upload, _cfg(thumbnail_shorts=False)) == "auto"
    assert thumbnail.arm(job, "short", item, upload, _cfg(thumbnail_shorts=True)) == "custom"
    rows = files.read_jsonl(files.state_dir() / "experiments.jsonl")
    assert all(r["video_id"] != upload.video_id for r in rows)  # Shorts are not in the test


# ---------------------------------------------------------------- the builder


def test_off_enqueues_nothing(tmp_path):
    job = _job(tmp_path)
    item, upload = _posted(1)
    cfg = _cfg(thumbnail_mode="off", thumbnail_shorts=False)
    assert thumbnail.build(job, item, upload, video_kind="full", index=None, cfg=cfg) is None
    s_item, s_upload = _posted(2, kind="short")
    assert thumbnail.build(job, s_item, s_upload, video_kind="short", index=1, cfg=cfg) is None
    assert extras.all_tasks() == []


@needs_ffmpeg
def test_the_auto_arm_enqueues_nothing_and_the_custom_arm_a_picture(tmp_path):
    job = _job(tmp_path)
    cfg = _cfg(thumbnail_mode="test")
    item1, up1 = _posted(1)
    task = thumbnail.build(job, item1, up1, video_kind="full", index=None, cfg=cfg)  # first: custom
    assert task is not None and task.kind == "thumbnail" and Path(task.file).is_file()
    assert datetime.fromisoformat(task.not_before) <= datetime.now(UTC)
    item2, up2 = _posted(2)
    assert thumbnail.build(job, item2, up2, video_kind="full", index=None, cfg=cfg) is None  # second: auto


@needs_ffmpeg
def test_a_short_waits_twenty_minutes_and_only_when_switched_on(tmp_path):
    job = _job(tmp_path, shorts=[_short_row(1)])
    item, upload = _posted(1, kind="short")
    task = thumbnail.build(job, item, upload, video_kind="short", index=1, cfg=_cfg(thumbnail_shorts=True))
    assert task is not None
    wait = datetime.fromisoformat(task.not_before) - datetime.now(UTC)
    assert timedelta(minutes=19) < wait <= timedelta(minutes=20)


# ----------------------------------------------------------------- the runner


def _task(tmp_path, video_kind="full", attempts=0):
    jpg = tmp_path / "t.jpg"
    jpg.write_bytes(b"\xff\xd8x")
    return extras.Task(kind="thumbnail", video_id="vid1", video_kind=video_kind, file=str(jpg), attempts=attempts)


def test_the_runner_sets_the_picture_and_spends_the_units(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ye, "set_thumbnail", lambda vid, jpg: calls.append((vid, str(jpg))) or {})
    out = thumbnail.run(_task(tmp_path))
    assert out.status == optional.DONE and calls and calls[0][0] == "vid1"
    assert quota.today()["spent"] >= 50 if "spent" in quota.today() else True


def test_an_unprocessed_short_is_retried_after_30_minutes_then_2_hours(tmp_path, monkeypatch):
    monkeypatch.setattr(ye, "processing_status", lambda vid: {"processingStatus": "processing"})
    monkeypatch.setattr(ye, "set_thumbnail", lambda *a: pytest.fail("set before processing finished"))
    now = datetime.now(UTC)
    first = thumbnail.run(_task(tmp_path, "short", attempts=0))
    second = thumbnail.run(_task(tmp_path, "short", attempts=1))
    assert first.status == second.status == optional.RETRY
    assert timedelta(minutes=29) < first.retry_at - now < timedelta(minutes=31)
    assert timedelta(hours=1, minutes=59) < second.retry_at - now < timedelta(hours=2, minutes=1)


def test_a_processed_short_gets_its_thumbnail(tmp_path, monkeypatch):
    monkeypatch.setattr(ye, "processing_status", lambda vid: {"processingStatus": "succeeded"})
    monkeypatch.setattr(ye, "set_thumbnail", lambda *a: {})
    assert thumbnail.run(_task(tmp_path, "short")).status == optional.DONE


def test_403_skips_and_blocks_thumbnails_for_a_day(tmp_path, monkeypatch):
    def refuse(*a):
        raise FakeHttpError(403, "forbidden")

    monkeypatch.setattr(ye, "set_thumbnail", refuse)
    out = thumbnail.run(_task(tmp_path))
    assert out.status == optional.SKIPPED and "verifying in Studio" in out.reason
    state = extras._read()
    until = extras._parse(state["blocked"]["thumbnail"])
    assert timedelta(hours=23) < until - datetime.now(UTC) <= timedelta(hours=24)


def test_429_retries_the_next_day(tmp_path, monkeypatch):
    def slow(*a):
        raise FakeHttpError(429)

    monkeypatch.setattr(ye, "set_thumbnail", slow)
    out = thumbnail.run(_task(tmp_path))
    assert out.status == optional.RETRY
    assert out.retry_at - datetime.now(UTC) > timedelta(hours=23)


def test_a_missing_picture_is_skipped(tmp_path):
    task = _task(tmp_path)
    Path(task.file).unlink()
    assert thumbnail.run(task).status == optional.SKIPPED


def _arm_rows(video_id):
    return [r["arm"] for r in files.read_jsonl(files.state_dir() / "experiments.jsonl")
            if r.get("video_id") == video_id and r.get("feature") == "thumbnail"]


def test_a_refused_picture_is_recorded_as_custom_failed_then_custom_when_it_lands(tmp_path, monkeypatch):
    item, upload = _posted(1)
    assert thumbnail.arm(SimpleNamespace(job_id="job1"), "full", item, upload, _cfg(thumbnail_mode="custom")) == "custom"

    def refuse(*a):
        raise FakeHttpError(403, "forbidden")

    monkeypatch.setattr(ye, "set_thumbnail", refuse)
    task = _task(tmp_path)
    task.video_id = upload.video_id
    assert thumbnail.run(task).status == optional.SKIPPED
    assert _arm_rows(upload.video_id) == ["custom", "custom_failed"]
    monkeypatch.setattr(ye, "set_thumbnail", lambda *a: {})
    assert thumbnail.run(task).status == optional.DONE
    assert _arm_rows(upload.video_id) == ["custom", "custom_failed", "custom"]


def test_a_picture_that_cannot_be_made_is_recorded_as_custom_failed(tmp_path, monkeypatch):
    job = _job(tmp_path)
    item, upload = _posted(2)

    def broken(*a, **k):
        raise RuntimeError("bad base frame")

    monkeypatch.setattr(thumbnail, "compose", broken)
    with pytest.raises(RuntimeError):
        thumbnail.build(job, item, upload, video_kind="full", index=None, cfg=_cfg(thumbnail_mode="custom"))
    assert _arm_rows(upload.video_id) == ["custom", "custom_failed"]


def test_the_module_registers_with_the_queue():
    thumbnail.extras.register("thumbnail", thumbnail.build, thumbnail.run)
    assert extras._builders["thumbnail"] is thumbnail.build and extras._runners["thumbnail"] is thumbnail.run


# ------------------------------------------------------------------ composing


def _size(path):
    out = subprocess.run([get_settings().ffprobe_bin, "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return tuple(int(v) for v in out.split(","))


def _lavfi_base(path, size):
    subprocess.run([get_settings().ffmpeg_bin, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"testsrc2=s={size[0]}x{size[1]}:d=1", "-frames:v", "1", str(path)], check=True)


@needs_ffmpeg
def test_compose_makes_a_1280x720_jpeg_under_2_mb(tmp_path):
    base = tmp_path / "b.jpg"
    _lavfi_base(base, (1280, 720))
    out = thumbnail.compose(base, "Caching cuts load time", "Space", thumbnail.PALETTE[0], tmp_path / "o.jpg")
    assert _size(out) == (1280, 720) and 0 < out.stat().st_size < 2 * 1024 * 1024


@needs_ffmpeg
def test_compose_a_short_gets_1080x1920(tmp_path):
    base = tmp_path / "b.jpg"
    _lavfi_base(base, (1080, 1920))
    out = thumbnail.compose(base, "Caching cuts load time", "Space", thumbnail.PALETTE[2], tmp_path / "s.jpg",
                            size=thumbnail.SIZE_SHORT)
    assert _size(out) == (1080, 1920)


@needs_ffmpeg
@pytest.mark.parametrize("label", ['Say "hello": 100% sure', "It's a C:\\new path", "50% off: don't stop"])
def test_quotes_colons_and_percent_signs_render(tmp_path, label):
    base = tmp_path / "b.jpg"
    _lavfi_base(base, (1280, 720))
    plain = thumbnail.compose(base, None, "Space", thumbnail.PALETTE[1], tmp_path / "plain.jpg")
    out = thumbnail.compose(base, label, "Space", thumbnail.PALETTE[1], tmp_path / "o.jpg")
    assert _size(out) == (1280, 720)
    assert out.read_bytes() != plain.read_bytes()  # the band and words were drawn


@needs_ffmpeg
def test_the_card_works_with_no_base(tmp_path):
    out = thumbnail.compose(None, "Sensor drift fix explained", "Robotics", thumbnail.PALETTE[3], tmp_path / "c.jpg")
    assert _size(out) == (1280, 720) and out.stat().st_size < 2 * 1024 * 1024


@needs_ffmpeg
def test_the_label_fits_in_two_lines_at_most():
    font = thumbnail.find_font(bold=True)
    if font is None:
        pytest.skip("no font")
    size, lines = thumbnail.fit(font, "Why every page loads so slowly", 1190)
    assert 64 <= size <= 110 and len(lines) <= 2
    assert all(thumbnail._text_width(font, line, size) <= 1190 for line in lines)
