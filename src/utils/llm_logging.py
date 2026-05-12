"""Shared logging helpers for LLM call sites.

Surfaces cached-token counts (OpenAI auto-caches prompts >=1024 tokens on
a ~5 min window via ``prompt_tokens_details.cached_tokens``) so we can
see whether the prefix layout actually hits the cache. Gemini's
OpenAI-compatible endpoint does not currently expose this field — the
helper degrades cleanly to "in / out" with no cached number.
"""
from __future__ import annotations

import logging
from typing import Any


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

    prompt = getattr(usage, "prompt_tokens", 0)
    completion = getattr(usage, "completion_tokens", 0)
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) if details is not None else 0

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
