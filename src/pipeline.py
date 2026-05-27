"""Orchestrates the AI-driven sync cycle."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import logfire

from notion_client import APIResponseError
from openai import OpenAI

from src.config import SyncConfig
from src.notion_client_wrapper import NotionClientWrapper
from src.deal_context import DealContextLoader, DealInfo
from src.semantic_dedup import SemanticDedup
from src.hierarchy_loader import (
    HierarchyLoader,
    attach_notes_to_hierarchy,
    load_hierarchy_notes,
)
from src import literal_notes_extractor
from src.meeting_db_registry import (
    MeetingDB, find_owner_for_page, load_registry,
)
from src.sources.single_source import SingleSource
from src.template_injector import fetch_template, inject_notes_section
from src.tracker.team_writer import TeamTaskTrackerWriter
from src.transcript_pipeline.context_loader import (
    load_terminology,
    load_org_chart_rows,
    format_org_chart,
    build_enriched_attendee_str,
)
from src.transcript_pipeline.fetch_transcript import (
    find_meeting_notes_block,
    extract_transcript_block_id,
    extract_attendee_ids,
    extract_governance_attendees,
    build_user_lookup,
    fetch_notes_text,
)
from src.transcript_pipeline.transcript_cleaner import clean as clean_transcript
from src.utils.blocks_to_text import blocks_to_text

logger = logging.getLogger(__name__)

# Hardcoded OpenAI base URL for LIGHT calls. We must pass this explicitly
# because the OpenAI SDK falls back to the OPENAI_BASE_URL env var when
# base_url is None — which would route light calls to Gemini if that env
# var is still set from an older config.
OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"


def _resolve_stage_creds(
    model_name: str, config: SyncConfig,
) -> tuple[str, str]:
    """Pick (api_key, base_url) for a stage based on the model name prefix.

    Convention (checked in order):
      - Contains ``/`` → OpenRouter slug (e.g. ``google/gemini-2.5-flash-preview``,
        ``deepseek/deepseek-chat-v3.1:free``). Routes via
        OPENROUTER_API_KEY + openrouter_base_url. Useful as a Gemini failover
        when Google returns 503 UNAVAILABLE on the merged-extraction call.
      - Starts with ``gemini-`` → Google Gemini direct. Routes via
        GEMINI_API_KEY + gemini_base_url. Only this path can use Gemini's
        native context cache (~25% input-token discount); OpenRouter loses it.
      - Otherwise → OpenAI direct. Routes via OPENAI_API_KEY + the hardcoded
        OpenAI base URL.

    Per-stage CLI overrides (``--correction-model``, ``--extraction-model``,
    ``--classification-model``) swap providers by passing a model name that
    matches one of these conventions — no extra flag needed.
    """
    if "/" in model_name:
        if not config.openrouter_api_key:
            raise RuntimeError(
                f"Model '{model_name}' looks like an OpenRouter slug but "
                "OPENROUTER_API_KEY is not set in .env."
            )
        return config.openrouter_api_key, config.openrouter_base_url
    if model_name.startswith("gemini-"):
        if not config.gemini_api_key:
            raise RuntimeError(
                f"Model '{model_name}' requires GEMINI_API_KEY. Set it in .env."
            )
        return config.gemini_api_key, config.gemini_base_url
    return config.openai_api_key, OPENAI_DEFAULT_BASE_URL


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

    Returns tasks created in the last 30 minutes excluding architecture rows
    (`Priority = [DETAILS INSIDE]`), with their title and parent title.
    """
    parent_titles = _flatten_hierarchy(hierarchy)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)

    try:
        response = client.query_database(
            database_id=database_id,
            filter={
                "and": [
                    {"property": "Priority", "select": {"does_not_equal": "[DETAILS INSIDE]"}},
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

    logger.debug("Loaded %d existing tasks for AI dedup context (last 30min)", len(tasks))
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


def _resolve_delegated_user(
    client: NotionClientWrapper,
    created_by_id: str,
    default_email: str | None,
) -> str | None:
    """Resolve the meeting page's Notion creator to a Workspace email.

    Returns the creator's email when retrievable, else `default_email`.
    The returned email is used to impersonate a Workspace user when calling
    Google Calendar via the service account.
    """
    if created_by_id:
        try:
            user = client.retrieve_user(created_by_id)
            email = (user.get("person") or {}).get("email") or ""
            if email:
                return email.strip().lower()
            logger.info(
                "Notion user %s has no email (bot or integration-gated); using default %s",
                created_by_id, default_email,
            )
        except Exception:
            logger.warning(
                "Failed to retrieve Notion user %s; using default %s",
                created_by_id, default_email, exc_info=True,
            )
    return default_email


def _enrich_attendee_names(
    attendees: list[dict[str, str]],
    org_chart_rows: list[dict] | None,
) -> list[dict[str, str]]:
    """Replace email-prefix placeholder names with Org Chart full names when available."""
    if not org_chart_rows:
        return attendees
    email_to_name = {
        row["email"]: row["name"]
        for row in org_chart_rows
        if row.get("email") and row.get("name")
    }
    enriched: list[dict[str, str]] = []
    for att in attendees:
        email = (att.get("email") or "").lower()
        if email and email in email_to_name:
            enriched.append({**att, "name": email_to_name[email]})
        else:
            enriched.append(att)
    return enriched


def _resolve_attendees(
    client: NotionClientWrapper,
    config: SyncConfig,
    mn_block: dict | None,
    page: dict,
    metadata: dict,
    *,
    org_chart_rows: list[dict] | None = None,
) -> list[dict[str, str]]:
    """Resolve meeting attendees via the priority chain.

    Priority:
    1. Google Calendar (when `config.gcal_enabled`) — impersonates the page's
       Notion creator via service account; falls back to default delegated
       user if the creator's email can't be resolved.
    2. Notion meeting_notes.calendar_event.attendees.
    3. Page's "Governance: Edit & View Access" people property.

    Returns list of {"id", "name", optional "email"} dicts. Notion-source
    attendees now also carry `email` (pulled from the workspace user
    record) so the org-chart enrichment step works regardless of whether
    GCal returned an event.
    """
    attendees: list[dict[str, str]] = []

    # Source 2: Notion meeting_notes attendees
    if mn_block is not None:
        attendee_ids = extract_attendee_ids(mn_block)
        if attendee_ids:
            workspace_users = {u.get("id"): u for u in client.list_users()}
            for uid in attendee_ids:
                u = workspace_users.get(uid, {})
                person = u.get("person") or {}
                email = (person.get("email") or "").strip().lower() or None
                name = u.get("name") or email or uid
                attendees.append({"id": uid, "name": name, "email": email})
            # Email-based canonical-name lookup against the Org Chart — same
            # step the GCal branch already runs. Without this, Notion display
            # names like "reyes" (lowercase email prefix) never resolve to
            # the org chart's "Reyes Rubio" entry.
            if org_chart_rows:
                attendees = _enrich_attendee_names(attendees, org_chart_rows)

    # Source 1: Google Calendar (overrides Notion when an event matches)
    gcal_ready = config.gcal_enabled and metadata.get("title") and metadata.get("date")
    if gcal_ready:
        try:
            from src.transcript_pipeline.gcal_attendees import get_gcal_attendees

            created_by_id = (metadata.get("created_by") or {}).get("id", "")
            delegated_user = _resolve_delegated_user(
                client, created_by_id, config.gcal_delegated_user_default,
            )
            if not delegated_user:
                logger.warning(
                    "No Workspace user to impersonate for GCal lookup — skipping",
                )
            else:
                logger.debug("GCal lookup impersonating %s", delegated_user)
                gcal_attendees = get_gcal_attendees(
                    metadata["title"], metadata["date"], delegated_user,
                )
                if gcal_attendees:
                    attendees = [
                        {"id": ga["email"], "name": ga["name"], "email": ga["email"]}
                        for ga in gcal_attendees
                    ]
                    attendees = _enrich_attendee_names(attendees, org_chart_rows)
                    logger.info("GCal attendees resolved: %d", len(attendees))
        except Exception:
            logger.warning("GCal lookup failed — using Notion attendees", exc_info=True)
    else:
        reasons = []
        if not config.gcal_enabled:
            reasons.append("gcal_enabled=False")
        if not metadata.get("title"):
            reasons.append("no title")
        if not metadata.get("date"):
            reasons.append("no date")
        logger.debug("GCal lookup skipped: %s", ", ".join(reasons))

    # Source 3: Governance fallback
    if not attendees:
        governance = extract_governance_attendees(page)
        if governance:
            attendees = [{**g, "email": None} for g in governance]
            logger.debug(
                "Using governance-access fallback (%d attendees)", len(attendees),
            )

    return attendees


def _process_via_transcript(
    client: NotionClientWrapper,
    config: SyncConfig,
    ctx: dict,
    page_id: str,
    page: dict,
    blocks: list[dict],
    mn_block: dict,
    metadata: dict,
    attendees: list[dict[str, str]],
) -> list[dict]:
    """Extract tasks from a meeting transcript (correct → extract → classify).

    Returns list of task dicts with category, parent_task_id, assignee_id,
    deal_page_id already set by the classifier.
    """
    from src.transcript_pipeline.task_extractor import TaskExtractor
    from src.transcript_pipeline.task_classifier import TaskClassifier

    # Fetch transcript text. The router only invokes this path when
    # extract_transcript_block_id(mn_block) returned a valid id, so the
    # re-call here is just to keep the signature un-coupled and cannot
    # legitimately return None.
    transcript_block_id = extract_transcript_block_id(mn_block)
    if transcript_block_id is None:
        logger.warning("Transcript block id missing — no tasks to extract")
        return []
    transcript_blocks = client.get_block_children(transcript_block_id)
    if not transcript_blocks:
        logger.warning("Transcript block has no children — no tasks to extract")
        return []
    transcript_text = blocks_to_text(transcript_blocks, client)
    if not transcript_text.strip():
        logger.warning("Transcript text is empty — no tasks to extract")
        return []

    # Deterministic noise cleanup (timestamps, bare speaker labels,
    # same-speaker run collapse, adjacent-identical sentence dedup).
    cleaned = clean_transcript(transcript_text)
    if cleaned.chars_before:
        logger.info(
            "Transcript cleaned: %d → %d chars (%.0f%% kept)",
            cleaned.chars_before, cleaned.chars_after, cleaned.ratio * 100,
        )
    transcript_text = cleaned.text

    # Fetch human notes from meeting_notes block
    notes_text = fetch_notes_text(mn_block, client)

    # Build enriched attendee string
    enriched_attendee_str = ""
    if ctx["org_chart_rows"] and attendees:
        enriched_attendee_str = build_enriched_attendee_str(attendees, ctx["org_chart_rows"])

    logger.info(
        "Transcript loaded: %.1f KB (%d chars)",
        len(transcript_text) / 1024, len(transcript_text),
    )

    extraction_model = config.extraction_model or config.gemini_model
    extraction_key, extraction_base = _resolve_stage_creds(extraction_model, config)

    # Single merged LLM call does correction + extraction inline.
    extractor = TaskExtractor(
        api_key=extraction_key,
        model=extraction_model,
        base_url=extraction_base,
    )
    t0 = time.perf_counter()
    tasks = extractor.extract_from_raw(
        transcript_text,
        attendees,
        system_prompt=ctx["merged_transcript_prompt"],
        org_chart=ctx["org_chart_text"],
        terminology=ctx["terminology"],
        meeting_title=metadata.get("title", ""),
        meeting_date=metadata.get("date", ""),
        enriched_attendee_str=enriched_attendee_str,
        notes_text=notes_text,
    )
    extract_elapsed = time.perf_counter() - t0
    if not tasks:
        logger.info(
            "Merged-extracted 0 tasks (%s, %.1fs)", extraction_model, extract_elapsed,
        )
        return []
    logger.info(
        "Merged-extracted %d tasks (%s, %.1fs)",
        len(tasks), extraction_model, extract_elapsed,
    )
    logger.debug("Extracted task payload: %s", json.dumps(tasks, ensure_ascii=False, indent=2))

    # Step 3: Classify tasks
    if not ctx["classifier_prompt"]:
        logger.warning("No classifier prompt — skipping classification, tasks will have no category/parent")
        return tasks

    classification_model = config.classification_model or config.openai_model
    classification_key, classification_base = _resolve_stage_creds(classification_model, config)
    classifier = TaskClassifier(
        api_key=classification_key,
        model=classification_model,
        base_url=classification_base,
    )
    meeting_taxonomy = {
        "macro_work_block": metadata.get("macro_work_block", ""),
        "detail": metadata.get("detail", []),
        "external_org": metadata.get("external_org", ""),
    }
    t0 = time.perf_counter()
    tasks = classifier.classify(
        tasks,
        ctx["classifier_prompt"],
        ctx["hierarchy"],
        ctx["all_users"],
        meeting_title=metadata.get("title", ""),
        meeting_date=metadata.get("date", ""),
        enriched_attendees=enriched_attendee_str,
        notes_text=notes_text,
        meeting_taxonomy=meeting_taxonomy,
    )
    logger.info(
        "Classified %d tasks (%s, %.1fs)",
        len(tasks), classification_model, time.perf_counter() - t0,
    )
    logger.debug("Classified task payload: %s", json.dumps(tasks, ensure_ascii=False, indent=2))
    return tasks


def _should_auto_extract(
    config: SyncConfig, owner: MeetingDB | None,
) -> bool:
    """Decide whether the transcript pipeline runs for this page.

    Priority: CLI override → owner's Org Chart flag → True (fail open when
    the owner can't be resolved, which keeps today's behaviour).
    """
    if config.auto_extract_tasks_override is not None:
        return config.auto_extract_tasks_override
    if owner is None:
        return True
    return owner.auto_extract_tasks


def _mirror_meeting_to_affinity(
    *,
    config: SyncConfig,
    client: NotionClientWrapper,
    page: dict,
    metadata: dict,
    attendees: list[dict[str, str]],
    page_id: str,
    short_id: str,
    db_owner: str,
) -> None:
    """Fundraising → Affinity LP Funnel branch.

    Fires for EVERY meeting whose ``Macro Work Block`` matches an active
    "Fire Affinity LP Funnel" rule in the Meeting Rules DB — like template
    injection, a tagged meeting is mirrored to its LP regardless of the
    extraction outcome. It does NOT matter whether the page yielded tasks,
    has any notes, or whether the owner opted into AI extraction: a
    fundraising meeting happening is itself worth logging against the LP, and
    when neither user notes nor the AI Summary are populated the note degrades
    to title + Notion backlink. The only downstream gate is whether an
    attendee maps to an LP opportunity (``Skipped: no LP match`` otherwise).

    Soft-fails — never raises. Emits a single structured ``fundraising
    outcome:`` line so skips and failures stay grep-able in CloudWatch.
    Skipped wholesale in dry-run.
    """
    if (
        config.dry_run
        or not config.fundraising_branch_enabled
        or not config.meeting_rules_db_id
    ):
        return

    fundraising_rule_label = ""
    try:
        from src.topic_mirror.route_registry import (
            ACTION_AFFINITY_LP_FUNNEL, load_routes, match_routes,
        )
        all_rules = load_routes(client, config.meeting_rules_db_id)
        affinity_rules = [
            r for r in all_rules if r.action == ACTION_AFFINITY_LP_FUNNEL
        ]
        matched_affinity = match_routes(affinity_rules, page.get("properties", {}))
        if matched_affinity:
            fundraising_rule_label = matched_affinity[0].label
    except Exception:
        logger.exception(
            "page=%s failed to evaluate Affinity LP Funnel rules — "
            "treating as no match",
            short_id,
        )

    logger.info(
        "page=%s db_owner=%s fundraising decision: rule=%r → %s",
        short_id, db_owner, fundraising_rule_label or None,
        "RUN" if fundraising_rule_label else "SKIP",
    )
    if not fundraising_rule_label:
        return

    from src.fundraising import write_to_affinity
    from src.fundraising.outcome import FundraisingStatus

    logger.info(
        "page=%s db_owner=%s fundraising branch: starting LP match",
        short_id, db_owner,
    )
    # ``tasks`` is vestigial in ``write_to_affinity`` (the note body comes
    # from user notes + AI Summary, not tasks), so an empty list is correct
    # here — this runs before extraction.
    outcome = write_to_affinity(
        config=config,
        tasks=[],
        metadata=metadata,
        attendees=attendees,
        notion_url=metadata.get("url", ""),
        page_id=page_id,
        notion_client=client,
    )
    # Single structured line per run — log level reflects severity so
    # CloudWatch filters can split actionable failures from expected skips.
    log_fn = (
        logger.error
        if outcome.status == FundraisingStatus.FAILED_API_ERROR
        else logger.info
    )
    log_fn(
        "fundraising outcome: page=%s db_owner=%s status=%s detail=%s",
        short_id, db_owner, outcome.status.value, outcome.detail,
    )


def _process_via_literal_notes(
    client: NotionClientWrapper,
    config: SyncConfig,
    ctx: dict,
    page_id: str,
    blocks: list[dict],
    metadata: dict,
    attendees: list[dict[str, str]] | None = None,
) -> list[dict]:
    """Notes-only LLM path: extract bullets verbatim, then classify.

    Used when the page's Org Chart owner has Auto-extract Tasks=False (or
    the CLI override forces FALSE). The extraction prompt (Notion-hosted)
    instructs the model to return one task per `## Action Items` bullet
    with the title kept as the author typed it. The existing classifier
    then resolves category/parent/deal_page_id and `assignee_id` from
    the internal/external split.
    """
    if not ctx.get("literal_notes_prompt"):
        logger.warning(
            "literal-notes: prompt not configured (LITERAL_NOTES_EXTRACTION_PROMPT_PAGE_ID) — "
            "cannot extract for page=%s",
            page_id,
        )
        return []

    extraction_model = config.openai_model
    extraction_key, extraction_base = _resolve_stage_creds(extraction_model, config)
    extraction_client = OpenAI(api_key=extraction_key, base_url=extraction_base)

    t0 = time.perf_counter()
    tasks = literal_notes_extractor.extract(
        client=client,
        page_blocks=blocks,
        metadata=metadata,
        attendees=attendees,
        all_users=ctx.get("all_users") or [],
        system_prompt_template=ctx["literal_notes_prompt"],
        openai_client=extraction_client,
        model=extraction_model,
    )
    logger.info(
        "literal-notes: extracted %d task(s) (%s, %.1fs)",
        len(tasks), extraction_model, time.perf_counter() - t0,
    )
    if not tasks:
        return []

    if not ctx["classifier_prompt"]:
        logger.warning(
            "literal-notes: no classifier prompt — skipping classification, "
            "tasks will have no category/parent/assignee",
        )
        return tasks

    from src.transcript_pipeline.task_classifier import TaskClassifier

    classification_model = config.classification_model or config.openai_model
    classification_key, classification_base = _resolve_stage_creds(
        classification_model, config,
    )
    classifier = TaskClassifier(
        api_key=classification_key,
        model=classification_model,
        base_url=classification_base,
    )
    meeting_taxonomy = {
        "macro_work_block": metadata.get("macro_work_block", ""),
        "detail": metadata.get("detail", []),
        "external_org": metadata.get("external_org", ""),
    }
    t0 = time.perf_counter()
    classified = classifier.classify(
        tasks,
        ctx["classifier_prompt"],
        ctx["hierarchy"],
        ctx["all_users"],
        meeting_title=metadata.get("title", ""),
        meeting_date=metadata.get("date", ""),
        meeting_taxonomy=meeting_taxonomy,
    )
    logger.info(
        "literal-notes: classified %d tasks (%s, %.1fs)",
        len(classified), classification_model, time.perf_counter() - t0,
    )
    return classified


def _meeting_fingerprint(db_id: str, title: str, date: str) -> str:
    """Normalize (db_id, title, date) into a per-DB dedup key.

    The db_id prefix is what makes the multi-DB model safe: two team members
    capturing notes for the same meeting in their own DBs share title+date
    but live in different DBs, so they MUST be processed independently. The
    " (1)" suffix Notion adds to duplicates within a single DB still
    collapses correctly.
    """
    normalized_db = (db_id or "").replace("-", "").lower()
    normalized_title = re.sub(r"\s*\(\d+\)\s*$", "", title).strip().lower()
    return f"{normalized_db}|{normalized_title}|{date}"


def _build_seen_fingerprints(source: SingleSource, db_id: str) -> set[str]:
    """Collect fingerprints from already-processed meetings within one DB."""
    seen: set[str] = set()
    try:
        processed_pages = source.get_processed_pages()
        for page in processed_pages:
            meta = source.get_page_metadata(page)
            if meta["date"]:
                fp = _meeting_fingerprint(db_id, meta["title"], meta["date"])
                seen.add(fp)
        logger.debug(
            "Loaded %d processed meeting fingerprints for db=%s",
            len(seen), db_id[:8],
        )
    except Exception:
        logger.exception("Failed to load processed meetings for dedup — proceeding without")
    return seen


_READ_ONLY_PROPERTY_TYPES = frozenset({
    "formula", "rollup", "created_time", "last_edited_time",
    "created_by", "last_edited_by", "unique_id",
})

# Hierarchy relations get dropped on archival — once parents are archived too,
# the references would dangle. Archive is a flat record of completed work.
_SKIP_PROPERTY_NAMES_ON_ARCHIVE = frozenset({"Parent item", "Sub-item"})


def _copy_property_for_write(prop: dict) -> dict | None:
    """Convert a Notion property from read shape to write shape.

    Returns None for read-only types so the caller can skip them.
    """
    ptype = prop.get("type")
    if not ptype or ptype in _READ_ONLY_PROPERTY_TYPES:
        return None

    if ptype == "title":
        items = prop.get("title") or []
        return {"title": [{"text": {"content": rt.get("plain_text", "")}} for rt in items]}

    if ptype == "rich_text":
        items = prop.get("rich_text") or []
        return {"rich_text": [{"text": {"content": rt.get("plain_text", "")}} for rt in items]}

    if ptype == "select":
        sel = prop.get("select")
        return {"select": {"name": sel["name"]} if sel else None}

    if ptype == "status":
        st = prop.get("status")
        return {"status": {"name": st["name"]} if st else None}

    if ptype == "multi_select":
        items = prop.get("multi_select") or []
        return {"multi_select": [{"name": it["name"]} for it in items if it.get("name")]}

    if ptype == "date":
        d = prop.get("date")
        if not d:
            return {"date": None}
        out: dict = {"start": d["start"]}
        if d.get("end"):
            out["end"] = d["end"]
        if d.get("time_zone"):
            out["time_zone"] = d["time_zone"]
        return {"date": out}

    if ptype == "people":
        items = prop.get("people") or []
        return {"people": [{"id": p["id"]} for p in items if p.get("id")]}

    if ptype == "relation":
        items = prop.get("relation") or []
        return {"relation": [{"id": r["id"]} for r in items if r.get("id")]}

    if ptype == "checkbox":
        return {"checkbox": bool(prop.get("checkbox"))}

    if ptype == "number":
        return {"number": prop.get("number")}

    if ptype == "url":
        return {"url": prop.get("url")}

    if ptype == "email":
        return {"email": prop.get("email")}

    if ptype == "phone_number":
        return {"phone_number": prop.get("phone_number")}

    return None


def _build_archive_payload(source_page: dict) -> dict:
    """Build a write-shape properties payload for an archive copy."""
    out: dict = {}
    for name, value in (source_page.get("properties") or {}).items():
        if name in _SKIP_PROPERTY_NAMES_ON_ARCHIVE:
            continue
        converted = _copy_property_for_write(value)
        if converted is not None:
            out[name] = converted

    out["Source Page ID"] = {
        "rich_text": [{"text": {"content": source_page["id"]}}],
    }
    return out


def _load_archived_source_ids(
    client: NotionClientWrapper, archive_database_id: str,
) -> set[str]:
    """Return Source Page IDs already present in the archive DB (idempotency)."""
    response = client.query_database(database_id=archive_database_id)
    seen: set[str] = set()
    for page in response.get("results", []):
        prop = (page.get("properties") or {}).get("Source Page ID") or {}
        if prop.get("type") == "rich_text":
            for rt in prop.get("rich_text", []):
                txt = (rt.get("plain_text") or "").strip()
                if txt:
                    seen.add(txt)
    return seen


def _archive_done_tasks(
    client: NotionClientWrapper,
    database_id: str,
    archive_database_id: str | None,
    grace_days: int = 5,
    dry_run: bool = False,
) -> int:
    """Sweep Done tasks older than grace_days into the archive DB.

    For each match: copy properties to `archive_database_id`, then archive the
    original. When `archive_database_id` is empty/None, the sweep is a no-op.
    Re-runs are idempotent via the `Source Page ID` marker on each archive copy.
    """
    if not archive_database_id:
        logger.warning("TASK_ARCHIVE_DB_ID not configured — skipping archive sweep")
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=grace_days)
    db_filter = {
        "and": [
            {"property": "Status", "status": {"equals": "Done"}},
            {"timestamp": "last_edited_time", "last_edited_time": {"before": cutoff.isoformat()}},
        ]
    }
    response = client.query_database(database_id=database_id, filter=db_filter)
    pages = response.get("results", [])
    if not pages:
        return 0

    already_archived = _load_archived_source_ids(client, archive_database_id)
    archived = 0
    for page in pages:
        page_id = page["id"]
        title = ""
        for prop in (page.get("properties") or {}).values():
            if prop.get("type") == "title":
                title = "".join(p.get("plain_text", "") for p in prop.get("title", []))
                break

        if page_id in already_archived:
            logger.info("Already archived, skipping: %s", title[:80])
            continue

        if dry_run:
            logger.info("DRY RUN — would archive done task: %s", title[:80])
            continue

        try:
            payload = _build_archive_payload(page)
            client.create_page(parent_database_id=archive_database_id, properties=payload)
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


def _load_sync_context(
    config: SyncConfig, client: NotionClientWrapper,
) -> dict:
    """Load shared context needed for AI extraction.

    Fetches prompt templates from Notion, loads hierarchy, categories,
    users, and recent tasks. Raises if prompts fail to load (required).
    """
    # Prompt templates from Notion — required

    hierarchy_loader = HierarchyLoader(client, config.team_tracker_db_id)

    # Hierarchy is optional
    try:
        hierarchy = hierarchy_loader.load()
        logger.debug("Categories loaded: %s", [h["title"] for h in hierarchy])
    except Exception:
        logger.exception("Failed to load hierarchy — proceeding without it")
        hierarchy = []

    # Enrich the tracker hierarchy with one-sentence `Notes` from the
    # canonical Hierarchy DB (joined via the `Tracker Node` relation).
    # The classifier feeds these inline so the LLM can disambiguate
    # similarly-named nodes (e.g. two "Marketing" nodes under different
    # macro blocks). Best-effort — pipeline keeps running without notes.
    if hierarchy and config.hierarchy_db_id:
        try:
            notes_map = load_hierarchy_notes(client, config.hierarchy_db_id)
            if notes_map:
                attach_notes_to_hierarchy(hierarchy, notes_map)
        except Exception:
            logger.exception(
                "Failed to attach hierarchy notes — classifier will run "
                "without disambiguation hints",
            )

    # Org chart — loaded once and reused for:
    #   1. team-members list for the classifier (joined with workspace users
    #      by email so each entry carries a Notion user ID for assignee
    #      resolution),
    #   2. attendee enrichment in the extractor / classifier prompts,
    #   3. the `org_chart_text` block in the extractor system prompt.
    # The Org Chart is the canonical staff roster; raw workspace users
    # include stale invites and bot/integration accounts.
    #
    # We DO NOT filter by `Active=true` here — `Active` only gates whether
    # a member's own meetings are polled (handled in `discover_meeting_dbs`).
    # An inactive member can still appear as an attendee in someone else's
    # meeting, and they should still get their role annotation and be
    # resolvable as an assignee.
    org_chart_rows: list[dict] = []
    org_chart_text = ""
    if config.org_chart_db_id:
        try:
            org_chart_rows = load_org_chart_rows(client, config.org_chart_db_id)
            org_chart_text = format_org_chart(org_chart_rows)
            logger.debug("Loaded %d org chart members", len(org_chart_rows))
        except Exception:
            logger.exception("Failed to load org chart — proceeding without")

    # Workspace users — needed to look up Notion user IDs by email for org
    # chart members (the org chart row itself doesn't store a Notion ID).
    all_users: list[dict[str, str]] = []
    try:
        workspace_by_email: dict[str, dict] = {}
        for u in client.list_users():
            if u.get("type") != "person":
                continue
            email = (u.get("person") or {}).get("email", "").strip().lower()
            if email:
                workspace_by_email[email] = u
        if org_chart_rows:
            for row in org_chart_rows:
                wu = workspace_by_email.get(row["email"]) if row.get("email") else None
                if not wu:
                    continue
                all_users.append({
                    "id": wu["id"],
                    "name": row["name"],
                    "email": row["email"],
                })
            logger.debug(
                "Resolved %d org-chart members to workspace user IDs",
                len(all_users),
            )
        else:
            logger.warning(
                "No org chart rows — falling back to all workspace users",
            )
            all_users = [
                {
                    "id": u["id"],
                    "name": u.get("name", ""),
                    "email": (u.get("person") or {}).get("email", ""),
                }
                for u in workspace_by_email.values()
            ]
    except Exception:
        logger.exception("Failed to load team members — will use attendees only")
        all_users = []

    # Recent tasks for AI dedup context
    existing_tasks = _load_existing_tasks(client, config.team_tracker_db_id, hierarchy)

    # Deal context (optional — enables deal-aware extraction).
    # Pass the clean hierarchy IDs so any deal whose "Team Task Tracker"
    # relation accidentally points at an extracted task is sanitized before
    # that ID reaches the classifier prompt as a "valid" parent_task_id.
    deals: list[DealInfo] = []
    if config.deal_workplans_db_id:
        try:
            deal_loader = DealContextLoader(client, config.deal_workplans_db_id)
            valid_parent_ids = set(_flatten_hierarchy(hierarchy).keys())
            deals = deal_loader.load_deals(valid_parent_ids=valid_parent_ids)
            logger.debug("Loaded %d deals with context", len(deals))
        except Exception:
            logger.exception("Failed to load deal context — proceeding without")

    writer = TeamTaskTrackerWriter(client, config.team_tracker_db_id, config.dry_run)

    # Semantic dedup — compare new task titles against existing ones via embeddings
    semantic_dedup: SemanticDedup | None = None
    try:
        # Force OpenAI endpoint — the SDK would otherwise pick up OPENAI_BASE_URL
        # from the environment and send embeddings to the wrong provider.
        openai_client = OpenAI(api_key=config.openai_api_key, base_url=OPENAI_DEFAULT_BASE_URL)
        existing_title_list = list(writer._existing_titles)
        semantic_dedup = SemanticDedup(
            openai_client, existing_title_list, config.semantic_dedup_threshold,
        )
        logger.debug("Semantic dedup initialized with %d existing titles", len(existing_title_list))
    except Exception:
        logger.exception("Failed to initialize semantic dedup — proceeding without")

    # --- Transcript pipeline context (optional) ---

    # Terminology dictionary for transcript correction
    terminology = ""
    if config.terminology_db_id:
        try:
            terminology = load_terminology(client, config.terminology_db_id)
            logger.debug("Loaded terminology dictionary (%d chars)", len(terminology))
        except Exception:
            logger.exception("Failed to load terminology — transcript correction will be less accurate")

    # Classifier prompt (required for transcript path, loaded once per cycle)
    classifier_prompt = ""
    if config.classifier_prompt_page_id:
        try:
            classifier_prompt = _fetch_page_text(client, config.classifier_prompt_page_id)
            if classifier_prompt.strip():
                logger.debug("Loaded classifier prompt (%d chars)", len(classifier_prompt))
            else:
                logger.warning("Classifier prompt page is empty")
        except Exception:
            logger.exception("Failed to load classifier prompt — transcript classification will fall back to notes path")

    # Merged transcript-extraction prompt (transcript path). Required —
    # no in-code fallback. A missing or empty page raises so the pipeline
    # never runs with a silently-degraded prompt.
    merged_transcript_prompt = _fetch_page_text(
        client, config.merged_transcript_extraction_prompt_page_id,
    )
    if not merged_transcript_prompt.strip():
        raise RuntimeError(
            "Merged transcript extraction prompt page "
            f"{config.merged_transcript_extraction_prompt_page_id} is empty — "
            "populate it in Notion before running the pipeline.",
        )
    logger.debug(
        "Loaded merged transcript extraction prompt (%d chars)",
        len(merged_transcript_prompt),
    )

    # Literal-notes extraction prompt (only used when an Org Chart row has
    # `Auto-extract Tasks = false`). Loading is best-effort: if the page is
    # missing, the literal-notes path will skip with a warning rather than
    # silently fall through to the transcript pipeline.
    literal_notes_prompt = ""
    if config.literal_notes_extraction_prompt_page_id:
        try:
            literal_notes_prompt = _fetch_page_text(
                client, config.literal_notes_extraction_prompt_page_id,
            )
            if literal_notes_prompt.strip():
                logger.debug(
                    "Loaded literal-notes extraction prompt (%d chars)",
                    len(literal_notes_prompt),
                )
            else:
                logger.warning("Literal-notes extraction prompt page is empty")
        except Exception:
            logger.exception(
                "Failed to load literal-notes extraction prompt — literal-notes "
                "path will be unavailable",
            )

    return {
        "hierarchy": hierarchy,
        "all_users": all_users,
        "existing_tasks": existing_tasks,
        "deals": deals,
        "writer": writer,
        "semantic_dedup": semantic_dedup,
        "terminology": terminology,
        "org_chart_text": org_chart_text,
        "org_chart_rows": org_chart_rows,
        "classifier_prompt": classifier_prompt,
        "merged_transcript_prompt": merged_transcript_prompt,
        "literal_notes_prompt": literal_notes_prompt,
    }


def run_sync(config: SyncConfig, client: NotionClientWrapper) -> None:
    """Execute one full sync cycle across every discovered Meeting Notes DB.

    Includes inactive members so the fundraising → Affinity branch can run on
    their meetings too; inactive members' pages skip task extraction (handled
    in ``process_meeting``).
    """
    try:
        registry = load_registry(config, client, include_inactive=True)
    except Exception:
        logger.exception("Failed to load Meeting Notes DB registry — aborting sync")
        raise

    if not registry:
        logger.info("No Meeting Notes DBs to poll — nothing to do")
        # Still run the workspace-wide archive sweep below.
        ctx = None
    else:
        try:
            ctx = _load_sync_context(config, client)
        except Exception:
            logger.exception("Failed to load sync context — aborting sync")
            raise

        parent_titles_map = _flatten_hierarchy(ctx["hierarchy"])
        total_tasks = 0

        # When --db-id (or MEETING_NOTES_DB_ID) is set, this is a manual
        # single-DB run. The created_time buffer exists to wait for AI
        # recordings to finish populating, but for explicit manual runs we
        # want every Processed=false page picked up immediately — including
        # pages the user just toggled to re-run.
        if config.meeting_notes_db_id:
            buffer_hours = None
            logger.info(
                "manual single-DB run — skipping created_time buffer "
                "(meeting_notes_db_id=%s)",
                config.meeting_notes_db_id,
            )
        else:
            buffer_hours = config.buffer_hours

        for member_db in registry:
            source = SingleSource(client, member_db.db_id)
            label = member_db.owner_name or member_db.db_id[:8]

            # Defensive: inactive member DBs are now polled too and may have a
            # divergent schema (e.g. no `Processed` column). A bad DB must not
            # abort the whole sweep — log and move on.
            try:
                pages = source.get_unprocessed_pages(buffer_hours)
            except Exception:
                logger.exception(
                    "[%s] failed to list unprocessed pages — skipping this DB", label,
                )
                continue
            if not pages:
                logger.debug("[%s] no unprocessed meetings", label)
                continue

            # Per-DB fingerprint set — shared across all pages in this DB so
            # subsequent pages see duplicates added during this run.
            seen_meetings = _build_seen_fingerprints(source, member_db.db_id)

            for page in pages:
                page_id = page["id"]
                title = source.get_page_metadata(page).get("title", page_id[:16])
                try:
                    count = run_sync_for_page(
                        config,
                        client,
                        page_id,
                        ctx=ctx,
                        seen_meetings=seen_meetings,
                        page=page,
                        owner=member_db,
                    )
                    total_tasks += count
                except Exception:
                    logger.exception(
                        "[%s] Failed to process '%s' — will retry next cycle",
                        label, title,
                    )
                    continue

        logger.info(
            "Extraction complete: %d task(s) processed across %d DB(s)",
            total_tasks, len(registry),
        )


# ---------------------------------------------------------------------------
# Single-page entry points (used by webhook / Lambda handlers)
# ---------------------------------------------------------------------------

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


def run_sync_for_page(
    config: SyncConfig,
    client: NotionClientWrapper,
    page_id: str,
    *,
    force: bool = False,
    ctx: dict | None = None,
    seen_meetings: set[str] | None = None,
    page: dict | None = None,
    owner: MeetingDB | None = None,
) -> int:
    """Run extraction on a single page — transcript-first, notes fallback.

    The page's owning DB is derived from the page itself, so this works for
    any per-member Meeting Notes DB.

    GCal attendee lookup is automatic when `config.gcal_enabled` is true
    (i.e., a service-account credential source and default delegated user
    are configured). Works identically in CLI and Lambda.

    Args:
        force: If True, skip the "already processed" / "processing"
            checkbox guards *and* the per-DB title+date fingerprint dedup,
            so an operator can deliberately re-process a page that has
            already been seen. Used by the CLI ``--write`` path. The batch
            loop and Lambda/webhook callers leave this False, so their
            duplicate-suppression is unaffected.
        ctx: Pre-loaded sync context (hierarchy, terminology, org chart,
            classifier prompt, etc.). When omitted, the context is loaded
            here — Lambda + webhook single-page invocations use this. The
            batch CLI loop passes a single pre-loaded ctx for the whole
            cycle to avoid N round-trips to Notion.
        seen_meetings: Pre-loaded per-DB fingerprint set used for dedup.
            When the batch loop passes one, this call also adds the
            current page's fingerprint so subsequent pages in the same DB
            see the duplicate.
        page: Pre-fetched page dict. When omitted, fetched here. The batch
            loop already has every page in memory from its DB query.

    Returns:
        Number of tasks created for this page (0 when none, including
        the early-return / skip paths).
    """
    if page is None:
        page = client.get_page(page_id)
    props = page.get("properties", {})
    page_db_id = (page.get("parent") or {}).get("database_id", "")
    if not page_db_id:
        raise RuntimeError(
            f"Page {page_id} has no parent database — cannot run sync.",
        )

    source = SingleSource(client, page_db_id)
    # 16-char prefix avoids the short-id collisions that hid Vicente's vs
    # Reyes' 2026-04-27 Unicaja runs (both UUIDs share the first 8 chars).
    short_id = page_id[:16]

    # Resolve the owning member's name for log line stamping. When the
    # batch caller has already loaded the registry, ``owner`` is passed in
    # and the lookup is skipped. Best-effort: if the registry can't be
    # loaded, we still process the page.
    db_owner = "?"
    if owner is None:
        try:
            # include_inactive so inactive members resolve with the correct
            # active flag — the fundraising-only gate below depends on it.
            registry = load_registry(config, client, include_inactive=True)
            owner = find_owner_for_page(registry, page_db_id)
        except Exception:
            logger.debug("Could not resolve db_owner for page %s", short_id, exc_info=True)
    if owner is not None and owner.owner_name:
        db_owner = owner.owner_name

    if not force:
        processed = props.get("Processed", {}).get("checkbox", False)
        if processed:
            logger.debug("page=%s already processed — skipping", short_id)
            return 0

        processing = props.get("Processing", {}).get("checkbox", False)
        if processing:
            logger.debug("page=%s already being processed by another invocation — skipping", short_id)
            return 0

    # Claim the page (concurrency lock)
    if not config.dry_run and not force:
        source.mark_processing(page_id)

    start = time.monotonic()
    tasks: list[dict] = []
    path = "?"
    try:
        if ctx is None:
            ctx = _load_sync_context(config, client)
        metadata = source.get_page_metadata(page)
        title = metadata["title"]

        # Dedup check (per-DB; same title+date in another member's DB is allowed).
        if seen_meetings is None:
            seen_meetings = _build_seen_fingerprints(source, page_db_id)
        fingerprint = _meeting_fingerprint(page_db_id, title, metadata["date"])
        if not force and fingerprint in seen_meetings:
            logger.info("page=%s '%s' skipped (duplicate meeting)", short_id, title[:60])
            if not config.dry_run:
                source.mark_page_processed(page_id)
            return 0
        # Add now so subsequent pages in the same DB batch see this one.
        seen_meetings.add(fingerprint)

        with logfire.span("process_meeting", meeting_title=title, page_id=page_id):
            # Decision point: does this page have a transcript?
            blocks = client.get_block_children(page_id)
            mn_block = find_meeting_notes_block(blocks)
            transcript_block_id = (
                extract_transcript_block_id(mn_block) if mn_block else None
            )

            # Resolve attendees once, up front. Both extraction paths need
            # them, and the Fundraising → Affinity branch (which fires on
            # every tagged meeting, immediately below) needs them too —
            # including on the no-transcript / no-tasks paths that return
            # early before any extraction runs.
            attendees: list[dict[str, str]] = _resolve_attendees(
                client, config, mn_block, page, metadata,
                org_chart_rows=ctx.get("org_chart_rows"),
            )

            # Fundraising → Affinity LP Funnel. Runs for EVERY meeting whose
            # Macro Work Block matches an active rule, regardless of what the
            # extraction below decides — like template injection, a tagged
            # meeting is mirrored to its LP whether or not it yields tasks,
            # has notes, or opted into AI extraction. Placed before the
            # extraction branches so their early returns can't skip it.
            _mirror_meeting_to_affinity(
                config=config, client=client, page=page, metadata=metadata,
                attendees=attendees, page_id=page_id, short_id=short_id,
                db_owner=db_owner,
            )

            # Inactive members are polled ONLY for the fundraising branch
            # (which just ran). They're not on the Team Task Tracker, so skip
            # extraction, the tracker write, and the topic-mirror entirely —
            # mark processed and stop here. (owner=None → treat as active.)
            if owner is not None and not owner.active:
                logger.info(
                    "page=%s db_owner=%s inactive member — fundraising-only, "
                    "skipping extraction",
                    short_id, db_owner,
                )
                if not config.dry_run and not force:
                    source.mark_page_processed(page_id)
                return 0

            if not _should_auto_extract(config, owner):
                # --- Literal-notes path (LLM extraction, verbatim) ---
                path = "literal_notes"
                logger.info(
                    "page=%s '%s' starting (path=literal_notes, auto_extract_tasks=False)",
                    short_id, title[:60],
                )
                tasks = _process_via_literal_notes(
                    config=config, client=client, ctx=ctx,
                    page_id=page_id, blocks=blocks, metadata=metadata,
                    attendees=attendees,
                )
                if not tasks:
                    logger.warning(
                        "page=%s '%s' skipped: no action items found in notes "
                        "(auto_extract_tasks=False)",
                        short_id, title[:60],
                    )
                    if not config.dry_run and not force:
                        source.mark_page_processed(page_id)
                    return 0

                # Assignee fallback: default to meeting creator
                creator = metadata.get("created_by", {})
                creator_id = creator.get("id")
                for task in tasks:
                    if not task.get("assignee_id") and creator_id:
                        task["assignee_id"] = [creator_id]
                        logger.debug(
                            "Assignee fallback → meeting creator for: %s",
                            task.get("title", "?")[:60],
                        )
            elif transcript_block_id:
                # --- Transcript path ---
                path = "transcript"
                logger.info("page=%s '%s' starting (path=transcript)", short_id, title[:60])

                tasks = _process_via_transcript(
                    client, config, ctx, page_id, page, blocks,
                    mn_block, metadata, attendees,
                )

                # Assignee fallback: default to meeting creator
                creator = metadata.get("created_by", {})
                creator_id = creator.get("id")
                for task in tasks:
                    if not task.get("assignee_id") and creator_id:
                        task["assignee_id"] = [creator_id]
                        logger.debug(
                            "Assignee fallback → meeting creator for: %s",
                            task.get("title", "?")[:60],
                        )
            else:
                # auto_extract_tasks=True but no transcript_block_id. The
                # notes-path fallback has been removed; mark processed and
                # return so the page isn't re-tried.
                logger.info(
                    "page=%s '%s' skipped: auto_extract_tasks=True but no transcript available",
                    short_id, title[:60],
                )
                if not config.dry_run:
                    source.mark_page_processed(page_id)
                return 0

            # Semantic dedup: filter out tasks similar to existing ones
            tasks = _run_semantic_dedup(tasks, ctx.get("semantic_dedup"))

            if tasks:
                created = ctx["writer"].write_batch(tasks)
                created_ids = [c["id"] for c in created if c.get("id")]
                if created_ids:
                    ctx["writer"].link_tasks_to_meeting(page_id, created_ids)

            # Meeting Mirrors branch — clone tagged pages into topic DBs.
            # Runs on every page; the route registry decides which (if any)
            # mirrors apply. NO_MATCH / DISABLED outcomes are not logged so
            # untagged pages don't flood CloudWatch.
            if config.topic_mirror_enabled and not config.dry_run:
                from src.topic_mirror import mirror_to_topic_dbs
                from src.topic_mirror.outcome import MirrorStatus

                # Owner identity: prefer the meeting page's Notion creator
                # (a real user UUID, suitable for the People property);
                # fall back to empty when created_by is missing (rare —
                # would leave Owner unset on the mirror). db_owner is the
                # human display name used for the merge-path H3 heading.
                creator = metadata.get("created_by") or {}
                owner_user_id = creator.get("id") or ""

                mirror_outcome = mirror_to_topic_dbs(
                    config=config,
                    client=client,
                    source_page=page,
                    metadata=metadata,
                    owner_user_id=owner_user_id,
                    owner_name=db_owner,
                )
                if mirror_outcome.status in (
                    MirrorStatus.FAILED, MirrorStatus.PARTIAL_FAILURE,
                ):
                    logger.error(
                        "topic mirror outcome: page=%s owner=%s status=%s detail=%s",
                        short_id, db_owner,
                        mirror_outcome.status.value, mirror_outcome.detail,
                    )
                elif mirror_outcome.status == MirrorStatus.POSTED:
                    logger.info(
                        "topic mirror outcome: page=%s owner=%s status=%s detail=%s",
                        short_id, db_owner,
                        mirror_outcome.status.value, mirror_outcome.detail,
                    )
                else:
                    # NO_MATCH / DISABLED — silent at INFO to avoid CloudWatch
                    # noise on untagged pages, but surfaced under --verbose
                    # so local diagnostic runs can confirm the branch ran.
                    logger.debug(
                        "topic mirror outcome: page=%s owner=%s status=%s detail=%s",
                        short_id, db_owner,
                        mirror_outcome.status.value, mirror_outcome.detail,
                    )

            if not config.dry_run and not force:
                source.mark_page_processed(page_id)

            elapsed = time.monotonic() - start
            logger.info(
                "page=%s db_owner=%s done in %.1fs (path=%s, tasks=%d)",
                short_id, db_owner, elapsed, path, len(tasks),
            )
            return len(tasks)
    except Exception:
        # Release the lock so the page retries next cycle
        if not config.dry_run and not force:
            try:
                source.clear_processing(page_id)
            except Exception:
                logger.exception("Failed to clear processing lock on %s", page_id)
        raise

    # Defensive: every code path inside the try returned a value; this is
    # only reached if the meeting_notes block was missing in a way that
    # bypassed every branch. Treat as zero tasks.
    return 0
