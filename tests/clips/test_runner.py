"""The runner's whole flow with fake stages and a real store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from clips.config import clips_settings
from clips.models import (
    ClipKind,
    ClipStatus,
    Copy,
    Fetched,
    Moment,
    Platform,
    Rendered,
    RightsBasis,
    RightsVerdict,
    Source,
    SourceVideo,
    Uploaded,
    VideoStatus,
)
from clips.runner import Runner, Stages
from clips.store import Store


class GuardError(RuntimeError):
    """Stands in for clips.guard.GuardError (matched by name)."""


class Fakes:
    """Records calls and lets a test change what the stages do."""

    def __init__(self):
        self.ok = True
        self.kill = False
        self.breaker = False
        self.videos = [
            SourceVideo(id="youtube:v1", source_id="s1", platform=Platform.YOUTUBE, native_id="v1"),
            SourceVideo(id="youtube:v2", source_id="s1", platform=Platform.YOUTUBE, native_id="v2"),
        ]
        self.moments = [
            Moment(start_s=1, end_s=30, kind=ClipKind.SHORT, score=0.9),
            Moment(start_s=40, end_s=70, kind=ClipKind.SHORT, score=0.8, spoiler=True),
            Moment(start_s=80, end_s=110, kind=ClipKind.SHORT, score=0.7, has_music=True),
            Moment(start_s=120, end_s=400, kind=ClipKind.LONG, score=0.6),
        ]
        self.problems: list[str] = []
        self.render_fail_start: float | None = None
        self.fetch_fail: set[str] = set()
        self.upload_error: Exception | None = None
        self.uploaded: list[str] = []
        self.ffmpeg = True
        self.fresh_ok = True  # does check_video pass

    def verdict(self, ok):
        return RightsVerdict(ok=ok, reasons=[] if ok else ["no basis"])

    def stages(self) -> Stages:
        def list_new(source, known, *, limit=15):
            return [v for v in self.videos if v.native_id not in known]

        def fetch(video, dest, *, max_height=1080):
            if video.id in self.fetch_fail:
                raise RuntimeError("download broke")
            return Fetched(video_path=f"/media/{video.native_id}.mp4", width=1920, height=1080)

        def render(src, moment, source, out_dir, *, clip_id, captions=None, credit=""):
            if self.render_fail_start is not None and moment.start_s == self.render_fail_start:
                raise RuntimeError("ffmpeg blew up")
            return Rendered(file=f"{out_dir}/{clip_id}.mp4", duration_s=30, layout="full_width")

        def upload(clip, source, store, *, service=None, dry_run=False):
            if self.upload_error:
                raise self.upload_error
            self.uploaded.append(clip.id)
            return Uploaded(youtube_id=f"yt-{clip.id}", url="https://youtu.be/x", publish_at=clip.publish_at)

        def next_slots(now, booked, settings, first_post, count=1):
            base = now + timedelta(hours=2)
            return [base + timedelta(hours=3 * (len(booked) + i)) for i in range(count)]

        return Stages(
            list_new_videos=list_new,
            fetch=fetch,
            candidate_windows=lambda fetched, *, kind, **kw: [],
            pick_moments=lambda *a, **kw: [m for m in self.moments],
            render_short=render,
            render_long=render,
            write_copy=lambda moment, source, video, *, kind, credit, llm=None: Copy(
                title="t", description="d"
            ),
            check_copy=lambda copy, *, kind, credit: list(self.problems),
            next_slots=next_slots,
            upload_clip=upload,
            check_source=lambda source: self.verdict(self.ok),
            check_video=lambda source, video: self.verdict(self.fresh_ok),
            credit_line=lambda source, video=None: "Source: Demo",
            kill_switch_on=lambda: self.kill,
            cleanup=lambda media_dir, keep_hours: 0,
            ffmpeg_available=lambda: self.ffmpeg,
            breaker_open=lambda store: self.breaker,
        )


@pytest.fixture(autouse=True)
def _downloads_on(_clips_env, monkeypatch):
    """Most runner tests use YouTube sources, which are only watched when downloads are switched on."""
    monkeypatch.setenv("CLIPS_YOUTUBE_DOWNLOADS", "true")
    clips_settings.cache_clear()


@pytest.fixture
def fakes():
    return Fakes()


@pytest.fixture
def store(clips_storage):
    s = Store(clips_storage / "clips.sqlite")
    s.save_source(
        Source(
            id="s1",
            name="Demo",
            platform=Platform.YOUTUBE,
            rights=RightsBasis.OWN,
            evidence_url="https://e.example",
            enabled=True,
        )
    )
    return s


def make_runner(store, fakes) -> Runner:
    return Runner(store, settings=clips_settings(), stages=fakes.stages())


def clips(store, status=None):
    return store.list_clips(status=status)


def test_watch_saves_new_videos_once(store, fakes):
    r = make_runner(store, fakes)
    assert r.watch() == 2
    assert r.watch() == 0
    assert {v.status for v in store.list_videos()} == {VideoStatus.NEW}


def test_watch_skips_video_that_fails_rights(store, fakes):
    fakes.fresh_ok = False
    make_runner(store, fakes).watch()
    videos = store.list_videos()
    assert all(v.status is VideoStatus.SKIPPED and "no basis" in v.skip_reason for v in videos)


def test_watch_ignores_source_failing_rights(store, fakes):
    fakes.ok = False
    assert make_runner(store, fakes).watch() == 0
    assert store.list_videos() == []


def test_watch_survives_a_broken_source(store, fakes):
    stages = fakes.stages()
    stages.list_new_videos = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api down"))
    r = Runner(store, settings=clips_settings(), stages=stages)
    assert r.watch() == 0
    assert any("api down" in e["message"] for e in store.list_events())


def test_process_drops_spoilers_and_music(store, fakes):
    r = make_runner(store, fakes)
    r.watch()
    assert r.process(limit=1) == 2  # one short plus one long; spoiler and music dropped
    made = clips(store)
    assert {c.kind for c in made} == {ClipKind.SHORT, ClipKind.LONG}
    assert all(c.status is ClipStatus.DRAFT for c in made)
    c = made[0]
    assert c.credit == "Source: Demo" and c.rights is RightsBasis.OWN
    assert c.evidence_url == "https://e.example" and c.text.title == "t"
    assert store.get_video("youtube:v1").status is VideoStatus.DONE
    assert store.get_video("youtube:v2").status is VideoStatus.NEW


def test_process_keeps_spoilers_when_source_allows(store, fakes):
    src = store.get_source("s1")
    src.avoid_spoilers = False
    store.save_source(src)
    r = make_runner(store, fakes)
    r.watch()
    assert r.process(limit=1) == 3


def test_process_respects_caps(store, fakes, monkeypatch):
    monkeypatch.setenv("CLIPS_LONG_PER_VIDEO", "0")
    clips_settings.cache_clear()
    r = make_runner(store, fakes)
    r.watch()
    assert r.process(limit=1) == 1
    assert [c.kind for c in clips(store)] == [ClipKind.SHORT]


def test_process_one_bad_moment_keeps_the_others(store, fakes):
    fakes.render_fail_start = 1
    r = make_runner(store, fakes)
    r.watch()
    assert r.process(limit=1) == 1  # the long clip survived
    assert store.get_video("youtube:v1").status is VideoStatus.DONE


def test_process_all_moments_failing_marks_video_failed(store, fakes):
    fakes.moments = fakes.moments[:1]
    fakes.render_fail_start = 1
    r = make_runner(store, fakes)
    r.watch()
    r.process(limit=1)
    video = store.get_video("youtube:v1")
    assert video.status is VideoStatus.FAILED and "ffmpeg blew up" in video.error


def test_process_failed_fetch_goes_on_to_next(store, fakes):
    fakes.fetch_fail = {"youtube:v1"}
    r = make_runner(store, fakes)
    r.watch()
    r.process(limit=2)
    assert store.get_video("youtube:v1").status is VideoStatus.FAILED
    assert store.get_video("youtube:v2").status is VideoStatus.DONE


def test_process_missing_ffmpeg_stops_video(store, fakes):
    fakes.ffmpeg = False
    r = make_runner(store, fakes)
    r.watch()
    r.process(limit=1)
    video = store.get_video("youtube:v1")
    assert video.status is VideoStatus.FAILED and "ffmpeg" in video.error
    assert clips(store) == []


def test_process_rechecks_rights(store, fakes):
    r = make_runner(store, fakes)
    r.watch()
    fakes.ok = False
    assert r.process(limit=2) == 0
    assert {v.status for v in store.list_videos()} == {VideoStatus.SKIPPED}


def test_process_rechecks_video_rights(store, fakes):
    r = make_runner(store, fakes)
    r.watch()
    fakes.fresh_ok = False
    r.process(limit=1)
    assert store.get_video("youtube:v1").status is VideoStatus.SKIPPED


def test_copy_problems_land_in_error_field(store, fakes):
    fakes.problems = ["title too long"]
    r = make_runner(store, fakes)
    r.watch()
    r.process(limit=1)
    assert all(c.error == "title too long" and c.status is ClipStatus.DRAFT for c in clips(store))


def test_full_flow_with_review(store, fakes):
    r = make_runner(store, fakes)
    r.watch()
    r.process(limit=1)
    assert r.schedule() == 0  # nothing approved yet
    first, second = clips(store)
    r.approve(first.id)
    r.reject(second.id, "boring")
    assert store.get_clip(second.id).reject_reason == "boring"
    assert r.schedule() == 1
    scheduled = store.get_clip(first.id)
    assert scheduled.status is ClipStatus.SCHEDULED and scheduled.publish_at
    assert r.upload_due() == 1
    done = store.get_clip(first.id)
    assert done.status is ClipStatus.UPLOADED and done.youtube_id == f"yt-{first.id}"
    assert r.upload_due() == 0


def test_review_off_schedules_drafts(store, fakes, monkeypatch):
    monkeypatch.setenv("CLIPS_REVIEW_REQUIRED", "false")
    clips_settings.cache_clear()
    r = make_runner(store, fakes)
    r.watch()
    r.process(limit=1)
    assert r.schedule() == 2
    assert {c.status for c in clips(store)} == {ClipStatus.SCHEDULED}


def test_review_off_still_holds_back_drafts_with_problems(store, fakes, monkeypatch):
    monkeypatch.setenv("CLIPS_REVIEW_REQUIRED", "false")
    clips_settings.cache_clear()
    fakes.problems = ["bad"]
    r = make_runner(store, fakes)
    r.watch()
    r.process(limit=1)
    assert r.schedule() == 0


def test_schedule_holds_clip_when_rights_gone(store, fakes):
    r = make_runner(store, fakes)
    r.watch()
    r.process(limit=1)
    c = clips(store)[0]
    r.approve(c.id)
    fakes.ok = False
    assert r.schedule() == 0
    held = store.get_clip(c.id)
    assert held.status is ClipStatus.HELD and "no basis" in held.error


def test_approve_refused_when_rights_fail_or_not_draft(store, fakes):
    r = make_runner(store, fakes)
    r.watch()
    r.process(limit=1)
    c = clips(store)[0]
    fakes.ok = False
    with pytest.raises(ValueError, match="cannot approve"):
        r.approve(c.id)
    fakes.ok = True
    r.approve(c.id)
    with pytest.raises(ValueError, match="only a draft"):
        r.approve(c.id)
    with pytest.raises(KeyError):
        r.approve("nope")


def _scheduled_clip(store, fakes, hours_ahead=1):
    r = make_runner(store, fakes)
    r.watch()
    r.process(limit=1)
    c = clips(store)[0]
    r.approve(c.id)
    r.schedule()
    store.set_clip_status(c.id, ClipStatus.SCHEDULED, publish_at=datetime.now(UTC) + timedelta(hours=hours_ahead))
    return r, c


def test_upload_only_when_due(store, fakes):
    r, c = _scheduled_clip(store, fakes, hours_ahead=72)
    assert r.upload_due() == 0
    assert store.get_clip(c.id).status is ClipStatus.SCHEDULED


def test_upload_guard_error_holds_clip(store, fakes):
    r, c = _scheduled_clip(store, fakes)
    fakes.upload_error = GuardError("wrong channel")
    assert r.upload_due() == 0
    held = store.get_clip(c.id)
    assert held.status is ClipStatus.HELD and "wrong channel" in held.error


def test_upload_other_error_stays_scheduled_and_counts(store, fakes):
    r, c = _scheduled_clip(store, fakes)
    fakes.upload_error = RuntimeError("503")
    seen = []
    r.stages.note_upload_result = lambda store, ok, detail="": seen.append((ok, detail))
    assert r.upload_due() == 0
    assert store.get_clip(c.id).status is ClipStatus.SCHEDULED
    assert seen == [(False, "503")]


def test_upload_stops_when_breaker_open(store, fakes):
    r, _c = _scheduled_clip(store, fakes)
    fakes.breaker = True
    assert r.upload_due() == 0
    assert fakes.uploaded == []


def test_tick_kill_switch_does_nothing(store, fakes):
    fakes.kill = True
    r = make_runner(store, fakes)
    assert r.tick() == {"killed": True}
    assert store.list_videos() == []


def test_tick_runs_every_step(store, fakes):
    result = make_runner(store, fakes).tick()
    assert result["watched"] == 2 and result["processed"] == 2
    assert result["scheduled"] == 0 and result["uploaded"] == 0


def test_tick_never_raises_when_a_stage_fails(store, fakes):
    stages = fakes.stages()
    stages.list_new_videos = lambda *a, **k: 1 / 0
    stages.cleanup = lambda *a, **k: 1 / 0
    stages.next_slots = lambda *a, **k: 1 / 0
    r = Runner(store, settings=clips_settings(), stages=stages)
    result = r.tick()
    assert result["watched"] == 0  # the broken source was logged, not fatal
    # a step that raises is caught too
    r.watch = lambda: 1 / 0
    assert "watched" in r.tick()["errors"]


def test_status_snapshot(store, fakes):
    r = make_runner(store, fakes)
    r.watch()
    r.process(limit=1)
    st = r.status()
    assert st["clips"]["draft"] == 2 and st["clips"]["uploaded"] == 0
    assert st["sources"] == {"enabled": 1, "disabled": 0}
    assert len(st["next_slots"]) == 3
    assert st["kill_switch"] is False and st["breaker_open"] is False
    assert st["channel"] is None and st["events"]
    fakes.kill = True
    assert r.status()["kill_switch"] is True


# ------------------------------------------------------------ review fixes


def _drafts(store, fakes):
    r = make_runner(store, fakes)
    r.watch()
    r.process(limit=1)
    return r, clips(store)


def _switch_off(store):
    src = store.get_source("s1")
    src.enabled = False
    store.save_source(src)


def _age_clip(store, clip_id, **fields):
    """Rewrite a clip's stored record directly (the store always stamps updated_at itself)."""
    data = store.get_clip(clip_id).model_copy(update=fields)
    with store._conn() as conn:
        conn.execute("UPDATE clips SET json = ? WHERE id = ?", (data.model_dump_json(), clip_id))


