"""Meeting-note template injection.

Task extraction + classification was carved out to the standalone
``nzyme-task-extraction`` project (2026-06-15). What remains here is the
meeting-template injection orchestration used by:
  - the ``--inject-templates`` CLI path (``run_inject_templates``), and
  - the Notion automation webhook (``run_inject_templates_for_page`` via
    ``src.webhook.handler``).

Attendee resolution (formerly ``_resolve_attendees`` and friends) moved to
``src.attendees`` — the Notion → Supabase sync (``src.meeting_row``) is now
its sole in-repo caller.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from notion_client import APIResponseError

from src.config import SyncConfig
from src.notion_client_wrapper import NotionClientWrapper
from src.meeting_db_registry import load_registry
from src.sources.single_source import SingleSource
from src.template_injector import fetch_template, inject_notes_section

logger = logging.getLogger(__name__)


def _inject_templates(
    client: NotionClientWrapper,
    database_id: str,
    template_blocks: list[dict],
    marker: tuple[str, str] | None,
    dry_run: bool = False,
) -> int:
    """Inject template blocks into recent unprocessed meeting pages.

    Only targets pages created in the last 12 hours to avoid touching old pages.
    Uses the "Template Injected" checkbox to track which pages have been processed.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
    response = client.query_database(
        database_id=database_id,
        filter={
            "and": [
                {"property": "Template Injected", "checkbox": {"equals": False}},
                {"timestamp": "created_time", "created_time": {"after": cutoff.isoformat()}},
            ]
        },
        sorts=[{"timestamp": "created_time", "direction": "descending"}],
    )
    pages = response.get("results", [])

    source = SingleSource(client, database_id)
    injected = 0
    for page in pages:
        page_id = page["id"]
        title = ""
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title":
                title = "".join(p.get("plain_text", "") for p in prop.get("title", []))
                break

        if dry_run:
            logger.info("DRY RUN — would inject template into: %s", title[:80])
        else:
            try:
                if inject_notes_section(client, page_id, template_blocks, marker):
                    source.mark_template_injected(page_id)
                    injected += 1
            except Exception:
                logger.exception("Failed to inject template into: %s", title[:80])

    return injected


def run_inject_templates(config: SyncConfig, client: NotionClientWrapper) -> None:
    """Inject meeting note template into new pages across every discovered DB."""
    if not config.inject_template:
        logger.info("INJECT_TEMPLATE disabled — skipping template injection")
        return
    if not config.meeting_template_page_id:
        logger.warning("MEETING_TEMPLATE_PAGE_ID not set — skipping template injection")
        return

    template_blocks, marker = fetch_template(client, config.meeting_template_page_id)
    if not template_blocks:
        logger.warning("Template page has no usable blocks — skipping")
        return

    try:
        registry = load_registry(config, client)
    except Exception:
        logger.exception("Failed to load Meeting Notes DB registry — skipping injection")
        return

    total = 0
    for member_db in registry:
        label = member_db.owner_name or member_db.db_id[:8]
        try:
            injected = _inject_templates(
                client, member_db.db_id,
                template_blocks, marker, config.dry_run,
            )
            if injected:
                logger.info("[%s] template injected into %d page(s)", label, injected)
            total += injected
        except Exception:
            logger.exception("[%s] template injection failed — continuing", label)
    logger.info("Template injection complete: %d pages updated across %d DB(s)", total, len(registry))


def run_inject_templates_for_page(
    config: SyncConfig, client: NotionClientWrapper, page_id: str,
) -> bool:
    """Inject template into a single page (webhook entry point).

    The page's parent database is derived from the page itself, so this
    works for any per-member Meeting Notes DB without prior knowledge of
    which one. Returns True if the template was injected, False if skipped.
    """
    if not config.inject_template:
        logger.info("INJECT_TEMPLATE disabled — skipping template injection")
        return False
    if not config.meeting_template_page_id:
        logger.warning("MEETING_TEMPLATE_PAGE_ID not set — skipping template injection")
        return False

    template_blocks, marker = fetch_template(client, config.meeting_template_page_id)
    if not template_blocks:
        logger.warning("Template page has no usable blocks — skipping")
        return False

    try:
        page = client.get_page(page_id)
    except APIResponseError as e:
        if e.status == 404:
            logger.info(
                "Page %s not accessible (deleted before webhook arrived) — skipping",
                page_id,
            )
            return False
        raise

    if page.get("archived") is True or page.get("in_trash") is True:
        logger.info(
            "Page %s is archived/in trash (deleted before webhook arrived) — skipping",
            page_id,
        )
        return False

    page_db_id = (page.get("parent") or {}).get("database_id", "")
    if not page_db_id:
        logger.warning("Page %s has no parent database — cannot mark Template Injected", page_id)
        return False

    source = SingleSource(client, page_db_id)
    try:
        if inject_notes_section(client, page_id, template_blocks, marker):
            if not config.dry_run:
                source.mark_template_injected(page_id)
            logger.info("Template injected into page %s", page_id)
            return True
        logger.debug("Template already present on page %s — skipped", page_id)
        return False
    except APIResponseError as e:
        # Notion returns 404 "Could not find block … shared with your integration"
        # for archived/in-trash blocks even when pages.retrieve worked moments
        # before — race between automation firing and the user deleting the page.
        if e.status == 404:
            logger.info(
                "Page %s gone during inject (deleted/archived race) — skipping",
                page_id,
            )
            return False
        logger.exception("Failed to inject template into page %s", page_id)
        raise
    except Exception:
        logger.exception("Failed to inject template into page %s", page_id)
        raise
