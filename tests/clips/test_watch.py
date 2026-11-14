import os
import time

import pytest

from clips import watch
from clips.models import Platform, RightsBasis, Source


def _yt(**kw):
    kw.setdefault("channel_id", "UCchan")
    return Source(id="s", name="S", platform=Platform.YOUTUBE, url="https://www.youtube.com/@chan", **kw)


def _entries():
    return [{**e, "channel_id": "UCchan"} for e in _raw_entries()]


def _raw_entries():
    return [
        {"id": "long1", "title": "Long one", "duration": 1800, "timestamp": 1760000000},
        {"id": "short1", "title": "Short", "duration": 45},
        {"id": "short2", "title": "Short url", "duration": 300, "url": "https://youtube.com/shorts/short2"},
        {"id": "live1", "title": "Live", "duration": None, "live_status": "is_live"},
        {"id": "up1", "title": "Upcoming", "duration": None},
        {"id": "known1", "title": "Known", "duration": 900},
        {"id": "long2", "title": "Long two", "duration": 600, "upload_date": "20261101",
         "license": "Creative Commons Attribution license (reuse allowed)"},
    ]


def test_youtube_filters_and_maps(monkeypatch):
    seen = {}
    monkeypatch.setattr(watch, "_ytdlp_list", lambda url, limit: seen.setdefault("url", url) and _entries())
    out = watch.list_new_videos(_yt(), {"known1"})
    assert seen["url"].endswith("/@chan/videos")
    assert [v.native_id for v in out] == ["long1", "long2"]
    assert out[0].id == "s:youtube:long1" and out[0].published_at.year >= 2025
    assert out[1].license == "creativeCommon" and out[1].published_at.day == 1


def test_limit_and_known(monkeypatch):
    monkeypatch.setattr(watch, "_ytdlp_list", lambda url, limit: _entries())
    out = watch.list_new_videos(_yt(), set(), limit=1)
    assert len(out) == 1


def test_cc_source_looks_up_license(monkeypatch):
    monkeypatch.setattr(watch, "_ytdlp_list", lambda url, limit: [{"id": "a", "duration": 700, "channel_id": "UCchan"}])
    monkeypatch.setattr(watch, "_ytdlp_video_info", lambda url: {"license": "Standard YouTube License"})
    out = watch.list_new_videos(_yt(rights=RightsBasis.CREATIVE_COMMONS), set())
    assert out[0].license == "youtube"


def test_license_mapping():
    assert watch.map_license("Creative Commons Attribution license (reuse allowed)") == "creativeCommon"
    assert watch.map_license("Standard YouTube License") == "youtube"
    assert watch.map_license(None) == ""


def test_no_url_is_plain_error():
    src = Source(id="s", name="S", platform=Platform.YOUTUBE, rights=RightsBasis.OWN)
    with pytest.raises(watch.WatchError, match="no channel URL"):
        watch.list_new_videos(src, set())


def test_channel_id_builds_url():
    src = Source(id="s", name="S", platform=Platform.YOUTUBE, channel_id="UC123")
    assert watch._channel_videos_url(src) == "https://www.youtube.com/channel/UC123/videos"


def test_twitch_returns_empty():
    src = Source(id="t", name="T", platform=Platform.TWITCH, url="https://twitch.tv/x")
    assert watch.list_new_videos(src, set()) == []


