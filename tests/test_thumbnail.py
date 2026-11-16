"""Thumbnails (publish/thumbnail.py): the label, the chip, the test arms, the
picture and the runner. Synthetic data only; no network."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config import get_settings
from ledger import files
from ledger.uploads import Item, get_ledger
from publish import (
    extras,
    optional,
    quota,
    series,
    thumbart,
    thumbdesign,
    thumbnail,
    youtube,
)
from publish import settings as publishing_settings
from publish import youtube_extra as ye


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


def test_the_auto_arm_enqueues_nothing_and_the_custom_arm_a_picture(tmp_path):
    job = _job(tmp_path)
    cfg = _cfg(thumbnail_mode="test")
    item1, up1 = _posted(1)
    task = thumbnail.build(job, item1, up1, video_kind="full", index=None, cfg=cfg)  # first: custom
    assert task is not None and task.kind == "thumbnail" and Path(task.file).is_file()
    assert datetime.fromisoformat(task.not_before) <= datetime.now(UTC)
    item2, up2 = _posted(2)
    assert thumbnail.build(job, item2, up2, video_kind="full", index=None, cfg=cfg) is None  # second: auto


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


def _jpeg_size(path):
    from PIL import Image

    with Image.open(path) as img:
        return img.size


def _base_jpg(path, size=(1280, 720)):
    """A synthetic stand-in for a slide frame: light, with a few dark lines."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, (236, 238, 230))
    draw = ImageDraw.Draw(img)
    for i in range(14):
        draw.rectangle([60, 40 + i * 38, size[0] // 2, 60 + i * 38], outline=(70, 90, 110))
    img.save(path, "JPEG", quality=90)
    return path


def test_compose_makes_a_1280x720_jpeg_under_2_mb(tmp_path):
    base = _base_jpg(tmp_path / "b.jpg")
    out = thumbnail.compose(base, "Caching cuts load time", "Space", thumbnail.PALETTE[0], tmp_path / "o.jpg")
    assert _jpeg_size(out) == (1280, 720) and 0 < out.stat().st_size < 2 * 1024 * 1024


def test_compose_a_short_gets_1080x1920(tmp_path):
    base = _base_jpg(tmp_path / "b.jpg", (1080, 1920))
    out = thumbnail.compose(base, "Caching cuts load time", "Space", thumbnail.PALETTE[2], tmp_path / "s.jpg",
                            size=thumbnail.SIZE_SHORT)
    assert _jpeg_size(out) == (1080, 1920)
    card = thumbnail.compose(None, "Caching cuts load time", "Space", thumbnail.PALETTE[2], tmp_path / "c.jpg",
                             size=thumbnail.SIZE_SHORT)
    assert _jpeg_size(card) == (1080, 1920)


@pytest.mark.parametrize("label", ['Say "hello": 100% sure', "It's a C:\\new path", "50% off: don't stop"])
def test_quotes_colons_and_percent_signs_render(tmp_path, label):
    base = _base_jpg(tmp_path / "b.jpg")
    plain = thumbnail.compose(base, None, "Space", thumbnail.PALETTE[1], tmp_path / "plain.jpg")
    out = thumbnail.compose(base, label, "Space", thumbnail.PALETTE[1], tmp_path / "o.jpg")
    assert _jpeg_size(out) == (1280, 720)
    assert out.read_bytes() != plain.read_bytes()  # the band and words were drawn


def test_the_frame_is_covered_by_a_solid_band_so_nothing_shows_behind_the_words(tmp_path):
    from PIL import Image

    base = _base_jpg(tmp_path / "b.jpg")
    out = thumbnail.compose(base, "Caching cuts load time", "Space", thumbnail.PALETTE[0], tmp_path / "o.jpg")
    with Image.open(out) as img:
        px = img.convert("RGB")
        top = px.getpixel((900, 100))  # the light slide, untouched
        low = px.getpixel((1000, 710))  # the bottom edge is the dark shade
    assert sum(top) > 600 and sum(low) < 120


def test_the_card_works_with_no_base(tmp_path):
    out = thumbnail.compose(None, "Sensor drift fix explained", "Robotics", thumbnail.PALETTE[3], tmp_path / "c.jpg")
    assert _jpeg_size(out) == (1280, 720) and out.stat().st_size < 2 * 1024 * 1024


def test_a_card_never_has_no_title(tmp_path):
    with pytest.raises(ValueError, match="title"):
        thumbnail.compose(None, "", "Robotics", thumbnail.PALETTE[3], tmp_path / "c.jpg")
    with pytest.raises(ValueError, match="title"):
        thumbart.render_card({"lines": [" ", ""], "chip": "x", "chip_colour": "336699"})


def test_every_illustration_and_template_draws(tmp_path):
    from PIL import Image

    seen = set()
    for art in thumbart.ART_IDS:
        img = thumbart.render_card({"art": art, "lines": ["Short title"], "chip": "Team", "chip_colour": "3A5BA0"})
        assert img.size == (1280, 720)
        seen.add(Image.Image.tobytes(img.crop((780, 100, 1260, 640))))
    assert len(seen) == len(thumbart.ART_IDS)  # each one looks different
    for template in thumbart.TEMPLATES:
        assert thumbart.render_card({"template": template, "lines": ["A title", "in two"], "accent_line": 1,
                                     "subtitle": "and a subtitle"}).size == (1280, 720)
    for palette in thumbart.PALETTES:
        assert thumbart.render_card({"palette": palette, "lines": ["Palette check"]}).size == (1280, 720)


def test_unknown_ids_fall_back_to_the_template_defaults():
    img = thumbart.render_card({"template": "bogus", "art": "nope", "palette": "pink", "lines": ["Fallback ids"]})
    assert img.size == (1280, 720)


def test_the_font_is_found():
    heavy, _thick, regular = thumbart.font_files()
    assert heavy is not None and Path(heavy).is_file() and regular is not None


# the text column ends before the illustration starts
def _column_and_art():
    lay = thumbart._layout((1280, 720))
    return lay["left"] + lay["col_w"], lay["art"][0] + 40  # art content starts about 40 px into its box


@pytest.mark.parametrize("title", [
    "Mapping the universe", "PRD to code overnight", "Offline voice AI", "Moving the session store to Redis now",
    "Infrastructure modernisation roadmap review", "Why every single page loads slowly today"])
def test_the_title_stays_in_its_column_and_off_the_art(title):
    words = [(w, False) for w in title.split()]
    col_edge, art_x = _column_and_art()
    lay = thumbart._layout((1280, 720))
    fitted = thumbart.fit_title(words, lay["col_w"], 400, max_lines=3, size_max=108, size_min=56)
    assert fitted is not None
    size, lines = fitted
    assert len(lines) <= 3 and 56 <= size <= 108
    assert max(thumbart._line_width(line, size) for line in lines) <= lay["col_w"]
    assert col_edge < art_x
    assert thumbart.block_height(size, len(lines)) <= 400


def test_a_long_word_shrinks_the_size_and_a_hopeless_one_is_refused():
    lay = thumbart._layout((1280, 720))
    big = thumbart.fit_title([("Mapping", False)], lay["col_w"], 400, max_lines=3, size_max=108, size_min=56)
    long = thumbart.fit_title([("Infrastructure", False)], lay["col_w"], 400, max_lines=3, size_max=108,
                              size_min=56)
    assert long is not None and long[0] < big[0]
    assert thumbart.fit_title([("W" * 40, False)], lay["col_w"], 400, max_lines=3, size_max=108, size_min=56) is None
    with pytest.raises(ValueError, match="fit"):
        thumbart.render_card({"lines": ["W" * 40]})


def test_the_given_line_breaks_are_kept_when_they_fit():
    lines = [[("PRD", False), ("to", False), ("code", False)], [("overnight", True)]]
    words = [w for line in lines for w in line]
    size, got = thumbart.fit_title(words, 650, 400, max_lines=3, size_max=108, size_min=56, lines=lines)
    assert got == lines and size >= 56


def test_the_key_part_is_picked_when_the_ai_did_not():
    two = thumbart.auto_accent([[("a", False)], [("b", False), ("c", False)]])
    assert [hot for line in two for _w, hot in line] == [False, True, True]
    one = thumbart.auto_accent([[("one", False), ("two", False), ("three", False)]])
    assert [hot for _w, hot in one[0]] == [False, False, True]


def test_pick_art_follows_the_words():
    assert thumbart.pick_art("Offline voice AI on a chip") == "chip_mic"
    assert thumbart.pick_art("Robot arm calibration") == "robot_arm"
    assert thumbart.pick_art("Nothing special here", "seed") in thumbart.ART_IDS
    assert thumbart.pick_art("Nothing special here", "seed") == thumbart.pick_art("Nothing special here", "seed")


def test_unsupported_characters_are_not_drawn():
    assert thumbart.renderable("Caching cuts load time: 80% – done")
    assert not thumbart.renderable("日本語のタイトル")
    assert thumbdesign.default_design("日本語のタイトル") is None


# --------------------------------------------------------------- the AI design


SAID = "we moved the session store to redis and logins are much faster now"


def _said_job(tmp_path, job_id="job1", speakers=()):
    job = _job(tmp_path, job_id)
    rows = [{"speaker": s, "start": 0, "end": 5, "text": SAID} for s in (speakers or ("SPEAKER_00",))]
    (get_settings().jobs_dir / job_id / "segments.json").write_text(json.dumps(rows))
    return job


class FakeUsage:
    def __init__(self):
        self.calls = 0

    def as_dict(self):
        return {"calls": self.calls, "prompt_tokens": 100 * self.calls, "output_tokens": 20 * self.calls,
                "cache_read_tokens": 0, "cache_write_tokens": 0}


class FakeCaller:
    model = "fake-model"

    def __init__(self, answer):
        self.answer, self.usage, self.sent = answer, FakeUsage(), []

    def generate(self, system, contents, schema=None):
        self.usage.calls += 1
        self.sent.append((system, contents, schema))
        if isinstance(self.answer, Exception):
            raise self.answer
        return SimpleNamespace(text=self.answer if isinstance(self.answer, str) else json.dumps(self.answer))


GOOD = {"template": "diagram", "art": "database", "title_lines": ["Session store", "moves to Redis"],
        "accent_line": 1, "subtitle": "logins are much faster", "palette": "violet"}


@pytest.fixture
def costs(monkeypatch):
    seen = []
    from costs import record

    monkeypatch.setattr(record, "record_call", lambda key, usage, **kw: seen.append((key, usage, kw)))
    return seen


def _ask(job, answer, **kw):
    caller = FakeCaller(answer)
    got = thumbdesign.ask(job, title="Moving the session store to Redis", hook="", team="Space", excerpt=SAID,
                          corpus=SAID + " Moving the session store to Redis", cost_key="vid1", job_id="job1",
                          meeting="Space team meeting", caller=caller, **kw)
    return got, caller


def test_the_design_call_returns_a_checked_design_and_records_its_cost(tmp_path, costs):
    got, caller = _ask(_said_job(tmp_path), GOOD)
    assert got == {"template": "diagram", "art": "database", "palette": "violet", "lines": ["Session store",
                   "moves to Redis"], "accent_line": 1, "subtitle": "logins are much faster"}
    assert len(caller.sent) == 1  # ONE call
    _system, contents, schema = caller.sent[0]
    sent = json.loads(contents)
    assert sent["title"] == "Moving the session store to Redis" and sent["team"] == "Space"
    assert "redis" in sent["said"] and set(schema["properties"]) >= {"template", "art", "title_lines", "palette"}
    assert [(key, usage["calls"]) for key, usage, _ in costs] == [("ai:thumbnail:vid1", 1)]
    assert costs[0][2] == {"job_id": "job1", "meeting": "Space team meeting"}


def test_a_json_fence_around_the_answer_is_fine(tmp_path, costs):
    got, _ = _ask(_said_job(tmp_path), "```json\n" + json.dumps(GOOD) + "\n```")
    assert got is not None and got["lines"][0] == "Session store"


@pytest.mark.parametrize("change", [
    {"title_lines": []},
    {"title_lines": ["a", "b", "c", "d"]},
    {"title_lines": "Session store"},
    {"title_lines": ["Session store", 5]},
    {"title_lines": ["Session store moves to redis today"]},  # a line over 24 characters
    {"title_lines": ["Quantum blockchain", "synergy platform"]},  # words nobody said
    {"title_lines": ["Redis cuts costs by 80%"]},  # a number nobody said
    {"title_lines": ["Session <b>store"]},
    {"title_lines": ["日本語"]},
])
def test_a_title_that_fails_a_check_gives_no_design(tmp_path, costs, change):
    got, _caller = _ask(_said_job(tmp_path), {**GOOD, **change})
    assert got is None
    assert costs  # the call still cost something and is recorded


def test_a_title_naming_a_person_or_a_never_post_word_gives_no_design(tmp_path, costs):
    job = _said_job(tmp_path, speakers=["Maria Lopez"])
    got, _ = _ask(job, {**GOOD, "title_lines": ["Maria moves", "the session store"]})
    assert got is None
    corpus = SAID + " investor update"
    caller = FakeCaller({**GOOD, "title_lines": ["Investor update", "session store"]})
    assert thumbdesign.ask(_said_job(tmp_path, "job2"), title="x", hook="", team="", excerpt="", corpus=corpus,
                           caller=caller) is None


def test_a_bad_subtitle_is_dropped_but_a_good_title_stays(tmp_path, costs):
    for subtitle in ("quantum blockchain synergy", "x" * 29, "Maria <b>"):
        got, _ = _ask(_said_job(tmp_path), {**GOOD, "subtitle": subtitle})
        assert got is not None and got["subtitle"] == "" and got["lines"] == ["Session store", "moves to Redis"]


def test_unknown_ids_and_a_bad_accent_fall_back_to_defaults(tmp_path, costs):
    got, _ = _ask(_said_job(tmp_path), {**GOOD, "template": "mystery", "art": "unicorn", "palette": "pink",
                                        "accent_line": 7})
    assert got["art"] in thumbart.ART_IDS and got["template"] in thumbart.TEMPLATES
    assert got["palette"] in thumbart.PALETTES and got["accent_line"] is None


@pytest.mark.parametrize("answer", ["not json", "[1, 2]", RuntimeError("API is down"), '{"title_lines": null}'])
def test_any_failure_gives_none_and_never_raises(tmp_path, costs, answer):
    got, _ = _ask(_said_job(tmp_path), answer)
    assert got is None


def test_no_provider_key_gives_none(tmp_path, monkeypatch):
    from decide import gemini_client

    def boom():
        raise RuntimeError("no key")

    monkeypatch.setattr(gemini_client, "make_caller", boom)
    assert thumbdesign.ask(_said_job(tmp_path), title="x", hook="", team="", excerpt="", corpus=SAID) is None


def test_a_slow_answer_times_out(tmp_path, costs):
    import time

    class Slow(FakeCaller):
        def generate(self, *a, **k):
            time.sleep(1.0)
            return super().generate(*a, **k)

    got = thumbdesign.ask(_said_job(tmp_path), title="x", hook="", team="", excerpt="", corpus=SAID,
                          caller=Slow(GOOD), timeout_s=0.1)
    assert got is None


def test_the_default_design_comes_from_the_topic_words():
    design = thumbdesign.default_design("Offline voice AI on a chip", "IoT")
    assert design["art"] == "chip_mic" and design["template"] == "hardware"
    assert " ".join(design["lines"]) == "Offline voice AI on a chip" and 1 <= len(design["lines"]) <= 3


# ----------------------------------------------------------- the choice of picture


def _thumbs_json(job_id, video="full", index=0, with_file=True):
    folder = get_settings().jobs_dir / job_id / "thumbnails"
    folder.mkdir(parents=True, exist_ok=True)
    name = "thumbnails/base_full_00.jpg"
    if with_file:
        _base_jpg(get_settings().jobs_dir / job_id / name)
    (folder / "thumbs.json").write_text(json.dumps(
        [{"video": video, "index": index, "kind": "share", "file": name if with_file else None}]))


def _posted_titled(n, title, topic="Space Weekly", kind="full"):
    item = Item(f"job:{n}|{kind}", f"job:{n}", kind, f"job{n}", topic, topic=topic)
    get_ledger().begin(item, title=title, privacy="public", by="test")
    return item, get_ledger().published(item.key, f"vid{n:08d}", by="test")


TITLE = "Moving the session store to Redis | FloLabs Space"


@pytest.fixture
def design_ai(monkeypatch, costs):
    """The design call answers GOOD through a fake caller (never a real API)."""
    from decide import gemini_client

    caller = FakeCaller(GOOD)
    monkeypatch.setattr(gemini_client, "make_caller", lambda: caller)
    return caller


def _build(tmp_path, cfg, title=TITLE, with_frame=False, n=1, kind="full", index=None):
    job = _said_job(tmp_path, f"job{n}")
    if with_frame:
        _thumbs_json(f"job{n}")
    item, upload = _posted_titled(n, title)
    return thumbnail.build(job, item, upload, video_kind=kind, index=index, cfg=cfg)


def test_smart_uses_the_real_frame_when_there_is_one(tmp_path, design_ai):
    task = _build(tmp_path, _cfg(thumbnail_mode="custom"), with_frame=True)
    assert task.data["card"] is False and task.data["design"] == "frame" and task.data["label"]
    assert Path(task.file).is_file() and _jpeg_size(task.file) == (1280, 720)
    assert design_ai.sent == []  # no AI call when a frame is used


def test_smart_makes_an_ai_card_without_a_good_frame(tmp_path, design_ai, costs):
    task = _build(tmp_path, _cfg(thumbnail_mode="custom"))
    assert task.state == extras.WAITING and Path(task.file).is_file()
    assert task.data["card"] is True and task.data["design"] == "ai"
    assert task.data["label"] == "Session store moves to Redis" and task.data["art"] == "database"
    assert len(design_ai.sent) == 1 and [c[0] for c in costs] == ["ai:thumbnail:vid00000001"]


def test_a_failed_ai_call_falls_back_to_a_card_from_the_topic_title(tmp_path, monkeypatch, costs):
    from decide import gemini_client

    monkeypatch.setattr(gemini_client, "make_caller", lambda: FakeCaller(RuntimeError("down")))
    task = _build(tmp_path, _cfg(thumbnail_mode="custom"))
    assert task.state == extras.WAITING and task.data["card"] is True and task.data["design"] == "topic"
    assert task.data["label"] == "Moving the session store to Redis" and Path(task.file).is_file()


def test_ai_off_makes_the_card_from_the_title_without_a_call(tmp_path, design_ai):
    task = _build(tmp_path, _cfg(thumbnail_mode="custom", thumbnail_ai=False))
    assert task.data["design"] == "topic" and design_ai.sent == []


def test_frame_only_with_no_frame_leaves_youtubes_own_picture(tmp_path, design_ai):
    task = _build(tmp_path, _cfg(thumbnail_mode="custom", thumbnail_design="frame_only"))
    assert task.state == extras.SKIPPED and task.file is None and design_ai.sent == []
    assert _arm_rows("vid00000001") == ["custom", "custom_failed"]


def test_frame_only_with_a_frame_uses_it(tmp_path, design_ai):
    task = _build(tmp_path, _cfg(thumbnail_mode="custom", thumbnail_design="frame_only"), with_frame=True)
    assert task.data["card"] is False and Path(task.file).is_file()


def test_card_only_ignores_a_good_frame(tmp_path, design_ai):
    task = _build(tmp_path, _cfg(thumbnail_mode="custom", thumbnail_design="card_only"), with_frame=True)
    assert task.data["card"] is True and task.data["design"] == "ai"


def test_no_safe_words_means_no_custom_thumbnail_never_an_empty_card(tmp_path, design_ai):
    titles = ("", "Investor update for the board | FloLabs Space", "日本語のタイトル | FloLabs Space")
    for n, title in enumerate(titles, start=10):
        task = _build(tmp_path, _cfg(thumbnail_mode="custom", thumbnail_ai=False), title=title, n=n)
        assert task.state == extras.SKIPPED and task.file is None, title
        assert task.reason == thumbnail.NO_PICTURE_TEXT


def test_the_label_setting_off_means_no_card(tmp_path, design_ai):
    task = _build(tmp_path, _cfg(thumbnail_mode="custom", thumbnail_label=False))
    assert task.state == extras.SKIPPED and task.file is None


def test_a_title_too_long_to_draw_loses_words_and_a_single_huge_word_is_skipped(tmp_path, design_ai):
    task = _build(tmp_path, _cfg(thumbnail_mode="custom", thumbnail_ai=False),
                     title="Pneumonoultramicroscopicsilicovolcanoconiosis | FloLabs Space", n=3)
    assert task.state == extras.SKIPPED and task.file is None


def test_test_mode_still_gives_half_the_videos_ours_and_half_youtubes(tmp_path, design_ai):
    cfg = _cfg(thumbnail_mode="test")
    first = _build(tmp_path, cfg, n=1)
    second = _build(tmp_path, cfg, n=2)
    assert first is not None and Path(first.file).is_file() and second is None


def test_a_short_card_is_portrait(tmp_path, design_ai):
    job = _said_job(tmp_path, "job1")
    (get_settings().jobs_dir / "job1" / "manifest.json").write_text(json.dumps({"shorts": [_short_row(1)]}))
    item, upload = _posted_titled(1, "Caching cuts load time | FloLabs Space", kind="short")
    task = thumbnail.build(job, item, upload, video_kind="short", index=1, cfg=_cfg(thumbnail_shorts=True))
    assert task is not None and _jpeg_size(task.file) == (1080, 1920)


# ------------------------------------------------------- redoing the empty cards


def _old_empty_card_task(n=1, title=TITLE, label="", card=True, state=extras.DONE, topic="Space Weekly"):
    item, upload = _posted_titled(n, title, topic)
    job = SimpleNamespace(job_id=f"job{n}", id=f"job{n}", title="Space team meeting")
    data = {"arm": "custom", "label": label, **({} if card is None else {"card": card})}  # old tasks have no "card"
    task = extras.Task(kind="thumbnail", video_kind="full", data=data)
    old = get_settings().storage_dir / "old.jpg"
    old.write_bytes(b"old")
    task.file = extras.stash_file(task.id, old, "jpg")
    extras._add(task, job, item, upload, "full", None)
    extras._update(task.id, state=state)
    return task.id, item, upload


def test_an_empty_card_is_redone_with_the_new_rules_and_queued_again(tmp_path, design_ai):
    task_id, _item, _upload = _old_empty_card_task()
    _said_job(tmp_path, "job1")
    assert thumbnail.redo_empty_cards() is True
    task = extras._get(task_id)
    assert task.state == extras.WAITING and task.attempts == 0
    assert task.data["redo"] is True and task.data["label"] and task.data["card"] is True
    assert Path(task.file).read_bytes() != b"old" and _jpeg_size(task.file) == (1280, 720)
    assert thumbnail.redo_empty_cards() is True  # nothing more to do: it is not redone twice
    assert len(design_ai.sent) == 1


def test_a_card_with_a_label_or_not_posted_by_the_app_is_left_alone(tmp_path, design_ai):
    kept, _i, _u = _old_empty_card_task(1, label="Real label")
    gone, item, _upload = _old_empty_card_task(2)
    with get_ledger()._lock:  # the video is no longer a published upload
        get_ledger()._uploads[item.key].state = "failed"
    assert thumbnail.redo_empty_cards() is True
    assert extras._get(kept).state == extras.DONE and extras._get(gone).state == extras.DONE
    assert design_ai.sent == []


def test_a_video_the_ai_cannot_label_is_marked_and_not_retried(tmp_path, design_ai):
    task_id, _i, _u = _old_empty_card_task(1, title="Investor update | FloLabs Space")
    assert thumbnail.redo_empty_cards() is True
    task = extras._get(task_id)
    assert task.state == extras.DONE and task.data["redo"] == "no picture"


def test_a_picture_that_cannot_be_made_is_tried_again_next_time(tmp_path, design_ai, monkeypatch):
    task_id, _i, _u = _old_empty_card_task()
    monkeypatch.setattr(thumbnail, "make_picture", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    assert thumbnail.redo_empty_cards() is False
    assert extras._get(task_id).state == extras.DONE


def test_the_redo_runs_once_after_a_deploy_through_the_queue(tmp_path, design_ai, monkeypatch):
    task_id, _i, _u = _old_empty_card_task()
    runs = []
    monkeypatch.setattr(extras, "_runners", {"thumbnail": thumbnail.run})
    monkeypatch.setattr(extras, "_once", [("thumbnail", "thumbnail_redo_empty_cards",
                                           lambda: runs.append(1) or thumbnail.redo_empty_cards())])
    extras._once_passes()
    extras._once_passes()
    assert runs == [1]
    assert extras._get(task_id).data["redo"] is True
    assert "thumbnail_redo_empty_cards" in extras._read()["once"]


def test_a_failed_once_pass_is_tried_again(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(extras, "_runners", {"thumbnail": thumbnail.run})
    monkeypatch.setattr(extras, "_once", [("thumbnail", "x", lambda: calls.append(1) or len(calls) > 1 or False)])
    extras._once_passes()
    assert "x" not in extras._read()["once"]
    extras._once_passes()
    assert "x" in extras._read()["once"] and calls == [1, 1]


def test_a_long_subtitle_stays_left_of_the_art_and_inside_the_picture():
    from PIL import ImageChops

    long_sub = "Wake word detection with streaming works"
    for size, limit in (((1280, 720), 769), ((1080, 1920), 1080)):
        base = thumbart.render_card({"art": "chip_mic", "lines": ["Voice on chips"]}, size)
        with_sub = thumbart.render_card({"art": "chip_mic", "lines": ["Voice on chips"], "subtitle": long_sub}, size)
        box = ImageChops.difference(base.convert("RGB"), with_sub.convert("RGB")).getbbox()
        assert box is not None and box[2] < limit - (0 if size[0] > size[1] else 40)


def test_art_draws_no_fixed_words():
    import inspect

    src = inspect.getsource(thumbart)
    for word in ("no cloud", '"PRD"', "meeting transcript"):
        assert word not in src


def test_an_old_task_with_no_job_folder_is_an_empty_card_to_redo():
    task = extras.Task(kind="thumbnail", video_kind="full", data={"arm": "custom", "label": ""}, job_id="gone-job")
    task.state = extras.DONE
    assert thumbnail._was_empty_card(task) is True
    task.data = {"arm": "custom", "label": "", "redo": True}  # once only
    assert thumbnail._was_empty_card(task) is False
    task.data = {"arm": "custom", "label": "Real label"}
    assert thumbnail._was_empty_card(task) is False


def test_an_old_empty_card_whose_job_is_gone_is_redone_from_the_ledger_with_an_ai_card(tmp_path, design_ai):
    task_id, _item, upload = _old_empty_card_task(card=None, title=TITLE)
    assert not (get_settings().jobs_dir / "job1").exists()  # the 30 hours are over: no job files at all
    assert thumbnail.redo_empty_cards() is True
    task = extras._get(task_id)
    assert task.state == extras.WAITING and task.attempts == 0 and task.video_id == upload.video_id
    assert task.data["redo"] is True and task.data["card"] is True and task.data["label"]
    assert task.data["design"] == "ai"
    assert Path(task.file).read_bytes() != b"old" and _jpeg_size(task.file) == (1280, 720)
    sent = json.loads(design_ai.sent[0][1])
    assert sent["title"] == TITLE and sent["team"]  # the posted title and the team, nothing else is known
    assert thumbnail.redo_empty_cards() is True and len(design_ai.sent) == 1  # once only


def test_an_old_empty_card_whose_job_is_gone_gets_the_title_card_when_the_ai_is_off(tmp_path, design_ai, monkeypatch):
    task_id, _item, _upload = _old_empty_card_task(card=None, title=TITLE)
    cfg = _cfg(thumbnail_ai=False)
    monkeypatch.setattr(publishing_settings, "current", lambda: cfg)
    assert thumbnail.redo_empty_cards() is True
    task = extras._get(task_id)
    assert design_ai.sent == [] and task.state == extras.WAITING
    assert task.data["card"] is True and task.data["design"] == "topic"
    assert task.data["label"] == "Moving the session store to Redis"  # the topic part of the title, never empty


def test_a_gone_job_still_leaves_unpublished_and_labelled_tasks_alone(tmp_path, design_ai):
    kept, _i, _u = _old_empty_card_task(1, label="Real label", card=None)
    gone, item, _upload = _old_empty_card_task(2, card=None)
    with get_ledger()._lock:
        get_ledger()._uploads[item.key].state = "failed"
    assert thumbnail.redo_empty_cards() is True
    assert extras._get(kept).state == extras.DONE and extras._get(gone).state == extras.DONE
    assert design_ai.sent == []


# ------------------------------------------------------------------- the font


def test_titles_use_montserrat_extra_bold_and_subtitles_semi_bold():
    heavy, thick, regular = thumbart.font_files()
    assert Path(heavy).name == "Montserrat-ExtraBold.ttf" and thick == 0.0
    assert Path(regular).name == "Montserrat-SemiBold.ttf"
    assert (Path(heavy).parent / "OFL.txt").is_file()  # the licence ships with the fonts


def test_without_the_font_files_the_vera_fonts_are_used_and_thickened(monkeypatch, tmp_path):
    monkeypatch.setattr(thumbart, "_ASSET_FONTS", tmp_path / "nothing")
    thumbart.font_files.cache_clear()
    try:
        heavy, thick, regular = thumbart.font_files()
        assert Path(heavy).name == "VeraBd.ttf" and thick > 0 and Path(regular).name == "Vera.ttf"
        assert thumbart.render_card({"lines": ["Still works"]}).size == (1280, 720)
    finally:
        thumbart.font_files.cache_clear()


def test_the_font_files_are_in_the_docker_image():
    root = Path(thumbart.__file__).resolve().parents[1]
    ignored = {line.strip() for line in (root / ".dockerignore").read_text().splitlines()}
    assert not ({"assets", "assets/", "assets/fonts", "*.ttf", "*.txt", "OFL.txt"} & ignored)
    dockerfile = (root / "Dockerfile").read_text()
    assert "COPY --chown=app:app . ." in dockerfile and "Montserrat-ExtraBold.ttf" in dockerfile


@pytest.mark.parametrize("size", [(1280, 720), (1080, 1920)])
@pytest.mark.parametrize("title", [
    "Mapping the universe into one shared 3D platform", "Why every single page loads slowly today and what",
    "Moving the session store to Redis now", "WWWWW MMMMM WWWWW", "ESP32 runs the Pi's power"])
def test_the_wide_font_still_keeps_every_title_in_its_column_and_off_the_art(title, size):
    import numpy as np

    lay = thumbart._layout(size)
    unit = lay["unit"]
    words = [(w, False) for w in title.split()]
    avail = lay["bottom"] - lay["top"] - 36 * unit - (40 * unit + 26 * unit)
    fitted = thumbart.fit_title(words, lay["col_w"], avail, max_lines=3 if size[0] >= size[1] else 4,
                                size_max=108 * unit, size_min=44 * unit)
    assert fitted is not None, title
    tsize, tlines = fitted
    canvas = thumbart.Canvas(size)
    thumbart._draw_title(canvas, lay["left"], 200, tsize, tlines, thumbart.YELLOW)
    ink = np.array(canvas.finish().convert("L")) > 40
    xs = np.nonzero(ink.any(axis=0))[0]
    assert xs.min() >= lay["left"] - 6 and xs.max() + 1 <= lay["left"] + lay["col_w"]
    if size[0] > size[1]:
        assert xs.max() + 1 < lay["art"][0] + 40  # the illustration's content starts about 40 px into its box


@pytest.mark.parametrize(
    "size,label",
    [
        ((1280, 720), "ESP32 runs the Pi's power"),
        ((1280, 720), "Caching cuts load time"),
        ((1080, 1920), "ESP32 runs the Pi's power"),
        ((1080, 1920), "One long label that needs three whole lines on a Short"),
    ],
)
def test_the_frame_chip_sits_in_the_band_above_the_bar_and_label(size, label):
    from publish import thumbart

    w, h = size
    unit = 1.0 if w >= h else 1.45
    words = [(x, False) for x in label.split()]
    fitted = thumbart.fit_title(words, w - (112 if w >= h else 140), 420 * unit, max_lines=2 if w >= h else 3,
                                size_max=96 * unit, size_min=44 * unit)
    lay = thumbart.frame_layout(size, label, "Internet of Things", fitted)
    chip, bar, text, band = lay["chip"], lay["bar"], lay["label"], lay["band"]
    assert chip[1] >= band[1] and chip[0] == text[0] and chip[2] <= w
    assert chip[3] <= bar[1] and bar[3] <= text[1]  # chip, then bar, then label: no overlap
    assert chip[1] > 0 and chip[1] >= h * 0.4  # nothing over the top of the picture