def test_disabled_source_makes_no_clips_and_keeps_its_video_new(store, fakes):
    r = make_runner(store, fakes)
    r.watch()
    _switch_off(store)
    assert r.process(limit=5) == 0
    assert clips(store) == []
    assert {v.status for v in store.list_videos()} == {VideoStatus.NEW}
    src = store.get_source("s1")
    src.enabled = True
    store.save_source(src)
    assert r.process(limit=1) > 0  # resumes once it is switched on again


def test_disabled_source_does_not_block_other_sources(store, fakes):
    store.save_source(
        Source(id="s2", name="Other", platform=Platform.YOUTUBE, rights=RightsBasis.OWN,
               evidence_url="https://e.example", enabled=True)
    )
    store.save_video(SourceVideo(id="youtube:w1", source_id="s2", platform=Platform.YOUTUBE, native_id="w1"))
    r = make_runner(store, fakes)
    r.watch()
    _switch_off(store)
    assert r.process(limit=1) > 0
    assert store.get_video("youtube:w1").status is VideoStatus.DONE


def test_approve_and_schedule_refuse_a_disabled_source(store, fakes):
    r, made = _drafts(store, fakes)
    r.approve(made[0].id)
    _switch_off(store)
    with pytest.raises(ValueError, match="switched off"):
        r.approve(made[1].id)
    assert r.schedule() == 0
    assert store.get_clip(made[0].id).status is ClipStatus.HELD


