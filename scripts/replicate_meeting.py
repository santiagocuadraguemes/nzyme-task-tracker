"""Replicate a Notion meeting page into the "Meeting Replicas (test)" DB.

Uses Notion's public REST API template mechanism: POST /v1/pages with
`template: {type: "template_id", template_id: <source_page_id>}`.

This produces a real clone of the source page, including the AI-managed
`meeting_notes` block (transcript, AI summary, notes, attendees), in a
single REST call — no duplicate→move sequence, no MCP server, no manual
synced-block wrap.

Template application is asynchronous: the create call returns a blank page,
and Notion populates the content over the next few seconds. The new page
ID and URL are valid immediately; refresh in Notion after ~5s to see the
content.

Caveats (empirically verified against Notion 2026-03-11):
  - `template_id` can be ANY page accessible to the integration — not just
    a page registered as a DB template.
  - The `children` parameter is forbidden when `template` is used.
  - The `properties` you pass at creation are preserved; they overlay
    whatever the template sets.
  - Properties not in the destination DB's schema are dropped silently.
  - `Date` gets reset unless you pass it explicitly in properties.

Usage (PowerShell, NOTION_API_TOKEN from .env — no LLM keys needed):
    ../venv/Scripts/python scripts/replicate_meeting.py <page_id>
    ../venv/Scripts/python scripts/replicate_meeting.py 35e83e67e2e780cc89f9d79b123ad412
    ../venv/Scripts/python scripts/replicate_meeting.py <page_id> --target-db <db_id>
    ../venv/Scripts/python scripts/replicate_meeting.py <page_id> --dry-run --verbose
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
from notion_client import APIResponseError, Client as NotionClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger("replicate_meeting")

DEFAULT_TARGET_DB_ID = "61d1aea536144e4aa627b02d1568b4c6"
OWNER_SUFFIX = " Meeting Notes"


def _title_plain_text(prop: dict) -> str:
    if not prop or prop.get("type") != "title":
        return ""
    return "".join(p.get("plain_text", "") for p in prop.get("title", []) or [])


def _date_value(prop: dict) -> dict | None:
    if not prop or prop.get("type") != "date":
        return None
    d = prop.get("date") or None
    if not d or not d.get("start"):
        return None
    out = {"start": d["start"]}
    if d.get("end"):
        out["end"] = d["end"]
    if d.get("time_zone"):
        out["time_zone"] = d["time_zone"]
    return out


def _rich_text(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text or ""}}]


def _resolve_owner(client: NotionClientWrapper, parent_db_id: str) -> str:
    try:
        db = client.retrieve_database(parent_db_id)
    except APIResponseError as e:
        logger.warning("Couldn't retrieve parent DB %s: %s", parent_db_id, e)
        return ""
    title = "".join(p.get("plain_text", "") for p in db.get("title", []) or [])
    title = title.strip()
    if title.endswith(OWNER_SUFFIX):
        title = title[: -len(OWNER_SUFFIX)].strip()
    return title


def replicate(
    client: NotionClientWrapper,
    page_id: str,
    target_db_id: str,
    *,
    dry_run: bool,
) -> int:
    src = client.get_page(page_id)
    props = src.get("properties", {})

    title_text = _title_plain_text(props.get("Meeting")) or "(untitled)"
    date_val = _date_value(props.get("Date"))
    source_url = src.get("url", f"https://www.notion.so/{page_id.replace('-', '')}")
    parent_db_id = (src.get("parent") or {}).get("database_id") or ""
    owner = _resolve_owner(client, parent_db_id) if parent_db_id else ""

    logger.info("Source page: %r", title_text)
    logger.info("  page_id    = %s", src.get("id"))
    logger.info("  parent DB  = %s (%s)", parent_db_id, owner or "?")
    logger.info("  date       = %s", date_val)

    mirror_properties: dict = {
        "Meeting": {"title": _rich_text(title_text)},
        "Source URL": {"url": source_url},
        "Source Page ID": {"rich_text": _rich_text(src.get("id", ""))},
    }
    if date_val:
        mirror_properties["Date"] = {"date": date_val}
    if owner:
        mirror_properties["Owner"] = {"rich_text": _rich_text(owner)}

    template = {"type": "template_id", "template_id": src["id"]}

    if dry_run:
        logger.info("[dry-run] would call pages.create with:")
        logger.info("    parent.database_id = %s", target_db_id)
        logger.info("    properties = %s", mirror_properties)
        logger.info("    template = %s", template)
        return 0

    try:
        mirror = client._call_with_retry(
            client._client.pages.create,
            parent={"database_id": target_db_id},
            properties=mirror_properties,
            template=template,
        )
    except APIResponseError as e:
        logger.error("pages.create with template failed (status=%s): %s", e.status, e)
        return 3

    mirror_id = mirror["id"]
    mirror_url = mirror.get("url") or f"https://www.notion.so/{mirror_id.replace('-', '')}"
    logger.info("Created mirror page: %s", mirror_url)
    logger.info(
        "Template application is async — Notion will populate the meeting_notes "
        "block over the next ~5–10 seconds. Refresh in Notion to see content."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("page_id", help="Source meeting page ID (with or without dashes).")
    parser.add_argument(
        "--target-db",
        default=os.environ.get("REPLICA_DB_ID", DEFAULT_TARGET_DB_ID),
        help=f"Target replica DB ID (default: {DEFAULT_TARGET_DB_ID}, override with $env:REPLICA_DB_ID).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print intended actions without writing.")
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
    return replicate(
        client,
        args.page_id,
        args.target_db,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
