from datetime import UTC, datetime, timedelta

import pytest

from clips.models import Clip, Moment, Platform, RightsBasis, Source, SourceVideo
from clips.rights import check_clip, check_source, check_video, credit_line

TODAY = datetime.now(UTC).date().isoformat()
FULL = {
    "evidence_url": "https://example.com/policy",
    "evidence_note": "Yes, clip anything (email, kept in the shared drive)",
    "evidence_date": "2026-01-05",
}
WORD = {"evidence_url": "link", "evidence_note": "note", "evidence_date": "date"}

NEEDS = {
    RightsBasis.WRITTEN_PERMISSION: {"evidence_note", "evidence_date"},
    RightsBasis.PUBLIC_PERMISSION: {"evidence_url", "evidence_note", "evidence_date"},
    RightsBasis.CAMPAIGN: {"evidence_url", "evidence_date"},
    RightsBasis.OFFICIAL: {"evidence_url", "evidence_date"},
}


def yt(rights, **kw) -> Source:
    return Source(id="s", name="Chan", platform=Platform.YOUTUBE, url="https://youtube.com/@c", rights=rights, **kw)


def test_none_never_ok_even_with_all_evidence():
    v = check_source(yt(RightsBasis.NONE, enabled=True, **FULL))
    assert not v.ok and v.reasons


def test_default_source_not_ok():
    assert not check_source(Source(id="x", name="x", platform=Platform.YOUTUBE)).ok


def test_own_needs_nothing_more():
    assert check_source(yt(RightsBasis.OWN)).ok


def test_creative_commons_ok_at_source_level():
    assert check_source(yt(RightsBasis.CREATIVE_COMMONS)).ok


@pytest.mark.parametrize("basis", list(NEEDS))
def test_full_evidence_ok(basis):
    assert check_source(yt(basis, **FULL)).ok


@pytest.mark.parametrize("basis", list(NEEDS))
def test_each_missing_field_is_refused(basis):
    for field in NEEDS[basis]:
        data = {**FULL, field: ""}
        v = check_source(yt(basis, **data))
        assert not v.ok, (basis, field)
        assert any(WORD[field] in r for r in v.reasons)


@pytest.mark.parametrize("basis", list(NEEDS))
def test_whitespace_only_counts_as_missing(basis):
    for field in NEEDS[basis]:
        assert not check_source(yt(basis, **{**FULL, field: "   "})).ok


@pytest.mark.parametrize("bad", ["2026-1-5", "05/01/2026", "20260105", "yesterday", "2026-13-01", "2026-02-30"])
def test_bad_dates(bad):
    v = check_source(yt(RightsBasis.OFFICIAL, **{**FULL, "evidence_date": bad}))
    assert not v.ok and any("date" in r for r in v.reasons)


def test_future_date_refused_today_ok():
    future = (datetime.now(UTC) + timedelta(days=3)).date().isoformat()
    v = check_source(yt(RightsBasis.OFFICIAL, **{**FULL, "evidence_date": future}))
    assert not v.ok and any("future" in r for r in v.reasons)
    assert check_source(yt(RightsBasis.OFFICIAL, **{**FULL, "evidence_date": TODAY})).ok


def test_folder_rules(tmp_path):
    base = {"id": "f", "name": "Mine", "platform": Platform.FOLDER, "rights": RightsBasis.OWN}
    assert not check_source(Source(**base)).ok
    assert not check_source(Source(**base, folder=str(tmp_path / "nope"))).ok
    assert check_source(Source(**base, folder=str(tmp_path))).ok


@pytest.mark.parametrize("platform", [Platform.YOUTUBE, Platform.TWITCH])
def test_online_source_needs_url(platform):
    s = Source(id="s", name="n", platform=platform, rights=RightsBasis.OWN)
    assert not check_source(s).ok
    assert check_source(s.model_copy(update={"url": "https://x"})).ok