def test_signals_are_computed_and_passed_to_the_picker(store, fakes):
    stages = fakes.stages()
    calls = []
    stages.loudness_curve = lambda path, **kw: calls.append(("loud", str(path))) or [(0.0, 0.5)]
    stages.scene_changes = lambda path, **kw: calls.append(("scenes", str(path))) or [4.0]
    seen = []
    stages.candidate_windows = lambda fetched, *, kind, **kw: seen.append(kw) or []
    r = Runner(store, settings=clips_settings(), stages=stages)
    r.watch()
    r.process(limit=1)
    assert {c[0] for c in calls} == {"loud", "scenes"}
    assert seen and all(kw["loudness"] == [(0.0, 0.5)] and kw["scenes"] == [4.0] for kw in seen)


def test_a_failing_signal_does_not_lose_the_video(store, fakes):
    stages = fakes.stages()
    stages.loudness_curve = lambda path, **kw: 1 / 0
    r = Runner(store, settings=clips_settings(), stages=stages)
    r.watch()
    assert r.process(limit=1) > 0


def test_a_global_problem_leaves_every_clip_scheduled(store, fakes):
    r, c = _scheduled_clip(store, fakes)
    r.stages.upload_preflight = lambda: "No YouTube channel is connected."
    assert r.upload_due() == 0
    assert store.get_clip(c.id).status is ClipStatus.SCHEDULED
    assert fakes.uploaded == []
    assert len([e for e in store.list_events() if "waiting" in e["message"]]) == 1
    r.upload_due()
    assert len([e for e in store.list_events() if "waiting" in e["message"]]) == 1  # said once
    r.stages.upload_preflight = lambda: ""
    assert r.upload_due() == 1


