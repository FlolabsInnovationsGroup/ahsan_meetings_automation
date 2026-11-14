"""The side panel's Publishing choices: GET /publishing/settings and POST."""

import dataclasses
import json

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from ledger import files
from publish import settings as publishing


@pytest.fixture
def client():
    return TestClient(api_main.app)


def test_get_returns_settings_and_defaults(client):
    body = client.get("/publishing/settings").json()
    assert body["settings"] == publishing.as_dict(publishing.DEFAULTS)
    assert body["defaults"] == publishing.as_dict(publishing.DEFAULTS)


def test_post_saves_and_get_returns_the_new_values(client):
    res = client.post("/publishing/settings", json={"max_per_day": 3, "hashtags": False, "links_text": "https://flolabs.com"})
    assert res.status_code == 200
    assert res.json()["settings"]["max_per_day"] == 3
    got = client.get("/publishing/settings").json()
    assert got["settings"]["max_per_day"] == 3
    assert got["settings"]["hashtags"] is False
    assert got["settings"]["links_text"] == "https://flolabs.com"
    assert got["defaults"]["max_per_day"] == publishing.DEFAULTS.max_per_day


def test_bad_value_is_400_with_a_message_and_saves_nothing(client):
    # one good value and one bad: neither is kept
    res = client.post("/publishing/settings", json={"max_per_day": 3, "review_hours": 500})
    assert res.status_code == 400
    assert "review_hours" in res.json()["detail"]
    assert client.get("/publishing/settings").json()["settings"]["max_per_day"] == publishing.DEFAULTS.max_per_day


def test_unknown_field_is_400(client):
    res = client.post("/publishing/settings", json={"post_everything": True})
    assert res.status_code == 400
    assert "post_everything" in res.json()["detail"]


def test_damaged_saved_value_falls_back_to_its_default(client):
    files.write_json(files.state_dir() / "prefs.json", {"publishing": {"max_per_day": "lots", "hashtags": False}})
    got = client.get("/publishing/settings").json()["settings"]
    assert got["max_per_day"] == publishing.DEFAULTS.max_per_day
    assert got["hashtags"] is False  # the good value next to it is kept
    assert json.dumps(got)


# --- the growth settings (all 49 new fields, looks(), capacity) ---------------

NEW_FIELDS = [
    "shorts_style", "shorts_hook_seconds", "shorts_top_text", "shorts_end_line", "shorts_caption_px",
    "shorts_caption_max_chars", "shorts_layout", "shorts_screen_zoom", "shorts_speaker_layout", "shorts_hd",
    "shorts_trim_pauses", "shorts_max_pause_s", "shorts_loudness", "full_loudness", "loudness_target_lufs",
    "audio_denoise", "full_hd", "full_hook_overlay", "thumbnail_mode", "thumbnail_shorts", "thumbnail_faces",
    "thumbnail_label", "captions_full", "captions_shorts", "translations", "translation_languages", "playlists",
    "playlist_shorts", "playlist_cuts", "playlist_full_template", "playlist_shorts_template",
    "playlist_cuts_template", "series_mode", "series_archive_privacy", "topic_cuts", "topic_cuts_per_meeting",
    "topic_min_minutes", "topic_max_minutes", "topic_min_score", "topic_cold_open", "topic_front_door",
    "shorts_link_target", "views_pull", "views_include_other", "views_watch_time", "views_rankings",
    "quota_daily_units", "quota_reserve_units", "extras_retry_days",
]
CHOICES = {
    "shorts_style": "classic", "shorts_top_text": "none", "shorts_layout": "fit", "shorts_screen_zoom": "zoom",
    "shorts_speaker_layout": "panel", "thumbnail_mode": "custom", "series_mode": "allow_all",
    "series_archive_privacy": "skip", "shorts_link_target": "full",
}
OUT_OF_RANGE = {
    "shorts_hook_seconds": [-1, 5.5], "shorts_caption_px": [55, 111], "shorts_caption_max_chars": [15, 41],
    "shorts_max_pause_s": [0.2, 2.5], "loudness_target_lufs": [-24, -9], "topic_cuts_per_meeting": [-1, 4],
    "topic_min_minutes": [1, 31], "topic_max_minutes": [4, 41], "topic_min_score": [-1, 11],
    "quota_daily_units": [999, 1000001], "quota_reserve_units": [-1, 9001], "extras_retry_days": [0, 31],
}


