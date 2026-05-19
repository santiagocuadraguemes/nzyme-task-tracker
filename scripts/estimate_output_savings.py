"""Offline estimator: rank candidate schema reductions WITHOUT firing real calls.

Reads a ``shadow-diff.json`` produced by ``shadow_diff_extraction.py``,
mechanically transforms each saved merged-call output into candidate-schema
shapes (drop ``sr``, drop scratch fields, drop ``a``, short-char enums, etc.),
re-serialises each to JSON and counts tokens with Gemini's ``count_tokens``
API. ``count_tokens`` is free and unlimited, so this whole script costs zero.

Usage:
    ../venv/Scripts/python scripts/estimate_output_savings.py shadow-diff.json

Output is a table — mean / median / total output-token estimates per
candidate, plus % reduction vs the estimator's baseline reconstruction.

Free-tier notes:
- ``count_tokens`` does not consume any per-day quota.
- Real shadow-diff calls (to produce the input ``shadow-diff.json``) DO
  consume quota. Run shadow-diff once, then iterate on this estimator.

The numbers are approximate: a real model run with a changed prompt may
produce slightly different content. The estimator only varies the
*serialisation* of the same task content, so it's a reliable ranking
signal but not an exact prediction.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")


# ---------------------------------------------------------------------------
# Candidate transforms
# ---------------------------------------------------------------------------
#
# Each candidate takes the raw merged-call payload (short-key, scratch
# fields included) and returns a transformed dict that represents what
# the model WOULD have emitted under that candidate's schema. We count
# tokens on the JSON serialisation of the result.
#
# Mappings for short-enum candidates. Long → short on the wire.
_CT_MAP = {"hard": "h", "conditional": "c", "soft": "s", "group": "g"}
_PRIORITY_MAP = {"High": "H", "Medium": "M", "Low": "L"}
_CONFIDENCE_MAP = {"high": "h", "medium": "m", "low": "l"}


def _drop(task: dict, *keys: str) -> dict:
    return {k: v for k, v in task.items() if k not in keys}


def _short_enums(task: dict) -> dict:
    out = dict(task)
    if "ct" in out:
        out["ct"] = _CT_MAP.get(out["ct"], out["ct"])
    if "p" in out:
        out["p"] = _PRIORITY_MAP.get(out["p"], out["p"])
    if "c" in out:
        out["c"] = _CONFIDENCE_MAP.get(out["c"], out["c"])
    return out


def _identity(payload: dict) -> dict:
    return payload


def _no_sr(payload: dict) -> dict:
    return {
        **payload,
        "tasks": [_drop(t, "sr") for t in payload.get("tasks", [])],
    }


def _no_scratch(payload: dict) -> dict:
    return {
        "tasks": payload.get("tasks", []),
    }


def _no_a(payload: dict) -> dict:
    # The display string `a` is "Name1, Name2" — derivable from ia+ea
    # in Python after parse, so we don't need the model to emit it.
    return {
        **payload,
        "tasks": [_drop(t, "a") for t in payload.get("tasks", [])],
    }


def _short_enum_payload(payload: dict) -> dict:
    return {
        **payload,
        "tasks": [_short_enums(t) for t in payload.get("tasks", [])],
    }


def _combined(payload: dict) -> dict:
    """no-sr + no-scratch + no-a + short-enums."""
    return {
        "tasks": [
            _short_enums(_drop(t, "sr", "a"))
            for t in payload.get("tasks", [])
        ],
    }


CANDIDATES: dict[str, Callable[[dict], dict]] = {
    "baseline":     _identity,
    "no-sr":        _no_sr,
    "no-scratch":   _no_scratch,
    "no-a":         _no_a,
    "short-enums":  _short_enum_payload,
    "combined":     _combined,
}


# ---------------------------------------------------------------------------
# Token counting via Gemini count_tokens
# ---------------------------------------------------------------------------

def _make_counter(model: str):
    """Return a callable ``(text) -> int`` using Gemini's free count_tokens API."""
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        sys.exit(
            "GEMINI_API_KEY (or GOOGLE_API_KEY) must be set — count_tokens "
            "requires a real Gemini API key even though the call itself is "
            "free."
        )
    client = genai.Client(api_key=api_key)

    def count(text: str) -> int:
        # count_tokens uses the same tokenizer Gemini bills against. Free
        # and unmetered (no daily-quota impact on free tier).
        result = client.models.count_tokens(model=model, contents=text)
        return int(result.total_tokens or 0)

    return count