def test_default_preflight_checks_isolation_channel_and_company_channel(monkeypatch):
    import sys
    import types

    from clips.runner import _default_preflight

    guard = types.ModuleType("clips.guard")
    up = types.ModuleType("clips.uploader")

    def check_channel(cid):
        if cid == "UCco":
            raise GuardError("company channel")

    guard.assert_isolated = lambda: None
    guard.check_channel = check_channel
    up.connected_channel = lambda: None
    monkeypatch.setitem(sys.modules, "clips.guard", guard)
    monkeypatch.setitem(sys.modules, "clips.uploader", up)
    assert "No YouTube channel" in _default_preflight()
    up.connected_channel = lambda: ("UCco", "Company")
    assert "company channel" in _default_preflight()
    up.connected_channel = lambda: ("UCclips", "Clips")
    assert _default_preflight() == ""

    def not_isolated():
        raise GuardError("wrong folder")

    guard.assert_isolated = not_isolated
    assert _default_preflight() == "wrong folder"


def test_kill_or_breaker_during_the_loop_leaves_the_clip_scheduled(store, fakes):
    r, c = _scheduled_clip(store, fakes)

    def upload(clip, source, store_, **kw):
        fakes.kill = True
        raise GuardError("The kill switch is on.")

    r.stages.upload_clip = upload
    assert r.upload_due() == 0
    assert store.get_clip(c.id).status is ClipStatus.SCHEDULED


def test_a_clip_busy_in_another_process_is_skipped_quietly(store, fakes):
    class ClipBusy(RuntimeError):
        pass

    r, c = _scheduled_clip(store, fakes)

    def busy(*a, **k):
        raise ClipBusy("taken")

    r.stages.upload_clip = busy
    seen = []
    r.stages.note_upload_result = lambda store_, ok, detail="": seen.append(ok)
    assert r.upload_due() == 0
    assert seen == [] and store.get_clip(c.id).status is ClipStatus.SCHEDULED


def test_release_puts_a_held_clip_back_through_the_checks(store, fakes):
    r, c = _scheduled_clip(store, fakes)
    fakes.upload_error = GuardError("wrong channel")
    r.upload_due()
    assert store.get_clip(c.id).status is ClipStatus.HELD
    fakes.ok = False
    with pytest.raises(ValueError, match="cannot release"):
        r.release(c.id)
    fakes.ok = True
    r.stages.check_clip = lambda clip, source: RightsVerdict(ok=False, reasons=["evidence changed"])
    with pytest.raises(ValueError, match="evidence changed"):
        r.release(c.id)
    r.stages.check_clip = lambda clip, source: RightsVerdict(ok=True)
    out = r.release(c.id)
    assert out.status is ClipStatus.APPROVED and out.publish_at is None and out.error == ""
    with pytest.raises(ValueError, match="only a held"):
        r.release(c.id)
    with pytest.raises(KeyError):
        r.release("nope")
    fakes.upload_error = None
    assert r.schedule() == 1  # and it gets a new slot


def test_an_overdue_slot_is_booked_again_never_posted_late(store, fakes):
    r, c = _scheduled_clip(store, fakes)
    store.set_clip_status(c.id, ClipStatus.SCHEDULED, publish_at=datetime.now(UTC) - timedelta(hours=30))
    assert r.upload_due() == 0 and fakes.uploaded == []
    assert r.schedule() == 1
    fresh = store.get_clip(c.id)
    assert fresh.status is ClipStatus.SCHEDULED and fresh.publish_at > datetime.now(UTC)


def test_a_cut_off_upload_is_picked_up_again(store, fakes):
    r, c = _scheduled_clip(store, fakes)
    store.set_clip_status(c.id, ClipStatus.UPLOADING)
    assert r.upload_due() == 0  # just started elsewhere: left alone
    _age_clip(store, c.id, updated_at=datetime.now(UTC) - timedelta(hours=5))
    assert r.upload_due() == 1


def test_a_video_cut_off_mid_work_goes_back_to_new(store, fakes):
    r = make_runner(store, fakes)
    r.watch()
    v = store.get_video("youtube:v1")
    v.status = VideoStatus.PICKED
    store.save_video(v)
    r.process(limit=0)
    assert store.get_video("youtube:v1").status is VideoStatus.NEW


