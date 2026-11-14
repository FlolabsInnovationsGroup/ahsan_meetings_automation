import threading
from datetime import UTC, date, datetime, timedelta

import pytest

from clips.models import (
    Clip,
    ClipKind,
    ClipStatus,
    Copy,
    Moment,
    Platform,
    RightsBasis,
    Source,
    SourceVideo,
    VideoStatus,
)
from clips.store import Store, new_clip_id


@pytest.fixture
def store(clips_storage):
    return Store(clips_storage / "t.sqlite")


def mk_clip(**kw) -> Clip:
    base = {"id": new_clip_id(), "source_id": "s", "video_id": "youtube:v", "moment": Moment(start_s=1.0, end_s=20.5)}
    base.update(kw)
    return Clip(**base)


def test_default_path_uses_settings(clips_storage):
    s = Store()
    assert s.path == clips_storage / "clips.sqlite"
    assert s.path.exists()


def test_new_clip_id():
    a, b = new_clip_id(), new_clip_id()
    assert len(a) == 12 and int(a, 16) >= 0 and a != b


def test_source_roundtrip_and_fill(store):
    src = Source(id="a", name="A", platform=Platform.YOUTUBE, facecam_box=(0.1, 0.2, 0.3, 0.4), rights=RightsBasis.OWN)
    saved = store.save_source(src)
    assert saved.added_at is not None and saved.added_at.tzinfo is not None
    assert store.get_source("a") == saved
    assert store.get_source("zzz") is None


def test_source_list_enabled_delete(store):
    store.save_source(Source(id="a", name="A", platform=Platform.FOLDER, enabled=True))
    store.save_source(Source(id="b", name="B", platform=Platform.FOLDER))
    assert [s.id for s in store.list_sources()] == ["a", "b"]
    assert [s.id for s in store.list_sources(enabled_only=True)] == ["a"]
    assert store.delete_source("a") is True
    assert store.delete_source("a") is False


def test_video_roundtrip_filters(store):
    t = datetime(2026, 1, 1, tzinfo=UTC)
    v1 = SourceVideo(id="youtube:1", source_id="a", platform=Platform.YOUTUBE, native_id="1", published_at=t)
    v2 = SourceVideo(
        id="youtube:2", source_id="a", platform=Platform.YOUTUBE, native_id="2", status=VideoStatus.DONE
    )
    v3 = SourceVideo(id="youtube:3", source_id="b", platform=Platform.YOUTUBE, native_id="3")
    for v in (v1, v2, v3):
        store.save_video(v)
    got = store.get_video("youtube:1")
    assert got.published_at == t and got.seen_at is not None
    assert [v.id for v in store.list_videos()] == ["youtube:1", "youtube:2", "youtube:3"]
    assert [v.id for v in store.list_videos(source_id="a")] == ["youtube:1", "youtube:2"]
    assert [v.id for v in store.list_videos(status=VideoStatus.DONE)] == ["youtube:2"]
    assert len(store.list_videos(limit=2)) == 2
    assert store.known_native_ids("a") == {"1", "2"}
    assert store.known_native_ids("none") == set()
    assert store.get_video("nope") is None


def test_video_replace_keeps_one_row(store):
    v = SourceVideo(id="youtube:1", source_id="a", platform=Platform.YOUTUBE, native_id="1")
    store.save_video(v)
    store.save_video(v.model_copy(update={"status": VideoStatus.FAILED, "error": "x"}))
    assert len(store.list_videos()) == 1
    assert store.get_video("youtube:1").status is VideoStatus.FAILED


def test_clip_roundtrip_exact(store):
    clip = mk_clip(text=Copy(title="T", description="D", tags=["a"]), kind=ClipKind.LONG)
    saved = store.save_clip(clip)
    assert saved.created_at and saved.updated_at
    assert store.get_clip(clip.id) == saved
    assert store.get_clip("nope") is None


