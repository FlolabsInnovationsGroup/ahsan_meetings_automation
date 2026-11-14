"""The raw YouTube helpers (publish/youtube_extra.py) against a fake service:
the right request body and part list, and no network."""

from publish import youtube_extra


class _Call:
    def __init__(self, service, name, answer):
        self.service, self.name, self.answer = service, name, answer

    def execute(self):
        return self.answer


class _Resource:
    def __init__(self, service, resource):
        self.service, self.resource = service, resource

    def __getattr__(self, method):
        def call(**kwargs):
            self.service.calls.append((self.resource, method, kwargs))
            answers = self.service.answers.get((self.resource, method)) or [{}]
            answer = answers.pop(0) if len(answers) > 1 else answers[0]
            return _Call(self.service, method, answer)

        return call


class FakeService:
    def __init__(self, **answers):
        self.calls = []
        self.answers = {tuple(k.split("__")): v for k, v in answers.items()}

    def __getattr__(self, resource):
        return lambda: _Resource(self, resource)


def _use(monkeypatch, service):
    monkeypatch.setattr(youtube_extra, "_service", lambda: service)
    return service


def test_set_thumbnail_sends_a_jpeg(monkeypatch, tmp_path):
    service = _use(monkeypatch, FakeService())
    jpg = tmp_path / "t.jpg"
    jpg.write_bytes(b"\xff\xd8\xff")
    youtube_extra.set_thumbnail("vid1", jpg)
    [(resource, method, kwargs)] = service.calls
    assert (resource, method) == ("thumbnails", "set")
    assert kwargs["videoId"] == "vid1"
    assert kwargs["media_body"].mimetype() == "image/jpeg"


def test_processing_status_asks_for_processing_details(monkeypatch):
    service = _use(monkeypatch, FakeService(
        videos__list=[{"items": [{"processingDetails": {"processingStatus": "succeeded"}}]}]))
    assert youtube_extra.processing_status("vid1") == {"processingStatus": "succeeded"}
    assert service.calls == [("videos", "list", {"part": "processingDetails", "id": "vid1"})]


def test_processing_status_of_a_missing_video_is_none(monkeypatch):
    _use(monkeypatch, FakeService(videos__list=[{"items": []}]))
    assert youtube_extra.processing_status("gone") is None


def test_video_privacy_returns_status_and_publish_time(monkeypatch):
    service = _use(monkeypatch, FakeService(videos__list=[
        {"items": [{"status": {"privacyStatus": "private", "publishAt": "2026-12-01T10:00:00Z", "other": 1}}]}]))
    assert youtube_extra.video_privacy("vid1") == {"privacyStatus": "private", "publishAt": "2026-12-01T10:00:00Z"}
    assert service.calls == [("videos", "list", {"part": "status", "id": "vid1"})]
    _use(monkeypatch, FakeService(videos__list=[{"items": []}]))
    assert youtube_extra.video_privacy("gone") is None


def test_insert_caption_body_and_mimetype(monkeypatch, tmp_path):
    service = _use(monkeypatch, FakeService(captions__insert=[{"id": "cap1"}]))
    srt = tmp_path / "c.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
    assert youtube_extra.insert_caption("vid1", srt, "en", "English") == {"id": "cap1"}
    [(resource, method, kwargs)] = service.calls
    assert (resource, method) == ("captions", "insert")
    assert kwargs["part"] == "snippet"
    assert kwargs["body"] == {"snippet": {"videoId": "vid1", "language": "en", "name": "English", "isDraft": False}}
    assert kwargs["media_body"].mimetype() == "application/octet-stream"


def test_find_playlist_matches_the_exact_title_across_pages(monkeypatch):
    service = _use(monkeypatch, FakeService(playlists__list=[
        {"items": [{"id": "p1", "snippet": {"title": "Space team meetings"}}], "nextPageToken": "t2"},
        {"items": [{"id": "p2", "snippet": {"title": "Space Team"}}]},
    ]))
    assert youtube_extra.find_playlist("Space Team") == "p2"
    assert service.calls[0] == ("playlists", "list", {"part": "snippet", "mine": True, "maxResults": 50})
    assert service.calls[1][2]["pageToken"] == "t2"


def test_find_playlist_reads_at_most_four_pages(monkeypatch):
    page = {"items": [], "nextPageToken": "more"}
    service = _use(monkeypatch, FakeService(playlists__list=[page, page]))
    assert youtube_extra.find_playlist("Nothing") is None
    assert len(service.calls) == 4


def test_create_playlist_body(monkeypatch):
    service = _use(monkeypatch, FakeService(playlists__insert=[{"id": "p9"}]))
    assert youtube_extra.create_playlist("Space Team", "Every meeting") == "p9"
    [(resource, method, kwargs)] = service.calls
    assert (resource, method) == ("playlists", "insert")
    assert kwargs["part"] == "snippet,status"
    assert kwargs["body"] == {"snippet": {"title": "Space Team", "description": "Every meeting"},
                              "status": {"privacyStatus": "public"}}


def test_playlist_has_video_asks_for_that_video(monkeypatch):
    service = _use(monkeypatch, FakeService(playlistItems__list=[{"items": [{"id": "x"}]}]))
    assert youtube_extra.playlist_has_video("p1", "vid1") is True
    assert service.calls == [("playlistItems", "list",
                              {"part": "id", "playlistId": "p1", "videoId": "vid1", "maxResults": 1})]
    _use(monkeypatch, FakeService(playlistItems__list=[{"items": []}]))
    assert youtube_extra.playlist_has_video("p1", "vid1") is False


def test_add_to_playlist_body(monkeypatch):
    service = _use(monkeypatch, FakeService(playlistItems__insert=[{"id": "item1"}]))
    assert youtube_extra.add_to_playlist("p1", "vid1") == "item1"
    [(resource, method, kwargs)] = service.calls
    assert (resource, method) == ("playlistItems", "insert")
    assert kwargs["part"] == "snippet"
    assert kwargs["body"] == {"snippet": {"playlistId": "p1",
                                          "resourceId": {"kind": "youtube#video", "videoId": "vid1"}}}