def test_a_video_cut_off_after_some_clips_is_failed_not_redone(store, fakes):
    r, _made = _drafts(store, fakes)
    v = store.get_video("youtube:v1")
    v.status = VideoStatus.PICKED
    store.save_video(v)
    r.process(limit=0)
    after = store.get_video("youtube:v1")
    assert after.status is VideoStatus.FAILED and "interrupted" in after.error


def test_a_video_being_worked_on_is_not_taken_twice(store, fakes):
    r = make_runner(store, fakes)
    r.watch()
    with r._claimed("video", "youtube:v1") as got:
        assert got
        assert r.process(limit=1) > 0  # v1 skipped, v2 done by this pass
        assert store.get_video("youtube:v1").status is VideoStatus.NEW
        assert store.get_video("youtube:v2").status is VideoStatus.DONE
        with r._claimed("video", "youtube:v1") as again:
            assert not again
    with r._claimed("video", "youtube:v1") as free:
        assert free


def test_a_dead_process_claim_expires(store, fakes):
    import os

    r = make_runner(store, fakes)
    with r._claimed("clip", "x") as got:
        assert got
        os.utime(r._claim_path("clip", "x"), (1, 1))
        with r._claimed("clip", "x") as retaken:
            assert retaken


def test_production_waits_while_the_backlog_covers_the_ready_days(store, fakes):
    from clips.config import ClipsSettings

    class Capped(ClipsSettings):
        max_ready_days: int = 1

    r = Runner(store, settings=Capped(), stages=fakes.stages())
    r.stages.allowance_on = lambda day, first, settings: 2
    r.watch()
    assert r.process(limit=1) == 2  # room for 2, none waiting yet
    assert r.process(limit=1) == 0  # 2 drafts already cover the day
    assert store.get_video("youtube:v2").status is VideoStatus.NEW


def test_production_stops_on_a_nearly_full_disk(store, fakes, monkeypatch):
    import clips.runner as runner_mod

    monkeypatch.setattr(runner_mod, "_free_bytes", lambda path: 10 * 1024**2)
    r = make_runner(store, fakes)
    r.watch()
    assert r.process(limit=2) == 0 and clips(store) == []
    assert any("free disk" in e["message"] for e in store.list_events())
    r.process(limit=2)
    assert len([e for e in store.list_events() if "free disk" in e["message"]]) == 1


def test_cleanup_deletes_renders_of_old_rejected_and_uploaded_clips(store, fakes, clips_storage):
    r, made = _drafts(store, fakes)
    renders = clips_storage / "renders"
    renders.mkdir(exist_ok=True)
    outside = clips_storage / "keep.mp4"
    outside.write_bytes(b"x")
    a, b = made[0].id, made[1].id
    files = {}
    for cid in (a, b):
        f = renders / f"{cid}.mp4"
        f.write_bytes(b"x")
        files[cid] = f
    store.set_clip_status(a, ClipStatus.REJECTED, file=str(files[a]))
    _age_clip(store, a, updated_at=datetime.now(UTC) - timedelta(days=10))
    store.set_clip_status(
        b, ClipStatus.UPLOADED, file=str(files[b]), publish_at=datetime.now(UTC) - timedelta(days=10)
    )
    assert r._cleanup_renders() == 2
    assert not files[a].exists() and not files[b].exists() and outside.exists()
    assert store.get_clip(a).file == "" and store.get_clip(b).file == ""


def test_cleanup_never_deletes_outside_the_renders_folder(store, fakes, clips_storage):
    r, made = _drafts(store, fakes)
    outside = clips_storage / "keep.mp4"
    outside.write_bytes(b"x")
    store.set_clip_status(made[0].id, ClipStatus.REJECTED, file=str(outside))
    _age_clip(store, made[0].id, updated_at=datetime.now(UTC) - timedelta(days=10))
    r._cleanup_renders()
    assert outside.exists()


def test_recent_rejected_clip_keeps_its_file(store, fakes, clips_storage):
    r, made = _drafts(store, fakes)
    renders = clips_storage / "renders"
    renders.mkdir(exist_ok=True)
    f = renders / "r.mp4"
    f.write_bytes(b"x")
    store.set_clip_status(made[0].id, ClipStatus.REJECTED, file=str(f))
    assert r._cleanup_renders() == 0 and f.exists()


def test_tick_writes_a_heartbeat(store, fakes, clips_storage):
    make_runner(store, fakes).tick()
    assert (clips_storage / "last_tick").exists()
    (clips_storage / "last_tick").unlink()
    fakes.kill = True
    make_runner(store, fakes).tick()
    assert (clips_storage / "last_tick").exists()  # a stopped loop is still alive


def test_status_leaves_out_the_channel_when_not_isolated(store, fakes):
    stages = fakes.stages()
    asked = []
    stages.connected_channel = lambda: asked.append(1) or ("UCx", "X")
    r = Runner(store, settings=clips_settings(), stages=stages)
    out = r.status()
    assert out["channel"] == {"id": "UCx", "title": "X"} and out["isolated"] is True
    asked.clear()

    def not_isolated():
        raise GuardError("company folder")

    stages.assert_isolated = not_isolated
    out = r.status()
    assert out["channel"] is None and out["isolated"] is False and asked == []


# ------------------------------------------------ four later changes


def _youtube_off(monkeypatch):
    monkeypatch.setenv("CLIPS_YOUTUBE_DOWNLOADS", "false")
    clips_settings.cache_clear()


