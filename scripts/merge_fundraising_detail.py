"""Merge three fundraising `Detail` tags into one, across every member DB.

For each per-member Meeting Notes DB (discovered via the Org Chart), find every
meeting whose `Detail` multi-select contains any of:

    "Portugal fundraising", "Germany fundraising", "Umbrella fundraising"

and retag it: drop those three, add "Regular fundraising activity" (preserving
every other Detail tag on the page, deduped). This is the ROW migration only —
it does NOT touch the `Detail` option list.

Removing the three options themselves is canonical-governed: delete those three
rows from the `Detail Options` Settings DB, then run
`python -m src.main --sync-hierarchy`, which tombstone-drops the options from
every member DB (see src/hierarchy/detail_applier_sync.py, CASE T). Run THIS
script first, while the options still exist, so the meetings are merged into
Regular rather than just stripped.

Uses the regular Notion API via NotionClientWrapper (paginated + rate-limited +
retried) — not the hosted MCP SQL engine.

Run from repo root (Notion only — no LLM keys needed):
    # Dry run (default): prints exactly what WOULD change, writes nothing.
    ../venv/Scripts/python scripts/merge_fundraising_detail.py
    # Apply the changes:
    ../venv/Scripts/python scripts/merge_fundraising_detail.py --apply
    # Restrict to Active=true Org Chart rows:
    ../venv/Scripts/python scripts/merge_fundraising_detail.py --only-active
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402
from notion_client import Client as NotionClient  # noqa: E402

from src.meeting_db_registry import discover_meeting_dbs  # noqa: E402
from src.notion_client_wrapper import NotionClientWrapper  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("merge_fundraising_detail")

DETAIL_PROPERTY = "Detail"
MERGE_FROM = ("Portugal fundraising", "Germany fundraising", "Umbrella fundraising")
MERGE_INTO = "Regular fundraising activity"

# Notion filter: Detail multi_select contains ANY of the three.
_FILTER = {
    "or": [
        {"property": DETAIL_PROPERTY, "multi_select": {"contains": name}}
        for name in MERGE_FROM
    ],
}


def _page_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(rt.get("plain_text", "") for rt in prop.get("title", []))
    return "(untitled)"


def _detail_names(page: dict) -> list[str]:
    ms = (page.get("properties", {}).get(DETAIL_PROPERTY) or {}).get("multi_select") or []
    return [o.get("name", "") for o in ms if o.get("name")]


def _new_names(current: list[str]) -> list[str]:
    """Drop the three merge-from tags; ensure MERGE_INTO present; preserve order."""
    kept = [n for n in current if n not in MERGE_FROM]
    if MERGE_INTO not in kept:
        kept.append(MERGE_INTO)
    # Dedupe, order-preserving.
    seen: set[str] = set()
    out: list[str] = []
    for n in kept:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Write the changes. Without this flag the script is a dry run.",
    )
    parser.add_argument(
        "--only-active", action="store_true",
        help="Restrict to Active=true Org Chart rows (default: include inactive).",
    )
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    token = os.environ["NOTION_API_TOKEN"]
    org_chart_db_id = os.environ["ORG_CHART_DB_ID"]

    notion = NotionClientWrapper(NotionClient(auth=token))
    members = discover_meeting_dbs(
        notion, org_chart_db_id, include_inactive=not args.only_active,
    )

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== Merge fundraising Detail tags — {mode} ===")
    print(f"Merging {MERGE_FROM} -> {MERGE_INTO!r}")
    print(f"Member DBs: {len(members)}\n")

    grand_pages = 0
    grand_updated = 0
    grand_errors = 0

    for m in sorted(members, key=lambda x: x.owner_name or ""):
        label = m.owner_name or m.db_id
        try:
            resp = notion.query_database(database_id=m.db_id, filter=_FILTER)
        except Exception as exc:  # noqa: BLE001
            grand_errors += 1
            print(f"[{label}] QUERY FAILED: {exc!r}")
            continue

        pages = resp.get("results", [])
        if not pages:
            print(f"[{label}] 0 affected")
            continue

        # Guard: only migrate rows whose CURRENT Detail actually contains one of
        # the three merge-from tags. Skips false positives like orphaned/stale
        # values on template pages (e.g. an empty Detail that still matches the
        # server-side filter), which would otherwise create a brand-new option.
        real = [p for p in pages if set(_detail_names(p)) & set(MERGE_FROM)]
        skipped = len(pages) - len(real)
        suffix = f" ({skipped} skipped: no live merge tag)" if skipped else ""

        if not real:
            print(f"[{label}] 0 affected{suffix}")
            continue

        print(f"[{label}] {len(real)} affected{suffix}:")
        for page in real:
            grand_pages += 1
            title = _page_title(page)
            before = _detail_names(page)
            after = _new_names(before)
            print(f"    • {title[:70]}")
            print(f"        {before} -> {after}")
            if not args.apply:
                continue
            try:
                notion.update_page(
                    page["id"],
                    {DETAIL_PROPERTY: {"multi_select": [{"name": n} for n in after]}},
                )
                grand_updated += 1
            except Exception as exc:  # noqa: BLE001
                grand_errors += 1
                print(f"        UPDATE FAILED: {exc!r}")

    print("\n=== Summary ===")
    print(f"  Affected pages found: {grand_pages}")
    if args.apply:
        print(f"  Pages updated:        {grand_updated}")
    else:
        print("  (dry run — no writes. Re-run with --apply to migrate.)")
    if grand_errors:
        print(f"  Errors:               {grand_errors}")

    print(
        "\nNext (option removal, canonical): delete the three rows from the "
        "'Detail Options' Settings DB, then run "
        "`python -m src.main --sync-hierarchy --verbose`."
    )


if __name__ == "__main__":
    main()