def _save(**changes):
    return publishing.save(changes)


def test_the_release_plan_defaults_are_six_a_day_two_hours_apart():
    # the owner's decision (2026-11-14): 6 a day, 2 h gaps (were 5 and 2.5)
    assert publishing.DEFAULTS.max_per_day == 6
    assert publishing.DEFAULTS.min_gap_hours == 2.0
    assert publishing.DEFAULTS.shorts_per_day == 3


def test_every_new_field_exists_and_comes_back_with_its_default(client):
    assert len(NEW_FIELDS) == 49
    body = client.get("/publishing/settings").json()
    for name in NEW_FIELDS:
        assert name in body["settings"], name
        assert body["settings"][name] == getattr(publishing.DEFAULTS, name)
    assert body["settings"]["shorts_style"] == "alternate"
    assert body["settings"]["translations"] is False
    assert body["settings"]["translation_languages"] == "es,pt,hi,id,ja,de"
    assert body["warning"] is None


def test_every_new_field_round_trips_through_save_and_current():
    changes = {n: getattr(publishing.DEFAULTS, n) for n in NEW_FIELDS}
    saved = publishing.save(changes)
    assert publishing.current() == saved
    for name in NEW_FIELDS:
        assert getattr(saved, name) == getattr(publishing.DEFAULTS, name)


def test_each_new_value_can_be_saved_and_read_back():
    publishing.save({**CHOICES, "shorts_hook_seconds": 0, "loudness_target_lufs": -16, "audio_denoise": True,
                     "topic_min_minutes": 3, "topic_max_minutes": 12, "quota_reserve_units": 500})
    got = publishing.current()
    for name, value in CHOICES.items():
        assert getattr(got, name) == value
    assert got.loudness_target_lufs == -16.0
    assert isinstance(got.loudness_target_lufs, float)
    assert got.audio_denoise is True
    assert got.shorts_hook_seconds == 0.0


@pytest.mark.parametrize("name", sorted(publishing._CHOICES))
def test_a_select_refuses_an_unknown_value(client, name):
    res = client.post("/publishing/settings", json={name: "sideways"})
    assert res.status_code == 400
    assert f"{name}: one of " in res.json()["detail"]


def test_title_styles_stay_exported():
    assert publishing.TITLE_STYLES == ("topic_first", "meeting_date")


@pytest.mark.parametrize(("name", "bad"), [(n, v) for n, vs in OUT_OF_RANGE.items() for v in vs])
def test_a_number_outside_its_range_is_refused(client, name, bad):
    res = client.post("/publishing/settings", json={name: bad})
    assert res.status_code == 400
    assert name in res.json()["detail"]


def test_a_number_must_be_a_number():
    with pytest.raises(ValueError, match="shorts_caption_px"):
        _save(shorts_caption_px="big")


@pytest.mark.parametrize("bad", ["x" * 41, "go <b>", "a > b"])
def test_end_line_too_long_or_with_angle_brackets_is_refused(bad):
    with pytest.raises(ValueError, match="shorts_end_line"):
        _save(shorts_end_line=bad)


def test_end_line_may_be_empty_and_is_stripped():
    assert _save(shorts_end_line="").shorts_end_line == ""
    assert _save(shorts_end_line="  Watch the rest  ").shorts_end_line == "Watch the rest"
    assert _save(shorts_end_line="x" * 40).shorts_end_line == "x" * 40


def test_translation_languages_are_normalised():
    assert _save(translation_languages="es, pt-BR").translation_languages == "es,pt-BR"
    assert publishing.current().translation_languages == "es,pt-BR"


