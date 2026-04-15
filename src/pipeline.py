"""Orchestrates the AI-driven sync cycle."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import logfire

from openai import OpenAI

from src.config import SyncConfig
from src.notion_client_wrapper import NotionClientWrapper
from src.deal_context import DealContextLoader, DealInfo
from src.semantic_dedup import SemanticDedup
from src.hierarchy_loader import HierarchyLoader
from src.ai_extractor import AIExtractor
from src.sources.single_source import SingleSource
from src.template_injector import fetch_template, inject_notes_section
from src.tracker.team_writer import TeamTaskTrackerWriter
from src.utils.blocks_to_text import blocks_to_text

logger = logging.getLogger(__name__)


def _load_categories(client: NotionClientWrapper, database_id: str) -> list[str]:
    """Read category select options from the Team Task Tracker data source schema."""
    ds = client.retrieve_data_source(database_id)
    cat_prop = ds.get("properties", {}).get("Category", {})
    options = cat_prop.get("select", {}).get("options", [])
    return [opt["name"] for opt in options]


def _flatten_hierarchy(nodes: list[dict]) -> dict[str, str]:
    """Build page_id → title mapping from hierarchy tree."""
    mapping: dict[str, str] = {}

    def _walk(node_list: list[dict]) -> None:
        for node in node_list:
            mapping[node["id"]] = node["title"]
            _walk(node.get("children", []))

    _walk(nodes)
    return mapping


def _load_existing_tasks(
    client: NotionClientWrapper,
    database_id: str,
    hierarchy: list[dict],
) -> list[dict]:
    """Load recently-created extracted tasks for AI dedup context.

    Returns tasks created in the last 30 minutes that have a Meeting - Relation,
    with their title and parent title for context.
    """
    parent_titles = _flatten_hierarchy(hierarchy)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)

    try:
        response = client.query_database(
            database_id=database_id,
            filter={
                "and": [
                    {"property": "Meeting - Relation", "relation": {"is_not_empty": True}},
                    {"timestamp": "created_time", "created_time": {"after": cutoff.isoformat()}},
                ]
            },
        )
    except Exception:
        logger.exception("Failed to load existing tasks for dedup context")
        return []

    tasks: list[dict] = []
    for page in response.get("results", []):
        title = ""
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title":
                title = "".join(
                    p.get("plain_text", "") for p in prop.get("title", [])
                )
                break
        if not title:
            continue

        parent_rel = page.get("properties", {}).get("Parent item", {}).get("relation", [])
        parent_id = parent_rel[0]["id"] if parent_rel else None
        parent_title = parent_titles.get(parent_id, "") if parent_id else ""

        tasks.append({"title": title, "parent_title": parent_title})

    logger.info("Loaded %d existing tasks for AI dedup context (last 30min)", len(tasks))
    return tasks


def _fetch_page_text(client: NotionClientWrapper, page_id: str) -> str:
    """Fetch a Notion page's block content as plain text."""
    page_blocks = client.get_block_children(page_id)
    return blocks_to_text(page_blocks, client)


def _substitute_placeholders(template: str, **kwargs: str) -> str:
    """Replace ``{{KEY}}`` markers in a template string with values."""
    for key, value in kwargs.items():
        template = template.replace(f"{{{{{key}}}}}", value)
    return template


def _format_existing_tasks(tasks: list[dict]) -> str:
    """Format existing tasks list for prompt injection."""
    if not tasks:
        return ""
    lines = [
        "## Existing Tasks (DO NOT duplicate)",
        "CRITICAL: Do NOT create any task that duplicates one below.",
        "Two tasks are duplicates if they describe the same core action, even if:",
        "- Worded differently or using synonyms",
        "- In a different language (e.g., Spanish vs English)",
        "- One is more detailed than the other",
        "- They have different deadlines or assignees",
        "If in doubt, do NOT create the task.",
        "",
    ]
    for t in tasks:
        parent_info = f" (under: {t['parent_title']})" if t.get("parent_title") else ""
        lines.append(f"- {t['title']}{parent_info}")
    return "\n".join(lines) + "\n"


def _format_team_members(users: list[dict]) -> str:
    """Format team members list with aliases for AI prompt injection."""
    if not users:
        return "No team members available — use attendees only"
    lines: list[str] = []
    for m in users:
        aliases: list[str] = []
        if m.get("email"):
            aliases.append(m["email"].split("@")[0])
        name_parts = m["name"].split()
        if name_parts and name_parts[0].lower() != m["name"].lower():
            aliases.append(name_parts[0])
        alias_suffix = f" (aliases: {', '.join(aliases)})" if aliases else ""
        lines.append(f"- {m['name']} (ID: {m['id']}){alias_suffix}")
    return "\n".join(lines)


