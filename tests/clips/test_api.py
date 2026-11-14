"""Tests for the clips web API, with a fake Store, Runner and sibling modules."""

from __future__ import annotations

import sys
import threading
import types
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from clips import api as clips_api
from clips.config import clips_settings
from clips.models import (
    Clip,
    ClipStatus,
    Copy,
    Moment,
    Platform,
    RightsBasis,
    RightsVerdict,
    Source,
)

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


class RightsError(ValueError):
    pass


class FakeStore:
    def __init__(self) -> None:
        self.sources: dict[str, Source] = {}
        self.clips: dict[str, Clip] = {}
        self.events: list[dict] = []

    def list_sources(self, enabled_only=False):
        return list(self.sources.values())

    def get_source(self, sid):
        return self.sources.get(sid)

    def delete_source(self, sid):
        return self.sources.pop(sid, None) is not None

    def get_clip(self, cid):
        return self.clips.get(cid)

    def list_clips(self, status=None, source_id=None, limit=500):
        wanted = [status] if isinstance(status, ClipStatus) else status
        return [c for c in self.clips.values() if not wanted or c.status in wanted][:limit]

    def set_clip_status(self, cid, status, **fields):
        clip = self.clips[cid].model_copy(update={"status": status, **fields})
        self.clips[cid] = clip
        return clip

    def add_event(self, kind, message, **kw):
        self.events.append({"at": NOW, "kind": kind, "message": message, "clip_id": "", "source_id": "", "data": {}})

    def list_events(self, limit=200, kind=None):
        return [e for e in self.events if not kind or e["kind"] == kind][:limit]

    def booked_times(self):
        return []

    def first_post_date(self):
        return None


class FakeRunner:
    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.ticked = threading.Event()
        self.gate = threading.Event()
        self.gate.set()
        self.auto = False

    def auto_approve_on(self):
        return self.auto

    def set_auto_approve(self, on):
        self.auto = on
        return on

    def status(self):
        return {"clips": len(self.store.clips)}

    def approve(self, cid):
        if cid not in self.store.clips:
            raise KeyError(cid)
        if self.store.clips[cid].status is not ClipStatus.DRAFT:
            raise ValueError("only a draft can be approved")
        return self.store.set_clip_status(cid, ClipStatus.APPROVED)

    def reject(self, cid, reason=""):
        return self.store.set_clip_status(cid, ClipStatus.REJECTED, reject_reason=reason)

    def release(self, cid):
        if self.store.clips[cid].status is not ClipStatus.HELD:
            raise ValueError("only a held clip can be released")
        return self.store.set_clip_status(cid, ClipStatus.APPROVED, publish_at=None)

    def tick(self):
        self.ticked.set()
        self.gate.wait(5)
        return {"watched": 0}


def _fake_sources_module(store: FakeStore):
    mod = types.ModuleType("clips.sources")
    mod.RightsError = RightsError

    def add_source(st, *, name, platform, rights=RightsBasis.NONE, enabled=False, **fields):
        if enabled and rights is RightsBasis.NONE:
            raise RightsError("no rights basis on file")
        src = Source(id=name.lower().replace(" ", "-"), name=name, platform=platform, rights=rights,
                     enabled=enabled, **fields)
        st.sources[src.id] = src
        return src

    def enable_source(st, sid):
        src = st.sources[sid]
        if src.rights is RightsBasis.NONE:
            raise RightsError("no rights basis on file")
        st.sources[sid] = src.model_copy(update={"enabled": True})
        return st.sources[sid]

    def disable_source(st, sid):
        st.sources[sid] = st.sources[sid].model_copy(update={"enabled": False})
        return st.sources[sid]

    mod.add_source, mod.enable_source, mod.disable_source = add_source, enable_source, disable_source
    return mod


def _fake_rights_module():
    mod = types.ModuleType("clips.rights")

    def check_source(source):
        ok = source.rights is not RightsBasis.NONE
        return RightsVerdict(ok=ok, reasons=[] if ok else ["no rights basis on file"])

    mod.check_source = check_source
    return mod


def _fake_package_module():
    mod = types.ModuleType("clips.package")
    mod.check_copy = lambda copy, *, kind, credit: ["title is too long"] if len(copy.title) > 100 else []
    return mod


