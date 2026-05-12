"""Classify extracted tasks for placement in the Team Task Tracker.

Loads a prompt template from Notion, fills {{PLACEHOLDERS}} with live
context (categories, hierarchy, team members, deals), and sends a
dedicated LLM call to resolve category, parent_task_id, and assignee_id.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import OpenAI

from src.utils.llm_logging import log_usage

logger = logging.getLogger(__name__)


def _substitute_placeholders(template: str, **kwargs: str) -> str:
    """Replace ``{{KEY}}`` markers in a template string with values."""
    for key, value in kwargs.items():
        template = template.replace(f"{{{{{key}}}}}", value)
    return template


def _format_team_members(users: list[dict[str, str]]) -> str:
    """Format team members list with aliases for AI prompt injection."""
    if not users:
        return "No team members available"
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


def _collect_hierarchy_ids(nodes: list[dict[str, Any]]) -> set[str]:
    """Recursively collect all page IDs from a hierarchy tree."""
    ids: set[str] = set()
    for node in nodes:
        ids.add(node["id"])
        ids.update(_collect_hierarchy_ids(node.get("children", [])))
    return ids


class TaskClassifier:
    """Classifies extracted tasks against the tracker's structure via LLM."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str | None = None,
    ) -> None:
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    def classify(
        self,
        tasks: list[dict[str, Any]],
        prompt_template: str,
        categories: list[str],
        hierarchy: list[dict[str, Any]],
        team_members: list[dict[str, str]],
        deal_context: str = "",
        meeting_title: str = "",
        meeting_date: str = "",
        enriched_attendees: str = "",
        notes_text: str = "",
    ) -> list[dict[str, Any]]:
        """Classify tasks and merge category/parent/assignee into each dict.

        Meeting context (title, date, attendees, notes) is injected into the
        user message so the classifier can infer deal/entity parents and avoid
        mapping external-party names to internal Notion users.

        Returns:
            The same task list with ``category``, ``parent_task_id``,
            ``assignee_id``, ``deal_page_id``, and ``external_assignees``
            (pass-through from the extractor) merged in.
        """
        if not tasks:
            return tasks

        # Build system prompt from Notion template + live context
        categories_text = " | ".join(f'"{c}"' for c in categories)
        hierarchy_text = json.dumps(hierarchy, indent=2)
        team_members_text = _format_team_members(team_members)

        system_prompt = _substitute_placeholders(
            prompt_template,
            CATEGORIES=categories_text,
            HIERARCHY=hierarchy_text,
            TEAM_MEMBERS=team_members_text,
            DEAL_CONTEXT=deal_context or "No active deals.",
        )

        # Build user message: meeting context block + tasks JSON
        task_input = [
            {
                "index": i,
                "title": t.get("title", ""),
                "assignee": t.get("assignee", ""),
                "internal_assignees": t.get("internal_assignees", []),
                "external_assignees": t.get("external_assignees", []),
                "priority": t.get("priority", "Medium"),
                "due_date": t.get("due_date"),
            }
            for i, t in enumerate(tasks)
        ]

        user_sections: list[str] = []
        if meeting_title or meeting_date:
            meeting_block = "=== MEETING CONTEXT ==="
            if meeting_title:
                meeting_block += f"\nTitle: {meeting_title}"
            if meeting_date:
                meeting_block += f"\nDate: {meeting_date}"
            user_sections.append(meeting_block)
        if enriched_attendees:
            user_sections.append(
                f"=== ATTENDEES (with org-chart role annotations where available) ===\n"
                f"{enriched_attendees}"
            )
        if notes_text:
            user_sections.append(
                f"=== HUMAN NOTES (note-taker's ground truth — highest priority) ===\n"
                f"{notes_text}"
            )
        user_sections.append(
            "=== TASKS TO CLASSIFY ===\n"
            + json.dumps(task_input, indent=2, ensure_ascii=False)
        )
        user_prompt = "\n\n".join(user_sections)

        logger.debug(
            "Classifying %d tasks with %s",
            len(tasks),
            self._model,
        )

        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content or "{}"

        log_usage(response, self._model, stage="Classification", logger=logger)

        data = json.loads(raw)
        classified = data.get("tasks", [])

        # Validate and merge classifications back into original tasks
        valid_categories = set(categories)
        valid_parent_ids = _collect_hierarchy_ids(hierarchy)
        valid_member_ids = {m["id"] for m in team_members}

        for entry in classified:
            idx = entry.get("index")
            if idx is None or not isinstance(idx, int) or idx < 0 or idx >= len(tasks):
                logger.warning("Invalid task index %s in classifier response", idx)
                continue

            task = tasks[idx]

            # Category validation
            category = entry.get("category", "Other")
            if category not in valid_categories:
                logger.warning(
                    "Invalid category '%s' for task '%s', defaulting to 'Other'",
                    category,
                    task.get("title", "?")[:60],
                )
                category = "Other"
            task["category"] = category

            # Parent task validation
            parent_id = entry.get("parent_task_id")
            if parent_id and parent_id not in valid_parent_ids:
                logger.warning(
                    "Invalid parent_task_id '%s' for task '%s', setting to null",
                    parent_id,
                    task.get("title", "?")[:60],
                )
                parent_id = None
            task["parent_task_id"] = parent_id

            # Assignee validation — normalize to list
            raw_assignee = entry.get("assignee_id")
            if isinstance(raw_assignee, str):
                assignee_ids = [raw_assignee] if raw_assignee else []
            elif isinstance(raw_assignee, list):
                assignee_ids = raw_assignee
            else:
                assignee_ids = []
            # Filter out invalid IDs
            valid_ids = []
            for aid in assignee_ids:
                if aid and aid in valid_member_ids:
                    valid_ids.append(aid)
                elif aid:
                    logger.warning(
                        "Invalid assignee_id '%s' for task '%s', dropping",
                        aid,
                        task.get("title", "?")[:60],
                    )

            # External assignee safety net: if the extractor flagged external
            # assignees and there are no internal ones for this task, drop any
            # IDs the classifier might have guessed via first-name aliasing.
            # This is the defence against e.g. "Miguel Serrano (external)"
            # being mapped to the internal Miguel.
            ext = task.get("external_assignees") or []
            internal = task.get("internal_assignees") or []
            if ext and not internal and valid_ids:
                logger.info(
                    "Dropping %d assignee_id(s) for task '%s' — only external "
                    "assignees (%s) were named; creator fallback will apply",
                    len(valid_ids),
                    task.get("title", "?")[:60],
                    ", ".join(ext),
                )
                valid_ids = []

            task["assignee_id"] = valid_ids

            # Deal page ID (no validation — trust the LLM here, Notion will
            # reject invalid IDs at write time)
            task["deal_page_id"] = entry.get("deal_page_id")

        # Ensure every task has at least a category
        for task in tasks:
            if "category" not in task:
                logger.warning(
                    "Task '%s' was not classified — defaulting to 'Other'",
                    task.get("title", "?")[:60],
                )
                task["category"] = "Other"

        logger.debug("Classified %d tasks", len(tasks))
        return tasks