def _format_deal_context(deals: list[DealInfo]) -> str:
    """Format deal context for AI prompt injection."""
    if not deals:
        return ""
    lines: list[str] = []
    for deal in deals:
        tracker_id = deal.tracker_page_id or "not linked"
        lines.append(f"### {deal.name}")
        lines.append(f"  - deal_page_id: {deal.deal_page_id}")
        lines.append(f"  - parent_task_id (Tracker page ID): {tracker_id}")
        if deal.workstreams:
            lines.append("Workstreams:")
            for ws in deal.workstreams:
                parts = [f"{ws.title} (Status: {ws.status}"]
                if ws.workstream_type:
                    parts.append(f", Type: {', '.join(ws.workstream_type)}")
                if ws.adviser:
                    parts.append(f", Adviser: {', '.join(ws.adviser)}")
                parts.append(")")
                lines.append(f"  - {''.join(parts)}")
        lines.append("")
    return "\n".join(lines)


def _detect_deals_from_title(title: str, deals: list[DealInfo]) -> list[DealInfo]:
    """Quick heuristic: check if any deal name appears in the meeting title."""
    title_lower = title.lower()
    return [d for d in deals if d.name.lower() in title_lower]


def _run_semantic_dedup(
    tasks: list[dict], semantic_dedup: SemanticDedup | None,
) -> list[dict]:
    """Filter tasks through semantic dedup, returning only non-duplicates."""
    if not semantic_dedup or not tasks:
        return tasks
    kept: list[dict] = []
    for task in tasks:
        is_dup, matched, score = semantic_dedup.is_duplicate(task["title"])
        if is_dup:
            logger.info(
                "SEMANTIC DEDUP — skipping '%s' (%.2f match: '%s')",
                task.get("title", "?")[:60], score, (matched or "")[:60],
            )
        else:
            kept.append(task)
            semantic_dedup.add_title(task["title"])
    if len(kept) < len(tasks):
        logger.info("Semantic dedup: %d → %d tasks", len(tasks), len(kept))
    return kept


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
    """Inject meeting note template into new pages (standalone command)."""
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

    injected = _inject_templates(
        client, config.meeting_notes_db_id,
        template_blocks, marker, config.dry_run,
    )
    logger.info("Template injection complete: %d pages updated", injected)


def _load_sync_context(
    config: SyncConfig, client: NotionClientWrapper,
) -> dict:
    """Load shared context needed for AI extraction.

    Fetches prompt templates from Notion, loads hierarchy, categories,
    users, and recent tasks. Raises if prompts fail to load (required).
    """
    # Prompt templates from Notion — required
    system_prompt_template = _fetch_page_text(client, config.system_prompt_page_id)
    user_prompt_template = _fetch_page_text(client, config.user_prompt_page_id)

    hierarchy_loader = HierarchyLoader(client, config.team_tracker_db_id)

    # Hierarchy is optional
    try:
        hierarchy = hierarchy_loader.load()
        logger.info("Categories: %s", [h["title"] for h in hierarchy])
    except Exception:
        logger.exception("Failed to load hierarchy — proceeding without it")
        hierarchy = []

    # Categories from DB schema
    try:
        categories = _load_categories(client, config.team_tracker_db_id)
        if not categories:
            logger.warning("No categories found in DB schema — using fallback")
            categories = ["Other"]
        logger.info("Category options: %s", categories)
    except Exception:
        logger.exception("Failed to load categories — using fallback")
        categories = ["Other"]

    # Workspace users (optional)
    try:
        all_users = [
            {
                "id": u["id"],
                "name": u.get("name", ""),
                "email": u.get("person", {}).get("email", ""),
            }
            for u in client.list_users()
            if u.get("type") == "person"
        ]
        logger.info("Loaded %d workspace users", len(all_users))
    except Exception:
        logger.exception("Failed to load workspace users — will use attendees only")
        all_users = []

    # Recent tasks for AI dedup context
    existing_tasks = _load_existing_tasks(client, config.team_tracker_db_id, hierarchy)

    # Deal context (optional — enables deal-aware extraction)
    deals: list[DealInfo] = []
    if config.deal_workplans_db_id:
        try:
            deal_loader = DealContextLoader(client, config.deal_workplans_db_id)
            deals = deal_loader.load_deals()
            logger.info("Loaded %d deals with context", len(deals))
        except Exception:
            logger.exception("Failed to load deal context — proceeding without")

    writer = TeamTaskTrackerWriter(client, config.team_tracker_db_id, config.dry_run)

    # Semantic dedup — compare new task titles against existing ones via embeddings
    semantic_dedup: SemanticDedup | None = None
    try:
        openai_client = OpenAI(api_key=config.openai_api_key)
        existing_title_list = list(writer._existing_titles)
        semantic_dedup = SemanticDedup(
            openai_client, existing_title_list, config.semantic_dedup_threshold,
        )
        logger.info("Semantic dedup initialized with %d existing titles", len(existing_title_list))
    except Exception:
        logger.exception("Failed to initialize semantic dedup — proceeding without")

    return {
        "system_prompt_template": system_prompt_template,
        "user_prompt_template": user_prompt_template,
        "hierarchy": hierarchy,
        "categories": categories,
        "all_users": all_users,
        "existing_tasks": existing_tasks,
        "deals": deals,
        "extractor": AIExtractor(config.openai_api_key, config.openai_model, config.openai_base_url),
        "writer": writer,
        "semantic_dedup": semantic_dedup,
    }


