import pytest

from clips.models import Platform, RightsBasis
from clips.sources import (
    RightsError,
    add_source,
    disable_source,
    enable_source,
    make_source_id,
)
from clips.store import Store

URL = "https://youtube.com/@c"
EVID = {"evidence_url": "https://example.com/p", "evidence_note": "n", "evidence_date": "2026-01-05"}


@pytest.fixture
def store(clips_storage):
    return Store(clips_storage / "s.sqlite")


def test_slug():
    assert make_source_id("  GTA Leaks & More!! ") == "gta-leaks-more"
    assert make_source_id("!!!") == "source"
    assert len(make_source_id("a" * 100)) <= 40


def test_add_defaults_disabled_none(store):
    s = add_source(store, name="Some Channel", platform=Platform.YOUTUBE, url=URL)
    assert s.id == "some-channel" and s.rights is RightsBasis.NONE and not s.enabled
    assert store.get_source("some-channel") == s


def test_add_same_name_gets_new_id(store):
    a = add_source(store, name="X", platform=Platform.YOUTUBE, url=URL)
    b = add_source(store, name="X", platform=Platform.YOUTUBE, url=URL)
    assert a.id == "x" and b.id == "x-2"


def test_add_enabled_with_none_refused(store):
    with pytest.raises(RightsError) as exc:
        add_source(store, name="X", platform=Platform.YOUTUBE, url=URL, enabled=True)
    assert exc.value.reasons
    assert store.list_sources() == []


def test_add_enabled_missing_evidence_refused(store):
    with pytest.raises(RightsError):
        add_source(store, name="X", platform=Platform.YOUTUBE, url=URL, rights=RightsBasis.OFFICIAL, enabled=True)
    assert store.list_sources() == []


def test_add_enabled_with_evidence_ok(store):
    s = add_source(
        store, name="X", platform=Platform.YOUTUBE, url=URL, rights=RightsBasis.OFFICIAL, enabled=True, **EVID
    )
    assert s.enabled


def test_enable_refused_for_none_always(store):
    s = add_source(store, name="X", platform=Platform.YOUTUBE, url=URL, **EVID)
    with pytest.raises(RightsError):
        enable_source(store, s.id)
    assert not store.get_source(s.id).enabled


def test_enable_after_recording_rights(store):
    s = add_source(store, name="X", platform=Platform.YOUTUBE, url=URL)
    store.save_source(s.model_copy(update={"rights": RightsBasis.OFFICIAL, **EVID}))
    assert enable_source(store, s.id).enabled
    assert store.get_source(s.id).enabled
    assert not disable_source(store, s.id).enabled
    assert not store.get_source(s.id).enabled


def test_enable_own_folder(store, tmp_path):
    s = add_source(store, name="Mine", platform=Platform.FOLDER, rights=RightsBasis.OWN, folder=str(tmp_path))
    assert enable_source(store, s.id).enabled


def test_unknown_source_keyerror(store):
    with pytest.raises(KeyError):
        enable_source(store, "nope")
    with pytest.raises(KeyError):
        disable_source(store, "nope")


def test_right_error_message_has_reasons():
    assert "because" in str(RightsError(["a because b"]))


def test_deleted_source_id_is_never_reused_and_its_queue_is_stopped(store):
    from clips.models import Clip, ClipStatus, Moment, SourceVideo, VideoStatus

    a = add_source(store, name="GTA Daily", platform=Platform.YOUTUBE, url=URL, rights=RightsBasis.OFFICIAL,
                   enabled=True, **EVID)
    store.save_video(SourceVideo(id="youtube:1", source_id=a.id, platform=Platform.YOUTUBE, native_id="1", url="u"))
    ids = {}
    for status in (ClipStatus.DRAFT, ClipStatus.APPROVED, ClipStatus.SCHEDULED, ClipStatus.UPLOADED, ClipStatus.REJECTED):
        clip = store.save_clip(Clip(id=f"c-{status.value}", source_id=a.id, video_id="youtube:1",
                                    moment=Moment(start_s=0, end_s=5), status=status))
        ids[status] = clip.id
    assert store.delete_source(a.id)
    assert store.get_video("youtube:1").status is VideoStatus.SKIPPED
    for status in (ClipStatus.DRAFT, ClipStatus.APPROVED, ClipStatus.SCHEDULED):
        assert store.get_clip(ids[status]).status is ClipStatus.HELD
    assert store.get_clip(ids[ClipStatus.UPLOADED]).status is ClipStatus.UPLOADED
    assert store.get_clip(ids[ClipStatus.REJECTED]).status is ClipStatus.REJECTED
    b = add_source(store, name="GTA Daily", platform=Platform.YOUTUBE, url=URL)
    assert b.id != a.id
    assert store.known_native_ids(b.id) == set()
    with pytest.raises(ValueError):
        add_source(store, name="x", platform=Platform.YOUTUBE, url=URL, id=a.id)