def vid(**kw) -> SourceVideo:
    base = {"id": "youtube:1", "source_id": "s", "platform": Platform.YOUTUBE, "native_id": "1", "url": "https://y/1"}
    base.update(kw)
    return SourceVideo(**base)


def test_check_video_passes_through_source_failure():
    assert not check_video(yt(RightsBasis.NONE), vid()).ok


def test_check_video_live_never_ok():
    assert not check_video(yt(RightsBasis.OWN), vid(is_live=True)).ok
    assert check_video(yt(RightsBasis.OWN), vid()).ok


def test_check_video_cc_needs_license():
    s = yt(RightsBasis.CREATIVE_COMMONS)
    assert check_video(s, vid(license="creativeCommon")).ok
    assert not check_video(s, vid(license="youtube")).ok
    assert not check_video(s, vid(license="")).ok


def test_license_only_matters_for_cc():
    assert check_video(yt(RightsBasis.OWN), vid(license="youtube")).ok


def mk_clip(**kw) -> Clip:
    base = {
        "id": "c",
        "source_id": "s",
        "video_id": "v",
        "moment": Moment(start_s=0, end_s=10),
        "rights": RightsBasis.OFFICIAL,
        "credit": "Clip from Chan",
        "evidence_url": FULL["evidence_url"],
    }
    base.update(kw)
    return Clip(**base)


def official(**kw) -> Source:
    return yt(RightsBasis.OFFICIAL, enabled=True, **{**FULL, **kw})


def test_clip_ok():
    assert check_clip(mk_clip(), official()).ok


def test_clip_source_missing():
    assert not check_clip(mk_clip(), None).ok


def test_clip_source_disabled():
    s = official().model_copy(update={"enabled": False})
    assert not check_clip(mk_clip(), s).ok


def test_clip_source_lost_evidence():
    assert not check_clip(mk_clip(), official(evidence_url="")).ok


def test_clip_rights_changed():
    s = yt(RightsBasis.CAMPAIGN, enabled=True, **FULL)
    v = check_clip(mk_clip(), s)
    assert not v.ok and any("changed" in r for r in v.reasons)


def test_clip_none_basis_never_passes():
    assert not check_clip(mk_clip(rights=RightsBasis.NONE), yt(RightsBasis.NONE, enabled=True)).ok
    assert not check_clip(mk_clip(rights=RightsBasis.NONE), official()).ok
    assert not check_clip(mk_clip(rights=RightsBasis.OFFICIAL), yt(RightsBasis.NONE, enabled=True)).ok


def test_clip_credit_needed_unless_own():
    assert not check_clip(mk_clip(credit=""), official()).ok
    assert not check_clip(mk_clip(credit="  "), official()).ok
    own_src = yt(RightsBasis.OWN, enabled=True)
    assert check_clip(mk_clip(rights=RightsBasis.OWN, credit="", evidence_url=""), own_src).ok


def test_credit_line():
    s = yt(RightsBasis.OFFICIAL, **FULL)
    assert credit_line(s) == "Clip from Chan"
    assert credit_line(s.model_copy(update={"credit": "Big Creator"}), vid()) == "Clip from Big Creator - https://y/1"
    assert credit_line(s, vid(url="")) == "Clip from Chan"
    assert credit_line(yt(RightsBasis.OWN), vid()) == ""


def test_clip_made_under_another_evidence_link_is_refused():
    v = check_clip(mk_clip(evidence_url="https://other.example/old-permission"), official())
    assert not v.ok and any("evidence" in r for r in v.reasons)


def test_folder_inside_the_company_jobs_is_refused(tmp_path):
    from clips.models import Platform

    base = tmp_path / "company"
    (base / "jobs" / "j1").mkdir(parents=True)
    (base / "jobs" / "j1" / "job.json").write_text("{}", encoding="utf-8")
    for folder in (base, base / "jobs" / "j1"):
        s = Source(id="f", name="F", platform=Platform.FOLDER, folder=str(folder), rights=RightsBasis.OWN, enabled=True)
        v = check_source(s)
        assert not v.ok and any("company" in r for r in v.reasons)