def run_sync(config: SyncConfig, client: NotionClientWrapper) -> None:
    """Execute one full sync cycle (AI extraction + archiving)."""
    source = SingleSource(client, config.meeting_notes_db_id)

    try:
        ctx = _load_sync_context(config, client)
    except Exception:
        logger.exception("Failed to load sync context — aborting sync")
        raise

    # Poll for unprocessed meetings
    pages = source.get_unprocessed_pages(config.buffer_hours)
    if not pages:
        logger.info("No unprocessed meetings found")
    else:
        # Build meeting-level dedup set from already-processed meetings
        seen_meetings = _build_seen_fingerprints(source)

        # Pre-compute per-cycle prompt substitutions
        categories_text = " | ".join(f'"{c}"' for c in ctx["categories"])
        hierarchy_text = json.dumps(ctx["hierarchy"], indent=2)
        existing_tasks_text = _format_existing_tasks(ctx["existing_tasks"])
        team_members_text = _format_team_members(ctx["all_users"])
        deal_context_text = _format_deal_context(ctx["deals"])
        parent_titles_map = _flatten_hierarchy(ctx["hierarchy"])

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

                    attendees_text = "\n".join(
                        f"- {a['name']} (ID: {a['id']})" for a in metadata["attendees"]
                    ) or "No attendees listed"
                    creator = metadata.get("created_by", {})
                    meeting_creator_text = (
                        f"{creator['name']} (ID: {creator['id']})"
                        if creator.get("id") else "Unknown"
                    )

                    system_prompt = _substitute_placeholders(
                        ctx["system_prompt_template"],
                        CATEGORIES=categories_text,
                        HIERARCHY=hierarchy_text,
                        EXISTING_TASKS=existing_tasks_text,
                        TEAM_MEMBERS=team_members_text,
                        ATTENDEES=attendees_text,
                        MEETING_CREATOR=meeting_creator_text,
                        DEAL_CONTEXT=deal_context_text,
                    )
                    user_prompt = _substitute_placeholders(
                        ctx["user_prompt_template"],
                        MEETING_TITLE=title,
                        MEETING_DATE=metadata["date"],
                        MEETING_TYPE=metadata["meeting_type"] or "Not specified",
                        MEETING_CONTENT=content,
                    )

                    # Deal detection hint from meeting title
                    if ctx["deals"]:
                        detected = _detect_deals_from_title(title, ctx["deals"])
                        if detected:
                            deal_names = ", ".join(d.name for d in detected)
                            user_prompt += f"\n\nNote: This meeting likely relates to deal(s): {deal_names}"

                    tasks = ctx["extractor"].extract(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        categories=ctx["categories"],
                    )

                    # Assignee fallback: default to meeting creator
                    creator_id = creator.get("id")
                    for task in tasks:
                        if not task.get("assignee_id") and creator_id:
                            task["assignee_id"] = creator_id
                            logger.info(
                                "Assignee fallback → meeting creator for: %s",
                                task.get("title", "?")[:60],
                            )

                    for task in tasks:
                        task["meeting_page_id"] = page_id

                    # Semantic dedup: filter out tasks similar to existing ones
                    tasks = _run_semantic_dedup(tasks, ctx.get("semantic_dedup"))

                    if tasks:
                        created = ctx["writer"].write_batch(tasks)
                        total_tasks += len(created) if not config.dry_run else len(tasks)
                        logger.info(
                            "Page '%s': %d tasks extracted", title, len(tasks)
                        )

                        # Accumulate for cross-meeting AI dedup context
                        for task in tasks:
                            pid = task.get("parent_task_id")
                            ctx["existing_tasks"].append({
                                "title": task["title"],
                                "parent_title": parent_titles_map.get(pid, "") if pid else "",
                            })
                        existing_tasks_text = _format_existing_tasks(ctx["existing_tasks"])
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


# ---------------------------------------------------------------------------
# Single-page entry points (used by webhook / Lambda handlers)
# ---------------------------------------------------------------------------

