"""Shared logging helpers for LLM call sites.

Two responsibilities:

1. Per-call log lines via ``log_usage`` / ``log_usage_genai``. Surfaces
   cached-token counts (OpenAI auto-caches prompts >=1024 tokens on a
   ~5 min window via ``prompt_tokens_details.cached_tokens``) so we can
   see whether the prefix layout actually hits the cache. Gemini's
   OpenAI-compatible endpoint does not currently expose this field — the
   helper degrades cleanly to "in / out" with no cached number.

2. Process-wide accumulator (``UsageTracker``). When a tracker is
   active, every ``log_usage`` / ``log_usage_genai`` call also records
   into it. ``print_usage_summary`` renders a final report with token
   totals and an estimated USD cost per stage, derived from
   ``MODEL_PRICING``.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from typing import Any, TextIO


# USD per 1,000,000 tokens. Update these when provider pricing changes.
# Unknown models still get tracked (token counts shown, cost rendered as
# "?"). Keys are matched as exact strings against the model name used at
# call time.
MODEL_PRICING: dict[str, dict[str, float]] = {
    # OpenAI
    "gpt-5-mini": {"input": 0.25, "cached_input": 0.025, "output": 2.00},
    "gpt-5": {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "cached_input": 0.075, "output": 0.60},
    "text-embedding-3-small": {"input": 0.02, "cached_input": 0.02, "output": 0.0},
    # Google (Gemini)
    "gemini-3-flash-preview": {"input": 0.30, "cached_input": 0.075, "output": 2.50},
    "gemini-2.5-flash": {"input": 0.30, "cached_input": 0.075, "output": 2.50},
    "gemini-2.5-pro": {"input": 1.25, "cached_input": 0.31, "output": 10.00},
}


@dataclass
class UsageRecord:
    """One LLM call's token counts."""

    stage: str
    model: str
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class UsageTracker:
    """Process-wide accumulator for LLM token usage and estimated cost."""

    records: list[UsageRecord] = field(default_factory=list)

    def record(
        self,
        *,
        stage: str,
        model: str,
        prompt_tokens: int,
        cached_tokens: int,
        completion_tokens: int,
    ) -> None:
        self.records.append(
            UsageRecord(
                stage=stage,
                model=model,
                prompt_tokens=prompt_tokens,
                cached_tokens=cached_tokens,
                completion_tokens=completion_tokens,
            )
        )


_tracker: UsageTracker | None = None


def start_tracking() -> UsageTracker:
    """Begin recording usage for this process. Idempotent — resets totals."""
    global _tracker
    _tracker = UsageTracker()
    return _tracker


def get_tracker() -> UsageTracker | None:
    return _tracker


def _record(
    *,
    stage: str,
    model: str,
    prompt_tokens: int,
    cached_tokens: int,
    completion_tokens: int,
) -> None:
    if _tracker is None:
        return
    _tracker.record(
        stage=stage,
        model=model,
        prompt_tokens=prompt_tokens,
        cached_tokens=cached_tokens,
        completion_tokens=completion_tokens,
    )


def log_usage(
    response: Any,
    model: str,
    *,
    stage: str,
    logger: logging.Logger,
) -> None:
    """Emit a single info line with prompt/completion/cached token counts."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return

    prompt = getattr(usage, "prompt_tokens", 0) or 0
    completion = getattr(usage, "completion_tokens", 0) or 0
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) if details is not None else 0
    cached = cached or 0

    if cached:
        logger.info(
            "%s tokens: %d in (%d cached) / %d out (model=%s)",
            stage, prompt, cached, completion, model,
        )
    else:
        logger.info(
            "%s tokens: %d in / %d out (model=%s)",
            stage, prompt, completion, model,
        )

    _record(
        stage=stage,
        model=model,
        prompt_tokens=prompt,
        cached_tokens=cached,
        completion_tokens=completion,
    )


def log_usage_genai(
    response: Any,
    model: str,
    *,
    stage: str,
    logger: logging.Logger,
) -> None:
    """Emit token counts for a native google-genai ``GenerateContentResponse``.

    Mirrors ``log_usage`` but reads the Gemini SDK's ``usage_metadata``
    shape (``prompt_token_count`` / ``candidates_token_count`` /
    ``cached_content_token_count``).
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return

    prompt = getattr(usage, "prompt_token_count", 0) or 0
    completion = getattr(usage, "candidates_token_count", 0) or 0
    cached = getattr(usage, "cached_content_token_count", 0) or 0

    if cached:
        logger.info(
            "%s tokens: %d in (%d cached) / %d out (model=%s)",
            stage, prompt, cached, completion, model,
        )
    else:
        logger.info(
            "%s tokens: %d in / %d out (model=%s)",
            stage, prompt, completion, model,
        )

    _record(
        stage=stage,
        model=model,
        prompt_tokens=prompt,
        cached_tokens=cached,
        completion_tokens=completion,
    )


