"""AI-driven task extraction using OpenAI function calling."""
from __future__ import annotations

import json
import logging

from openai import OpenAI

logger = logging.getLogger(__name__)


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
                        "description": (
                            "ISO date (YYYY-MM-DD) or null. Resolve relative dates "
                            "using the meeting date: 'manana/tomorrow' = meeting_date + 1, "
                            "'miercoles/Wednesday' = next occurrence, "
                            "'viernes/Friday/end of week' = next Friday, "
                            "'fin de mes/end of month' = last day of month, "
                            "'esta semana/this week' = Friday of meeting week. "
                            "Set null only if no deadline is mentioned at all."
                        ),
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
                    "deal_page_id": {
                        "type": ["string", "null"],
                        "description": (
                            "The Deal page ID from the deal context (labeled 'deal_page_id'). "
                            "Links the task to the Deal Workplans database. "
                            "Do NOT use the Tracker page ID here — that goes in parent_task_id. "
                            "Set null for non-deal tasks."
                        ),
                    },
                    "status": {
                        "type": "string",
                        "enum": ["Not Started", "In Progress", "Done"],
                        "default": "Not Started",
                    },
                },
                "required": ["title", "priority", "category"],
            },
        },
    }


class AIExtractor:
    """Extracts tasks from meeting content using OpenAI function calling."""

    # TEMPORARY: base_url param allows using Gemini's OpenAI-compatible endpoint.
    def __init__(self, api_key: str, model: str = "gpt-5-mini", base_url: str | None = None) -> None:
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    def extract(
        self,
        system_prompt: str,
        user_prompt: str,
        categories: list[str],
    ) -> list[dict]:
        """Call OpenAI with pre-built prompts and return extracted task dicts.

        The prompts are built by the pipeline from Notion-hosted templates
        with placeholders substituted at runtime.
        """
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
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

        logger.info("AI extracted %d tasks", len(tasks))
        for task in tasks:
            logger.info(
                "  Task: '%s' | parent: %s | category: %s",
                task.get("title", "?")[:60],
                task.get("parent_task_id") or "NONE",
                task.get("category", "?"),
            )
        return tasks
