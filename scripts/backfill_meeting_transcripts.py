"""One-time backfill: copy every Meeting Notes page into Supabase.

Walks the Org Chart to discover per-member Meeting Notes DBs, paginates
through each DB, and upserts every page into `public.meeting_transcripts`
in Supabase. Re-runnable thanks to `on_conflict=page_id`.

Env vars required:
    NOTION_API_TOKEN, ORG_CHART_DB_ID  (from existing .env)
    SUPABASE_URL                       e.g. https://yphbrpbwpakjduhmoimw.supabase.co
    SUPABASE_SERVICE_ROLE_KEY          server-side key — RLS bypass

Usage:
    python scripts/backfill_meeting_transcripts.py                # full
    python scripts/backfill_meeting_transcripts.py --limit 3      # first 3 pages
    python scripts/backfill_meeting_transcripts.py --dry-run      # extract + print only
    python scripts/backfill_meeting_transcripts.py --db <db_id>   # restrict to one DB
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

# Windows PowerShell defaults to cp1252 stdout, which mangles Spanish accents
# and breaks on arrows/em-dashes in titles. Force UTF-8 unconditionally.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from notion_client import Client as NotionClient

# Allow running as `python scripts/backfill_...py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.meeting_db_registry import discover_meeting_dbs, MeetingDB
from src.meeting_row import extract_row
from src.notion_client_wrapper import NotionClientWrapper
from src.supabase_writer import upsert_meetings

logger = logging.getLogger("backfill")


# ------------------------------------------------------------------
# Main loop
# ------------------------------------------------------------------

def iter_pages(client: NotionClientWrapper, db_id: str):
    """Yield every page in a Meeting Notes DB."""
    resp = client.query_database(database_id=db_id)
    yield from resp.get("results", [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N pages total across all DBs")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print rows as JSON; do not write to Supabase")
    parser.add_argument("--db", type=str, default=None,
                        help="Only process this Meeting Notes DB ID")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    notion_token = os.environ.get("NOTION_API_TOKEN")
    org_chart_db_id = os.environ.get("ORG_CHART_DB_ID")
    if not notion_token or not org_chart_db_id:
        sys.exit("NOTION_API_TOKEN and ORG_CHART_DB_ID must be set in .env")

    if not args.dry_run:
        sb_url = os.environ.get("SUPABASE_URL")
        # Accept either SUPABASE_SERVICE_ROLE_KEY (explicit) or SUPABASE_KEY (short).
        # Must be a service_role key — RLS bypass is required since the table has
        # RLS enabled with no policies.
        sb_key = (
            os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
            or os.environ.get("SUPABASE_KEY")
        )
        if not sb_url or not sb_key:
            sys.exit(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY) "
                "must be set in .env for writes. Use --dry-run to print rows "
                "without writing.",
            )
    else:
        sb_url = sb_key = ""

    # meeting_notes block type requires Notion API v2026-03-11.
    client = NotionClientWrapper(
        NotionClient(auth=notion_token, notion_version="2026-03-11"),
    )

    if args.db:
        registry = [MeetingDB(db_id=args.db, owner_name="?", owner_email="")]
    else:
        registry = discover_meeting_dbs(client, org_chart_db_id)

    if not registry:
        sys.exit("No Meeting Notes DBs to process.")

    total_pages = 0
    total_written = 0
    started = datetime.now()

    for db in registry:
        logger.info("=== DB %s (%s) ===", db.db_id, db.owner_name or "?")
        db_pages = 0
        batch: list[dict[str, Any]] = []
        for page in iter_pages(client, db.db_id):
            if args.limit is not None and total_pages >= args.limit:
                break
            try:
                row = extract_row(page, db, client)
            except Exception as exc:
                logger.exception("Skipping page %s: %s", page.get("id"), exc)
                continue
            total_pages += 1
            db_pages += 1
            t = len(row.get("transcript") or "")
            s = len(row.get("notion_summary") or "")
            n = len(row.get("notes") or "")
            logger.info(
                "  %s  '%s'  t=%d s=%d n=%d",
                row["page_id"], (row["title"][:50] or "?"), t, s, n,
            )
            if args.dry_run:
                print(json.dumps(row, ensure_ascii=False))
            else:
                batch.append(row)
                if len(batch) >= 10:
                    upsert_meetings(batch)
                    total_written += len(batch)
                    batch.clear()
        if not args.dry_run and batch:
            upsert_meetings(batch)
            total_written += len(batch)
            batch.clear()
        logger.info("  → %d pages from %s", db_pages, db.owner_name or db.db_id)
        if args.limit is not None and total_pages >= args.limit:
            break

    elapsed = (datetime.now() - started).total_seconds()
    logger.info(
        "Done. %d pages processed, %d rows written, in %.1fs.",
        total_pages, total_written, elapsed,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