@pytest.mark.parametrize("bad", [
    "", "e", "es,,pt", "es,p t", "es,pt!", "es,ES", "en", "en-GB,es", "abcdefghi",
    "aa,bb,cc,dd,ee,ff,gg,hh,ii,jj,kk",
])
def test_bad_translation_languages_are_refused(bad):
    with pytest.raises(ValueError, match="translation_languages|Translations"):
        _save(translation_languages=bad)


def test_ten_languages_are_allowed():
    ten = "aa,bb,cc,dd,ee,ff,gg,hh,ii,jj"
    assert _save(translation_languages=ten).translation_languages == ten


def test_translations_may_not_include_the_main_language_even_with_a_region():
    with pytest.raises(ValueError, match="own language"):
        _save(language="pt-BR", translation_languages="es,pt")
    with pytest.raises(ValueError, match="own language"):
        _save(translation_languages="en-GB,es")


@pytest.mark.parametrize("lang", ["de", "pt-BR", "es"])
def test_a_saved_file_from_before_translations_keeps_its_values(client, lang):
    files.write_json(files.state_dir() / "prefs.json",
                     {"publishing": {"language": lang, "scheduled": False, "links_text": "my links"}})
    p = publishing.current()
    assert p.language == lang and p.scheduled is False and p.links_text == "my links"
    main = lang.split("-")[0]
    assert all(c.split("-")[0] != main for c in p.translation_languages.split(","))


def test_saving_another_language_trims_the_default_translation_list():
    assert "es" not in _save(language="es").translation_languages.split(",")


@pytest.mark.parametrize("name", ["playlist_full_template", "playlist_shorts_template", "playlist_cuts_template"])
@pytest.mark.parametrize("bad", [
    "Team meetings", "{series} and {series}", "{series} {x}", "{series} }", "{ {series}",
    "<b>{series}", "{series} >", "{series}" + "x" * 101,
])
def test_a_bad_playlist_template_is_refused(name, bad):
    with pytest.raises(ValueError, match=name):
        publishing.save({name: bad})


def test_a_playlist_template_is_stripped_and_the_length_counts_a_filled_series():
    assert _save(playlist_full_template="  Team {series}  ").playlist_full_template == "Team {series}"
    # {series} counts as 50 characters: 50 + 100 = 150 is the longest allowed
    assert _save(playlist_cuts_template="{series}" + "x" * 100).playlist_cuts_template == "{series}" + "x" * 100


def test_topic_minimum_must_be_below_the_maximum():
    with pytest.raises(ValueError, match="shortest length"):
        _save(topic_min_minutes=10, topic_max_minutes=10)
    with pytest.raises(ValueError, match="shortest length"):
        _save(topic_min_minutes=12, topic_max_minutes=8)


def test_quota_reserve_must_be_below_the_daily_quota():
    with pytest.raises(ValueError, match="reserve"):
        _save(quota_daily_units=1000, quota_reserve_units=1000)
    assert _save(quota_daily_units=1000, quota_reserve_units=999).quota_reserve_units == 999


def test_a_refused_change_saves_nothing(client):
    res = client.post("/publishing/settings", json={"shorts_hd": False, "shorts_style": "wild"})
    assert res.status_code == 400
    assert client.get("/publishing/settings").json()["settings"]["shorts_hd"] is True


def test_damaged_new_saved_values_keep_their_defaults(client):
    files.write_json(files.state_dir() / "prefs.json", {"publishing": {
        "shorts_style": "wild", "shorts_caption_px": 5, "translation_languages": "x", "shorts_end_line": 7,
        "playlist_full_template": "no series", "topic_min_minutes": 9, "shorts_hd": False}})
    got = client.get("/publishing/settings").json()["settings"]
    d = publishing.DEFAULTS
    assert got["shorts_style"] == d.shorts_style
    assert got["shorts_caption_px"] == d.shorts_caption_px
    assert got["translation_languages"] == d.translation_languages
    assert got["playlist_full_template"] == d.playlist_full_template
    assert got["shorts_hd"] is False
    assert got["topic_min_minutes"] == 9


