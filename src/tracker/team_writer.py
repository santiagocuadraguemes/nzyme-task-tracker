"""Team Task Tracker writer — creates task pages from AI-extracted dicts."""
from __future__ import annotations

import logging
from typing import Any

from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)


class TeamTaskTrackerWriter:
    """Writes task dicts to the Team Task Tracker Notion database.

    On initialization, loads all existing task titles for deduplication.
    """

    def __init__(
        self,
        client: NotionClientWrapper,
        database_id: str,
        dry_run: bool = False,
    ) -> None:
        self._client = client
        self._db_id = database_id
        self._dry_run = dry_run
        self._existing_titles: set[str] = set()
        self._load_existing_titles()

    @staticmethod
    def _normalize_title(title: str) -> str:
        return title.strip().lower()

    @staticmethod
    def _get_title(page: dict[str, Any]) -> str:
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title":
                return "".join(
                    p.get("plain_text", "") for p in prop.get("title", [])
                )
        return ""

    def _load_existing_titles(self) -> None:
        """Query all tasks in the tracker and cache their titles for dedup."""
        try:
            response = self._client.query_database(database_id=self._db_id)
            for page in response.get("results", []):
                title = self._get_title(page)
                if title:
                    self._existing_titles.add(self._normalize_title(title))
            logger.debug("Loaded %d existing task titles for dedup", len(self._existing_titles))
        except Exception:
            logger.exception("Failed to load existing titles for dedup — proceeding without dedup")

    def create_task(self, task: dict[str, Any]) -> dict[str, Any] | None:
        """Create a single page in the Team Task Tracker.

        Skips if a task with the same title (case-insensitive) already exists.
        """
        title = self._build_display_title(task)
        normalized = self._normalize_title(title)
        if normalized in self._existing_titles:
            logger.debug("DEDUP — skipping duplicate: %s", title[:80])
            return None

        properties: dict[str, Any] = {
            "Task": {
                "title": [{"text": {"content": title[:2000]}}]
            },
            "Status": {
                "status": {"name": task.get("status", "Not Started")}
            },
        }

        if task.get("assignee_id"):
            ids = task["assignee_id"] if isinstance(task["assignee_id"], list) else [task["assignee_id"]]
            people = [{"id": uid} for uid in ids if uid]
            if people:
                properties["Assignee (edit access)"] = {"people": people}

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

        if task.get("deal_page_id"):
            properties["Deal Relation (only for deal tasks)"] = {
                "relation": [{"id": task["deal_page_id"]}]
            }

        if task.get("meeting_page_id"):
            properties["Meeting - Relation"] = {
                "relation": [{"id": task["meeting_page_id"]}]
            }

        if self._dry_run:
            logger.info("DRY RUN — would create: %s", title[:80])
            self._existing_titles.add(normalized)
            return None

        result = self._client.create_page(self._db_id, properties)
        self._existing_titles.add(normalized)
        logger.debug("Created task: %s (id=%s)", title[:80], result.get("id"))
        return result

    @staticmethod
    def _build_display_title(task: dict[str, Any]) -> str:
        """Prepend an `[Ext: Name1, Name2]` marker when the task is assigned to
        external people (portfolio staff, advisers, etc.).

        The marker surfaces who the task is really for while the Assignee
        (edit access) field stays routable to an internal owner.
        """
        base_title = task.get("title", "")
        externals = task.get("external_assignees") or []
        externals = [name for name in externals if isinstance(name, str) and name.strip()]
        if not externals:
            return base_title
        prefix = f"[Ext: {', '.join(externals)}] "
        # Respect Notion's 2000-char title cap: prefix stays, base truncates.
        max_base = max(0, 2000 - len(prefix))
        return prefix + base_title[:max_base]

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