def test_watch_skips_youtube_sources_while_downloads_are_off_and_says_so_once_a_day(store, fakes, monkeypatch):
    _youtube_off(monkeypatch)
    r = make_runner(store, fakes)
    assert r.watch() == 0 and r.watch() == 0 and r.watch() == 0
    assert store.list_videos() == []
    notes = [e for e in store.list_events(kind="watch") if "YOUTUBE_DOWNLOADS" in e["message"]]
    assert len(notes) == 1 and notes[0]["source_id"] == "s1"


def test_the_off_note_comes_again_the_next_day(store, fakes, monkeypatch):
    _youtube_off(monkeypatch)
    r = make_runner(store, fakes)
    r.watch()
    store.set_flag("yt_off:s1", "2000-01-01")
    r.watch()
    assert len([e for e in store.list_events(kind="watch") if "YOUTUBE_DOWNLOADS" in e["message"]]) == 2


def test_folder_sources_are_watched_while_downloads_are_off(store, fakes, monkeypatch, tmp_path):
    _youtube_off(monkeypatch)
    store.save_source(Source(id="f1", name="Folder", platform=Platform.FOLDER, rights=RightsBasis.OWN,
                             folder=str(tmp_path), enabled=True))
    r = make_runner(store, fakes)
    r.watch()
    assert store.list_videos(source_id="s1") == []
    assert len(store.list_videos(source_id="f1")) == 2  # the fake lists two videos for any source


def test_status_says_downloads_are_off_for_each_youtube_source(store, fakes, monkeypatch):
    _youtube_off(monkeypatch)
    st = make_runner(store, fakes).status()
    assert st["youtube_downloads"] is False
    assert [s["id"] for s in st["youtube_sources_off"]] == ["s1"]
    assert "CLIPS_YOUTUBE_DOWNLOADS" in st["youtube_sources_off"][0]["note"]


def test_status_has_no_download_notes_when_downloads_are_on(store, fakes):
    st = make_runner(store, fakes).status()
    assert st["youtube_downloads"] is True and st["youtube_sources_off"] == []


def test_a_bad_release_date_setting_stops_watching_with_one_event_and_tick_still_works(store, fakes, monkeypatch):
    monkeypatch.setenv("CLIPS_NOT_BEFORE", "soon")
    clips_settings.cache_clear()
    r = make_runner(store, fakes)
    result = r.tick()
    assert result["watched"] == 0 and "errors" not in result
    r.tick()
    notes = [e for e in store.list_events(kind="watch") if "CLIPS_NOT_BEFORE" in e["message"]]
    assert len(notes) == 1 and "YYYY-MM-DD" in notes[0]["message"]


def _scheduled(store, fakes, minutes):
    r = make_runner(store, fakes)
    r.watch()
    r.process(limit=1)
    clip = clips(store)[0]
    r.approve(clip.id)
    store.set_clip_status(
        clip.id, ClipStatus.SCHEDULED, publish_at=datetime.now(UTC) + timedelta(minutes=minutes)
    )
    return r, clip


@pytest.mark.parametrize("minutes", [-30, 3, 14])
def test_a_slot_passed_or_under_fifteen_minutes_away_goes_back_to_approved(store, fakes, minutes):
    r, clip = _scheduled(store, fakes, minutes)
    assert r.upload_due() == 0
    back = store.get_clip(clip.id)
    assert back.status is ClipStatus.APPROVED and back.publish_at is None
    assert fakes.uploaded == []
    assert any(clip.id in e["message"] and "new slot" in e["message"] for e in store.list_events(kind="upload"))


def test_the_next_schedule_step_gives_the_late_clip_a_new_slot_even_with_review_off(store, fakes, monkeypatch):
    monkeypatch.setenv("CLIPS_REVIEW_REQUIRED", "false")
    clips_settings.cache_clear()
    r, clip = _scheduled(store, fakes, 3)
    r.upload_due()
    assert r.schedule() >= 1
    again = store.get_clip(clip.id)
    assert again.status is ClipStatus.SCHEDULED and again.publish_at > datetime.now(UTC) + timedelta(hours=1)


def test_a_slot_over_fifteen_minutes_away_is_uploaded(store, fakes):
    r, clip = _scheduled(store, fakes, 60)
    assert r.upload_due() == 1 and fakes.uploaded == [clip.id]


def test_a_not_scheduled_refusal_from_the_uploader_sends_the_clip_back_without_counting(store, fakes):
    class NotScheduledError(RuntimeError):
        pass

    r, clip = _scheduled(store, fakes, 60)
    fakes.upload_error = NotScheduledError("no publish time")
    counted = []
    stages = fakes.stages()
    stages.note_upload_result = lambda s, ok, detail="": counted.append(ok)
    r = Runner(store, settings=clips_settings(), stages=stages)
    assert r.upload_due() == 0
    assert store.get_clip(clip.id).status is ClipStatus.APPROVED and counted == []


# ------------------------------------------------ the language model is connected


def test_runner_asks_the_stages_for_a_model_and_hands_it_to_pick_and_copy(store, fakes):
    sentinel = object()
    stages = fakes.stages()
    stages.make_llm = lambda: sentinel
    seen = {}

    def pick(*a, **kw):
        seen["pick"] = kw.get("llm")
        return [Moment(start_s=1, end_s=30)]

    def copy(m, s, v, *, kind, credit, llm=None):
        seen["copy"] = llm
        return Copy(title="t", description="d")

    stages.pick_moments = pick
    stages.write_copy = copy
    r = Runner(store, settings=clips_settings(), stages=stages)
    r.watch()
    r.process(limit=1)
    assert seen == {"pick": sentinel, "copy": sentinel}


def test_a_model_passed_in_wins_over_the_default(store, fakes):
    stages = fakes.stages()
    stages.make_llm = lambda: "default"
    assert Runner(store, llm="mine", settings=clips_settings(), stages=stages).llm == "mine"