def record_embedding_usage(
    response: Any,
    model: str,
    *,
    stage: str = "Embedding",
) -> None:
    """Record token usage from an OpenAI ``embeddings.create`` response.

    No log line is emitted — embeddings are called in tight loops and
    individual call counts would be noisy. Tokens still roll up into the
    final summary so the cost is visible.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    _record(
        stage=stage,
        model=model,
        prompt_tokens=prompt,
        cached_tokens=0,
        completion_tokens=0,
    )


def _cost_for(model: str, prompt: int, cached: int, completion: int) -> float | None:
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        return None
    uncached = max(prompt - cached, 0)
    return (
        uncached * pricing["input"]
        + cached * pricing["cached_input"]
        + completion * pricing["output"]
    ) / 1_000_000


def print_usage_summary(
    tracker: UsageTracker | None = None,
    *,
    file: TextIO | None = None,
) -> None:
    """Print a per-stage and total token/cost summary.

    Stages are grouped by ``(stage, model)`` so multi-call stages
    (classification fires once per task) collapse into one row.
    """
    tr = tracker if tracker is not None else _tracker
    if tr is None or not tr.records:
        return

    out = file or sys.stderr

    groups: dict[tuple[str, str], dict[str, int]] = {}
    for r in tr.records:
        key = (r.stage, r.model)
        agg = groups.setdefault(
            key, {"calls": 0, "prompt": 0, "cached": 0, "completion": 0}
        )
        agg["calls"] += 1
        agg["prompt"] += r.prompt_tokens
        agg["cached"] += r.cached_tokens
        agg["completion"] += r.completion_tokens

    print("", file=out)
    print("=== LLM USAGE SUMMARY ===", file=out)
    print(
        f"  {'Stage':<22} {'Model':<26} {'Calls':>5}  {'Input':>10}  {'Cached':>8}  {'Output':>8}  {'Cost (USD)':>12}",
        file=out,
    )
    print(f"  {'-' * 22} {'-' * 26} {'-' * 5}  {'-' * 10}  {'-' * 8}  {'-' * 8}  {'-' * 12}", file=out)

    total_prompt = 0
    total_cached = 0
    total_completion = 0
    total_cost = 0.0
    any_unknown = False

    for (stage, model), agg in groups.items():
        cost = _cost_for(model, agg["prompt"], agg["cached"], agg["completion"])
        cost_str = f"${cost:.4f}" if cost is not None else "?"
        if cost is None:
            any_unknown = True
        else:
            total_cost += cost
        total_prompt += agg["prompt"]
        total_cached += agg["cached"]
        total_completion += agg["completion"]
        print(
            f"  {stage[:22]:<22} {model[:26]:<26} {agg['calls']:>5}  "
            f"{agg['prompt']:>10,}  {agg['cached']:>8,}  {agg['completion']:>8,}  "
            f"{cost_str:>12}",
            file=out,
        )

    print(f"  {'-' * 22} {'-' * 26} {'-' * 5}  {'-' * 10}  {'-' * 8}  {'-' * 8}  {'-' * 12}", file=out)
    total_cost_str = f"${total_cost:.4f}" + (" (partial)" if any_unknown else "")
    print(
        f"  {'TOTAL':<22} {'':<26} {'':>5}  "
        f"{total_prompt:>10,}  {total_cached:>8,}  {total_completion:>8,}  "
        f"{total_cost_str:>12}",
        file=out,
    )
    if any_unknown:
        unknown_models = sorted(
            {m for (_s, m), _a in groups.items() if m not in MODEL_PRICING}
        )
        print(
            f"  (no pricing for: {', '.join(unknown_models)} - add to MODEL_PRICING in src/utils/llm_logging.py)",
            file=out,
        )
