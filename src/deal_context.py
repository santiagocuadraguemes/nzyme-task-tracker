"""Load deal context from the Deal Workplans database for AI enrichment."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)


@dataclass
class DealWorkstream:
    id: str
    title: str
    status: str  # "Not started", "In progress", "Done", "At risk", "n/a"
    workstream_type: list[str] = field(default_factory=list)  # ["DD", "Docs", ...]
    adviser: list[str] = field(default_factory=list)  # ["A&M", "DLA", ...]


@dataclass
class DealInfo:
    name: str
    deal_page_id: str  # ID in Deal Workplans DB
    tracker_page_id: str | None  # ID in Team Task Tracker (from relation)
    workplan_db_id: str | None = None
    action_items_db_id: str | None = None
    workstreams: list[DealWorkstream] = field(default_factory=list)


class DealContextLoader:
    """Loads deal information from the Deal Workplans DB.

    Discovers per-deal inline databases (Workplan, Action Items) by
    fetching each deal page's child blocks and matching by title pattern.
    """

    def __init__(self, client: NotionClientWrapper, deal_workplans_db_id: str) -> None:
        self._client = client
        self._db_id = deal_workplans_db_id

    def load_deals(self) -> list[DealInfo]:
        """Query Deal Workplans DB and load context for each deal."""
        response = self._client.query_database(database_id=self._db_id)
        pages = response.get("results", [])

        deals: list[DealInfo] = []
        for page in pages:
            try:
                deal = self._parse_deal(page)
                if deal:
                    deals.append(deal)
            except Exception:
                title = self._get_title(page)
                logger.exception("Failed to load deal context for '%s' — skipping", title)

        logger.info("Loaded %d deals from Deal Workplans DB", len(deals))
        return deals

    def _parse_deal(self, page: dict[str, Any]) -> DealInfo | None:
        """Parse a deal page into a DealInfo, discovering inline DBs."""
        name = self._get_title(page)
        if not name:
            return None

        deal_page_id = page["id"]

        # Get Team Task Tracker relation (🖇️ Team Task Tracker property)
        tracker_page_id = self._get_tracker_page_id(page)

        # Discover inline databases from the deal page's child blocks
        inline_dbs = self._discover_inline_dbs(deal_page_id)

        # Load workstreams if workplan DB found
        workstreams: list[DealWorkstream] = []
        workplan_db_id = inline_dbs.get("workplan")
        if workplan_db_id:
            try:
                workstreams = self._load_workstreams(workplan_db_id)
            except Exception:
                logger.warning("Failed to load workstreams for deal '%s'", name)

        return DealInfo(
            name=name,
            deal_page_id=deal_page_id,
            tracker_page_id=tracker_page_id,
            workplan_db_id=workplan_db_id,
            action_items_db_id=inline_dbs.get("action_items"),
            workstreams=workstreams,
        )

    def _discover_inline_dbs(self, deal_page_id: str) -> dict[str, str]:
        """Fetch deal page blocks, find child_database blocks by title pattern."""
        blocks = self._client.get_block_children(deal_page_id)
        result: dict[str, str] = {}

        for block in blocks:
            if block.get("type") != "child_database":
                continue
            db_title = block.get("child_database", {}).get("title", "").lower()
            db_id = block["id"]

            if "workplan" in db_title and "action" not in db_title:
                result["workplan"] = db_id
            elif "action item" in db_title:
                result["action_items"] = db_id

        return result

    def _load_workstreams(self, workplan_db_id: str) -> list[DealWorkstream]:
        """Query a deal's Workplan DB for active workstreams."""
        response = self._client.query_database(
            database_id=workplan_db_id,
            filter={"property": "Status", "status": {"does_not_equal": "Done"}},
        )
        workstreams: list[DealWorkstream] = []
        for page in response.get("results", []):
            title = self._get_title(page)
            if not title:
                continue
            props = page.get("properties", {})
            workstreams.append(DealWorkstream(
                id=page["id"],
                title=title,
                status=self._get_status(props),
                workstream_type=self._get_multi_select(props, "Type"),
                adviser=self._get_multi_select(props, "Adviser"),
            ))
        return workstreams

    @staticmethod
    def _get_title(page: dict[str, Any]) -> str:
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title":
                return "".join(
                    p.get("plain_text", "") for p in prop.get("title", [])
                )
        return ""

    @staticmethod
    def _get_tracker_page_id(page: dict[str, Any]) -> str | None:
        """Extract the first related page ID from the Team Task Tracker relation."""
        # Look for the specific relation property (name contains "Team Task Tracker")
        props = page.get("properties", {})
        for prop_name, prop in props.items():
            if prop.get("type") == "relation" and "task tracker" in prop_name.lower():
                relations = prop.get("relation", [])
                if relations:
                    return relations[0]["id"]
        return None

    @staticmethod
    def _get_status(props: dict[str, Any]) -> str:
        status = props.get("Status", {}).get("status")
        return status.get("name", "Not started") if status else "Not started"

    @staticmethod
    def _get_multi_select(props: dict[str, Any], prop_name: str) -> list[str]:
        ms = props.get(prop_name, {}).get("multi_select", [])
        return [opt.get("name", "") for opt in ms if opt.get("name")]