def _serialise(payload: dict) -> str:
    """Serialise a candidate payload the way the model would.

    Compact form (no whitespace) — Gemini under ``responseMimeType=
    application/json`` typically emits compact JSON. Consistent
    serialisation across candidates is what matters for *relative*
    rankings; absolute numbers are approximate.
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("baseline", type=Path,
                        help="shadow-diff.json from shadow_diff_extraction.py")
    parser.add_argument("--model", default="gemini-3-flash-preview",
                        help="Model name for count_tokens (default matches prod)")
    parser.add_argument("--candidates", nargs="*", default=None,
                        help=f"Subset to evaluate (default: all of "
                             f"{list(CANDIDATES)})")
    parser.add_argument("--verbose", action="store_true",
                        help="Print per-page numbers, not just the corpus table")
    args = parser.parse_args()

    raw = json.loads(args.baseline.read_text(encoding="utf-8"))
    pages = [r for r in raw if r.get("merged") and r["merged"].get("raw_data")]
    if not pages:
        sys.exit(
            f"No pages in {args.baseline} carry merged.raw_data. "
            f"Re-run shadow_diff_extraction.py with the updated harness "
            f"so raw_data is preserved."
        )

    selected = args.candidates or list(CANDIDATES)
    unknown = set(selected) - set(CANDIDATES)
    if unknown:
        sys.exit(f"Unknown candidate(s): {sorted(unknown)}. "
                 f"Available: {list(CANDIDATES)}")

    count = _make_counter(args.model)

    # estimated[candidate][page_idx] = output_tokens
    estimated: dict[str, list[int]] = {c: [] for c in selected}
    real_baseline_tokens: list[int] = []
    task_counts: list[int] = []

    for i, page in enumerate(pages, 1):
        payload = page["merged"]["raw_data"]
        real_baseline_tokens.append(int(page["merged"].get("output_tokens", 0) or 0))
        task_counts.append(len(payload.get("tasks", []) or []))

        for c in selected:
            transformed = CANDIDATES[c](payload)
            text = _serialise(transformed)
            tokens = count(text)
            estimated[c].append(tokens)

        if args.verbose:
            line = f"[{i:2}/{len(pages)}] {page.get('meeting_title', '?')[:40]:<40}"
            for c in selected:
                line += f"  {c}={estimated[c][-1]:>5}"
            print(line)

    # ----- corpus summary -----
    n = len(pages)
    real_total = sum(real_baseline_tokens)
    print()
    print(f"=== OFFLINE OUTPUT-TOKEN ESTIMATE (n={n} pages, model={args.model}) ===")
    print(f"  Real baseline output tokens (from shadow-diff.json): "
          f"{real_total:,} total, {real_total / n:.0f} avg/page")
    if "baseline" in selected:
        est_base = sum(estimated["baseline"])
        print(f"  Estimator baseline reconstruction: "
              f"{est_base:,} total, {est_base / n:.0f} avg/page "
              f"(serialisation drift vs real: "
              f"{(est_base - real_total) / max(real_total, 1) * 100:+.1f}%)")
    print()

    base_for_pct = (
        sum(estimated["baseline"]) if "baseline" in selected else real_total
    )

    header = f"  {'candidate':<14}  {'mean/page':>10}  {'total':>10}  {'% vs baseline':>14}"
    print(header)
    print(f"  {'-' * 14}  {'-' * 10}  {'-' * 10}  {'-' * 14}")
    for c in selected:
        total = sum(estimated[c])
        mean = total / n
        pct = (total - base_for_pct) / max(base_for_pct, 1) * 100
        sign = "" if pct == 0 else ("+" if pct > 0 else "")
        print(f"  {c:<14}  {mean:>10.0f}  {total:>10,}  {sign}{pct:>12.1f}%")

    # Per-task averages
    total_tasks = sum(task_counts)
    if total_tasks:
        print()
        print(f"  Total tasks across corpus: {total_tasks}")
        for c in selected:
            per_task = sum(estimated[c]) / total_tasks
            print(f"    {c:<14}  {per_task:>6.1f} tok/task")


if __name__ == "__main__":
    main()
