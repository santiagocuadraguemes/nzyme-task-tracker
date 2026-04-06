"""Core webhook event handler for Notion automation payloads."""
from __future__ import annotations

import logging

from src.config import SyncConfig
from src.notion_client_wrapper import NotionClientWrapper
from src.pipeline import run_inject_templates_for_page

logger = logging.getLogger(__name__)


def handle_automation_webhook(
    payload: dict,
    config: SyncConfig,
    client: NotionClientWrapper,
) -> dict:
    """Process a Notion automation webhook payload (page.created).

    Validates the payload comes from the expected database, then injects
    the meeting template into the new page.

    Returns a dict with ``status`` and ``page_id`` keys.
    """
    source = payload.get("source", {})
    if source.get("type") != "automation":
        logger.warning("Ignoring non-automation payload: source.type=%s", source.get("type"))
        return {"status": "ignored", "reason": "not an automation payload"}

    data = payload.get("data", {})
    page_id = data.get("id")
    if not page_id:
        logger.warning("Payload missing data.id — skipping")
        return {"status": "error", "reason": "missing page id"}

    # Verify the page belongs to the Meeting Notes database
    parent = data.get("parent", {})
    db_id = parent.get("database_id", "").replace("-", "")
    expected_db = config.meeting_notes_db_id.replace("-", "")
    if db_id != expected_db:
        logger.info(
            "Ignoring page %s from database %s (expected %s)",
            page_id, db_id, expected_db,
        )
        return {"status": "ignored", "reason": "wrong database"}

    injected = run_inject_templates_for_page(config, client, page_id)
    return {
        "status": "injected" if injected else "skipped",
        "page_id": page_id,
    }
