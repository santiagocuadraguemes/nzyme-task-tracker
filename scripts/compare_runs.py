"""Diff two shadow-diff / compare-candidate JSON dumps side-by-side.

Usage:
    ../venv/Scripts/python scripts/compare_runs.py baseline.json cand-no-sr.json

Pairs pages by ``page_id`` and tasks by fuzzy title match (difflib). Prints:
- output-token totals & per-page averages, both paths
- mean tasks/page on each side
- title-overlap %, ia/ea/p/dd agreement on matched pairs

No automated pass/fail — n is small. Read the numbers by eye and decide.

Works for any two files in the shadow-diff JSON shape. If a file is a
shadow-diff (has both ``legacy`` and ``merged``), only ``merged`` is read.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _load(path: Path) -> dict[str, dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for r in raw:
        pid = r.get("page_id")
        merged = r.get("merged") or {}
        if pid and merged:
            out[pid] = merged
    return out


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def _pair_tasks(a_tasks: list[dict], b_tasks: list[dict]) -> list[tuple[dict | None, dict | None]]:
    """Match tasks by title similarity; unmatched ones come back paired with None."""
    used_b: set[int] = set()
    pairs: list[tuple[dict | None, dict | None]] = []
    b_titles = [_norm(t.get("title", "")) for t in b_tasks]
    for a in a_tasks:
        a_title = _norm(a.get("title", ""))
        best_idx = None
        best_score = 0.0
        for j, b_title in enumerate(b_titles):
            if j in used_b or not b_title:
                continue
            score = difflib.SequenceMatcher(None, a_title, b_title).ratio()
            if score > best_score:
                best_score = score
                best_idx = j
        if best_idx is not None and best_score >= 0.55:
            used_b.add(best_idx)
            pairs.append((a, b_tasks[best_idx]))
        else:
            pairs.append((a, None))
    for j, b in enumerate(b_tasks):
        if j not in used_b:
            pairs.append((None, b))
    return pairs


def _agree(va, vb) -> bool:
    if isinstance(va, list) and isinstance(vb, list):
        return set(_norm(str(x)) for x in va) == set(_norm(str(x)) for x in vb)
    return _norm(str(va or "")) == _norm(str(vb or ""))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()

    a = _load(args.baseline)
    b = _load(args.candidate)

    common = sorted(set(a) & set(b))
    if not common:
        sys.exit("No overlapping page_ids between the two files.")

    a_out = sum(a[p].get("output_tokens", 0) for p in common)
    b_out = sum(b[p].get("output_tokens", 0) for p in common)
    a_tasks_total = sum(len(a[p].get("tasks", [])) for p in common)
    b_tasks_total = sum(len(b[p].get("tasks", [])) for p in common)

    # Field-level agreement on matched task pairs.
    matched = 0
    a_only = 0
    b_only = 0
    agree_ia = agree_ea = agree_p = agree_dd = 0
    for pid in common:
        for ta, tb in _pair_tasks(a[pid].get("tasks", []), b[pid].get("tasks", [])):
            if ta is None:
                b_only += 1
            elif tb is None:
                a_only += 1
            else:
                matched += 1
                if _agree(ta.get("internal_assignees"), tb.get("internal_assignees")):
                    agree_ia += 1
                if _agree(ta.get("external_assignees"), tb.get("external_assignees")):
                    agree_ea += 1
                if _agree(ta.get("priority"), tb.get("priority")):
                    agree_p += 1
                if _agree(ta.get("due_date"), tb.get("due_date")):
                    agree_dd += 1

    n = len(common)
    print(f"=== compare_runs ({args.baseline.name} vs {args.candidate.name}) ===")
    print(f"  pages compared: {n}")
    print()
    print(f"  {'':<22} {'baseline':>14} {'candidate':>14} {'delta':>10}")
    print(f"  {'-' * 22} {'-' * 14} {'-' * 14} {'-' * 10}")
    pct = (b_out - a_out) / max(a_out, 1) * 100
    print(f"  {'mean out tokens/page':<22} {a_out / n:>14,.0f} {b_out / n:>14,.0f} {pct:>9.1f}%")
    print(f"  {'total out tokens':<22} {a_out:>14,} {b_out:>14,} {pct:>9.1f}%")
    print(f"  {'tasks total':<22} {a_tasks_total:>14} {b_tasks_total:>14} "
          f"{(b_tasks_total - a_tasks_total) / max(a_tasks_total, 1) * 100:>9.1f}%")
    print(f"  {'tasks / page':<22} {a_tasks_total / n:>14.1f} {b_tasks_total / n:>14.1f}")
    print()
    print(f"  Matched task pairs: {matched}  (baseline-only: {a_only}, candidate-only: {b_only})")
    if matched:
        print(f"    ia agreement: {agree_ia / matched * 100:.0f}%")
        print(f"    ea agreement: {agree_ea / matched * 100:.0f}%")
        print(f"    p  agreement: {agree_p / matched * 100:.0f}%")
        print(f"    dd agreement: {agree_dd / matched * 100:.0f}%")


if __name__ == "__main__":
    main()
