"""Build a hierarchy snapshot of the Team Task Tracker."""
from __future__ import annotations

import logging
from typing import Any

from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)


class HierarchyLoader:
    """Queries the Team Task Tracker and builds a parent-child tree.

    Caches the result for the lifetime of the instance (one sync cycle).
    """

    def __init__(self, client: NotionClientWrapper, database_id: str) -> None:
        self._client = client
        self._db_id = database_id
        self._cache: list[dict[str, Any]] | None = None

    def load(self) -> list[dict[str, Any]]:
        """Return the hierarchy as a list of root nodes with nested children."""
        if self._cache is not None:
            return self._cache

        response = self._client.query_database(
            database_id=self._db_id,
            filter={"property": "Status", "status": {"does_not_equal": "Done"}},
        )
        pages = response.get("results", [])

        # Index all pages
        page_map: dict[str, dict[str, Any]] = {}
        for page in pages:
            pid = page["id"]
            parent_rel = (
                page.get("properties", {})
                .get("Parent item", {})
                .get("relation", [])
            )
            parent_id = parent_rel[0]["id"] if parent_rel else None
            page_map[pid] = {
                "id": pid,
                "title": self._get_title(page),
                "category": self._get_category(page),
                "parent_id": parent_id,
                "children": [],
            }

        # Build tree
        roots: list[dict[str, Any]] = []
        for node in page_map.values():
            parent_id = node.pop("parent_id")
            if parent_id and parent_id in page_map:
                page_map[parent_id]["children"].append(node)
            else:
                roots.append(node)

        self._cache = roots
        logger.info(
            "Loaded hierarchy: %d roots, %d total pages", len(roots), len(pages)
        )
        return self._cache

    @staticmethod
    def _get_title(page: dict[str, Any]) -> str:
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title":
                return "".join(
                    p.get("plain_text", "") for p in prop.get("title", [])
                )
        return ""

    @staticmethod
    def _get_category(page: dict[str, Any]) -> str:
        cat = page.get("properties", {}).get("Category", {}).get("select")
        return cat.get("name", "") if cat else ""