def test_save_clip_keeps_created_at_and_bumps_updated(store):
    first = store.save_clip(mk_clip())
    second = store.save_clip(first)
    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at


def test_list_clips_filters_and_order(store):
    ids = []
    for status, src in [
        (ClipStatus.DRAFT, "s"),
        (ClipStatus.APPROVED, "s"),
        (ClipStatus.APPROVED, "t"),
        (ClipStatus.REJECTED, "t"),
    ]:
        c = store.save_clip(mk_clip(status=status, source_id=src))
        ids.append(c.id)
    assert [c.id for c in store.list_clips()] == ids
    assert [c.id for c in store.list_clips(ClipStatus.APPROVED)] == ids[1:3]
    assert [c.id for c in store.list_clips([ClipStatus.DRAFT, ClipStatus.REJECTED])] == [ids[0], ids[3]]
    assert [c.id for c in store.list_clips(ClipStatus.APPROVED, source_id="t")] == [ids[2]]
    assert len(store.list_clips(limit=1)) == 1


def test_set_clip_status_sets_fields(store):
    clip = store.save_clip(mk_clip())
    when = datetime(2026, 12, 1, 15, tzinfo=UTC)
    out = store.set_clip_status(clip.id, ClipStatus.SCHEDULED, publish_at=when, error="")
    assert out.status is ClipStatus.SCHEDULED and out.publish_at == when
    again = store.set_clip_status(clip.id, ClipStatus.REJECTED, reject_reason="dull")
    assert again.reject_reason == "dull" and again.publish_at == when
    assert store.get_clip(clip.id) == again
    out = store.set_clip_status(clip.id, ClipStatus.DRAFT, text=Copy(title="x", description="y"))
    assert out.text.title == "x"


def test_set_clip_status_errors(store):
    with pytest.raises(KeyError):
        store.set_clip_status("missing", ClipStatus.APPROVED)
    clip = store.save_clip(mk_clip())
    with pytest.raises(ValueError):
        store.set_clip_status(clip.id, ClipStatus.APPROVED, bogus=1)


def test_booked_times_and_first_post_date(store):
    assert store.booked_times() == [] and store.first_post_date() is None
    t1 = datetime(2026, 12, 3, 23, 30, tzinfo=UTC)
    t2 = datetime(2026, 12, 1, 10, tzinfo=UTC)
    store.save_clip(mk_clip(status=ClipStatus.SCHEDULED, publish_at=t1))
    store.save_clip(mk_clip(status=ClipStatus.UPLOADED, publish_at=t2))
    store.save_clip(mk_clip(status=ClipStatus.UPLOADING, publish_at=t2 + timedelta(hours=5)))
    store.save_clip(mk_clip(status=ClipStatus.REJECTED, publish_at=t2 - timedelta(days=9)))
    store.save_clip(mk_clip(status=ClipStatus.SCHEDULED))  # no time: not booked
    assert store.booked_times() == [t2, t2 + timedelta(hours=5), t1]
    assert store.first_post_date() == date(2026, 12, 1)


def test_first_post_date_is_utc(store):
    tz = timezone_plus(-5)
    store.save_clip(mk_clip(status=ClipStatus.UPLOADED, publish_at=datetime(2026, 12, 1, 22, tzinfo=tz)))
    assert store.first_post_date() == date(2026, 12, 2)


def timezone_plus(hours):
    from datetime import timezone

    return timezone(timedelta(hours=hours))


def test_events(store):
    store.add_event("watch", "one")
    store.add_event("upload", "two", clip_id="c", source_id="s", data={"n": 1})
    ev = store.list_events()
    assert [e["message"] for e in ev] == ["two", "one"]
    assert ev[0]["clip_id"] == "c" and ev[0]["source_id"] == "s" and ev[0]["data"] == {"n": 1}
    assert ev[1]["data"] == {}
    assert [e["message"] for e in store.list_events(kind="watch")] == ["one"]
    assert len(store.list_events(limit=1)) == 1


