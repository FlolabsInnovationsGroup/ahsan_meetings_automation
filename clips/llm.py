"""The language model for clips: the app's own configured provider behind a
tiny `.json(system, user, images=..., max_tokens=...)` interface."""

from __future__ import annotations

import base64
import json
import re
import uuid

from core.logging import get_logger

logger = get_logger(__name__)

_KEY_FIELDS = {"anthropic": "anthropic_api_key", "openai": "openai_api_key", "gemini": "gemini_api_key"}
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json_text(text: str) -> dict:
    """Parse the model's answer as a JSON object, tolerating code fences and chatter around it."""
    cleaned = _FENCE.sub("", (text or "").strip())
    try:
        data = json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("the model did not answer with JSON") from None
        data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise TypeError("the model's JSON answer is not an object")
    return data


def _image_blocks(images: list[bytes]) -> list[dict]:
    return [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(i).decode()}}
        for i in images
    ]


class AppLLM:
    """Calls the model the app is configured for and records the usage in the cost meter."""

    def __init__(self, caller, provider: str, vision_model: str = "") -> None:
        self.caller = caller
        self.provider = provider
        self.vision_model = vision_model

    def json(self, system: str, user: str, *, images: list[bytes] | None = None, max_tokens: int = 2000) -> dict:
        before = self.caller.usage.as_dict()
        self.caller.deadline = None  # a long-lived caller must not inherit a stage deadline
        try:
            text = self._ask(system, user, images, max_tokens)
        finally:
            self._record(before)
        return parse_json_text(text)

    def _ask(self, system: str, user: str, images: list[bytes] | None, max_tokens: int) -> str:
        if images and self.provider == "anthropic":
            return self._ask_with_images(system, user, images, max_tokens)
        # The other providers answer text-only here; pictures are dropped, not faked.
        self.caller.max_output_tokens = max_tokens
        return self.caller.generate(system, user).text

    def _ask_with_images(self, system: str, user: str, images: list[bytes], max_tokens: int) -> str:
        from decide.anthropic_client import parse_message, request_params

        model = self.vision_model or self.caller.model
        params = request_params(model, system, user, None, max_output_tokens=max_tokens)
        params["messages"] = [{"role": "user", "content": [*_image_blocks(images), {"type": "text", "text": user}]}]
        response = parse_message(self._send(params))
        usage = self.caller.usage
        usage.calls += 1
        cache_read = int(response.usage.get("cache_read_input_tokens") or 0)
        cache_write = int(response.usage.get("cache_creation_input_tokens") or 0)
        usage.prompt_tokens += int(response.usage.get("input_tokens") or 0) + cache_read + cache_write
        usage.cache_read_tokens += cache_read
        usage.cache_write_tokens += cache_write
        usage.output_tokens += int(response.usage.get("output_tokens") or 0)
        return response.text

    def _send(self, params: dict):
        """The one network call for pictures. Tests replace this."""
        from decide.anthropic_client import _call_anthropic

        return _call_anthropic(self.caller.client, params)

    def _record(self, before: dict) -> None:
        """Put this call's usage in the cost meter. Never raises."""
        try:
            now = self.caller.usage.as_dict()
            delta = {k: now[k] - before.get(k, 0) for k in now}
            if delta.get("calls"):
                from costs.record import record_call

                record_call(f"ai:clips:{uuid.uuid4().hex[:12]}", delta, meeting="clips")
        except Exception:
            logger.exception("clips: could not record language model usage")


def default_llm():
    """The language model the app is configured for, or None when no key is set."""
    from clips.config import clips_settings
    from core.config import get_settings

    settings = get_settings()
    provider = (settings.llm_provider or "").strip().lower()
    field = _KEY_FIELDS.get(provider)
    if not field or not (getattr(settings, field, "") or "").strip():
        return None
    from decide.gemini_client import make_caller

    return AppLLM(make_caller(), provider, clips_settings().vision_model.strip())