def _with_key(monkeypatch):
    from core.config import get_settings

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    get_settings.cache_clear()
    return get_settings


def test_default_runner_with_an_app_key_has_a_model(store, monkeypatch):
    from clips.llm import AppLLM

    get_settings = _with_key(monkeypatch)
    try:
        assert isinstance(Runner(store).llm, AppLLM)  # default stages, nothing passed in
    finally:
        get_settings.cache_clear()


def test_default_runner_without_a_key_has_no_model(store):
    assert Runner(store).llm is None


def test_no_model_is_built_off_the_clips_folder(store, monkeypatch, tmp_path):
    """Off the clips folder the cost meter would write into the company's storage: no model at all."""
    get_settings = _with_key(monkeypatch)
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "company-storage"))
    get_settings.cache_clear()
    try:
        assert Runner(store).llm is None
    finally:
        get_settings.cache_clear()


def test_the_command_line_and_the_api_runners_get_the_model(monkeypatch, clips_storage):
    from clips import __main__ as cli
    from clips import api as clips_api
    from clips.llm import AppLLM

    get_settings = _with_key(monkeypatch)
    monkeypatch.setattr(clips_api, "_store", None)
    monkeypatch.setattr(clips_api, "_runner", None)
    try:
        assert isinstance(cli._runner().llm, AppLLM)
        assert isinstance(clips_api.get_runner().llm, AppLLM)
    finally:
        get_settings.cache_clear()


# ---------------------------------------- a stop reaches videos that are already up


def _uploaded_waiting(store, fakes, hours=3):
    _r, clip = _scheduled(store, fakes, 60)
    store.set_clip_status(
        clip.id, ClipStatus.UPLOADED, youtube_id="vid1", publish_at=datetime.now(UTC) + timedelta(hours=hours)
    )
    return clip


def test_tick_with_the_kill_switch_on_pulls_back_what_is_already_uploaded(store, fakes):
    clip = _uploaded_waiting(store, fakes)
    fakes.kill = True
    stages = fakes.stages()
    calls = []
    stages.pull_back = lambda s: calls.append(1) or {"pulled_back": [clip.id], "failed": [], "skipped": ""}
    result = Runner(store, settings=clips_settings(), stages=stages).tick()
    assert calls and result["killed"] is True and result["pulled_back"] == 1
    assert "watched" not in result  # and nothing else ran


def test_a_kill_file_stops_a_clip_going_public_through_the_real_guard(store, fakes, monkeypatch):
    """README step 12's `touch KILL` path: no button, no CLI, only the file."""
    from clips import guard, uploader

    clip = _uploaded_waiting(store, fakes)
    clips_settings().kill_path.write_text("", encoding="utf-8")
    pulled = []

    def fake_pull_back(st, service=None):
        pulled.extend(c.id for c in guard.pending_releases(st))
        return [clip.id], []

    monkeypatch.setattr(uploader, "pull_back_pending", fake_pull_back)
    stages = fakes.stages()
    stages.kill_switch_on = guard.kill_switch_on
    stages.pull_back = guard.pull_back_now
    result = Runner(store, settings=clips_settings(), stages=stages).tick()
    assert result["killed"] is True and pulled == [clip.id]
    assert any(e["kind"] == "kill" for e in store.list_events())


def test_a_clip_that_finishes_uploading_after_stop_was_pressed_is_pulled_back(store, fakes):
    _r, clip = _scheduled(store, fakes, 60)
    stages = fakes.stages()
    pulls = []

    def upload(c, source, st, *, service=None, dry_run=False):
        fakes.kill = True  # Stop is pressed while the bytes go up
        return Uploaded(youtube_id="vid9", url="u", publish_at=c.publish_at)

    def pull_back(s):
        pulls.append(s.get_clip(clip.id).status)
        return {"pulled_back": [clip.id], "failed": [], "skipped": ""}

    stages.upload_clip = upload
    stages.pull_back = pull_back
    assert Runner(store, settings=clips_settings(), stages=stages).upload_due() == 1
    assert pulls == [ClipStatus.UPLOADED]  # asked after the clip was recorded as uploaded


def test_no_pull_back_after_an_upload_when_the_switch_is_off(store, fakes):
    _scheduled(store, fakes, 60)
    stages = fakes.stages()
    stages.pull_back = lambda s: pytest.fail("nothing to pull back")
    assert Runner(store, settings=clips_settings(), stages=stages).upload_due() == 1


def test_tick_does_nothing_when_not_on_the_clips_folder(store, fakes):
    stages = fakes.stages()

    def refuse():
        raise GuardError("STORAGE_DIR is the company folder")

    stages.assert_isolated = refuse
    made = []
    stages.make_llm = lambda: made.append(1)
    result = Runner(store, settings=clips_settings(), stages=stages).tick()
    assert "company folder" in result["blocked"] and "watched" not in result
    assert store.list_videos() == [] and made == []


# ------------------------------------------------------------ auto-approve


def _auto_runner(store, fakes, on=True):
    r = make_runner(store, fakes)
    if on:
        r.set_auto_approve(True)
    return r


def _kinds(store, status):
    return sorted(c.kind.value for c in clips(store, status))


def test_auto_approve_is_off_by_default(store, fakes):
    r = make_runner(store, fakes)
    assert r.auto_approve_on() is False
    r.watch()
    r.process(limit=1)
    assert {c.status for c in clips(store)} == {ClipStatus.DRAFT}
    assert r.status()["auto_approve"] is False


