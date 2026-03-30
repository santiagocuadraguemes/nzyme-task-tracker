"""Team Task Tracker writer — creates task pages from AI-extracted dicts."""
from __future__ import annotations

import logging
from typing import Any

from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)


class TeamTaskTrackerWriter:
    """Writes task dicts to the Team Task Tracker Notion database."""

    def __init__(
        self,
        client: NotionClientWrapper,
        database_id: str,
        dry_run: bool = False,
    ) -> None:
        self._client = client
        self._db_id = database_id
        self._dry_run = dry_run

    def create_task(self, task: dict[str, Any]) -> dict[str, Any] | None:
        """Create a single page in the Team Task Tracker.

        Parameters
        ----------
        task:
            Dict with keys: title, assignee_id, due_date, priority,
            category, parent_task_id, status. Only title is required.
        """
        properties: dict[str, Any] = {
            "Task": {
                "title": [{"text": {"content": task["title"][:2000]}}]
            },
            "Status": {
                "status": {"name": task.get("status", "Not Started")}
            },
        }

        if task.get("assignee_id"):
            properties["Assignee (edit access)"] = {
                "people": [{"id": task["assignee_id"]}]
            }

        if task.get("due_date"):
            properties["Due Date"] = {
                "date": {"start": task["due_date"]}
            }

        if task.get("priority"):
            properties["Priority"] = {"select": {"name": task["priority"]}}

        if task.get("category"):
            properties["Category"] = {"select": {"name": task["category"]}}

        if task.get("parent_task_id"):
            properties["Parent item"] = {
                "relation": [{"id": task["parent_task_id"]}]
            }

        if self._dry_run:
            logger.info("DRY RUN — would create: %s", task["title"][:80])
            return None

        result = self._client.create_page(self._db_id, properties)
        logger.info("Created task: %s (id=%s)", task["title"][:80], result.get("id"))
        return result

    def write_batch(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Write multiple tasks. Failures on individual tasks don't abort the batch."""
        created: list[dict[str, Any]] = []
        for task in tasks:
            try:
                result = self.create_task(task)
                if result:
                    created.append(result)
            except Exception:
                logger.exception("Failed to create: %s", task.get("title", "?")[:80])
        return created
