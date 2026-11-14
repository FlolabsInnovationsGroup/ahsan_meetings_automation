"""The command line, through main(argv). The guard module is faked so these
tests do not depend on it; the store and the runner are real (fake stages)."""

from __future__ import annotations

import json
import sys
import types

import pytest

from clips import __main__ as cli
from clips.config import clips_settings
from clips.models import ClipStatus, Platform, RightsBasis, Source
from clips.runner import Runner
from clips.store import Store

from .test_runner import Fakes


class FakeGuard(types.ModuleType):
    def __init__(self):
        super().__init__("clips.guard")
        self.calls: list[tuple] = []
        self.isolated = True

    def assert_isolated(self):
        if not self.isolated:
            raise RuntimeError("not the clips folder")
        self.calls.append(("assert_isolated",))

    def init_storage(self, path):
        self.calls.append(("init_storage", str(path)))

    def set_kill_switch(self, on, reason=""):
        self.calls.append(("kill", on, reason))

    def reset_breaker(self, store):
        self.calls.append(("reset_breaker",))


@pytest.fixture
def guard(monkeypatch):
    g = FakeGuard()
    monkeypatch.setitem(sys.modules, "clips.guard", g)
    return g


@pytest.fixture(autouse=True)
def _downloads_on(_clips_env, monkeypatch):
    monkeypatch.setenv("CLIPS_YOUTUBE_DOWNLOADS", "true")
    clips_settings.cache_clear()


@pytest.fixture
def env(monkeypatch, guard, clips_storage):
    fakes = Fakes()
    store = Store(clips_storage / "clips.sqlite")
    store.save_source(
        Source(id="s1", name="Demo", platform=Platform.YOUTUBE, rights=RightsBasis.OWN, enabled=True)
    )
    runner = Runner(store, settings=clips_settings(), stages=fakes.stages())
    monkeypatch.setattr(cli, "_runner", lambda: runner)
    monkeypatch.setattr(cli, "_store", lambda: store)
    return runner, store, fakes


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_init_and_status_skip_the_isolation_check(env, guard, capsys):
    guard.isolated = False
    code, out, _ = run(capsys, "init")
    assert code == 0 and "ready" in out
    code, out, _ = run(capsys, "status")
    assert code == 0 and json.loads(out)["sources"]["enabled"] == 1
    assert ("assert_isolated",) not in guard.calls


def test_other_commands_refuse_when_not_isolated(env, guard, capsys):
    guard.isolated = False
    code, _, err = run(capsys, "watch")
    assert code == 1 and "not the clips folder" in err
    assert env[0].store.list_videos() == []


def test_sources_listing(env, capsys):
    code, out, _ = run(capsys, "sources")
    assert code == 0 and "s1" in out and "enabled" in out


def test_add_enable_disable_source(env, capsys):
    code, out, _ = run(
        capsys,
        "add-source",
        "Cool Channel",
        "--platform",
        "youtube",
        "--url",
        "https://youtube.com/@cool",
        "--rights",
        "written_permission",
        "--evidence-note",
        "email from creator 2026-11-01",
        "--evidence-date",
        "2026-11-01",
    )
    assert code == 0 and "added source" in out
    new = next(s for s in env[1].list_sources() if s.id != "s1")
    code, out, _ = run(capsys, "enable-source", new.id)
    assert code == 0 and env[1].get_source(new.id).enabled
    code, out, _ = run(capsys, "disable-source", new.id)
    assert code == 0 and not env[1].get_source(new.id).enabled


def test_enable_source_without_rights_is_an_error(env, capsys):
    run(capsys, "add-source", "No Basis", "--platform", "youtube")
    new = next(s for s in env[1].list_sources() if s.id != "s1")
    code, _, err = run(capsys, "enable-source", new.id)
    assert code == 1 and err.startswith("error:")
    assert not env[1].get_source(new.id).enabled


def test_add_source_enabled_without_rights_is_refused(env, capsys):
    code, _, err = run(capsys, "add-source", "Bad", "--platform", "youtube", "--enable")
    assert code == 1 and "error:" in err


def test_flow_commands(env, capsys):
    _runner, store, _fakes = env
    assert "2 new" in run(capsys, "watch")[1]
    assert "2 clip" in run(capsys, "process", "--limit", "1")[1]
    clip = store.list_clips()[0]
    other = store.list_clips()[1]
    assert run(capsys, "approve", clip.id)[0] == 0
    assert run(capsys, "reject", other.id, "not", "good")[0] == 0
    assert store.get_clip(other.id).reject_reason == "not good"
    assert "1 clip" in run(capsys, "schedule")[1]
    store.set_clip_status(clip.id, ClipStatus.SCHEDULED, publish_at=store.get_clip(clip.id).publish_at)
    assert "1 clip" in run(capsys, "upload")[1]
    assert store.get_clip(clip.id).status is ClipStatus.UPLOADED


