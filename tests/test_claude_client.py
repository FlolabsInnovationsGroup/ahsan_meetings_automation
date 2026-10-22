import json
from types import SimpleNamespace

import pytest

import decide
from core.config import get_settings
from core.models import Segment
from decide import claude_client
from decide.gemini_client import DecisionError

pytest.importorskip("anthropic")


@pytest.fixture(autouse=True)
def _anthropic_settings(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _segments(n):
    return [Segment(speaker="A", start=float(i), end=float(i) + 1, text=f"s{i}") for i in range(n)]


def _response(segments, decision="keep", stop_reason="end_turn", drop=0):
    items = [{"start": s.start, "end": s.end, "decision": decision, "reason": "r"} for s in segments]
    items = items[: len(items) - drop] if drop else items
    block = SimpleNamespace(type="text", text=json.dumps({"decisions": items}))
    return SimpleNamespace(stop_reason=stop_reason, content=[block])


def test_dispatches_to_claude_and_returns_decisions(monkeypatch):
    segments = _segments(3)
    monkeypatch.setattr(claude_client, "_call_claude", lambda c, m, segs, p, note="": _response(segs, "remove"))
    decisions = decide.get_decisions(segments)
    assert [d.decision for d in decisions] == ["remove"] * 3


def test_retries_once_when_count_is_wrong(monkeypatch):
    segments = _segments(2)
    calls = []

    def fake(c, m, segs, p, note=""):
        calls.append(note)
        return _response(segs, drop=1 if len(calls) == 1 else 0)

    monkeypatch.setattr(claude_client, "_call_claude", fake)
    assert len(decide.get_decisions(segments)) == 2
    assert calls[0] == "" and calls[1]  # second call carries the retry note


def test_truncated_response_splits_the_chunk(monkeypatch):
    segments = _segments(4)
    sizes = []

    def fake(c, m, segs, p, note=""):
        sizes.append(len(segs))
        return _response(segs, stop_reason="max_tokens" if len(segs) == 4 else "end_turn")

    monkeypatch.setattr(claude_client, "_call_claude", fake)
    assert len(decide.get_decisions(segments)) == 4
    assert sizes == [4, 2, 2]


def test_batches_by_max_segments_and_reports_progress(monkeypatch):
    monkeypatch.setenv("GEMINI_MAX_SEGMENTS_PER_CALL", "2")
    get_settings.cache_clear()
    monkeypatch.setattr(claude_client, "_call_claude", lambda c, m, segs, p, note="": _response(segs))
    progress = []
    decide.get_decisions(_segments(5), on_progress=lambda done, total: progress.append((done, total)))
    assert progress == [(2, 5), (4, 5), (5, 5)]


def test_single_segment_that_never_parses_raises(monkeypatch):
    bad = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="not json")])
    monkeypatch.setattr(claude_client, "_call_claude", lambda *a, **k: bad)
    with pytest.raises(DecisionError):
        decide.get_decisions(_segments(1))


def test_missing_key_raises(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(DecisionError, match="ANTHROPIC_API_KEY"):
        decide.get_decisions(_segments(1))


def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "nope")
    get_settings.cache_clear()
    with pytest.raises(DecisionError, match="LLM_PROVIDER"):
        decide.get_decisions(_segments(1))
