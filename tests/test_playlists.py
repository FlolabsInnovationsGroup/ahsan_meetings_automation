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

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def api(monkeypatch):
    fake = Api()
    for name in ("find_playlist", "create_playlist", "video_privacy", "playlist_has_video", "add_to_playlist"):
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