def test_folder(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    old = time.time() - 600
    for name in ("a.mp4", "b.MKV", "c.txt", "known.mp4"):
        (folder / name).write_bytes(b"x")
        os.utime(folder / name, (old, old))
    (folder / "fresh.mp4").write_bytes(b"x")  # still being written
    src = Source(id="f", name="F", platform=Platform.FOLDER, folder=str(folder))
    out = watch.list_new_videos(src, {"known.mp4"})
    assert sorted(v.native_id for v in out) == ["a.mp4", "b.MKV"]
    assert all(v.path and v.id == f"f:folder:{v.native_id}" for v in out)


def test_missing_folder(tmp_path):
    src = Source(id="f", name="F", platform=Platform.FOLDER, folder=str(tmp_path / "nope"))
    assert watch.list_new_videos(src, set()) == []


def test_ytdlp_error_message(monkeypatch):
    msg = watch._explain("u", RuntimeError("Sign in to confirm you're not a bot"))
    assert "cookies" in msg.lower()


def test_other_creators_videos_are_dropped(monkeypatch):
    entries = [
        {"id": "mine", "duration": 900, "channel_id": "UCchan"},
        {"id": "theirs", "duration": 900, "channel_id": "UCother"},
        {"id": "nochan", "duration": 900},
    ]
    monkeypatch.setattr(watch, "_ytdlp_list", lambda url, limit: entries)
    out = watch.list_new_videos(_yt(rights=RightsBasis.WRITTEN_PERMISSION), set())
    assert [v.native_id for v in out] == ["mine"]


def test_non_own_youtube_source_needs_channel_id():
    src = Source(id="s", name="S", platform=Platform.YOUTUBE, url="https://www.youtube.com/@c",
                 rights=RightsBasis.WRITTEN_PERMISSION)
    with pytest.raises(watch.WatchError, match="channel id"):
        watch.list_new_videos(src, set())


@pytest.mark.parametrize("url", ["https://www.youtube.com/playlist?list=PL1", "https://www.youtube.com/watch?v=abc"])
def test_playlist_and_video_urls_refused_unless_own(url):
    src = _yt(rights=RightsBasis.WRITTEN_PERMISSION)
    src.url = url
    with pytest.raises(watch.WatchError, match="playlist or single-video"):
        watch._channel_videos_url(src)
    src.rights = RightsBasis.OWN
    assert watch._channel_videos_url(src) == url


def test_same_name_in_two_folder_sources_has_different_ids(tmp_path):
    ids = set()
    for sid in ("a", "b"):
        folder = tmp_path / sid
        folder.mkdir()
        (folder / "gameplay.mp4").write_bytes(b"x")
        old = time.time() - 600
        os.utime(folder / "gameplay.mp4", (old, old))
        src = Source(id=sid, name=sid, platform=Platform.FOLDER, folder=str(folder))
        ids |= {v.id for v in watch.list_new_videos(src, set())}
    assert len(ids) == 2


def test_folder_files_are_dated_by_their_modified_time(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    stamp = 1_700_000_000
    (folder / "a.mp4").write_bytes(b"x")
    os.utime(folder / "a.mp4", (stamp, stamp))
    src = Source(id="f", name="F", platform=Platform.FOLDER, folder=str(folder))
    (video,) = watch.list_new_videos(src, set())
    assert video.published_at is not None
    assert int(video.published_at.timestamp()) == stamp


# --- streams

def _stream_entries():
    return [
        {"id": "st1", "title": "Stream", "duration": 7200, "live_status": "was_live", "timestamp": 1760100000},
        {"id": "now", "title": "Live now", "duration": None, "live_status": "is_live"},
        {"id": "soon", "title": "Upcoming", "duration": None, "live_status": "is_upcoming"},
        {"id": "proc", "title": "Processing", "duration": 5000, "live_status": "post_live"},
        {"id": "nodur", "title": "Not processed", "duration": None},
    ]


def _two_tabs(monkeypatch, uploads, streams):
    urls = []

    def fake(url, limit):
        urls.append(url)
        data = streams if url.endswith("/streams") else uploads
        if isinstance(data, Exception):
            raise data
        return [{**e, "channel_id": "UCchan"} for e in data]

    monkeypatch.setattr(watch, "_ytdlp_list", fake)
    return urls


def test_both_tabs_are_listed_and_streams_are_marked(monkeypatch):
    urls = _two_tabs(monkeypatch, [{"id": "up", "duration": 900, "timestamp": 1760000000}], _stream_entries())
    out = watch.list_new_videos(_yt(), set())
    assert urls == ["https://www.youtube.com/@chan/videos", "https://www.youtube.com/@chan/streams"]
    assert {v.native_id: v.was_live for v in out} == {"up": False, "st1": True}
    assert [v.native_id for v in out] == ["up", "st1"]  # tabs alternate, uploads first


def test_live_upcoming_and_unprocessed_streams_are_skipped(monkeypatch):
    _two_tabs(monkeypatch, [], _stream_entries())
    assert [v.native_id for v in watch.list_new_videos(_yt(), set())] == ["st1"]


def test_streams_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(watch, "_include_streams", lambda: False)
    urls = _two_tabs(monkeypatch, [{"id": "up", "duration": 900}], _stream_entries())
    out = watch.list_new_videos(_yt(), set())
    assert urls == ["https://www.youtube.com/@chan/videos"] and [v.native_id for v in out] == ["up"]


def test_no_id_is_returned_twice(monkeypatch):
    same = {"id": "x", "duration": 900, "timestamp": 1760000000}
    _two_tabs(monkeypatch, [same], [{**same, "live_status": "was_live"}])
    out = watch.list_new_videos(_yt(), set())
    assert [v.native_id for v in out] == ["x"]


def test_a_failing_live_tab_keeps_the_uploads(monkeypatch):
    _two_tabs(monkeypatch, [{"id": "up", "duration": 900}], watch.WatchError("no streams tab"))
    assert [v.native_id for v in watch.list_new_videos(_yt(), set())] == ["up"]


def test_a_failing_uploads_tab_keeps_the_streams(monkeypatch):
    _two_tabs(monkeypatch, watch.WatchError("boom"), _stream_entries())
    assert [v.native_id for v in watch.list_new_videos(_yt(), set())] == ["st1"]


def test_both_tabs_failing_raises(monkeypatch):
    _two_tabs(monkeypatch, watch.WatchError("first"), watch.WatchError("second"))
    with pytest.raises(watch.WatchError, match="first"):
        watch.list_new_videos(_yt(), set())


def test_undated_listings_alternate_so_streams_are_not_starved(monkeypatch):
    ups = [{"id": f"u{i}", "duration": 900} for i in range(5)]
    sts = [{"id": f"s{i}", "duration": 3000, "live_status": "was_live"} for i in range(5)]
    _two_tabs(monkeypatch, ups, sts)
    out = watch.list_new_videos(_yt(), set(), limit=4)
    assert [v.native_id for v in out] == ["u0", "s0", "u1", "s1"]


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/@chan",
        "https://www.youtube.com/@chan/",
        "https://www.youtube.com/@chan/videos",
        "https://www.youtube.com/@chan/streams/",
        "https://www.youtube.com/@chan/featured?si=abc",
        "@chan",
        "www.youtube.com/@chan",
    ],
)
def test_handle_link_forms(url):
    src = _yt()
    src.url = url
    assert watch._channel_videos_url(src) == "https://www.youtube.com/@chan/videos"
    assert watch._channel_streams_url(src) == "https://www.youtube.com/@chan/streams"


@pytest.mark.parametrize(
    "url,root",
    [
        ("https://www.youtube.com/channel/UCabc", "https://www.youtube.com/channel/UCabc"),
        ("https://www.youtube.com/channel/UCabc/streams", "https://www.youtube.com/channel/UCabc"),
        ("https://www.youtube.com/c/name/", "https://www.youtube.com/c/name"),
        ("https://www.youtube.com/c/name/videos", "https://www.youtube.com/c/name"),
        ("https://www.youtube.com/user/name", "https://www.youtube.com/user/name"),
        ("https://www.youtube.com/user/name/streams", "https://www.youtube.com/user/name"),
    ],
)
def test_other_link_forms(url, root):
    src = _yt()
    src.url = url
    assert watch._channel_videos_url(src) == root + "/videos"
    assert watch._channel_streams_url(src) == root + "/streams"


def test_own_single_video_link_has_no_streams_tab():
    src = _yt(rights=RightsBasis.OWN)
    src.url = "https://www.youtube.com/watch?v=abc"
    assert watch._channel_streams_url(src) is None
