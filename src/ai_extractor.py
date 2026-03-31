"""AI-driven task extraction using OpenAI function calling."""
from __future__ import annotations

import json
import logging

from openai import OpenAI

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_TEMPLATE = """You are a task extraction assistant for a PE/VC fund team.
Your job is to extract action items from meeting notes and create tasks.

## Rules (Playbook)
{playbook}

## Team Task Tracker Schema
- Task (title): string
- Status: "Not Started" | "In Progress" | "Done"
- Assignee: Notion user ID (from team members list — prefer attendees when ambiguous)
- Due Date: ISO date string (YYYY-MM-DD) or null
- Priority: "High" | "Medium" | "Low"
- Category: {categories}
- Parent item: page ID of parent task from hierarchy (optional)

## Existing Hierarchy

Below is the current Team Task Tracker hierarchy as JSON. Each node has an "id", "title", and "children" array.

When creating tasks, you MUST set "parent_task_id" to the "id" of the most specific matching node:
- If the task relates to a specific entity (company, project, fund), find that entity in the hierarchy and use its "id"
- If no specific entity matches, find the best-matching category node and use its "id"
- Only set "parent_task_id" to null if absolutely nothing in the hierarchy is relevant

IMPORTANT: Always try to place tasks in the hierarchy. Most tasks should have a parent_task_id.

{hierarchy}

## Team Members (available assignees)
{team_members}

## Attendees in this meeting
{attendees}"""

USER_PROMPT_TEMPLATE = """Extract action items from this meeting:

Meeting: {title}
Date: {date}
Type: {meeting_type}

{content}"""


def _build_tool_definition(categories: list[str]) -> dict:
    """Build the create_task tool definition with dynamic category enum."""
    return {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "Create a task in the Team Task Tracker",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Task title — clear, actionable",
                    },
                    "assignee_id": {
                        "type": "string",
                        "description": "Notion user ID of assignee",
                    },
                    "due_date": {
                        "type": ["string", "null"],
                        "description": "ISO date (YYYY-MM-DD) or null",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["High", "Medium", "Low"],
                    },
                    "category": {
                        "type": "string",
                        "enum": categories,
                    },
                    "parent_task_id": {
                        "type": ["string", "null"],
                        "description": "Page ID of parent task from hierarchy, or null",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["Not Started", "In Progress", "Done"],
                        "default": "Not Started",
                    },
                },
                "required": ["title", "assignee_id", "priority", "category"],
            },
        },
    }


class AIExtractor:
    """Extracts tasks from meeting content using OpenAI function calling."""

    # TEMPORARY: base_url param allows using Gemini's OpenAI-compatible endpoint.
    # Remove once we switch back to OpenAI (target model: gpt-5-mini).
    def __init__(self, api_key: str, model: str = "gpt-4.1", base_url: str | None = None) -> None:
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    def extract(
        self,
        meeting_title: str,
        meeting_date: str,
        meeting_type: str,
        meeting_content: str,
        attendees: list[dict],
        team_members: list[dict],
        playbook: str,
        hierarchy: list[dict],
        categories: list[str],
    ) -> list[dict]:
        """Call OpenAI and return a list of task dicts extracted from the meeting."""
        attendees_text = "\n".join(
            f"- {a['name']} (ID: {a['id']})" for a in attendees
        ) or "No attendees listed"

        team_members_text = "\n".join(
            f"- {m['name']} (ID: {m['id']})" for m in team_members
        ) or "No team members available — use attendees only"

        system_msg = SYSTEM_PROMPT_TEMPLATE.format(
            playbook=playbook,
            hierarchy=json.dumps(hierarchy, indent=2),
            attendees=attendees_text,
            team_members=team_members_text,
            categories=" | ".join(f'"{c}"' for c in categories),
        )
        user_msg = USER_PROMPT_TEMPLATE.format(
            title=meeting_title,
            date=meeting_date,
            meeting_type=meeting_type or "Not specified",
            content=meeting_content,
        )

        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            tools=[_build_tool_definition(categories)],
            tool_choice="auto",
        )

        tasks: list[dict] = []
        tool_calls = response.choices[0].message.tool_calls
        if tool_calls:
            for tc in tool_calls:
                if tc.function.name == "create_task":
                    try:
                        task = json.loads(tc.function.arguments)
                        tasks.append(task)
                    except json.JSONDecodeError:
                        logger.warning(
                            "Failed to parse tool call: %s",
                            tc.function.arguments[:200],
                        )

        logger.info(
            "AI extracted %d tasks from '%s'", len(tasks), meeting_title[:60]
        )
        for task in tasks:
            logger.info(
                "  Task: '%s' | parent: %s | category: %s",
                task.get("title", "?")[:60],
                task.get("parent_task_id") or "NONE",
                task.get("category", "?"),
            )
        return tasks
