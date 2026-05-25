"""One-off: repin ``work_area_option_mappings`` for every member DB whose
option_ids drifted from Notion's real API ids, and drop the stray Work area
options the latest ``macro_block_sync`` run created on the two members
Santiago just made Active (Niklas Born + Guillermo Puebla).

Why
---
Same root cause as the earlier ``cleanup_stray_work_area_options.py`` script
(Jacob + Santiago, 2026-05-21): the seeded mapping rows in
``public.work_area_option_mappings`` carry option_ids that don't match the
real Notion API ids for those options in each member DB. When the planner in
``src/hierarchy/macro_block_sync.py`` looks them up via ``mapping.option_id in
by_id``, the check fails — so CASE A (id-preserving rename) is skipped and
the bootstrap-create CASE D runs instead, producing duplicate options.

Today Santiago marked Niklas's + Guillermo Puebla's DBs Active and renamed
the canonical Tier-0 row ``Sourcing, Investing & Divesting (Dealflow)`` to
``PPP Sourcing, Investing & Divesting (Dealflow)``. The applier hit the bug
on both DBs and created 3 stray options each (``Operations & AI enablement``,
``PPP Sourcing Investing & Divesting (Dealflow)``, ``Talent attraction &
development``) alongside the original ``and``-variant options.

The other 6 inactive members (Fernando, Pablo, Vicente, Alvaro, Reyes, Aris)
still have only the originals — no applier has run for them — but their
mappings carry the same stale option_ids, so the same trap is waiting if
they're ever made Active. We backfill those too.

Jacob + Santiago were already cleaned up by the earlier script; we skip them.

What the script does (per member DB)
------------------------------------
1. Retrieves the current ``Work area`` options via the real Notion API.
2. If strays are listed for this member: PATCHes the schema back to the
   originals (preserving every original option_id → existing tags stay
   valid). The strays had zero or near-zero tagged pages — they were minutes
   old at most — so we drop by omission, no page-migration needed.
3. Upserts the 6 mappings into ``work_area_option_mappings`` using the REAL
   option_ids returned by Notion. After this, the next ``macro_block_sync``
   tick takes CASE A on the 3 drift rows for Niklas + Guillermo (id-preserving
   rename ``Operations and AI enablement → Operations & AI enablement`` and
   the equivalents) instead of bootstrap-creating new options again.

Endpoints: Notion + Supabase only. **No GEMINI_API_KEY / OPENAI_API_KEY needed.**

Usage::

    ../venv/Scripts/python scripts/backfill_member_work_area_mappings.py --dry-run
    ../venv/Scripts/python scripts/backfill_member_work_area_mappings.py
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
logger = logging.getLogger("backfill")

_WORK_AREA = "Work area"

# (member_db_id, owner_label, [stray option_ids to drop])
#
# Strays for Niklas + Guillermo Puebla were created today by the
# macro_block_sync run that fired after Santiago marked them Active and
# renamed the canonical Sourcing row. Their UUIDs were base64-decoded from
# the ``collectionPropertyOption://…`` URL fragments in the data-source
# response (Notion's MCP exposes option_ids in URL-safe base64 form;
# decoding round-trips to the real API id).
_TARGETS: list[tuple[str, str, list[str]]] = [
    (
        "36783e67-e2e7-806a-ab53-c608ecd3f404",
        "Niklas Born",
        [
            "72431529-73a2-4366-801e-0f702aa2ae06",  # Operations & AI enablement
            "45255c7a-72fa-4b3f-b9b3-5b8909536dd1",  # PPP Sourcing Investing & Divesting (Dealflow)
            "bf9136d0-b77b-4f40-8298-176dbf82ceca",  # Talent attraction & development
        ],
    ),
    (
        "34583e67-e2e7-804f-8ce8-fb6c078d2050",
        "Guillermo Puebla",
        [
            "b0fd19a7-7186-4982-b489-f321260facd3",  # Operations & AI enablement
            "97db7fc6-ea4b-4cdd-b1c2-206871069e0f",  # PPP Sourcing Investing & Divesting (Dealflow)
            "b1a2a4f5-1831-4b9e-b148-fca2542fb164",  # Talent attraction & development
        ],
    ),
    ("34583e67-e2e7-80ff-9ec1-d06c815f5425", "Fernando Díaz-Solís", []),
    ("35083e67-e2e7-8018-b7e5-d7169b4098a5", "Pablo Campos", []),
    ("34583e67-e2e7-803e-ba6e-f02c33615471", "Vicente Vázquez", []),
    ("35083e67-e2e7-8035-938d-fbb5c531bb7d", "Alvaro Fresnillo", []),
    ("b0797647-2620-499f-a4b8-9be7b03c07d0", "Reyes Rubio", []),
    ("34f83e67-e2e7-800f-ad95-f5d9a723f376", "Aris Degiacomi", []),
]

# Canonical hierarchy_page_id → the original member-DB option NAME we want to
# re-pin the mapping to. Names match the current Notion option names on every
# affected DB (``and``-variants for the 3 drift rows; exact-match for the
# other 3). Same dict as the earlier Jacob+Santiago cleanup script.
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

    if stray_ids:
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

    # Sanity: every keeper must carry an id — we want id-preserving rewrite.
    keepers_with_id = [o for o in keepers if o.get("id")]
    if len(keepers_with_id) != len(keepers):
        raise RuntimeError(
            f"{owner}: {len(keepers) - len(keepers_with_id)} keeper option(s) "
            "missing an id — aborting to avoid losing tags",
        )

    # Only PATCH when there's something to drop. Skipping the PATCH for
    # already-clean DBs (Fernando/Pablo/Vicente/Alvaro/Reyes/Aris) avoids a
    # no-op schema rewrite and keeps the script idempotent.
    if found_strays:
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
    else:
        logger.info("  no strays — skipping PATCH")

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
