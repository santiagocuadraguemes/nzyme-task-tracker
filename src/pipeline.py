"""Orchestrates the AI-driven sync cycle."""
from __future__ import annotations

import logging

from src.config import SyncConfig
from src.notion_client_wrapper import NotionClientWrapper
from src.playbook_loader import PlaybookLoader
from src.hierarchy_loader import HierarchyLoader
from src.ai_extractor import AIExtractor
from src.sources.single_source import SingleSource
from src.tracker.team_writer import TeamTaskTrackerWriter

logger = logging.getLogger(__name__)


def _load_categories(client: NotionClientWrapper, database_id: str) -> list[str]:
    """Read category select options from the Team Task Tracker DB schema."""
    db = client.retrieve_database(database_id)
    cat_prop = db.get("properties", {}).get("Category", {})
    options = cat_prop.get("select", {}).get("options", [])
    return [opt["name"] for opt in options]


def run_sync(config: SyncConfig, client: NotionClientWrapper) -> None:
    """Execute one full sync cycle."""
    source = SingleSource(client, config.meeting_notes_db_id)
    playbook_loader = PlaybookLoader(client, config.playbook_page_id)
    hierarchy_loader = HierarchyLoader(client, config.team_tracker_db_id)
    extractor = AIExtractor(config.openai_api_key, config.openai_model)
    writer = TeamTaskTrackerWriter(client, config.team_tracker_db_id, config.dry_run)

    # Load playbook (required — abort if fails)
    try:
        playbook = playbook_loader.load()
    except Exception:
        logger.exception("Failed to load playbook — aborting sync")
        raise

    # Load hierarchy (optional — proceed without if fails)
    try:
        hierarchy = hierarchy_loader.load()
    except Exception:
        logger.warning("Failed to load hierarchy — proceeding without it")
        hierarchy = []

    # Load categories dynamically from DB schema
    try:
        categories = _load_categories(client, config.team_tracker_db_id)
    except Exception:
        logger.warning("Failed to load categories — using fallback")
        categories = ["Other"]

    # Poll for unprocessed meetings
    pages = source.get_unprocessed_pages(config.buffer_hours)
    if not pages:
        logger.info("No unprocessed meetings found")
        return

    total_tasks = 0
    for page in pages:
        page_id = page["id"]
        metadata = source.get_page_metadata(page)
        title = metadata["title"]

        try:
            content = source.get_page_content(page_id)
            if not content.strip():
                logger.info("Page '%s' has no content — marking processed", title)
                if not config.dry_run:
                    source.mark_page_processed(page_id)
                continue

            tasks = extractor.extract(
                meeting_title=title,
                meeting_date=metadata["date"],
                meeting_type=metadata["meeting_type"],
                meeting_content=content,
                attendees=metadata["attendees"],
                playbook=playbook,
                hierarchy=hierarchy,
                categories=categories,
            )

            if tasks:
                created = writer.write_batch(tasks)
                total_tasks += len(created) if not config.dry_run else len(tasks)
                logger.info(
                    "Page '%s': %d tasks extracted", title, len(tasks)
                )
            else:
                logger.info("Page '%s': no tasks found", title)

            if not config.dry_run:
                source.mark_page_processed(page_id)

        except Exception:
            logger.exception("Failed to process '%s' — will retry next cycle", title)
            continue

    logger.info("Sync complete: %d tasks processed", total_tasks)