class FakeGuard(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("clips.guard")
        self.kill = False
        self.breaker = True
        self.isolated = True

    def assert_isolated(self):
        if not self.isolated:
            raise RuntimeError("STORAGE_DIR is the company folder")

    def set_kill_switch(self, on, reason=""):
        self.kill = on

    def kill_switch_on(self):
        return self.kill

    def breaker_open(self, store):
        return self.breaker

    def reset_breaker(self, store):
        self.breaker = False

    def check_channel(self, channel_id, settings=None):
        if channel_id == "UCcompany":
            raise RuntimeError("that is the company channel")


class FakeUploader(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("clips.uploader")
        self.channel = ("UCclips", "Clips channel")
        self.calls: list[str] = []

    def auth_url(self, state=""):
        self.calls.append("auth_url")
        return f"https://accounts.example/auth?state={state}"

    def finish_auth(self, code):
        self.calls.append("finish_auth")
        return ("UCcompany", "Company") if code == "bad" else self.channel

    def connected_channel(self):
        self.calls.append("connected_channel")
        return self.channel


@pytest.fixture
def world(monkeypatch):
    store = FakeStore()
    runner = FakeRunner(store)
    guard = FakeGuard()
    uploader = FakeUploader()
    monkeypatch.setattr(clips_api, "get_store", lambda: store)
    monkeypatch.setattr(clips_api, "get_runner", lambda: runner)
    for name, mod in {
        "sources": _fake_sources_module(store), "rights": _fake_rights_module(),
        "package": _fake_package_module(), "guard": guard, "uploader": uploader,
    }.items():
        monkeypatch.setitem(sys.modules, f"clips.{name}", mod)
    monkeypatch.setattr(clips_api, "_tick_state", {"running": False, "last": None, "error": ""})
    clips_api._pending_states.clear()
    return types.SimpleNamespace(store=store, runner=runner, guard=guard, uploader=uploader)


@pytest.fixture
def client(world):
    return TestClient(clips_api.app)


def make_clip(world, cid="c1", status=ClipStatus.DRAFT, file="", thumb=""):
    clip = Clip(id=cid, source_id="s", video_id="v", moment=Moment(start_s=1, end_s=20), status=status,
                text=Copy(title="Title", description="Desc"), credit="Credit: S", file=file, thumb=thumb)
    world.store.clips[cid] = clip
    return clip


def set_token(monkeypatch, token="secret"):
    monkeypatch.setenv("CLIPS_API_TOKEN", token)
    clips_settings.cache_clear()


# ------------------------------------------------------------------ auth


def test_open_when_no_token(client):
    assert client.get("/api/sources").status_code == 200


def test_token_required_and_public_paths(client, monkeypatch):
    set_token(monkeypatch)
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/api/sources").status_code == 401
    assert client.get("/nothing-here").status_code == 401
    assert client.get("/api/sources", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/sources", headers={"X-API-Key": "secret"}).status_code == 200


def test_login_sets_cookie(client, monkeypatch):
    set_token(monkeypatch)
    assert client.post("/api/login", json={"token": "no"}).status_code == 401
    assert client.post("/api/login", json={"token": "secret"}).status_code == 200
    assert client.get("/api/sources").status_code == 200  # cookie now carried


def test_page_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Review queue" in r.text


# ------------------------------------------------------------------ status


def test_status_merges_extras(client, world):
    data = client.get("/api/status").json()
    assert data["clips"] == 0
    assert data["channel"] == {"id": "UCclips", "title": "Clips channel"}
    assert data["kill_switch"] is False and data["breaker_open"] is True


# ------------------------------------------------------------------ sources


def test_add_source_without_rights_but_cannot_enable(client, world):
    r = client.post("/api/sources", json={"name": "Some Guy", "platform": "youtube"})
    assert r.status_code == 200
    body = r.json()
    assert body["rights"] == "none" and body["can_clip"] is False and body["reasons"]
    bad = client.post("/api/sources/some-guy/enable")
    assert bad.status_code == 400 and bad.json()["detail"]["reasons"]
    assert world.store.sources["some-guy"].enabled is False


def test_add_enabled_without_rights_refused(client):
    r = client.post("/api/sources", json={"name": "X", "platform": "youtube", "enabled": True})
    assert r.status_code == 400 and "no rights" in r.json()["detail"]["error"]


def test_source_with_rights_enable_disable_delete(client, world):
    r = client.post("/api/sources", json={"name": "Own Rec", "platform": "folder", "rights": "own",
                                          "evidence_note": "on the NAS"})
    assert r.json()["can_clip"] is True
    assert client.post("/api/sources/own-rec/enable").json()["enabled"] is True
    assert client.post("/api/sources/own-rec/disable").json()["enabled"] is False
    assert client.delete("/api/sources/own-rec").status_code == 200
    assert client.delete("/api/sources/own-rec").status_code == 404
    assert client.post("/api/sources/own-rec/enable").status_code == 404


def test_add_source_validates_body(client):
    assert client.post("/api/sources", json={"name": "", "platform": "youtube"}).status_code == 422
    assert client.post("/api/sources", json={"name": "a", "platform": "tiktok"}).status_code == 422


# ------------------------------------------------------------------ clips


def test_list_and_get_clips(client, world):
    make_clip(world, "c1")
    make_clip(world, "c2", ClipStatus.APPROVED)
    assert [c["id"] for c in client.get("/api/clips?status=draft").json()] == ["c1"]
    assert len(client.get("/api/clips").json()) == 2
    assert client.get("/api/clips?status=nope").status_code == 400
    one = client.get("/api/clips/c1").json()
    assert one["problems"] == [] and one["media_url"] == ""
    assert client.get("/api/clips/zzz").status_code == 404


def test_edit_clip(client, world):
    make_clip(world)
    r = client.patch("/api/clips/c1", json={"title": "New", "hashtags": ["#gta6"]})
    assert r.status_code == 200
    assert world.store.clips["c1"].text.title == "New"
    assert world.store.clips["c1"].text.hashtags == ["#gta6"]
    assert world.store.clips["c1"].text.description == "Desc"


def test_edit_refused_with_problems_or_wrong_status(client, world):
    make_clip(world)
    r = client.patch("/api/clips/c1", json={"title": "x" * 101})
    assert r.status_code == 422 and r.json()["detail"]["problems"] == ["title is too long"]
    assert world.store.clips["c1"].text.title == "Title"
    make_clip(world, "c2", ClipStatus.UPLOADED)
    assert client.patch("/api/clips/c2", json={"title": "y"}).status_code == 409
    assert client.patch("/api/clips/none", json={"title": "y"}).status_code == 404


def test_approve_and_reject(client, world):
    make_clip(world, "c1")
    make_clip(world, "c2")
    assert client.post("/api/clips/c1/approve").json()["status"] == "approved"
    assert client.post("/api/clips/c1/approve").status_code == 409
    r = client.post("/api/clips/c2/reject", json={"reason": "boring"}).json()
    assert r["status"] == "rejected" and r["reject_reason"] == "boring"
    assert client.post("/api/clips/nope/approve").status_code == 404
    assert client.post("/api/clips/nope/reject").status_code == 404


# ------------------------------------------------------------------ media


def test_media_only_inside_renders(client, world, tmp_path):
    renders = clips_settings().renders_dir
    renders.mkdir(parents=True)
    inside = renders / "a.mp4"
    inside.write_bytes(b"0123456789")
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"nope")
    make_clip(world, "in", file=str(inside), thumb=str(renders / "a.jpg"))
    make_clip(world, "out", file=str(outside))
    make_clip(world, "trick", file=str(renders / ".." / "secret.mp4"))
    make_clip(world, "none")

    ok = client.get("/media/in")
    assert ok.status_code == 200 and ok.content == b"0123456789"
    part = client.get("/media/in", headers={"Range": "bytes=2-4"})
    assert part.status_code == 206 and part.content == b"234"
    assert client.get("/media/out").status_code == 404
    assert client.get("/media/trick").status_code == 404
    assert client.get("/media/none").status_code == 404
    assert client.get("/media/missing").status_code == 404
    assert client.get("/thumb/in").status_code == 404  # file not there
    (renders / "a.jpg").write_bytes(b"jpg")
    assert client.get("/thumb/in").status_code == 200
    assert client.get("/media/..%2F..%2Fsecret").status_code in (404, 400)


# ------------------------------------------------------------------ controls


def test_kill_resume_breaker(client, world):
    assert client.post("/api/kill", json={"reason": "testing"}).json() == {"kill_switch": True}
    assert world.guard.kill is True
    assert client.get("/api/status").json()["kill_switch"] is True
    assert client.post("/api/resume").json() == {"kill_switch": False}
    assert world.guard.kill is False
    assert client.post("/api/kill").status_code == 200  # no body is fine
    client.post("/api/breaker/reset")
    assert world.guard.breaker is False
    assert {e["kind"] for e in client.get("/api/events").json()} >= {"kill", "resume", "breaker"}
    assert client.get("/api/events?kind=resume").json()[0]["message"] == "kill switch off"


def test_tick_runs_in_background_one_at_a_time(client, world):
    world.runner.gate.clear()
    assert client.post("/api/tick").status_code == 202
    assert world.runner.ticked.wait(5)
    assert client.post("/api/tick").status_code == 409
    world.runner.gate.set()
    for _ in range(100):
        if not client.get("/api/tick").json()["running"]:
            break
        threading.Event().wait(0.05)
    state = client.get("/api/tick").json()
    assert state["running"] is False and state["last"] == {"watched": 0}
    assert client.post("/api/tick").status_code == 202


def test_off_the_clips_folder_a_pass_is_refused_and_nothing_runs(client, world):
    world.guard.isolated = False
    r = client.post("/api/tick")
    assert r.status_code == 503 and "company folder" in r.text
    assert not world.runner.ticked.is_set()
    assert client.get("/api/tick").json()["running"] is False


def test_kill_tells_the_page_when_a_video_could_not_be_pulled_back(client, world):
    world.guard.set_kill_switch = lambda on, reason="": {"pulled_back": [], "failed": ["c9"], "skipped": ""}
    body = client.post("/api/kill", json={"reason": "x"}).json()
    assert body["kill_switch"] is True and body["pull_back"]["failed"] == ["c9"]


# ------------------------------------------------------------------ sign-in


def test_youtube_connect_and_callback(client, world):
    r = client.get("/youtube/connect", follow_redirects=False)
    assert r.status_code == 302
    state = r.headers["location"].split("state=")[1]
    ok = client.get(f"/youtube/callback?state={state}&code=good")
    assert ok.status_code == 200 and "Clips channel" in ok.text
    again = client.get(f"/youtube/callback?state={state}&code=good")  # state is one-time
    assert again.status_code == 400


def test_youtube_callback_refusals(client, world, monkeypatch):
    assert client.get("/youtube/callback?state=forged&code=x").status_code == 400
    set_token(monkeypatch)
    assert client.get("/youtube/connect", follow_redirects=False).status_code == 401
    state = client.get("/youtube/connect", headers={"X-API-Key": "secret"}, follow_redirects=False)
    state = state.headers["location"].split("state=")[1]
    refused = client.get(f"/youtube/callback?state={state}&code=bad")  # public path, no key needed
    assert refused.status_code == 400 and "company channel" in refused.text
    state = client.get("/youtube/connect", headers={"X-API-Key": "secret"}, follow_redirects=False)
    state = state.headers["location"].split("state=")[1]
    assert client.get(f"/youtube/callback?state={state}&error=access_denied").status_code == 400


def test_platform_enum_exposed():
    assert Platform.YOUTUBE.value == "youtube"


# ------------------------------------------------------------------ isolation and the open API


def test_off_the_clips_folder_status_never_reads_the_token(client, world):
    world.guard.isolated = False
    data = client.get("/api/status").json()
    assert data["channel"] is None and data["isolated"] is False
    assert "company folder" in data["isolation_problem"]
    assert "connected_channel" not in world.uploader.calls


def test_off_the_clips_folder_sign_in_is_refused(client, world):
    world.guard.isolated = False
    assert client.get("/youtube/connect", follow_redirects=False).status_code == 503
    clips_api._pending_states.add("s1")
    r = client.get("/youtube/callback?state=s1&code=abc")
    assert r.status_code == 503
    assert world.uploader.calls == []  # neither auth_url nor finish_auth ran, so no token was written


def test_no_key_on_a_public_address_serves_only_the_page(client, monkeypatch):
    monkeypatch.setenv("CLIPS_PUBLIC_BASE_URL", "https://clips.example.com")
    clips_settings.cache_clear()
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200
    for path in ("/api/sources", "/api/status"):
        assert client.get(path).status_code == 503
    assert client.post("/api/resume").status_code == 503
    assert client.post("/api/breaker/reset").status_code == 503


@pytest.mark.parametrize("base", ["http://localhost:8091", "http://127.0.0.1:8091"])
def test_no_key_on_localhost_stays_open(client, monkeypatch, base):
    monkeypatch.setenv("CLIPS_PUBLIC_BASE_URL", base)
    clips_settings.cache_clear()
    assert client.get("/api/sources").status_code == 200


def test_a_key_on_a_public_address_works(client, monkeypatch):
    monkeypatch.setenv("CLIPS_PUBLIC_BASE_URL", "https://clips.example.com")
    set_token(monkeypatch)
    assert client.get("/api/sources").status_code == 401
    assert client.get("/api/sources", headers={"X-API-Key": "secret"}).status_code == 200


def test_release_puts_a_held_clip_back(client, world):
    make_clip(world, "h1", ClipStatus.HELD)
    r = client.post("/api/clips/h1/release")
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert client.post("/api/clips/h1/release").status_code == 409
    assert client.post("/api/clips/nope/release").status_code == 404


def test_page_shows_that_youtube_downloads_are_off_next_to_the_source(client, monkeypatch):
    yt = client.post("/api/sources", json={"name": "Some Guy", "platform": "youtube"}).json()
    folder = client.post("/api/sources", json={"name": "Own Rec", "platform": "folder", "rights": "own"}).json()
    assert "CLIPS_YOUTUBE_DOWNLOADS" in yt["download_note"]
    assert folder["download_note"] == ""
    monkeypatch.setenv("CLIPS_YOUTUBE_DOWNLOADS", "true")
    clips_settings.cache_clear()
    assert all(s["download_note"] == "" for s in client.get("/api/sources").json())


def test_page_script_renders_the_download_note(client):
    assert "download_note" in client.get("/").text


# ------------------------------------------------------------ auto-approve


def test_status_shows_auto_approve_and_the_route_flips_it(client, world):
    assert client.get("/api/status").json()["auto_approve"] is False
    assert client.post("/api/auto-approve", json={"on": True}).json() == {"auto_approve": True}
    assert world.runner.auto is True
    assert client.get("/api/status").json()["auto_approve"] is True
    assert client.post("/api/auto-approve", json={"on": False}).json() == {"auto_approve": False}
    assert client.get("/api/status").json()["auto_approve"] is False


def test_auto_approve_route_needs_the_token(client, world, monkeypatch):
    set_token(monkeypatch)
    assert client.post("/api/auto-approve", json={"on": True}).status_code == 401
    assert client.post("/api/auto-approve", json={"on": True}, headers={"X-API-Key": "wrong"}).status_code == 401
    assert world.runner.auto is False
    ok = client.post("/api/auto-approve", json={"on": True}, headers={"X-API-Key": "secret"})
    assert ok.status_code == 200 and world.runner.auto is True


def test_auto_approve_route_is_refused_off_the_clips_folder(client, world):
    world.guard.isolated = False
    assert client.post("/api/auto-approve", json={"on": True}).status_code == 503
    assert world.runner.auto is False


@pytest.mark.parametrize("body", [{}, {"on": "yes"}, {"on": None}, {"on": 1}, {"state": True}])
def test_auto_approve_route_refuses_a_bad_body(client, world, body):
    assert client.post("/api/auto-approve", json=body).status_code == 422
    assert world.runner.auto is False
    assert client.post("/api/auto-approve", content="not json").status_code == 422


def test_page_has_the_auto_approve_switch_and_the_confirmation(client):
    page = client.get("/").text
    assert "Auto-approve Shorts" in page and "/api/auto-approve" in page
    assert (
        "Every Short will be scheduled and posted without anyone looking at it, including ones with warnings. "
        "Longer clips still wait for you. Turn it on?"
    ) in page
