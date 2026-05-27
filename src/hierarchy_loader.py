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
            filter={
                "and": [
                    {"property": "Status", "status": {"does_not_equal": "Done"}},
                    {"property": "Priority", "select": {"equals": "[DETAILS INSIDE]"}},
                ],
            },
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
                "has_children": False,
            }

        # Build tree
        roots: list[dict[str, Any]] = []
        for node in page_map.values():
            parent_id = node.pop("parent_id")
            if parent_id and parent_id in page_map:
                page_map[parent_id]["children"].append(node)
                page_map[parent_id]["has_children"] = True
            else:
                roots.append(node)

        # Prune: keep top 4 levels (categories + sub-categories + entities + deals).
        # At max depth, only keep nodes that have children (organizational
        # nodes like deals), filtering out individual leaf tasks.
        pruned = self._prune(roots, max_depth=4)

        self._cache = pruned
        logger.info(
            "Loaded hierarchy: %d categories, %d total pages (pruned from %d)",
            len(pruned), sum(1 + len(r["children"]) for r in pruned), len(pages),
        )
        return self._cache

    @staticmethod
    def _prune(nodes: list[dict[str, Any]], max_depth: int, depth: int = 0) -> list[dict[str, Any]]:
        """Keep only nodes down to max_depth, removing empty titles.

        At max_depth, only keeps nodes that originally had children
        (organizational nodes like deals), filtering out leaf tasks.
        """
        result: list[dict[str, Any]] = []
        for node in nodes:
            if not node.get("title"):
                continue
            if depth < max_depth:
                node["children"] = HierarchyLoader._prune(
                    node["children"], max_depth, depth + 1
                )
            else:
                # At max depth: only keep organizational nodes (those with children)
                if not node.get("has_children"):
                    continue
                node["children"] = []
            node.pop("has_children", None)
            result.append(node)
        return result

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


def load_hierarchy_notes(
    client: NotionClientWrapper, hierarchy_db_id: str,
) -> dict[str, str]:
    """Build ``{tracker_node_page_id: notes_text}`` from the Hierarchy DB.

    Queries active rows in the Meeting Notes & Task Tracker Hierarchy DB,
    pulls each row's ``Notes`` rich_text and the ``Tracker Node`` relation,
    and indexes the notes under each relation target id. Inactive rows are
    skipped — their tracker counterparts are soft-archived and shouldn't
    carry live disambiguation notes.

    The returned map is consumed by ``attach_notes_to_hierarchy`` to enrich
    the slim hierarchy tree the classifier sees, so the LLM can
    disambiguate similarly-named nodes (e.g. ``Marketing`` under Ops vs
    ``Marketing & Data Room Materials`` under IR&F).
    """
    try:
        response = client.query_database(
            database_id=hierarchy_db_id,
            filter={"property": "Active", "checkbox": {"equals": True}},
        )
    except Exception:
        logger.exception("Failed to load hierarchy notes from %s", hierarchy_db_id)
        return {}

    notes_map: dict[str, str] = {}
    for row in response.get("results", []):
        props = row.get("properties", {})
        notes_rt = props.get("Notes", {}).get("rich_text", [])
        notes = "".join(p.get("plain_text", "") for p in notes_rt).strip()
        if not notes:
            continue
        tracker_rel = props.get("Tracker Node", {}).get("relation", [])
        for ref in tracker_rel:
            tid = ref.get("id")
            if tid:
                notes_map[tid] = notes
    logger.debug("Loaded %d hierarchy notes", len(notes_map))
    return notes_map


def attach_notes_to_hierarchy(
    nodes: list[dict[str, Any]], notes_map: dict[str, str],
) -> None:
    """Walk the tree and set ``notes`` on nodes whose id is in ``notes_map``.

    Mutates ``nodes`` in place. Nodes without a matching entry are left
    untouched (no empty ``notes`` key) so downstream renderers can rely on
    ``"notes" in node`` to check presence.
    """
    for node in nodes:
        nid = node.get("id")
        if nid and nid in notes_map:
            node["notes"] = notes_map[nid]
        attach_notes_to_hierarchy(node.get("children", []), notes_map)
