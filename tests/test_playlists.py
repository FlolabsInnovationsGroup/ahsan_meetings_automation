"""Team playlists (publish/playlists.py): a fake YouTube API, synthetic teams."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from autopilot.store import store
from ledger.uploads import Item, get_ledger
from publish import extras, optional, playlists, quota, series, youtube, youtube_extra


class FakeHttpError(Exception):
    def __init__(self, status, reason):
        super().__init__(f"{status} {reason}")
        self.resp = SimpleNamespace(status=status)
        self.content = json.dumps({"error": {"message": reason, "errors": [{"reason": reason}]}}).encode()


class Api:
    def __init__(self):
        self.calls = []
        self.existing = {}  # title -> id
        self.items = set()  # (playlist, video)
        self.privacy = {}  # video -> {"privacyStatus", "publishAt"}
        self.deleted = set()
        self.n = 0
        self.errors = {}  # call name -> exception to raise once

    def _hit(self, name, *args):
        self.calls.append((name, *args))
        err = self.errors.pop(name, None)
        if err:
            raise err

    def find_playlist(self, title):
        self._hit("find", title)
        return self.existing.get(title)

    def create_playlist(self, title, description, privacy="public"):
        self._hit("create", title, privacy)
        self.n += 1
        pid = f"PLmade{self.n:010d}"
        self.existing[title] = pid
        return pid

    def video_privacy(self, video_id):
        self._hit("privacy", video_id)
        return self.privacy.get(video_id)

    def playlist_has_video(self, playlist_id, video_id):
        self._hit("has", playlist_id, video_id)
        return (playlist_id, video_id) in self.items

    def add_to_playlist(self, playlist_id, video_id):
        self._hit("add", playlist_id, video_id)
        if playlist_id in self.deleted:
            raise FakeHttpError(404, "playlistNotFound")
        self.items.add((playlist_id, video_id))
        return "item"

    def playlist_item_id(self, playlist_id, video_id):
        self._hit("item", playlist_id, video_id)
        return f"item-{playlist_id}-{video_id}" if (playlist_id, video_id) in self.items else None

    def remove_from_playlist(self, item_id):
        self._hit("remove", item_id)
        for pair in list(self.items):
            if item_id == f"item-{pair[0]}-{pair[1]}":
                self.items.discard(pair)

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def api(monkeypatch):
    fake = Api()
    for name in ("find_playlist", "create_playlist", "video_privacy", "playlist_has_video", "add_to_playlist",
                 "playlist_item_id", "remove_from_playlist"):
        monkeypatch.setattr(youtube_extra, name, getattr(fake, name))
    monkeypatch.setattr(youtube, "has_scope", lambda scope: True)
    monkeypatch.setattr(extras, "_builders", {"playlist": playlists._builder})
    monkeypatch.setattr(extras, "_runners", {"playlist": playlists._runner})
    monkeypatch.setattr(extras, "_loaded", True)
    store._state = None
    series.set_row("space", label="Space", tier="public", aliases=["space systems"])
    yield fake
    store._state = None


def _task(video="vid00000001", key="space", kind="full"):
    which = {"full": "full", "short": "shorts", "topic": "cuts"}[kind]
    return extras.Task(kind="playlist", video_id=video, series_key=key, video_kind=kind, data={"which": which})


def _public(api, video="vid00000001"):
    api.privacy[video] = {"privacyStatus": "public", "publishAt": None}


def _upload(topic, privacy="public", publish_at=None, kind="full", n=1):
    item = Item(f"job:{n}|{kind}", f"job:{n}", kind, f"job{n}", topic, topic=topic)
    ledger = get_ledger()
    ledger.begin(item, title="t", privacy=privacy, by="test", publish_at=publish_at,
                 notify=True if publish_at else None)
    return item, ledger.published(item.key, f"vid{n:08d}", by="test")


def _enqueue(item, posted, video_kind="full"):
    extras.enqueue_for_upload(SimpleNamespace(id=item.job_id), item, posted, video_kind=video_kind)
    return extras.all_tasks()


def test_the_template_fills_series(api):
    _public(api)
    assert playlists._runner(_task()).status == optional.DONE
    assert ("create", "FloLabs Space: team meetings", "public") in api.calls


def test_each_kind_has_its_own_title(api):
    for kind, title in (("short", "FloLabs Space: Shorts"), ("topic", "FloLabs Space: topics")):
        _public(api, f"v{kind}")
        assert playlists._runner(_task(video=f"v{kind}", kind=kind)).status == optional.DONE
        assert ("create", title, "public") in api.calls


def test_which_for_follows_the_settings():
    cfg = SimpleNamespace(playlists=True, playlist_shorts=False, playlist_cuts=True)
    assert playlists.which_for("full", cfg) == "full"
    assert playlists.which_for("short", cfg) is None
    assert playlists.which_for("topic", cfg) == "cuts"
    off = SimpleNamespace(playlists=False, playlist_shorts=True, playlist_cuts=True)
    assert playlists.which_for("full", off) is None


def test_a_registry_hit_makes_no_list_call(api):
    series.set_playlist("space", "full", "PLknown1234567")
    _public(api)
    assert playlists._runner(_task()).status == optional.DONE
    assert "find" not in api.names() and "create" not in api.names()
    assert ("PLknown1234567", "vid00000001") in api.items


def test_a_miss_reuses_a_playlist_with_that_title(api):
    api.existing["FloLabs Space: team meetings"] = "PLfound123456"
    _public(api)
    assert playlists._runner(_task()).status == optional.DONE
    assert "create" not in api.names()
    assert series.lookup_key("space").playlists["full"] == "PLfound123456"


def test_a_miss_with_none_creates_once(api):
    _public(api, "vid00000001")
    _public(api, "vid00000002")
    playlists._runner(_task())
    playlists._runner(_task(video="vid00000002"))
    assert api.names().count("create") == 1
    assert series.lookup_key("space").playlists["full"].startswith("PLmade")


def test_two_spellings_share_one_playlist(api):
    _public(api, "vid00000001")
    _public(api, "vid00000002")
    first, posted1 = _upload("Space Meeting (2026-01-05)", n=1)
    second, posted2 = _upload("Space Systems Meeting (2026-01-12)", n=2)
    _enqueue(first, posted1)
    tasks = _enqueue(second, posted2)
    assert [t.series_key for t in tasks] == ["space", "space"]
    for task in tasks:
        assert playlists._runner(task).status == optional.DONE
    assert api.names().count("create") == 1
    assert len({p for p, _v in api.items}) == 1 and len(api.items) == 2


def test_unlisted_and_private_enqueue_nothing(api):
    for n, privacy in ((1, "unlisted"), (2, "private")):
        item, posted = _upload("Space Meeting (2026-01-05)", privacy=privacy, n=n)
        assert _enqueue(item, posted) == []


def test_public_enqueues_a_task(api):
    item, posted = _upload("Space Meeting (2026-01-05)")
    [task] = _enqueue(item, posted)
    assert task.kind == "playlist" and task.data["which"] == "full" and task.series_key == "space"
    assert not task.data["waits_for_public"]


def test_playlists_off_enqueues_nothing(api, monkeypatch):
    monkeypatch.setattr(playlists, "which_for", lambda *a, **k: None)
    item, posted = _upload("Space Meeting (2026-01-05)")
    assert _enqueue(item, posted) == []


def test_scheduled_waits_until_publish_at(api):
    when = (datetime.now(UTC) + timedelta(hours=5)).replace(microsecond=0)
    item, posted = _upload("Space Meeting (2026-01-05)", publish_at=when.isoformat())
    [task] = _enqueue(item, posted)
    assert task.data["waits_for_public"] is True
    assert extras._parse(task.not_before) == when
    assert extras.run_due() == 0  # not due yet: nothing is called
    assert api.calls == []


def test_still_scheduled_retries_after_publish_at(api):
    when = datetime.now(UTC) + timedelta(hours=1)
    api.privacy["vid00000001"] = {"privacyStatus": "private", "publishAt": when.isoformat()}
    out = playlists._runner(_task())
    assert out.status == optional.RETRY
    assert abs((out.retry_at - (when + timedelta(minutes=15))).total_seconds()) < 1
    assert api.names() == ["privacy"]


def test_a_video_pulled_to_private_is_skipped(api):
    api.privacy["vid00000001"] = {"privacyStatus": "private", "publishAt": None}
    out = playlists._runner(_task())
    assert out.status == optional.SKIPPED and "not public" in out.reason
    assert api.names() == ["privacy"]


def test_a_missing_video_is_skipped(api):
    assert playlists._runner(_task()).status == optional.SKIPPED


def test_no_duplicate_item(api):
    series.set_playlist("space", "full", "PLknown1234567")
    _public(api)
    api.items.add(("PLknown1234567", "vid00000001"))
    assert playlists._runner(_task()).status == optional.DONE
    assert "add" not in api.names()


def test_a_404_playlist_is_forgotten_and_made_again(api):
    series.set_playlist("space", "full", "PLgone12345678")
    api.deleted.add("PLgone12345678")
    _public(api)
    assert playlists._runner(_task()).status == optional.RETRY
    assert series.lookup_key("space").playlists["full"] == ""
    assert playlists._runner(_task()).status == optional.DONE
    assert api.names().count("create") == 1
    assert series.lookup_key("space").playlists["full"].startswith("PLmade")


def test_a_missing_scope_needs_reconnect(api, monkeypatch):
    monkeypatch.setattr(youtube, "has_scope", lambda scope: False)
    out = playlists._runner(_task())
    assert out.status == optional.NEEDS_RECONNECT
    assert "Reconnect to turn on playlists" in out.reason
    assert api.calls == []


def test_a_video_404_is_skipped_and_keeps_the_playlist(api):
    series.set_playlist("space", "full", "PLknown1234567")
    _public(api)
    api.errors["add"] = FakeHttpError(404, "videoNotFound")
    assert playlists._runner(_task()).status == optional.SKIPPED
    assert series.lookup_key("space").playlists["full"] == "PLknown1234567"


def test_units_for_one_add(api):
    _public(api)
    api.existing["FloLabs Space: team meetings"] = "PLfound123456"
    before = quota.today()["used"]
    playlists._runner(_task())
    assert quota.today()["used"] - before == 1 + 1 + 1 + 50  # privacy, find, has, add


def test_units_when_a_playlist_is_created(api):
    _public(api)
    before = quota.today()["used"]
    playlists._runner(_task())
    assert quota.today()["used"] - before == 1 + 1 + 50 + 1 + 50


# ------------------------------------------------------------------ backfill


def _listing(monkeypatch, videos):
    from publish import channel_check

    monkeypatch.setattr(channel_check, "_build_service", lambda: None)
    monkeypatch.setattr(channel_check, "_videos", lambda service: (videos, False))


def _video(n, title, privacy="public", duration=1800):
    return {"id": f"old{n:08d}", "title": title, "privacy": privacy, "publish_at": None,
            "published_at": f"2025-01-{n:02d}T10:00:00Z", "duration": duration}


def _backfill_tasks():
    return [t for t in extras.all_tasks() if t.data.get("backfill")]


def test_backfill_queues_past_public_videos_per_team(api, monkeypatch):
    series.set_row("ai", label="AI", tier="public")
    _listing(monkeypatch, [_video(1, "Space Meeting (2025-01-01)"), _video(2, "AI Meeting (2025-01-02)"),
                           _video(3, "Space Meeting (2025-01-03)", duration=40)])
    assert playlists.playlist_backfill() is True
    by_id = {t.video_id: t for t in _backfill_tasks()}
    assert by_id["old00000001"].series_key == "space" and by_id["old00000001"].data["which"] == "full"
    assert by_id["old00000002"].series_key == "ai"
    assert by_id["old00000003"].data["which"] == "shorts"
    assert playlists.backfill_status()["added"] == 3
    # and the same runner adds it, with no ledger record needed
    monkeypatch.setattr(youtube, "youtube_status", lambda: {"connected": True})
    _public(api, "old00000001")
    assert extras.run_due() >= 1
    assert any(v == "old00000001" for _p, v in api.items)


def test_backfill_skips_unlisted_never_and_unmatched(api, monkeypatch):
    _listing(monkeypatch, [
        _video(1, "Space Meeting (2025-01-01)", privacy="unlisted"),
        _video(2, "Space Meeting (2025-01-02)", privacy="private"),
        _video(3, "Space HR Meeting (2025-01-03)"),
        _video(4, "Random Chat Yesterday"),
        _video(5, "Space Meeting (2025-01-05)")])
    playlists.playlist_backfill()
    assert [t.video_id for t in _backfill_tasks()] == ["old00000005"]
    status = playlists.backfill_status()
    assert status["skipped_not_public"] == 2 and status["skipped_never"] == 1 and status["skipped_no_team"] == 1


def test_backfill_skips_teams_set_to_archive(api, monkeypatch):
    series.set_row("old", label="Old", tier="archive")
    _listing(monkeypatch, [_video(1, "Old Meeting (2025-01-01)")])
    playlists.playlist_backfill()
    assert _backfill_tasks() == [] and playlists.backfill_status()["skipped_never"] == 1


def test_backfill_per_day_cap_newest_first(api, monkeypatch):
    monkeypatch.setattr(playlists, "PLAYLIST_BACKFILL_PER_DAY", 2)
    _listing(monkeypatch, [_video(n, f"Space Meeting (2025-01-{n:02d})") for n in range(1, 6)])
    playlists.playlist_backfill()
    assert sorted(t.video_id for t in _backfill_tasks()) == ["old00000004", "old00000005"]
    assert playlists.backfill_status()["left_for_tomorrow"] == 3
    playlists.playlist_backfill()  # the next day: the rest, not the same ones again
    assert len(_backfill_tasks()) == 4


def test_backfill_does_not_requeue_added_videos(api, monkeypatch):
    _listing(monkeypatch, [_video(1, "Space Meeting (2025-01-01)")])
    playlists.playlist_backfill()
    playlists.playlist_backfill()
    assert len(_backfill_tasks()) == 1
    assert playlists.backfill_status()["added"] == 0


def test_backfill_does_nothing_without_permission(api, monkeypatch):
    monkeypatch.setattr(youtube, "has_scope", lambda scope: False)
    _listing(monkeypatch, [_video(1, "Space Meeting (2025-01-01)")])
    assert playlists.playlist_backfill() is False
    assert _backfill_tasks() == []
    assert "Reconnect YouTube" in playlists.backfill_status()["note"]


def test_backfill_does_nothing_when_playlists_off(api, monkeypatch):
    monkeypatch.setattr(playlists.publishing_settings, "current", lambda: SimpleNamespace(
        playlists=False, playlist_shorts=True, playlist_cuts=True))
    _listing(monkeypatch, [_video(1, "Space Meeting (2025-01-01)")])
    assert playlists.playlist_backfill() is False
    assert _backfill_tasks() == []


# ------------------------------------------------------------------ moving a merged team


def _two_teams(api):
    series.set_row("neuro", label="Neuroscience", tier="public",
                   playlists={"full": "PLneuro0000001", "cuts": "", "shorts": ""})
    series.set_row("neuro bio", label="Neuroscience and Biomedical", tier="public",
                   playlists={"full": "PLoldbio000001", "cuts": "", "shorts": ""})


def test_merge_moves_posted_videos_into_the_target_playlists(api, monkeypatch):
    _two_teams(api)
    item, posted = _upload("Neuro Bio Meeting (2026-11-03)", n=1)
    _s, short = _upload("Neuro Bio Meeting (2026-11-03)", kind="short", n=2)
    _o, other = _upload("Space Meeting (2026-11-03)", n=3)
    _p, private = _upload("Neuro Bio Meeting (2026-11-04)", privacy="private", n=4)
    _n, never = _upload("Operations Meeting (2026-11-04)", n=5)
    api.items.add(("PLoldbio000001", posted.video_id))
    _enqueue(item, posted)  # it had a playlist task before: it went to the old playlist
    src = series.lookup_key("neuro bio")
    series.merge("neuro bio", "neuro")
    out = playlists.move_team([src.key, *src.aliases], "neuro", src.playlists, src.label)
    assert out["queued"] == 2 and out["label"] == "Neuroscience"
    moves = [t for t in extras.all_tasks() if t.data.get("move")]
    assert {t.video_id for t in moves} == {posted.video_id, short.video_id}
    assert {t.data["which"] for t in moves} == {"full", "shorts"}
    assert all(t.series_key == "neuro" for t in moves)
    assert out["old_playlists"] == [{"id": "PLoldbio000001", "title": playlists.title_for(src.label, "full")}]
    assert not any(t.video_id in (other.video_id, private.video_id, never.video_id) and t.data.get("move")
                   for t in extras.all_tasks())
    # running them: added to the new playlist, taken out of the old one
    monkeypatch.setattr(youtube, "youtube_status", lambda: {"connected": True})
    _public(api, posted.video_id)
    for _ in range(4):
        extras.run_due()
    assert ("PLneuro0000001", posted.video_id) in api.items
    assert ("PLoldbio000001", posted.video_id) not in api.items
    assert "remove" in api.names()


def test_a_second_move_for_the_same_video_is_not_queued_twice(api):
    _two_teams(api)
    _upload("Neuro Bio Meeting (2026-11-03)", n=1)
    src = series.lookup_key("neuro bio")
    series.merge("neuro bio", "neuro")
    playlists.move_team([src.key], "neuro", src.playlists, src.label)
    playlists.move_team([src.key], "neuro", src.playlists, src.label)
    assert len([t for t in extras.all_tasks() if t.data.get("move")]) == 1


def test_move_clears_the_backfill_memory_and_takes_backfilled_videos(api, monkeypatch):
    _two_teams(api)
    _listing(monkeypatch, [_video(1, "Neuro Bio Meeting (2025-01-01)")])
    assert playlists.playlist_backfill() is True
    assert "old00000001" in files_read_queued()
    src = series.lookup_key("neuro bio")
    series.merge("neuro bio", "neuro")
    out = playlists.move_team([src.key], "neuro", src.playlists, src.label)
    assert out["queued"] == 1
    assert "old00000001" not in files_read_queued()


def files_read_queued():
    from ledger import files

    return files.read_json(playlists._backfill_path(), {}).get("queued", {})
