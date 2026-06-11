"""One-off remediation: re-derive titles for `meeting_transcripts` rows that
were stored as "(untitled)" because the source DB's title property isn't named
"Meeting" (e.g. "Note" in Álvaro Lozano's DB, "Título" in Jaime Gervás's).

This is the data-side companion to the `meeting_row._title_text` fix (locate the
title by property TYPE, not name). It is intentionally surgical:

  * Reads only the `page_id`s currently stored as "(untitled)".
  * Re-fetches each page (properties only — no block/transcript reads).
  * Re-extracts the title with the fixed, type-based `_title_text`.
  * Upserts ONLY `{page_id, title}` — `upsert_meetings` merges on page_id and
    leaves every other column (transcript, notes, summary, attendees…) untouched.

Dry-run by default; pass --apply to write.

    python scripts/fix_untitled_titles.py            # preview
    python scripts/fix_untitled_titles.py --apply     # write the titles
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from notion_client import APIResponseError, Client as NotionClient

from src.meeting_row import _title_text
from src.notion_client_wrapper import NotionClientWrapper
from src.supabase_writer import _credentials

logger = logging.getLogger("fix-untitled")

SENTINEL = "(untitled)"


def _patch_title(page_id: str, title: str) -> None:
    """UPDATE meeting_transcripts.title for one existing row (never inserts)."""
    url, key = _credentials()
    qs = urllib.parse.urlencode({
        "page_id": f"eq.{page_id}",
        "title": f"eq.{SENTINEL}",  # guard: only overwrite the sentinel
    })
    body = json.dumps({"title": title}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{url.rstrip('/')}/rest/v1/meeting_transcripts?{qs}",
        data=body,
        method="PATCH",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def _fetch_untitled_page_ids() -> list[str]:
    """page_ids in meeting_transcripts whose title is the "(untitled)" sentinel."""
    url, key = _credentials()
    qs = urllib.parse.urlencode({
        "select": "page_id",
        "title": f"eq.{SENTINEL}",
        "limit": "5000",
    })
    req = urllib.request.Request(
        f"{url.rstrip('/')}/rest/v1/meeting_transcripts?{qs}",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        rows = json.loads(resp.read().decode("utf-8"))
    return [r["page_id"] for r in rows if r.get("page_id")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Write the re-derived titles (default: dry-run).")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    notion_token = os.environ.get("NOTION_API_TOKEN")
    if not notion_token:
        sys.exit("NOTION_API_TOKEN must be set in .env")

    client = NotionClientWrapper(
        NotionClient(auth=notion_token, notion_version="2026-03-11"),
    )

    page_ids = _fetch_untitled_page_ids()
    logger.info("%d row(s) currently stored as %r", len(page_ids), SENTINEL)

    recovered: list[dict[str, str]] = []
    still_blank = 0
    gone = 0
    for i, page_id in enumerate(page_ids, 1):
        try:
            page = client.get_page(page_id)
        except APIResponseError as e:
            if e.status == 404:
                gone += 1
                logger.warning("  [%d/%d] %s — page gone (404), skipping",
                               i, len(page_ids), page_id)
                continue
            raise
        title = _title_text(page.get("properties", {})).strip()
        if not title:
            still_blank += 1
            logger.info("  [%d/%d] %s — no title in Notion either, leaving as-is",
                        i, len(page_ids), page_id)
            continue
        recovered.append({"page_id": page_id, "title": title})
        logger.info("  [%d/%d] %s → %r", i, len(page_ids), page_id, title[:80])

    logger.info(
        "Recovered %d title(s); %d genuinely blank; %d gone (404).",
        len(recovered), still_blank, gone,
    )

    if not recovered:
        logger.info("Nothing to write.")
        return 0

    if not args.apply:
        logger.info("DRY-RUN — pass --apply to write these %d title(s).",
                    len(recovered))
        return 0

    # PATCH each row's title in place (filtered to the "(untitled)" sentinel so
    # a row that was re-synced correctly in the meantime is never clobbered).
    written = 0
    for r in recovered:
        _patch_title(r["page_id"], r["title"])
        written += 1
    logger.info("Wrote %d title(s) to meeting_transcripts.", written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