def test_upload_dry_run_sends_nothing_real(env, capsys):
    runner, store, _fakes = env
    run(capsys, "watch")
    run(capsys, "process", "--limit", "1")
    clip = store.list_clips()[0]
    run(capsys, "approve", clip.id)
    run(capsys, "schedule")
    seen = []
    runner.stages.upload_clip = lambda c, s, st, *, dry_run=False, **kw: seen.append(dry_run)
    code, out, _ = run(capsys, "upload", "--dry-run")
    assert code == 0 and seen == [True] and "dry run" in out
    assert store.get_clip(clip.id).status is ClipStatus.SCHEDULED


def test_approve_unknown_clip_exits_one(env, capsys):
    code, _, err = run(capsys, "approve", "doesnotexist")
    assert code == 1 and "doesnotexist" in err


def test_tick_prints_counts(env, capsys):
    code, out, _ = run(capsys, "tick")
    assert code == 0 and json.loads(out)["watched"] == 2


def test_kill_resume_breaker_reset(env, guard, capsys):
    assert run(capsys, "kill", "too", "risky")[0] == 0
    assert run(capsys, "resume")[0] == 0
    assert run(capsys, "breaker-reset")[0] == 0
    assert ("kill", True, "too risky") in guard.calls
    assert ("kill", False, "") in guard.calls
    assert ("reset_breaker",) in guard.calls


def test_loop_survives_a_bad_tick(env, monkeypatch, capsys):
    runner, _, _ = env
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return {"watched": 0}

    runner.tick = flaky
    sleeps = []
    monkeypatch.setattr(cli, "_sleep", sleeps.append)
    code, out, _ = run(capsys, "loop", "--max-ticks", "3")
    assert code == 0 and calls["n"] == 3
    assert "tick failed: boom" in out and sleeps == [15 * 60, 15 * 60]


def test_loop_stops_on_interrupt(env, monkeypatch, capsys):
    def stop(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_sleep", stop)
    env[0].tick = dict
    code, out, _ = run(capsys, "loop")
    assert code == 0 and "stopped" in out


def test_bad_arguments_return_nonzero(env, capsys):
    assert run(capsys, "nope")[0] != 0
    assert run(capsys, "add-source", "x", "--platform", "myspace")[0] != 0


def test_release_command_puts_a_held_clip_back(env, capsys):
    runner, store, _fakes = env
    runner.watch()
    runner.process(limit=1)
    clip = store.list_clips()[0]
    runner.approve(clip.id)
    store.set_clip_status(clip.id, ClipStatus.HELD, error="wrong channel")
    code, out, _ = run(capsys, "release", clip.id)
    assert code == 0 and clip.id in out
    assert store.get_clip(clip.id).status is ClipStatus.APPROVED
    code, _, err = run(capsys, "release", clip.id)
    assert code == 1 and "only a held clip" in err


def test_status_without_isolation_does_not_ask_for_the_channel(env, guard, capsys):
    runner, _, _ = env
    asked = []
    runner.stages.connected_channel = lambda: asked.append(1) or ("UCx", "X")
    guard.isolated = False
    runner.stages.assert_isolated = guard.assert_isolated
    code, out, _ = run(capsys, "status")
    data = json.loads(out)
    assert code == 0 and data["isolated"] is False and data["channel"] is None and asked == []


def test_kill_says_which_videos_could_not_be_pulled_back(env, guard, capsys):
    from datetime import UTC, datetime, timedelta

    from clips.models import Clip, Moment

    _runner, store, _ = env
    store.save_clip(
        Clip(id="c9", source_id="s1", video_id="v", moment=Moment(start_s=1, end_s=9), status=ClipStatus.UPLOADED,
             youtube_id="vid9", publish_at=datetime.now(UTC) + timedelta(hours=2))
    )
    guard.set_kill_switch = lambda on, reason="": {"pulled_back": ["c1"], "failed": ["c9"], "skipped": ""}
    code, out, _ = run(capsys, "kill", "strike")
    assert code == 0
    assert "1 uploaded video(s)" in out and "could NOT" in out
    assert "https://studio.youtube.com/video/vid9/edit" in out