def run_inject_templates_for_page(
    config: SyncConfig, client: NotionClientWrapper, page_id: str,
) -> bool:
    """Inject template into a single page (webhook entry point).

    Returns True if the template was injected, False if skipped.
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

    source = SingleSource(client, config.meeting_notes_db_id)
    try:
        if inject_notes_section(client, page_id, template_blocks, marker):
            if not config.dry_run:
                source.mark_template_injected(page_id)
            logger.info("Template injected into page %s", page_id)
            return True
        logger.debug("Template already present on page %s — skipped", page_id)
        return False
    except Exception:
        logger.exception("Failed to inject template into page %s", page_id)
        raise


def run_sync_for_page(
    config: SyncConfig, client: NotionClientWrapper, page_id: str,
) -> None:
    """Run AI extraction on a single page (webhook/cron entry point).

    Guards: skips if page is already processed or if the meeting date is in
    the future.
    """
    source = SingleSource(client, config.meeting_notes_db_id)

    # Fetch page and validate state
    page = client.get_page(page_id)
    props = page.get("properties", {})

    processed = props.get("Processed", {}).get("checkbox", False)
    if processed:
        logger.info("Page %s already processed — skipping", page_id)
        return

    processing = props.get("Processing", {}).get("checkbox", False)
    if processing:
        logger.info("Page %s already being processed by another invocation — skipping", page_id)
        return

    # Claim the page (concurrency lock)
    if not config.dry_run:
        source.mark_processing(page_id)

    try:
        # Load context and extract
        ctx = _load_sync_context(config, client)

        metadata = source.get_page_metadata(page)
        title = metadata["title"]

        # Dedup check
        seen_meetings = _build_seen_fingerprints(source)
        fingerprint = _meeting_fingerprint(title, metadata["date"])
        if fingerprint in seen_meetings:
            logger.info("DEDUP — skipping duplicate meeting: '%s'", title)
            if not config.dry_run:
                source.mark_page_processed(page_id)
            return

        with logfire.span("process_meeting", meeting_title=title, page_id=page_id):
            content = source.get_page_content(page_id, include_ai_notes=config.include_ai_notes)
            if not content.strip():
                logger.info("Page '%s' has no content — marking processed", title)
                if not config.dry_run:
                    source.mark_page_processed(page_id)
                return

            attendees_text = "\n".join(
                f"- {a['name']} (ID: {a['id']})" for a in metadata["attendees"]
            ) or "No attendees listed"
            team_members_text = _format_team_members(ctx["all_users"])
            creator = metadata.get("created_by", {})
            meeting_creator_text = (
                f"{creator['name']} (ID: {creator['id']})"
                if creator.get("id") else "Unknown"
            )

            system_prompt = _substitute_placeholders(
                ctx["system_prompt_template"],
                CATEGORIES=" | ".join(f'"{c}"' for c in ctx["categories"]),
                HIERARCHY=json.dumps(ctx["hierarchy"], indent=2),
                EXISTING_TASKS=_format_existing_tasks(ctx["existing_tasks"]),
                TEAM_MEMBERS=team_members_text,
                ATTENDEES=attendees_text,
                MEETING_CREATOR=meeting_creator_text,
                DEAL_CONTEXT=_format_deal_context(ctx["deals"]),
            )
            user_prompt = _substitute_placeholders(
                ctx["user_prompt_template"],
                MEETING_TITLE=title,
                MEETING_DATE=metadata["date"],
                MEETING_TYPE=metadata["meeting_type"] or "Not specified",
                MEETING_CONTENT=content,
            )

            # Deal detection hint from meeting title
            if ctx["deals"]:
                detected = _detect_deals_from_title(title, ctx["deals"])
                if detected:
                    deal_names = ", ".join(d.name for d in detected)
                    user_prompt += f"\n\nNote: This meeting likely relates to deal(s): {deal_names}"

            tasks = ctx["extractor"].extract(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                categories=ctx["categories"],
            )

            # Assignee fallback: default to meeting creator
            creator_id = creator.get("id")
            for task in tasks:
                if not task.get("assignee_id") and creator_id:
                    task["assignee_id"] = creator_id
                    logger.info(
                        "Assignee fallback → meeting creator for: %s",
                        task.get("title", "?")[:60],
                    )

            for task in tasks:
                task["meeting_page_id"] = page_id

            # Semantic dedup: filter out tasks similar to existing ones
            tasks = _run_semantic_dedup(tasks, ctx.get("semantic_dedup"))

            if tasks:
                ctx["writer"].write_batch(tasks)
                logger.info("Page '%s': %d tasks extracted", title, len(tasks))
            else:
                logger.info("Page '%s': no tasks found", title)

            if not config.dry_run:
                source.mark_page_processed(page_id)
    except Exception:
        # Release the lock so the page retries next cycle
        if not config.dry_run:
            try:
                source.clear_processing(page_id)
            except Exception:
                logger.exception("Failed to clear processing lock on %s", page_id)
        raise
