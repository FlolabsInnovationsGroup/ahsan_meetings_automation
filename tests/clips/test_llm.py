import pytest

from clips import llm
from decide.gemini_client import LLMUsage


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeCaller:
    model = "claude-test"
    deadline = 123.0
    max_output_tokens = 0

    def __init__(self, text='{"a": 1}'):
        self.usage = LLMUsage()
        self.text = text
        self.calls = []

    def generate(self, system, contents, schema=None):
        self.calls.append((system, contents))
        self.usage.calls += 1
        self.usage.output_tokens += 5
        return FakeResponse(self.text)


def test_parse_json_text_variants():
    assert llm.parse_json_text('{"a": 1}') == {"a": 1}
    assert llm.parse_json_text('```json\n{"a": 2}\n```') == {"a": 2}
    assert llm.parse_json_text('Sure! {"a": 3} hope it helps') == {"a": 3}
    for bad in ("no json", "[1, 2]", ""):
        with pytest.raises((ValueError, TypeError)):
            llm.parse_json_text(bad)


def test_json_text_call_records_usage(monkeypatch):
    recorded = []
    monkeypatch.setattr("costs.record.record_call", lambda key, usage, **kw: recorded.append((key, usage, kw)))
    caller = FakeCaller()
    out = llm.AppLLM(caller, "openai").json("sys", "user", max_tokens=777)
    assert out == {"a": 1}
    assert caller.calls == [("sys", "user")]
    assert caller.deadline is None
    assert caller.max_output_tokens == 777
    key, usage, _kw = recorded[0]
    assert key.startswith("ai:clips:") and usage["calls"] == 1 and usage["output_tokens"] == 5


def test_images_go_as_jpeg_blocks_with_vision_model(monkeypatch):
    monkeypatch.setattr("costs.record.record_call", lambda *a, **k: None)
    sent = []

    class Msg:
        stop_reason = "end_turn"

        def __init__(self):
            self.content = [type("B", (), {"type": "text", "text": '{"ok": true}'})()]
            self.usage = type("U", (), {"model_dump": lambda self: {"input_tokens": 10, "output_tokens": 3}})()

    caller = FakeCaller()
    ai = llm.AppLLM(caller, "anthropic", vision_model="claude-vision")
    monkeypatch.setattr(ai, "_send", lambda params: sent.append(params) or Msg())
    assert ai.json("sys", "look", images=[b"\xff\xd8abc"]) == {"ok": True}
    params = sent[0]
    assert params["model"] == "claude-vision"
    blocks = params["messages"][0]["content"]
    assert blocks[0]["source"]["media_type"] == "image/jpeg"
    assert blocks[-1] == {"type": "text", "text": "look"}
    assert caller.usage.calls == 1 and caller.usage.prompt_tokens == 10
    assert caller.calls == []  # the text path was not used


def test_images_ignored_for_other_providers(monkeypatch):
    monkeypatch.setattr("costs.record.record_call", lambda *a, **k: None)
    caller = FakeCaller()
    assert llm.AppLLM(caller, "gemini").json("s", "u", images=[b"x"]) == {"a": 1}
    assert len(caller.calls) == 1


def test_usage_recording_failure_does_not_break(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("costs.record.record_call", boom)
    assert llm.AppLLM(FakeCaller(), "anthropic").json("s", "u") == {"a": 1}


def test_default_llm_none_without_key(monkeypatch):
    from core.config import get_settings

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    try:
        assert llm.default_llm() is None
    finally:
        get_settings.cache_clear()


def test_default_llm_built_when_key_set(monkeypatch):
    from core.config import get_settings

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    get_settings.cache_clear()
    try:
        made = llm.default_llm()  # building the caller makes no network call
        assert isinstance(made, llm.AppLLM) and made.provider == "anthropic"
    finally:
        get_settings.cache_clear()