def test_folder_that_is_the_apps_unisolated_storage_is_refused(tmp_path, monkeypatch):
    from clips.config import clips_settings
    from clips.models import Platform

    company = tmp_path / "company-storage"
    (company / "videos").mkdir(parents=True)
    monkeypatch.setenv("STORAGE_DIR", str(company))
    from core.config import get_settings

    get_settings.cache_clear()
    clips_settings.cache_clear()
    try:
        s = Source(id="f", name="F", platform=Platform.FOLDER, folder=str(company / "videos"),
                   rights=RightsBasis.OWN, enabled=True)
        assert not check_source(s).ok
    finally:
        get_settings.cache_clear()


def test_an_ordinary_folder_is_still_fine(tmp_path):
    from clips.models import Platform

    (tmp_path / "mine").mkdir()
    s = Source(id="f", name="F", platform=Platform.FOLDER, folder=str(tmp_path / "mine"),
               rights=RightsBasis.OWN, enabled=True)
    assert check_source(s).ok


# ------------------------------------------------------------ release-date gate

def _gated(monkeypatch, value="2026-11-19"):
    from clips.config import clips_settings

    monkeypatch.setenv("CLIPS_NOT_BEFORE", value)
    clips_settings.cache_clear()


def _video(published_at, **kw):
    return SourceVideo(id="v", source_id="s", platform=Platform.YOUTUBE, native_id="v", published_at=published_at, **kw)


def test_the_default_gate_is_the_release_date(monkeypatch):
    from clips.config import ClipsSettings

    monkeypatch.delenv("CLIPS_NOT_BEFORE", raising=False)
    settings = ClipsSettings(_env_file=None)
    assert settings.not_before == "2026-11-19" and settings.youtube_downloads is False


def test_a_video_before_the_gate_is_refused(monkeypatch):
    _gated(monkeypatch)
    v = check_video(yt(RightsBasis.OWN), _video(datetime(2026, 11, 18, 23, 0, tzinfo=UTC)))
    assert not v.ok
    assert "dated before CLIPS_NOT_BEFORE (the game's release date)" in v.reasons


def test_a_video_on_or_after_the_gate_passes(monkeypatch):
    _gated(monkeypatch)
    assert check_video(yt(RightsBasis.OWN), _video(datetime(2026, 11, 19, 0, 0, tzinfo=UTC))).ok
    assert check_video(yt(RightsBasis.OWN), _video(datetime(2027, 1, 1, tzinfo=UTC))).ok


def test_a_video_with_no_known_date_passes(monkeypatch):
    _gated(monkeypatch)
    assert check_video(yt(RightsBasis.OWN), _video(None)).ok


def test_official_sources_are_exempt(monkeypatch):
    _gated(monkeypatch)
    source = yt(RightsBasis.OFFICIAL, evidence_url="https://e.example", evidence_date="2026-01-05")
    assert check_video(source, _video(datetime(2026, 5, 1, tzinfo=UTC))).ok


def test_an_empty_gate_is_off(monkeypatch):
    _gated(monkeypatch, "")
    assert check_video(yt(RightsBasis.OWN), _video(datetime(2020, 1, 1, tzinfo=UTC))).ok


def test_a_gate_that_is_not_a_date_refuses_with_a_clear_message_and_does_not_raise(monkeypatch):
    _gated(monkeypatch, "soon")
    v = check_video(yt(RightsBasis.OWN), _video(datetime(2030, 1, 1, tzinfo=UTC)))
    assert not v.ok and any("CLIPS_NOT_BEFORE" in r and "YYYY-MM-DD" in r for r in v.reasons)


def test_a_naive_date_counts_as_utc(monkeypatch):
    _gated(monkeypatch)
    assert not check_video(yt(RightsBasis.OWN), _video(datetime(2026, 11, 1))).ok
