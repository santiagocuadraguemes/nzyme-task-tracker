"""Local, on-disk capture of every LLM call — the CLI debugging tool.

Logfire is a *remote* observability product. For interactive CLI testing
it fights you: its default scrubber redacts any prompt containing words
like ``auth`` / ``token`` (so Gemini system instructions show up as
``[Scrubbed due to 'auth']``), and its google-genai instrumentation only
half-captures native Gemini calls. This module sidesteps all of that by
writing the **exact, unscrubbed** system message, user prompt, raw
response text, and full token usage of each LLM call to a single
append-only JSONL file under ``.llm_logs/``.

Organised so nothing scatters and nothing is lost:
  * one run = one file, ``.llm_logs/<UTC-timestamp>.jsonl``
  * one line = one LLM call (stage, model, usage, system, user, response)
  * the file is created lazily on the first call, so runs that touch no
    LLM leave no empty files behind.

Enable with ``NZYME_DEBUG_LLM=1`` (the transcript CLI's ``--dump-llm``
flag calls :func:`enable_llm_debug` for you). The same flag also turns
off Logfire scrubbing on local (non-Lambda) runs — see
:func:`src.utils.llm_logging.configure_logfire`.

Inspect a run from PowerShell, e.g.::

    Get-Content .llm_logs/<file>.jsonl | ConvertFrom-Json | Format-List
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ENV_FLAG = "NZYME_DEBUG_LLM"
_DUMP_DIR = Path(".llm_logs")
_TRUTHY = {"1", "true", "yes", "on"}

# JSONL path for the current process, created lazily on the first dump.
_run_path: Path | None = None


def llm_debug_enabled() -> bool:
    """True when local LLM debug capture is switched on for this process."""
    return os.environ.get(_ENV_FLAG, "").strip().lower() in _TRUTHY


def enable_llm_debug() -> None:
    """Turn capture on for the rest of this process (used by ``--dump-llm``)."""
    os.environ[_ENV_FLAG] = "1"


def _current_path() -> Path:
    global _run_path
    if _run_path is None:
        _DUMP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        _run_path = _DUMP_DIR / f"{stamp}.jsonl"
    return _run_path


def dump_call(
    *,
    stage: str,
    model: str,
    system: str,
    user: str,
    raw_response: str,
    usage: dict[str, Any] | None = None,
) -> None:
    """Append one LLM call's full payload to this run's JSONL file.

    No-op unless ``NZYME_DEBUG_LLM`` is set, so production and ordinary
    CLI runs pay nothing. A write failure is logged as a warning (visible,
    per the no-silent-failures rule) but never propagates — a debug side
    channel must not be able to kill a real extraction run.
    """
    if not llm_debug_enabled():
        return
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "model": model,
        "usage": usage or {},
        "system": system,
        "user": user,
        "raw_response": raw_response,
    }
    try:
        with _current_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("LLM dump write failed (%s): %s", stage, e)


def dump_path() -> Path | None:
    """Return the current run's dump file, or ``None`` if nothing was written."""
    return _run_path
