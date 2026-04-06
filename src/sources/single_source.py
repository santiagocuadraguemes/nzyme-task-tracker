"""Meeting Notes source — queries for unprocessed meetings with buffer delay."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from src.notion_client_wrapper import NotionClientWrapper
from src.utils.blocks_to_text import blocks_to_text

logger = logging.getLogger(__name__)

# Block types that represent human-written content.
# AI meeting notes (ai_block, etc.) are excluded from this set.
HUMAN_CONTENT_BLOCK_TYPES = frozenset({
    "heading_1", "heading_2", "heading_3",
    "paragraph", "bulleted_list_item", "numbered_list_item",
    "to_do", "toggle", "callout", "quote", "divider",
    "code", "table", "table_row", "column_list", "column",
    "image", "video", "file", "pdf", "bookmark", "embed",
    "audio", "equation", "breadcrumb", "link_preview",
    "synced_block", "template", "link_to_page",
})


class SingleSource:
    """Fetches and processes meeting pages from a single Notion database."""

    def __init__(self, client: NotionClientWrapper, database_id: str) -> None:
        self._client = client
        self._database_id = database_id

    def get_unprocessed_pages(self, buffer_hours: int | None = 2) -> list[dict]:
        """Return pages where Processed = false.

        When *buffer_hours* is set, also requires created_time < (now - buffer).
        When *buffer_hours* is None, returns all unprocessed pages regardless of age.
        """
        if buffer_hours is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=buffer_hours)
            db_filter: dict = {
                "and": [
                    {"property": "Processed", "checkbox": {"equals": False}},
                    {"timestamp": "created_time", "created_time": {"before": cutoff.isoformat()}},
                ]
            }
        else:
            db_filter = {"property": "Processed", "checkbox": {"equals": False}}
        response = self._client.query_database(
            database_id=self._database_id,
            filter=db_filter,
            sorts=[{"timestamp": "last_edited_time", "direction": "descending"}],
        )
        pages = response.get("results", [])
        logger.info("Found %d unprocessed pages (buffer=%dh)", len(pages), buffer_hours)
        return pages

    def get_processed_pages(self) -> list[dict]:
        """Return all pages where Processed = true (for dedup fingerprinting)."""
        response = self._client.query_database(
            database_id=self._database_id,
            filter={"property": "Processed", "checkbox": {"equals": True}},
        )
        return response.get("results", [])

    def get_page_content(self, page_id: str, include_ai_notes: bool = True) -> str:
        """Fetch all blocks from a page and convert to plain text.

        When *include_ai_notes* is False, blocks whose type is not in the
        human-content whitelist are dropped (along with their children).
        """
        blocks = self._client.get_block_children(page_id)
        if not include_ai_notes:
            blocks = [b for b in blocks if b.get("type") in HUMAN_CONTENT_BLOCK_TYPES]
        return blocks_to_text(blocks, self._client)

    def get_page_metadata(self, page: dict) -> dict:
        """Extract title, date, meeting type, and attendees from a page object."""
        props = page.get("properties", {})

        title = ""
        for prop in props.values():
            if prop.get("type") == "title":
                title = "".join(
                    p.get("plain_text", "") for p in prop.get("title", [])
                )
                break

        date_prop = props.get("Date", {}).get("date")
        date_str = date_prop.get("start", "") if date_prop else ""

        mt_prop = props.get("Meeting type", {}).get("select")
        meeting_type = mt_prop.get("name", "") if mt_prop else ""

        attendees = [
            {"id": p.get("id", ""), "name": p.get("name", "")}
            for p in props.get("Attendees", {}).get("people", [])
        ]

        return {
            "title": title,
            "date": date_str,
            "meeting_type": meeting_type,
            "attendees": attendees,
        }

    def get_ready_pages(self, idle_minutes: int = 3) -> list[dict]:
        """Return pages ready for AI extraction.

        A page is "ready" when:
        - Processed = false (not yet extracted)
        - last_edited_time < now - idle_minutes (no one actively editing)
        """
        idle_cutoff = datetime.now(timezone.utc) - timedelta(minutes=idle_minutes)
        logger.info(
            "get_ready_pages: idle_cutoff=%s, db=%s",
            idle_cutoff.isoformat(), self._database_id,
        )
        db_filter: dict = {
            "and": [
                {"property": "Processed", "checkbox": {"equals": False}},
                {
                    "timestamp": "last_edited_time",
                    "last_edited_time": {"before": idle_cutoff.isoformat()},
                },
            ]
        }
        response = self._client.query_database(
            database_id=self._database_id,
            filter=db_filter,
            sorts=[{"timestamp": "last_edited_time", "direction": "descending"}],
        )
        pages = response.get("results", [])
        logger.info("Found %d pages ready for extraction (idle>%dmin)", len(pages), idle_minutes)
        if pages:
            for p in pages[:5]:
                pid = p.get("id", "?")
                let = p.get("last_edited_time", "?")
                logger.info("  ready page: id=%s last_edited=%s", pid, let)
        return pages

    def mark_template_injected(self, page_id: str) -> None:
        """Set Template Injected = true on a meeting page."""
        self._client.update_page(
            page_id=page_id,
            properties={"Template Injected": {"checkbox": True}},
        )
        logger.debug("Marked page %s as template injected", page_id)

    def mark_page_processed(self, page_id: str) -> None:
        """Set Processed = true on a meeting page."""
        self._client.update_page(
            page_id=page_id,
            properties={"Processed": {"checkbox": True}},
        )
        logger.debug("Marked page %s as processed", page_id)
