from core.config import get_settings

from . import claude_client, gemini_client
from .gemini_client import DecisionError
from .segments import build_segments


def get_decisions(segments, mode="crosstalk", on_progress=None):
    """Keep/remove pass, using the provider chosen by LLM_PROVIDER."""
    provider = get_settings().llm_provider
    if provider == "anthropic":
        return claude_client.get_decisions(segments, mode=mode, on_progress=on_progress)
    if provider == "gemini":
        return gemini_client.get_decisions(segments, mode=mode, on_progress=on_progress)
    raise DecisionError(f"unknown LLM_PROVIDER {provider!r}, expected 'gemini' or 'anthropic'")


__all__ = ["DecisionError", "build_segments", "get_decisions"]
