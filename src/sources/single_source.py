"""Meeting Notes source — queries for unprocessed meetings with buffer delay."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from src.notion_client_wrapper import NotionClientWrapper
from src.utils.blocks_to_text import blocks_to_text

logger = logging.getLogger(__name__)


class SingleSource:
    """Fetches and processes meeting pages from a single Notion database."""

    def __init__(self, client: NotionClientWrapper, database_id: str) -> None:
        self._client = client
        self._database_id = database_id

    def get_unprocessed_pages(self, buffer_hours: int = 2) -> list[dict]:
        """Return pages where Date < (now - buffer) AND Processed = false."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=buffer_hours)
        db_filter = {
            "and": [
                {"property": "Processed", "checkbox": {"equals": False}},
                {"property": "Date", "date": {"before": cutoff.isoformat()}},
            ]
        }
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

    def get_page_content(self, page_id: str) -> str:
        """Fetch all blocks from a page and convert to plain text."""
        blocks = self._client.get_block_children(page_id)
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

    def mark_page_processed(self, page_id: str) -> None:
        """Set Processed = true on a meeting page."""
        self._client.update_page(
            page_id=page_id,
            properties={"Processed": {"checkbox": True}},
        )
        logger.debug("Marked page %s as processed", page_id)