def test_the_flag_persists_and_wins_over_the_setting(store, fakes, monkeypatch):
    make_runner(store, fakes).set_auto_approve(True)
    assert make_runner(store, fakes).auto_approve_on() is True  # a new Runner object sees it
    monkeypatch.setenv("CLIPS_AUTO_APPROVE", "true")
    clips_settings.cache_clear()
    other = Store(store.path)
    assert make_runner(other, fakes).auto_approve_on() is True
    make_runner(store, fakes).set_auto_approve(False)  # the flag says off: the setting no longer counts
    assert make_runner(store, fakes).auto_approve_on() is False


def test_the_setting_is_the_starting_value(store, fakes, monkeypatch):
    monkeypatch.setenv("CLIPS_AUTO_APPROVE", "true")
    clips_settings.cache_clear()
    assert make_runner(store, fakes).auto_approve_on() is True


def test_switching_writes_an_event(store, fakes):
    r = make_runner(store, fakes)
    r.set_auto_approve(True)
    r.set_auto_approve(False)
    messages = [e["message"] for e in store.list_events(kind="auto_approve")]
    assert any("switched off" in m for m in messages) and any("switched on" in m for m in messages)


def test_shorts_are_approved_after_process_and_longs_wait(store, fakes):
    r = _auto_runner(store, fakes)
    r.watch()
    r.process(limit=1)
    assert _kinds(store, ClipStatus.APPROVED) == ["short"]
    assert _kinds(store, ClipStatus.DRAFT) == ["long"]
    logged = store.list_events(kind="auto_approve")
    assert {e["clip_id"] for e in logged if e["clip_id"]} == {c.id for c in clips(store, ClipStatus.APPROVED)}


def test_a_short_with_warnings_is_approved_too(store, fakes):
    fakes.problems = ["title too long"]
    r = _auto_runner(store, fakes)
    r.watch()
    r.process(limit=1)
    approved = clips(store, ClipStatus.APPROVED)
    assert [c.kind for c in approved] == [ClipKind.SHORT]
    assert _kinds(store, ClipStatus.DRAFT) == ["long"]
    event = store.list_events(kind="auto_approve")[0]
    assert "title too long" in event["data"]["warnings"] and "had warnings" in event["message"]


def test_a_clip_rejected_during_approval_stays_rejected(store, fakes, monkeypatch):
    r = make_runner(store, fakes)
    r.watch()
    r.process(limit=1)
    short = next(c for c in clips(store) if c.kind is ClipKind.SHORT)
    real = fakes.verdict

    def reject_midway(ok):
        r.reject(short.id, "no")  # the person's click lands while approve() is checking the source
        return real(ok)

    monkeypatch.setattr(fakes, "verdict", reject_midway)
    with pytest.raises(ValueError):
        r.approve(short.id)
    assert store.get_clip(short.id).status is ClipStatus.REJECTED
    r.set_auto_approve(True)
    assert r.auto_approve_drafts() == 0
    assert store.get_clip(short.id).status is ClipStatus.REJECTED


def test_drafts_already_waiting_are_approved_at_schedule(store, fakes):
    r = make_runner(store, fakes)
    r.watch()
    r.process(limit=1)
    assert {c.status for c in clips(store)} == {ClipStatus.DRAFT}
    r.set_auto_approve(True)
    assert r.schedule() == 1  # the one Short got approved, then a slot
    assert _kinds(store, ClipStatus.SCHEDULED) == ["short"]
    assert _kinds(store, ClipStatus.DRAFT) == ["long"]


@pytest.mark.parametrize("how", ["disabled", "deleted", "rights_none"])
def test_a_draft_whose_source_fails_is_not_approved(store, fakes, how):
    r = make_runner(store, fakes)
    r.watch()
    r.process(limit=1)
    r.set_auto_approve(True)
    if how == "disabled":
        _switch_off(store)
    elif how == "deleted":
        store.delete_source("s1")
    else:
        fakes.ok = False  # the rights check now says no (a source with rights NONE)
    assert r.auto_approve_drafts() == 0
    # deleting a source already holds its clips; either way none is approved
    assert {c.status for c in clips(store)} <= {ClipStatus.DRAFT, ClipStatus.HELD}
    if how != "deleted":
        with pytest.raises(ValueError, match="cannot approve"):
            r.approve(clips(store)[0].id)  # the manual path refuses the same way


def test_nothing_is_auto_approved_while_the_kill_switch_is_on(store, fakes):
    r = make_runner(store, fakes)
    r.watch()
    r.process(limit=1)
    r.set_auto_approve(True)
    fakes.kill = True
    assert r.auto_approve_drafts() == 0 and r.schedule() == 0
    assert {c.status for c in clips(store)} == {ClipStatus.DRAFT}
    fakes.kill = False
    assert r.auto_approve_drafts() == 1


def test_tick_reports_the_auto_approved_count(store, fakes):
    r = _auto_runner(store, fakes)
    result = r.tick()
    assert result["auto_approved"] == 1  # one video per tick, one Short kept from it
    assert result["scheduled"] == 1
    assert _kinds(store, ClipStatus.DRAFT) == ["long"]


def test_tick_counts_zero_when_off(store, fakes):
    assert make_runner(store, fakes).tick()["auto_approved"] == 0


def test_review_off_and_auto_approve_differ(store, fakes, monkeypatch):
    # review_required=false schedules drafts of every kind straight away; auto-approve covers Shorts only
    monkeypatch.setenv("CLIPS_REVIEW_REQUIRED", "false")
    clips_settings.cache_clear()
    r = _auto_runner(store, fakes)
    r.watch()
    r.process(limit=1)
    assert r.schedule() == 2
    assert _kinds(store, ClipStatus.SCHEDULED) == ["long", "short"]
