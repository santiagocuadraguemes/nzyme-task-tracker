"""LLM-based extraction of action items from a meeting page's notes.

Used when a Meeting Notes DB owner has `Auto-extract Tasks = false` on the
Org Chart. The author writes the bullets themselves; this module hands the
notes content to a constrained LLM prompt that returns one task per bullet
with the title kept as the author typed it. Unlike the transcript path,
this does NOT correct the transcript or paraphrase titles — the model is
instructed to keep the author's wording.

Pipeline shape:
1. Locate the human-written notes container on the page (preferring the
   `meeting_notes.notes_block_id` where the template injector renders
   `## Action Items`). Fall back to the page's top-level blocks for
   pre-template meetings.
2. Render that container as plain markdown text (`blocks_to_text`).
3. Call the configured LLM (light, OpenAI by default) with a Notion-hosted
   system prompt + a code-built user message containing meeting context
   and the notes markdown. The model returns:
     {"tasks": [{"title", "assignee", "internal_assignees",
                 "external_assignees", "due_date", "priority",
                 "supporting_quote"}]}
4. The downstream classifier (same one used by the transcript path) adds
   category, parent, deal — and resolves `assignee_id` from the
   internal/external split. `due_date` and `priority` are pass-through
   to the writer.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from openai import OpenAI

from src.notion_client_wrapper import NotionClientWrapper
from src.transcript_pipeline.fetch_transcript import find_meeting_notes_block
from src.utils.blocks_to_text import blocks_to_text
from src.utils.llm_logging import log_usage

logger = logging.getLogger(__name__)


def _format_team_members(users: list[dict[str, Any]]) -> str:
    """Mirror the classifier's team-members format so prompts read identically."""
    if not users:
        return "No team members available"
    lines: list[str] = []
    for m in users:
        aliases: list[str] = []
        if m.get("email"):
            aliases.append(m["email"].split("@")[0])
        name_parts = (m.get("name") or "").split()
        if name_parts and name_parts[0].lower() != (m.get("name") or "").lower():
            aliases.append(name_parts[0])
        alias_suffix = f" (aliases: {', '.join(aliases)})" if aliases else ""
        lines.append(f"- {m.get('name', '')} (ID: {m.get('id', '')}){alias_suffix}")
    return "\n".join(lines)


def _substitute_placeholders(template: str, **kwargs: str) -> str:
    """Replace ``{{KEY}}`` markers in a template string with values."""
    for key, value in kwargs.items():
        template = template.replace(f"{{{{{key}}}}}", value)
    return template


def fetch_notes_markdown(
    client: NotionClientWrapper,
    page_blocks: list[dict[str, Any]],
) -> str:
    """Return the human-written notes content of a meeting page as markdown.

    Prefers the `meeting_notes.notes_block_id` container (where the template
    injector renders `## Action Items` + `## Notes`). Falls back to the
    page's top-level blocks for pre-template meetings.
    """
    mn_block = find_meeting_notes_block(page_blocks)
    if mn_block is not None:
        notes_block_id = (
            (mn_block.get("meeting_notes") or {})
            .get("children", {})
            .get("notes_block_id")
        )
        if notes_block_id:
            try:
                notes_children = client.get_block_children(notes_block_id)
            except Exception:
                logger.exception(
                    "literal-notes: failed to fetch notes_block children",
                )
                notes_children = []
            if notes_children:
                return blocks_to_text(notes_children, client)

    # Fallback: top-level page blocks (pre-template pages).
    return blocks_to_text(page_blocks, client)


def _build_user_message(
    notes_markdown: str,
    metadata: dict[str, Any],
    attendees: list[dict[str, Any]] | None,
) -> str:
    """Build the user message that pairs with the Notion-hosted system prompt."""
    sections: list[str] = []

    creator = metadata.get("created_by") or {}
    creator_line = ""
    if creator.get("id"):
        creator_line = f"\nCreator: {creator.get('name', '')} (ID: {creator['id']})"

    sections.append(
        "=== MEETING CONTEXT ==="
        f"\nTitle: {metadata.get('title', '')}"
        f"\nDate: {metadata.get('date', '')}"
        f"{creator_line}",
    )

    if attendees:
        attendee_lines = [
            f"- {a.get('name') or a.get('id') or '?'}"
            + (f" (ID: {a['id']})" if a.get("id") else "")
            for a in attendees
        ]
    else:
        attendee_lines = ["No attendees listed"]
    sections.append("=== ATTENDEES ===\n" + "\n".join(attendee_lines))

    sections.append("=== NOTES ===\n" + (notes_markdown or "(no notes)"))

    return "\n\n".join(sections)


def extract(
    *,
    client: NotionClientWrapper,
    page_blocks: list[dict[str, Any]],
    metadata: dict[str, Any],
    attendees: list[dict[str, Any]] | None,
    all_users: list[dict[str, Any]],
    system_prompt_template: str,
    openai_client: OpenAI,
    model: str,
) -> list[dict[str, Any]]:
    """Run the literal-notes LLM extraction.

    Returns a list of task dicts shaped for the classifier:
        {
          "title": str,                       # cleaned, self-contained
          "assignee": str,                    # human-readable display string
          "internal_assignees": list[str],    # Kibo team-member names
          "external_assignees": list[str],    # outsider names
          "due_date": str | None,             # ISO YYYY-MM-DD if model
                                              # resolved an inline deadline
          "priority": str | None,             # "High" / "Medium" / "Low"
                                              # only when bullet has an
                                              # urgency signal
          "supporting_quote": str,            # original bullet text
        }
    Returns [] when no notes content was found.

    Raises on hard LLM/API errors so the caller can surface them; the legacy
    "no silent failures" rule applies during dev.
    """
    notes_markdown = fetch_notes_markdown(client, page_blocks)
    if not notes_markdown.strip():
        logger.info("literal-notes: page has no notes content — nothing to extract")
        return []

    system_prompt = _substitute_placeholders(
        system_prompt_template,
        TEAM_MEMBERS=_format_team_members(all_users),
    )
    user_message = _build_user_message(notes_markdown, metadata, attendees)

    logger.debug(
        "literal-notes: calling %s with %d chars of notes",
        model, len(notes_markdown),
    )

    response = openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content or "{}"

    log_usage(response, model, stage="literal-notes extraction", logger=logger)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.exception("literal-notes: model returned invalid JSON: %s", raw[:500])
        return []

    raw_tasks = data.get("tasks") or []
    if not isinstance(raw_tasks, list):
        logger.warning(
            "literal-notes: 'tasks' field is not a list (got %s) — dropping",
            type(raw_tasks).__name__,
        )
        return []

    tasks: list[dict[str, Any]] = []
    for entry in raw_tasks:
        if not isinstance(entry, dict):
            continue
        title = (entry.get("title") or "").strip()
        if not title:
            continue

        due_date = entry.get("due_date")
        if not isinstance(due_date, str) or not due_date.strip():
            due_date = None

        priority = entry.get("priority")
        if priority not in ("High", "Medium", "Low"):
            priority = None

        tasks.append({
            "title": title,
            "assignee": entry.get("assignee") or "",
            "internal_assignees": list(entry.get("internal_assignees") or []),
            "external_assignees": list(entry.get("external_assignees") or []),
            "due_date": due_date,
            "priority": priority,
            "supporting_quote": entry.get("supporting_quote") or title,
        })

    logger.info("literal-notes: model returned %d task(s)", len(tasks))
    return tasks