def test_looks_are_frozen_snapshots_that_match_current():
    p = publishing.current()
    lk = publishing.looks()
    assert lk.shorts.style == p.shorts_style
    assert lk.shorts.loudness == p.shorts_loudness
    assert lk.shorts.denoise == p.audio_denoise
    assert lk.shorts.target_lufs == p.loudness_target_lufs
    assert lk.full.loudness == p.full_loudness
    assert lk.full.hd == p.full_hd
    assert lk.full.hook_overlay == p.full_hook_overlay
    sd = lk.shorts.as_dict()
    assert sd == {"style": p.shorts_style, "hook_seconds": p.shorts_hook_seconds, "top_text": p.shorts_top_text,
                  "end_line": p.shorts_end_line, "caption_px": p.shorts_caption_px,
                  "caption_max_chars": p.shorts_caption_max_chars, "layout": p.shorts_layout,
                  "screen_zoom": p.shorts_screen_zoom, "speaker_layout": p.shorts_speaker_layout,
                  "hd": p.shorts_hd, "loudness": p.shorts_loudness, "target_lufs": p.loudness_target_lufs,
                  "denoise": p.audio_denoise, "trim_pauses": p.shorts_trim_pauses,
                  "max_pause_s": p.shorts_max_pause_s}
    assert lk.full.as_dict() == {"loudness": p.full_loudness, "target_lufs": p.loudness_target_lufs,
                                 "hd": p.full_hd, "hook_overlay": p.full_hook_overlay}
    assert lk.as_dict() == {"shorts": sd, "full": lk.full.as_dict()}
    with pytest.raises(dataclasses.FrozenInstanceError):
        lk.shorts.style = "new"
    # a later change does not alter a snapshot already taken
    publishing.save({"shorts_style": "classic", "full_hd": True})
    assert lk.shorts.style == "alternate"
    assert lk.full.hd is False
    assert publishing.looks().shorts.style == "classic"
    assert publishing.looks().full.hd is True


def test_looks_can_be_built_from_given_settings():
    lk = publishing.looks(publishing.Publishing(shorts_loudness=False, full_hook_overlay=True))
    assert lk.shorts.loudness is False
    assert lk.full.hook_overlay is True


def test_capacity_warning_keeps_todays_messages_and_defaults_fit():
    assert publishing.capacity_warning(publishing.DEFAULTS) is None
    assert "some Shorts would be skipped" in publishing.capacity_warning(publishing.Publishing(shorts_per_day=1))
    # without topic cuts the second message is word for word as before
    msg = publishing.capacity_warning(publishing.Publishing(topic_cuts=False, max_per_day=3, shorts_per_day=3))
    assert msg == "About 9 public meetings a week make 27 videos, but only 21 fit in a week: some would be skipped."


def test_capacity_warning_counts_topic_cuts():
    # 9 meetings + 9 * 0.6 cuts + 18 Shorts = 32.4 videos a week
    tight = publishing.Publishing(max_per_day=4, shorts_per_day=3, min_gap_hours=1.0)
    assert "topic cuts" in publishing.capacity_warning(tight)
    assert publishing.capacity_warning(dataclasses.replace(tight, topic_cuts=False)) is None  # 27 fit in 28
    assert publishing.capacity_warning(
        publishing.Publishing(max_per_day=5, shorts_per_day=3, min_gap_hours=1.0)) is None
    # more cuts per meeting push it over
    assert "topic cuts" in publishing.capacity_warning(
        publishing.Publishing(max_per_day=5, shorts_per_day=3, min_gap_hours=1.0, topic_cuts_per_meeting=3))


def test_capacity_warning_says_when_the_release_hours_cannot_hold_the_videos():
    p = publishing.Publishing(window_start_hour=10, window_end_hour=22, min_gap_hours=2.5, max_per_day=6)
    assert publishing.capacity_warning(p) == (
        "Only 5 videos fit in the release hours with 2.5 h between them, but videos per day is 6.")
    # no gap, or off: no such warning
    assert publishing.capacity_warning(dataclasses.replace(p, min_gap_hours=0)) is None
    assert publishing.capacity_warning(dataclasses.replace(p, scheduled=False)) is None
    assert publishing.capacity_warning(dataclasses.replace(p, min_gap_hours=2.0)) is None  # 7 fit
