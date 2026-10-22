"""Claude (Anthropic) backend for the keep/remove decision pass.

Same prompts, batching and split-on-failure strategy as gemini_client, so
both providers behave the same way; only the API call differs. Claude
answers with structured output constrained to the decision-list schema.
"""

import json
import time

from pydantic import ValidationError

from core.config import get_settings
from core.logging import get_logger
from core.models import Decision, Segment

from .gemini_client import (
    _SYSTEM_PROMPTS,
    DecisionError,
    _chunk,
    _decision_list_adapter,
    _segments_payload,
)

logger = get_logger(__name__)

# Structured output: the response text is guaranteed to match this schema.
_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "decision": {"type": "string", "enum": ["keep", "remove"]},
                    "reason": {"type": "string"},
                },
                "required": ["start", "end", "decision", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["decisions"],
    "additionalProperties": False,
}
_RETRY_NOTE = "Your previous answer did not contain exactly one valid decision per segment. Try again, strictly."


class _Truncated(Exception):
    pass


def _call_claude(client, model: str, segments: list[Segment], system_prompt: str, retry_note: str = ""):
    prompt = _segments_payload(segments)
    if retry_note:
        prompt = f"{retry_note}\n\n{prompt}"
    return client.messages.create(
        model=model,
        max_tokens=get_settings().anthropic_max_tokens,
        system=system_prompt,
        output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )


def _parse(response) -> list[Decision]:
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise _Truncated("response hit max_tokens")
    text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", None) == "text")
    if not text:
        raise ValueError("no text in response")
    return _decision_list_adapter.validate_python(json.loads(text)["decisions"])


def _split_and_retry(client, model, segments, system_prompt) -> list[Decision]:
    mid = len(segments) // 2
    logger.warning("splitting a %d-segment chunk into %d + %d and retrying", len(segments), mid, len(segments) - mid)
    return _decisions_for_chunk(client, model, segments[:mid], system_prompt) + _decisions_for_chunk(
        client, model, segments[mid:], system_prompt
    )


def _decisions_for_chunk(client, model: str, segments: list[Segment], system_prompt: str) -> list[Decision]:
    import anthropic

    last_error: Exception | None = None
    for attempt, retry_note in enumerate(["", _RETRY_NOTE]):
        try:
            decisions = _parse(_call_claude(client, model, segments, system_prompt, retry_note))
        except _Truncated as exc:
            logger.warning("claude response truncated for a %d-segment chunk", len(segments))
            if len(segments) > 1:
                return _split_and_retry(client, model, segments, system_prompt)
            last_error = exc
            continue
        except (ValidationError, ValueError, KeyError, AttributeError, TypeError) as exc:
            logger.warning("claude decision parse failed on attempt %d: %s", attempt + 1, exc)
            last_error = exc
            continue
        except anthropic.RateLimitError as exc:
            # The SDK already retried with backoff; pause once more before our own retry.
            logger.warning("claude rate limit on attempt %d, backing off: %s", attempt + 1, exc)
            time.sleep(30)
            last_error = exc
            continue
        except (anthropic.APIConnectionError, anthropic.InternalServerError) as exc:
            logger.warning("claude API error on attempt %d: %s", attempt + 1, exc)
            last_error = exc
            continue

        if len(decisions) != len(segments):
            logger.warning(
                "claude returned %d decisions for %d segments on attempt %d",
                len(decisions), len(segments), attempt + 1,
            )
            last_error = DecisionError("decision count did not match segment count")
            continue
        return decisions

    if len(segments) > 1:
        return _split_and_retry(client, model, segments, system_prompt)
    raise DecisionError(f"LLM returned invalid decisions after retry: {last_error}") from last_error


def get_decisions(
    segments: list[Segment],
    mode: str = "crosstalk",
    on_progress: "callable[[int, int], None] | None" = None,
) -> list[Decision]:
    if not segments:
        return []
    if mode not in _SYSTEM_PROMPTS:
        raise ValueError(f"unknown decide mode {mode!r}, expected one of {sorted(_SYSTEM_PROMPTS)}")

    import anthropic

    settings = get_settings()
    if not settings.anthropic_api_key:
        raise DecisionError("ANTHROPIC_API_KEY is not set")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=4)
    system_prompt = _SYSTEM_PROMPTS[mode]

    total = len(segments)
    decisions: list[Decision] = []
    for chunk in _chunk(segments, settings.gemini_max_segments_per_call):
        decisions.extend(_decisions_for_chunk(client, settings.anthropic_model, chunk, system_prompt))
        if on_progress:
            on_progress(len(decisions), total)
    return decisions
