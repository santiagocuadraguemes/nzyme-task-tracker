"""Orchestrates the AI-driven sync cycle."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import logfire

from src.config import SyncConfig
from src.notion_client_wrapper import NotionClientWrapper
from src.playbook_loader import PlaybookLoader
from src.hierarchy_loader import HierarchyLoader
from src.ai_extractor import AIExtractor
from src.sources.single_source import SingleSource
from src.template_injector import fetch_template, inject_notes_section
from src.tracker.team_writer import TeamTaskTrackerWriter

logger = logging.getLogger(__name__)


def _load_categories(client: NotionClientWrapper, database_id: str) -> list[str]:
    """Read category select options from the Team Task Tracker data source schema."""
    ds = client.retrieve_data_source(database_id)
    cat_prop = ds.get("properties", {}).get("Category", {})
    options = cat_prop.get("select", {}).get("options", [])
    return [opt["name"] for opt in options]


def _meeting_fingerprint(title: str, date: str) -> str:
    """Normalize meeting title + date into a dedup key.

    Strips trailing (1), (2) suffixes that Notion adds to duplicates,
    lowercases, and combines with date.
    """
    normalized = re.sub(r"\s*\(\d+\)\s*$", "", title).strip().lower()
    return f"{normalized}|{date}"


def _build_seen_fingerprints(source: SingleSource) -> set[str]:
    """Collect fingerprints from already-processed meetings for cross-cycle dedup."""
    seen: set[str] = set()
    try:
        processed_pages = source.get_processed_pages()
        for page in processed_pages:
            meta = source.get_page_metadata(page)
            if meta["date"]:
                fp = _meeting_fingerprint(meta["title"], meta["date"])
                seen.add(fp)
        logger.info("Loaded %d processed meeting fingerprints for dedup", len(seen))
    except Exception:
        logger.exception("Failed to load processed meetings for dedup — proceeding without")
    return seen


def _archive_done_tasks(
    client: NotionClientWrapper,
    database_id: str,
    grace_days: int = 3,
    dry_run: bool = False,
) -> int:
    """Archive tasks marked Done whose last edit is older than grace_days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=grace_days)
    db_filter = {
        "and": [
            {"property": "Status", "status": {"equals": "Done"}},
            {"timestamp": "last_edited_time", "last_edited_time": {"before": cutoff.isoformat()}},
        ]
    }
    response = client.query_database(database_id=database_id, filter=db_filter)
    pages = response.get("results", [])

    archived = 0
    for page in pages:
        page_id = page["id"]
        title = ""
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title":
                title = "".join(p.get("plain_text", "") for p in prop.get("title", []))
                break

        if dry_run:
            logger.info("DRY RUN — would archive done task: %s", title[:80])
        else:
            try:
                client.archive_page(page_id)
                logger.info("Archived done task: %s", title[:80])
                archived += 1
            except Exception:
                logger.exception("Failed to archive task: %s", title[:80])

    return archived


def _inject_templates(
    client: NotionClientWrapper,
    database_id: str,
    template_blocks: list[dict],
    marker: tuple[str, str] | None,
    dry_run: bool = False,
) -> int:
    """Inject template blocks into recent unprocessed meeting pages.

    Only targets pages created in the last 12 hours to avoid touching old pages.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
    response = client.query_database(
        database_id=database_id,
        filter={
            "and": [
                {"property": "Processed", "checkbox": {"equals": False}},
                {"timestamp": "created_time", "created_time": {"after": cutoff.isoformat()}},
            ]
        },
        sorts=[{"timestamp": "created_time", "direction": "descending"}],
    )
    pages = response.get("results", [])

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
                    injected += 1
            except Exception:
                logger.exception("Failed to inject template into: %s", title[:80])

    return injected


def run_inject_templates(config: SyncConfig, client: NotionClientWrapper) -> None:
    """Inject meeting note template into new pages (standalone command)."""
    if not config.meeting_template_page_id:
        logger.warning("MEETING_TEMPLATE_PAGE_ID not set — skipping template injection")
        return

    template_blocks, marker = fetch_template(client, config.meeting_template_page_id)
    if not template_blocks:
        logger.warning("Template page has no usable blocks — skipping")
        return

    injected = _inject_templates(
        client, config.meeting_notes_db_id,
        template_blocks, marker, config.dry_run,
    )
    logger.info("Template injection complete: %d pages updated", injected)


def run_sync(config: SyncConfig, client: NotionClientWrapper) -> None:
    """Execute one full sync cycle (AI extraction + archiving)."""
    source = SingleSource(client, config.meeting_notes_db_id)
    playbook_loader = PlaybookLoader(client, config.playbook_page_id)
    hierarchy_loader = HierarchyLoader(client, config.team_tracker_db_id)
    extractor = AIExtractor(config.openai_api_key, config.openai_model, config.openai_base_url)
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
        logger.info("Categories: %s", [h["title"] for h in hierarchy])
    except Exception:
        logger.exception("Failed to load hierarchy — proceeding without it")
        hierarchy = []

    # Load categories dynamically from DB schema
    try:
        categories = _load_categories(client, config.team_tracker_db_id)
        if not categories:
            logger.warning("No categories found in DB schema — using fallback")
            categories = ["Other"]
        logger.info("Category options: %s", categories)
    except Exception:
        logger.exception("Failed to load categories — using fallback")
        categories = ["Other"]

    # Load all workspace users (optional — fall back to attendees-only)
    try:
        all_users = [
            {"id": u["id"], "name": u.get("name", "")}
            for u in client.list_users()
            if u.get("type") == "person"
        ]
        logger.info("Loaded %d workspace users", len(all_users))
    except Exception:
        logger.exception("Failed to load workspace users — will use attendees only")
        all_users = []

    # Poll for unprocessed meetings
    pages = source.get_unprocessed_pages(config.buffer_hours)
    if not pages:
        logger.info("No unprocessed meetings found")
    else:
        # Build meeting-level dedup set from already-processed meetings
        seen_meetings = _build_seen_fingerprints(source)

        total_tasks = 0
        for page in pages:
            page_id = page["id"]
            metadata = source.get_page_metadata(page)
            title = metadata["title"]

            # Meeting-level dedup: skip if same meeting (title+date) already processed
            fingerprint = _meeting_fingerprint(title, metadata["date"])
            if fingerprint in seen_meetings:
                logger.info("DEDUP — skipping duplicate meeting: '%s'", title)
                if not config.dry_run:
                    source.mark_page_processed(page_id)
                continue
            seen_meetings.add(fingerprint)

            try:
                with logfire.span("process_meeting", meeting_title=title, page_id=page_id):
                    content = source.get_page_content(page_id, include_ai_notes=config.include_ai_notes)
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
                        team_members=all_users,
                        playbook=playbook,
                        hierarchy=hierarchy,
                        categories=categories,
                    )

                    for task in tasks:
                        task["meeting_page_id"] = page_id

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

        logger.info("Extraction complete: %d tasks processed", total_tasks)

    # Archive done tasks (3-day grace period)
    try:
        archived = _archive_done_tasks(
            client, config.team_tracker_db_id, grace_days=3, dry_run=config.dry_run,
        )
        if archived:
            logger.info("Archived %d done tasks", archived)
    except Exception:
        logger.exception("Failed to archive done tasks — will retry next cycle")
