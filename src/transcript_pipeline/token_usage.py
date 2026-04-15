"""Token usage logging and cost estimation for LLM calls."""

from __future__ import annotations

import sys

# Pricing per 1M tokens: (input, cached_input, output)
# Sources: OpenAI — https://openai.com/api/pricing/
#          Gemini — https://ai.google.dev/gemini-api/docs/pricing
#          OpenRouter — https://openrouter.ai
# Last updated: 2026-04-13
MODEL_PRICING: dict[str, tuple[float, float, float]] = {
    # OpenAI models — (input, cached_input, output) per 1M tokens
    "gpt-4.1-mini":  (0.40, 0.10, 1.60),
    "gpt-5-mini":    (0.25, 0.025, 2.00),
    "gpt-4.1":       (2.00, 0.50, 8.00),
    "gpt-5":         (1.25, 0.125, 10.00),
    "gpt-5.4-mini":  (0.75, 0.075, 4.50),
    "gpt-5.4":       (2.50, 0.25, 15.00),
    # Google Gemini models — (input, cached_input, output) per 1M tokens
    "gemini-2.5-flash":          (0.30, 0.03, 2.50),
    "gemini-2.5-pro":            (1.25, 0.125, 10.00),
    "gemini-3-flash-preview":    (0.50, 0.05, 3.00),
}


def _estimate_cost(
    model: str,
    prompt_tokens: int,
    cached_tokens: int,
    completion_tokens: int,
) -> float | None:
    """Estimate cost in USD. Returns None if model not in pricing table."""
    pricing = MODEL_PRICING.get(model)
    if not pricing:
        return None
    input_price, cached_price, output_price = pricing
    uncached_tokens = prompt_tokens - cached_tokens
    cost = (
        (uncached_tokens / 1_000_000) * input_price
        + (cached_tokens / 1_000_000) * cached_price
        + (completion_tokens / 1_000_000) * output_price
    )
    return cost


def log_token_usage(model: str, usage: object) -> None:
    """Print token counts and estimated cost from an OpenAI-style usage object."""
    if not usage:
        return

    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage, "completion_tokens", 0) or 0
    total_tokens = getattr(usage, "total_tokens", 0) or 0
    details = getattr(usage, "prompt_tokens_details", None)
    cached_tokens = (getattr(details, "cached_tokens", 0) or 0) if details else 0

    cost = _estimate_cost(model, prompt_tokens, cached_tokens, completion_tokens)
    cost_str = f", est. cost: ${cost:.4f}" if cost is not None else ""

    print(
        f"  Tokens — prompt: {prompt_tokens} (cached: {cached_tokens}), "
        f"completion: {completion_tokens}, total: {total_tokens}{cost_str}",
        file=sys.stderr,
    )
