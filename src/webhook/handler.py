"""Core webhook event handler for Notion automation payloads."""
from __future__ import annotations

import logging
import time

from notion_client import APIResponseError

from src.config import SyncConfig
from src.meeting_db_registry import load_registry
from src.notion_client_wrapper import NotionClientWrapper
from src.pipeline import run_inject_templates_for_page

logger = logging.getLogger(__name__)

# load_registry() is backed by a single Org Chart data_sources.query that
# intermittently returns a partial result set (observed in prod: 1-3 of 13
# member DBs), which wrongly rejects a real member's page as coming from an
# "unknown database" and skips template injection. On a miss we reload the
# registry a few times before giving up — a partial read almost always
# resolves within a couple retries, while a genuinely unknown (non-member)
# DB stays absent across every reload.
_REGISTRY_LOOKUP_ATTEMPTS = 3
_REGISTRY_LOOKUP_RETRY_DELAY_SECONDS = 1.0


def _set_date_to_created_time(
    client: NotionClientWrapper, page_id: str,
) -> None:
    """Set the page's ``Date`` property to its ``created_time`` (with hour).

    Idempotent — re-running it on a page produces the same value. Failures
    are logged but never raised, so the rest of the webhook flow continues.
    """
    try:
        page = client.get_page(page_id)
    except APIResponseError as e:
        if e.status == 404:
            logger.info("Page %s not accessible — skipping Date update", page_id)
            return
        logger.exception("Failed to fetch page %s for Date update", page_id)
        return

    created_time = page.get("created_time")
    if not created_time:
        logger.warning("Page %s has no created_time — skipping Date update", page_id)
        return

    try:
        client.update_page(
            page_id=page_id,
            properties={"Date": {"date": {"start": created_time}}},
        )
        logger.info("Set Date=%s on page %s", created_time, page_id)
    except Exception:
        logger.exception("Failed to set Date on page %s", page_id)


def handle_automation_webhook(
    payload: dict,
    config: SyncConfig,
    client: NotionClientWrapper,
) -> dict:
    """Process a Notion automation webhook payload (page.created).

    Validates the payload comes from one of the per-member Meeting Notes
    databases (discovered via the Org Chart), then injects the meeting
    template into the new page.

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

    # Verify the page belongs to one of the registered Meeting Notes DBs.
    #
    # Use include_inactive=True (no `Active` checkbox filter): the unfiltered
    # Org Chart query is empirically far more complete than the filtered one,
    # and an inactive member's meeting should still get a template. Reload a
    # few times on a miss to ride out partial reads (see module constants).
    parent = data.get("parent", {})
    db_id = parent.get("database_id", "").replace("-", "").lower()

    known = False
    registry_size = 0
    for attempt in range(1, _REGISTRY_LOOKUP_ATTEMPTS + 1):
        try:
            registry = load_registry(config, client, include_inactive=True)
        except Exception:
            logger.exception("Failed to load Meeting Notes DB registry — rejecting webhook")
            return {"status": "error", "reason": "registry load failed"}

        known_db_ids = {db.db_id.replace("-", "").lower() for db in registry}
        registry_size = len(known_db_ids)
        if db_id in known_db_ids:
            known = True
            break
        if attempt < _REGISTRY_LOOKUP_ATTEMPTS:
            logger.info(
                "Page %s DB %s not in registry (attempt %d/%d, registry has %d "
                "DB(s)) — reloading",
                page_id, db_id, attempt, _REGISTRY_LOOKUP_ATTEMPTS, registry_size,
            )
            time.sleep(_REGISTRY_LOOKUP_RETRY_DELAY_SECONDS)

    if not known:
        logger.info(
            "Ignoring page %s from unknown database %s (registry has %d DB(s) "
            "after %d attempt(s))",
            page_id, db_id, registry_size, _REGISTRY_LOOKUP_ATTEMPTS,
        )
        return {"status": "ignored", "reason": "unknown database"}

    _set_date_to_created_time(client, page_id)

    injected = run_inject_templates_for_page(config, client, page_id)
    return {
        "status": "injected" if injected else "skipped",
        "page_id": page_id,
    }