def test_flags(store):
    assert store.get_flag("k") == "" and store.get_flag("k", "d") == "d"
    store.set_flag("k", "1")
    store.set_flag("k", "2")
    assert store.get_flag("k", "d") == "2"


def test_data_survives_reopen(clips_storage):
    p = clips_storage / "r.sqlite"
    Store(p).set_flag("a", "b")
    assert Store(p).get_flag("a") == "b"


def test_wal_mode(store):
    import sqlite3

    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_concurrent_writes_from_threads_and_two_stores(clips_storage):
    p = clips_storage / "c.sqlite"
    a, b = Store(p), Store(p)
    errors: list[BaseException] = []

    def work(store: Store, tag: str) -> None:
        try:
            for i in range(25):
                store.save_clip(mk_clip(source_id=tag))
                store.add_event("t", f"{tag}{i}")
                store.set_flag(f"{tag}{i}", "x")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(a if i % 2 else b, f"s{i}")) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(a.list_clips()) == 100
    assert len(b.list_events(limit=500)) == 100


def test_concurrent_status_updates_do_not_lose_fields(clips_storage):
    p = clips_storage / "d.sqlite"
    store = Store(p)
    clip = store.save_clip(mk_clip())
    other = Store(p)

    def a() -> None:
        for _ in range(20):
            store.set_clip_status(clip.id, ClipStatus.APPROVED, youtube_id="yt")

    def b() -> None:
        for _ in range(20):
            other.set_clip_status(clip.id, ClipStatus.APPROVED, error="boom")

    ts = [threading.Thread(target=a), threading.Thread(target=b)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    final = store.get_clip(clip.id)
    assert final.youtube_id == "yt" and final.error == "boom"


def test_claim_upload_is_won_once(store):
    clip = store.save_clip(mk_clip(status=ClipStatus.SCHEDULED))
    wins = []
    threads = [threading.Thread(target=lambda: wins.append(store.claim_upload(clip.id))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1
    assert store.get_clip(clip.id).status is ClipStatus.UPLOADING
    assert store.claim_upload("nope") is False


def test_claim_upload_refuses_clips_that_are_not_scheduled(store):
    for status in (ClipStatus.DRAFT, ClipStatus.APPROVED, ClipStatus.HELD, ClipStatus.UPLOADED):
        clip = store.save_clip(mk_clip(status=status))
        assert store.claim_upload(clip.id) is False


def test_stale_uploading_clips_go_back_to_scheduled(store):
    fresh = store.save_clip(mk_clip(status=ClipStatus.UPLOADING))
    old = store.save_clip(mk_clip(status=ClipStatus.UPLOADING))
    # age one of them: write the old stamp straight into the row
    stale = old.model_copy(update={"updated_at": datetime.now(UTC) - timedelta(hours=5)})
    with store._conn() as conn:
        store._write_clip(conn, stale)
    assert store.recover_stale_uploads(older_than_hours=2) == [old.id]
    assert store.get_clip(old.id).status is ClipStatus.SCHEDULED
    assert store.get_clip(fresh.id).status is ClipStatus.UPLOADING


def test_rebook_overdue_sends_missed_slots_back_to_approved(store):
    now = datetime.now(UTC)
    late = store.save_clip(mk_clip(status=ClipStatus.SCHEDULED, publish_at=now - timedelta(hours=1)))
    soon = store.save_clip(mk_clip(status=ClipStatus.SCHEDULED, publish_at=now + timedelta(minutes=5)))
    later = store.save_clip(mk_clip(status=ClipStatus.SCHEDULED, publish_at=now + timedelta(hours=4)))
    moved = store.rebook_overdue(now + timedelta(minutes=30))
    assert set(moved) == {late.id, soon.id}
    got = store.get_clip(late.id)
    assert got.status is ClipStatus.APPROVED and got.publish_at is None
    assert store.get_clip(later.id).status is ClipStatus.SCHEDULED
