"""Local CLI for the recurring Notion → Supabase sync.

Mirrors the Lambda paths so local runs exercise the exact same code.

    python scripts/sync_meeting_transcripts.py                # incremental
    python scripts/sync_meeting_transcripts.py --full         # safety-net (14d)
    python scripts/sync_meeting_transcripts.py --full --days 30
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from notion_client import Client as NotionClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import load_config
from src.notion_client_wrapper import NotionClientWrapper
from src.supabase_sync import run_full, run_incremental


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true",
                        help="Run the safety-net full sweep (last N days)")
    parser.add_argument("--days", type=int, default=14,
                        help="Lookback window for --full (default 14)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    config = load_config()
    client = NotionClientWrapper(
        NotionClient(auth=config.notion_api_token, notion_version="2026-03-11"),
    )

    if args.full:
        n = run_full(config, client, lookback_days=args.days)
    else:
        n = run_incremental(config, client)
    print(f"Done. Rows upserted: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
