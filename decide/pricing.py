"""Approximate AI cost of a job, for the report and the manifest.

List prices in US dollars per 1M tokens (October 2026); they change, so this
is an estimate, not a bill. A model not listed gets no estimate.
"""

from __future__ import annotations

# model id prefix: (input, output, cache read, cache write)
# Checked 2026-11-14 (cache write = the 5-minute rate). Gemini has no checked
# price, so it is left out: the cost meter's price table lets the owner add it.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),  # prompts up to 100K tokens
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25),
    "claude-fable-5-2": (8.00, 40.00, 0.20, 10.00),
    "gpt-6-luna": (0.10, 0.50, 0.01, 0.10),
}


def estimate_cost_usd(usage: dict, model: str) -> float | None:
    """Dollars for a job's llm_usage (prompt_tokens includes the cached and
    cache-written input, which are priced separately)."""
    price = next((p for prefix, p in PRICES.items() if (model or "").startswith(prefix)), None)
    if price is None or not usage or not usage.get("calls"):
        return None
    inp, out, cache_read, cache_write = price
    read = float(usage.get("cache_read_tokens") or 0)
    write = float(usage.get("cache_write_tokens") or 0)
    plain = max(0.0, float(usage.get("prompt_tokens") or 0) - read - write)
    output = float(usage.get("output_tokens") or 0)
    return round((plain * inp + read * cache_read + write * cache_write + output * out) / 1e6, 4)
