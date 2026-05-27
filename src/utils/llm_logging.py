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
import os
import sys
from dataclasses import dataclass, field
from typing import Any, TextIO


def allow_gen_ai_content(match: Any) -> Any:
    """Logfire scrubbing callback that keeps ``gen_ai.*`` attributes intact.

    Logfire's default scrubber redacts any value containing words like
    ``auth``, ``token``, ``password``, ``secret``. That redacts entire LLM
    prompts/completions whenever the meeting transcript or the system
    prompt happens to contain one of those substrings (e.g. ``authority``,
    ``authentic``, ``Authorization``).

    Pass this to ``logfire.configure(scrubbing=ScrubbingOptions(callback=...))``
    so OpenTelemetry GenAI attributes (``gen_ai.input.*`` /
    ``gen_ai.output.*`` / ``gen_ai.system_instructions`` / ``gen_ai.prompt``
    / ``gen_ai.completion``) flow through untouched. All other attribute
    paths fall through to Logfire's default redaction.
    """
    for part in getattr(match, "path", ()) or ():
        if isinstance(part, str) and part.startswith("gen_ai."):
            return match.value
    return None


def configure_logfire(token: str | None, *, service_name: str) -> None:
    """Configure Logfire + OpenAI/Gemini instrumentation in one place.

    Centralises what used to be four copy-pasted blocks (CLI, watch,
    transcript CLI, Lambda). Two behaviours worth knowing:

    * Native google-genai calls only capture prompt/completion content
      when ``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`` is set
      *before* ``instrument_google_genai()``. We set it here so the CLI
      paths match Lambda (which set it already).
    * On local (non-Lambda) runs with ``NZYME_DEBUG_LLM`` on, scrubbing is
      turned **off** so Gemini system instructions are readable in the
      Logfire UI instead of ``[Scrubbed due to 'auth']``. Lambda always
      keeps scrubbing on — it's your own machine vs. a shared service.
    """
    import logfire

    from src.utils.llm_dump import llm_debug_enabled

    os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")

    in_lambda = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    scrubbing: Any = (
        False
        if (llm_debug_enabled() and not in_lambda)
        else logfire.ScrubbingOptions(callback=allow_gen_ai_content)
    )

    logfire.configure(
        token=token,
        service_name=service_name,
        send_to_logfire="if-token-present",
        scrubbing=scrubbing,
    )
    logfire.instrument_openai()
    logfire.instrument_google_genai()


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
    """One LLM call's token counts.

    ``reasoning_tokens`` is the hidden chain-of-thought portion of
    ``completion_tokens`` (a *subset*, not an addition) — gpt-5-mini's
    OpenAI ``completion_tokens_details.reasoning_tokens`` and Gemini's
    ``thoughts_token_count``. It's billed at the output rate and is the
    usual reason ``completion_tokens`` dwarfs the visible response.
    """

    stage: str
    model: str
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0


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
        reasoning_tokens: int = 0,
    ) -> None:
        self.records.append(
            UsageRecord(
                stage=stage,
                model=model,
                prompt_tokens=prompt_tokens,
                cached_tokens=cached_tokens,
                completion_tokens=completion_tokens,
                reasoning_tokens=reasoning_tokens,
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
    reasoning_tokens: int = 0,
) -> None:
    if _tracker is None:
        return
    _tracker.record(
        stage=stage,
        model=model,
        prompt_tokens=prompt_tokens,
        cached_tokens=cached_tokens,
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
    )


def _as_int(value: Any) -> int:
    """Coerce a usage field to int. Non-int (None, mocks) → 0.

    Provider SDKs always return ints here; the guard keeps the eager
    log-line formatting from choking on a missing field or a test mock.
    """
    return value if isinstance(value, int) else 0


def _fmt_token_line(
    stage: str, model: str, prompt: int, cached: int, completion: int, reasoning: int
) -> str:
    """Build the per-call log line, showing cached/reasoning only when nonzero."""
    in_part = f"{prompt:,} in"
    if cached:
        in_part += f" ({cached:,} cached)"
    out_part = f"{completion:,} out"
    if reasoning:
        out_part += f" ({reasoning:,} reasoning)"
    return f"{stage} tokens: {in_part} / {out_part} (model={model})"


def log_usage(
    response: Any,
    model: str,
    *,
    stage: str,
    logger: logging.Logger,
) -> dict[str, int] | None:
    """Emit a token line and record the call. Returns the parsed counts.

    ``completion_tokens`` on a reasoning model (gpt-5-mini, o-series)
    already *includes* the hidden reasoning tokens — that's why it can be
    several times the size of the visible response. We surface that subset
    via ``completion_tokens_details.reasoning_tokens`` so the totals stop
    looking mysterious. The returned dict feeds ``llm_dump.dump_call``.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return None

    prompt = _as_int(getattr(usage, "prompt_tokens", 0))
    completion = _as_int(getattr(usage, "completion_tokens", 0))
    details = getattr(usage, "prompt_tokens_details", None)
    cached = _as_int(getattr(details, "cached_tokens", 0) if details is not None else 0)
    ctd = getattr(usage, "completion_tokens_details", None)
    reasoning = _as_int(getattr(ctd, "reasoning_tokens", 0) if ctd is not None else 0)

    logger.info(_fmt_token_line(stage, model, prompt, cached, completion, reasoning))

    _record(
        stage=stage,
        model=model,
        prompt_tokens=prompt,
        cached_tokens=cached,
        completion_tokens=completion,
        reasoning_tokens=reasoning,
    )
    return {
        "prompt": prompt,
        "cached": cached,
        "completion": completion,
        "reasoning": reasoning,
    }


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
    ``cached_content_token_count`` / ``thoughts_token_count``).

    Unlike OpenAI, the Gemini SDK reports thinking tokens *separately*
    from ``candidates_token_count`` (the visible answer). Both bill at the
    output rate, so the true billed output is ``candidates + thoughts`` —
    we record that as ``completion`` (the old code counted only
    ``candidates`` and under-reported Gemini cost) and expose ``thoughts``
    as the reasoning subset. Returns the parsed counts for ``dump_call``.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None

    prompt = _as_int(getattr(usage, "prompt_token_count", 0))
    candidates = _as_int(getattr(usage, "candidates_token_count", 0))
    cached = _as_int(getattr(usage, "cached_content_token_count", 0))
    reasoning = _as_int(getattr(usage, "thoughts_token_count", 0))
    completion = candidates + reasoning

    logger.info(_fmt_token_line(stage, model, prompt, cached, completion, reasoning))

    _record(
        stage=stage,
        model=model,
        prompt_tokens=prompt,
        cached_tokens=cached,
        completion_tokens=completion,
        reasoning_tokens=reasoning,
    )
    return {
        "prompt": prompt,
        "cached": cached,
        "completion": completion,
        "reasoning": reasoning,
    }


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
            key,
            {"calls": 0, "prompt": 0, "cached": 0, "completion": 0, "reasoning": 0},
        )
        agg["calls"] += 1
        agg["prompt"] += r.prompt_tokens
        agg["cached"] += r.cached_tokens
        agg["completion"] += r.completion_tokens
        agg["reasoning"] += r.reasoning_tokens

    # "Output" is the full billed completion; "Reason" is the hidden
    # chain-of-thought subset of it (shown so the Output figure stops
    # looking inexplicable). Cost is computed from Output — never add
    # Reason on top, it would double-count.
    print("", file=out)
    print("=== LLM USAGE SUMMARY ===", file=out)
    print(
        f"  {'Stage':<22} {'Model':<26} {'Calls':>5}  {'Input':>10}  {'Cached':>8}  {'Output':>8}  {'Reason':>8}  {'Cost (USD)':>12}",
        file=out,
    )
    print(f"  {'-' * 22} {'-' * 26} {'-' * 5}  {'-' * 10}  {'-' * 8}  {'-' * 8}  {'-' * 8}  {'-' * 12}", file=out)

    total_prompt = 0
    total_cached = 0
    total_completion = 0
    total_reasoning = 0
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
        total_reasoning += agg["reasoning"]
        print(
            f"  {stage[:22]:<22} {model[:26]:<26} {agg['calls']:>5}  "
            f"{agg['prompt']:>10,}  {agg['cached']:>8,}  {agg['completion']:>8,}  "
            f"{agg['reasoning']:>8,}  {cost_str:>12}",
            file=out,
        )

    print(f"  {'-' * 22} {'-' * 26} {'-' * 5}  {'-' * 10}  {'-' * 8}  {'-' * 8}  {'-' * 8}  {'-' * 12}", file=out)
    total_cost_str = f"${total_cost:.4f}" + (" (partial)" if any_unknown else "")
    print(
        f"  {'TOTAL':<22} {'':<26} {'':>5}  "
        f"{total_prompt:>10,}  {total_cached:>8,}  {total_completion:>8,}  "
        f"{total_reasoning:>8,}  {total_cost_str:>12}",
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
