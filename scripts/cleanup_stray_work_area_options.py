"""One-off: undo the 3 stray Work area options the first macro_block_sync run
created on Jacob's + Santiago's member DBs (2026-05-21 ~10:12 Madrid).

The mappings I hand-seeded into ``public.work_area_option_mappings`` used
option_ids extracted from Notion MCP URL fragments. Those URL fragments are a
URL-safe encoding of the real Notion API option_ids — the planner's
``mapping.option_id in by_id`` check failed for every row because ``by_id`` is
keyed on the raw API ids. For the 3 drift rows (``and`` vs ``&``), the
bootstrap-adopt-by-sanitized-name fallback also failed, so the planner created
new options instead of renaming the originals in place.

This script:
  1. Retrieves the current ``Work area`` options for each affected DB via the
     real Notion API (not the MCP).
  2. PATCHes back the 6 originals only, preserving every existing option_id →
     every historical meeting tag stays valid. The 3 stray options (which had
     zero tagged pages — they were minutes old) are removed by omission.
  3. Re-upserts the 12 mappings into ``work_area_option_mappings`` using the
     REAL option_ids returned by the retrieve call, so the next
     ``macro_block_sync`` tick takes CASE A (id-preserving rename) for the 3
     drift rows instead of creating new options again.

Endpoints: Notion + Supabase only. **No GEMINI_API_KEY / OPENAI_API_KEY needed.**

Usage::

    ../venv/Scripts/python scripts/cleanup_stray_work_area_options.py            # live
    ../venv/Scripts/python scripts/cleanup_stray_work_area_options.py --dry-run  # preview
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from notion_client import Client

# Ensure src/ is importable when run from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.hierarchy.canonical_mirror_sync import _http  # noqa: E402
from src.hierarchy.macro_block_sync import _sanitize_option_name  # noqa: E402
from src.notion_client_wrapper import NotionClientWrapper  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cleanup")

_WORK_AREA = "Work area"

# (member_db_id, owner_label, [stray option_ids to drop])
_TARGETS: list[tuple[str, str, list[str]]] = [
    (
        "35083e67-e2e7-80b0-9d8b-c213e9b161f3",
        "Jacob Hinz",
        [
            "ac7ed55a-eb26-4385-99f3-7b2931438a19",  # Operations & AI enablement
            "c4fdc839-5cdf-46f6-a675-6fb2cd1d120c",  # PPP Sourcing Investing & Divesting (Dealflow)
            "ac6edeec-101b-4f4d-98ca-b84c1261f0d1",  # Talent attraction & development
        ],
    ),
    (
        "34583e67-e2e7-8081-b515-f5e33926f153",
        "Santiago Cuadra",
        [
            "c75f88d4-df09-49eb-bf38-d304398f35f3",  # Operations & AI enablement
            "839c3f45-b427-4376-a84e-3f95b2822996",  # PPP Sourcing Investing & Divesting (Dealflow)
            "401269f3-2eb8-4616-b10c-3128e2068677",  # Talent attraction & development
        ],
    ),
]

# Canonical hierarchy_page_id → the original member-DB option NAME we want to
# re-pin the mapping to. Names match the current Notion option names (pre-PATCH
# state on Jacob + Santiago) so we can look them up after retrieve_data_source.
_CANONICAL_TO_ORIGINAL_NAME: dict[str, str] = {
    "46ff5472-8e78-426d-911b-07e315d597bf": "Sourcing Investing and Divesting (Dealflow)",
    "c3a645bf-edae-4176-9373-4b0f958f3c72": "Value Creation for Portfolio",
    "d912d24c-83b5-4464-ac60-510efacfc9cc": "Investor Relations & Fundraising",
    "8901cbcc-8612-4a47-9596-77ad11dfda5f": "Talent attraction and development",
    "7937af99-72fc-4f18-be00-75458366637c": "Operations and AI enablement",
    "00f55934-ca91-4c65-8aa0-b3cc24ab6413": "Growth & Expansion (Special Projects & Partnerships)",
}


def _build_client() -> NotionClientWrapper:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    token = os.environ.get("NOTION_API_TOKEN")
    if not token:
        raise SystemExit("NOTION_API_TOKEN missing from environment")
    return NotionClientWrapper(Client(auth=token, notion_version="2026-03-11"))


def _process_member(
    client: NotionClientWrapper,
    member_db_id: str,
    owner: str,
    stray_ids: list[str],
    *,
    dry_run: bool,
) -> None:
    logger.info("=== %s (%s) ===", owner, member_db_id)

    ds = client.retrieve_data_source(member_db_id)
    props = ds.get("properties") or {}
    work_area = props.get(_WORK_AREA)
    if not work_area or work_area.get("type") != "select":
        logger.error("  no '%s' select property — skipping", _WORK_AREA)
        return

    current = work_area.get("select", {}).get("options", []) or []
    logger.info("  current options: %d", len(current))
    stray_set = set(stray_ids)
    keepers = [o for o in current if o.get("id") not in stray_set]
    found_strays = [o for o in current if o.get("id") in stray_set]

    logger.info(
        "  keepers=%d strays_found=%d (expected %d)",
        len(keepers), len(found_strays), len(stray_ids),
    )
    for s in found_strays:
        logger.info("    drop: id=%s name=%r", s.get("id"), s.get("name"))

    missing = stray_set - {o.get("id") for o in current}
    if missing:
        logger.warning(
            "  %d expected stray id(s) not present (already deleted?): %s",
            len(missing), sorted(missing),
        )

    # Sanity: every keeper must carry an id (we want id-preserving rewrite).
    keepers_with_id = [o for o in keepers if o.get("id")]
    if len(keepers_with_id) != len(keepers):
        raise RuntimeError(
            f"{owner}: {len(keepers) - len(keepers_with_id)} keeper option(s) "
            "missing an id — aborting to avoid losing tags",
        )

    # Build the PATCH payload — preserve id+name+color verbatim for every keeper.
    patch_options = [
        {"id": o["id"], "name": o["name"], "color": o.get("color", "default")}
        for o in keepers
    ]

    if dry_run:
        logger.info("  DRY RUN — would PATCH %d options, dropping %d strays",
                    len(patch_options), len(found_strays))
    else:
        client.update_data_source(
            member_db_id,
            {_WORK_AREA: {"select": {"options": patch_options}}},
        )
        logger.info("  PATCHed: %d options remaining", len(patch_options))

    # Map canonical → real option_id by NAME lookup on the keepers list.
    by_name: dict[str, dict[str, Any]] = {o["name"]: o for o in keepers}
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    upserts: list[dict[str, Any]] = []
    for hierarchy_page_id, original_name in _CANONICAL_TO_ORIGINAL_NAME.items():
        opt = by_name.get(original_name)
        if not opt:
            logger.error(
                "  canonical %s expects option %r but it's missing — "
                "mapping NOT upserted (manual investigation needed)",
                hierarchy_page_id[:8], original_name,
            )
            continue
        upserts.append({
            "hierarchy_page_id": hierarchy_page_id,
            "member_db_id": member_db_id,
            "option_id": opt["id"],
            "option_name": _sanitize_option_name(opt["name"]),
            "last_synced_at": now_iso,
        })

    if dry_run:
        logger.info("  DRY RUN — would upsert %d mappings", len(upserts))
        for u in upserts:
            logger.info(
                "    %s → option_id=%s name=%r",
                u["hierarchy_page_id"][:8], u["option_id"], u["option_name"],
            )
    else:
        _http(
            "POST",
            "/rest/v1/work_area_option_mappings?on_conflict=hierarchy_page_id,member_db_id",
            body=upserts,
            prefer="resolution=merge-duplicates,return=minimal",
        )
        logger.info("  upserted %d mappings", len(upserts))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview the PATCH + upserts without writing anything.")
    args = ap.parse_args()

    client = _build_client()
    for member_db_id, owner, stray_ids in _TARGETS:
        _process_member(client, member_db_id, owner, stray_ids,
                        dry_run=args.dry_run)

    logger.info("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
