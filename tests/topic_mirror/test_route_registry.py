"""Tests for the Meeting Rules registry (was: Topic Mirror Routes)."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.topic_mirror.route_registry import (
    ACTION_AFFINITY_LP_FUNNEL,
    ACTION_AFFINITY_LP_FUNNEL_TRANSCRIPT,
    ACTION_MIRROR_TO_DB,
    AFFINITY_LP_ACTIONS,
    MATCH_DETAIL,
    MATCH_MACRO_WORK_BLOCK,
    load_routes,
    match_routes,
)


_DB_URL = "https://www.notion.so/dc0e537633cb4e8c9c2b97210878d7d2"
_DB_ID = "dc0e537633cb4e8c9c2b97210878d7d2"


def _make_row(
    *,
    match_property: str,
    match_value: str,
    target_db_url: str = _DB_URL,
    action: str | None = None,
    route_title: str = "",
    active: bool = True,
    page_id: str = "row-1",
) -> dict:
    props = {
        "Match Property": {
            "type": "select",
            "select": {"name": match_property},
        },
        "Match Value": {
            "type": "rich_text",
            "rich_text": [{"plain_text": match_value}],
        },
        "Target DB": {"type": "url", "url": target_db_url},
        "Active": {"type": "checkbox", "checkbox": active},
        "Route": {
            "type": "title",
            "title": [{"plain_text": route_title}] if route_title else [],
        },
    }
    if action is not None:
        props["Action"] = {
            "type": "select",
            "select": {"name": action} if action else None,
        }
    return {"id": page_id, "properties": props}


class TestLoadRoutes:
    def test_loads_mirror_to_db_route_with_action_set(self):
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                _make_row(
                    match_property=MATCH_DETAIL,
                    match_value="AI & Tech",
                    action=ACTION_MIRROR_TO_DB,
                ),
            ],
        }
        routes = load_routes(client, "db-rules")
        assert len(routes) == 1
        assert routes[0].action == ACTION_MIRROR_TO_DB
        assert routes[0].target_db_id == _DB_ID

    def test_action_empty_defaults_to_mirror_to_db(self):
        """Back-compat: rows pre-dating the Action column still load."""
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                # action=None means the Action property is absent entirely.
                _make_row(
                    match_property=MATCH_DETAIL,
                    match_value="Legal DD",
                ),
            ],
        }
        routes = load_routes(client, "db-rules")
        assert len(routes) == 1
        assert routes[0].action == ACTION_MIRROR_TO_DB

    def test_affinity_action_loads_without_target_db(self):
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                _make_row(
                    match_property=MATCH_MACRO_WORK_BLOCK,
                    match_value="LPs & Fundraising",
                    target_db_url="",
                    action=ACTION_AFFINITY_LP_FUNNEL,
                    route_title="Affinity LP Funnel",
                ),
            ],
        }
        routes = load_routes(client, "db-rules")
        assert len(routes) == 1
        assert routes[0].action == ACTION_AFFINITY_LP_FUNNEL
        assert routes[0].target_db_id == ""
        assert routes[0].match_property == MATCH_MACRO_WORK_BLOCK
        assert routes[0].match_value == "LPs & Fundraising"
        assert routes[0].label == "Affinity LP Funnel"

    def test_affinity_with_transcript_action_loads(self):
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                _make_row(
                    match_property=MATCH_MACRO_WORK_BLOCK,
                    match_value="LPs & Fundraising",
                    target_db_url="",
                    action=ACTION_AFFINITY_LP_FUNNEL_TRANSCRIPT,
                ),
            ],
        }
        routes = load_routes(client, "db-rules")
        assert len(routes) == 1
        assert routes[0].action == ACTION_AFFINITY_LP_FUNNEL_TRANSCRIPT
        assert routes[0].action in AFFINITY_LP_ACTIONS

    def test_legacy_affinity_action_normalizes_to_no_transcript(self):
        """A row still carrying the pre-rename 'Fire Affinity LP Funnel' tag
        keeps firing, as the no-transcript variant."""
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                _make_row(
                    match_property=MATCH_MACRO_WORK_BLOCK,
                    match_value="LPs & Fundraising",
                    target_db_url="",
                    action="Fire Affinity LP Funnel",
                ),
            ],
        }
        routes = load_routes(client, "db-rules")
        assert len(routes) == 1
        assert routes[0].action == ACTION_AFFINITY_LP_FUNNEL

    def test_unknown_action_skips_row(self):
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                _make_row(
                    match_property=MATCH_DETAIL,
                    match_value="AI & Tech",
                    action="Send Telegram Message",
                ),
            ],
        }
        routes = load_routes(client, "db-rules")
        assert routes == []

    def test_mirror_action_without_target_db_skips(self):
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                _make_row(
                    match_property=MATCH_DETAIL,
                    match_value="AI & Tech",
                    target_db_url="",
                    action=ACTION_MIRROR_TO_DB,
                ),
            ],
        }
        routes = load_routes(client, "db-rules")
        assert routes == []

    def test_legacy_meeting_type_match_property_is_rejected(self):
        """Match Property must be 'Macro Work Block' (or Detail / External Org)."""
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                _make_row(
                    match_property="Meeting type",
                    match_value="Fundraising",
                    action=ACTION_MIRROR_TO_DB,
                ),
            ],
        }
        routes = load_routes(client, "db-rules")
        assert routes == []


class TestMatchRoutesByWorkArea:
    def test_select_value_matches(self):
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                _make_row(
                    match_property=MATCH_MACRO_WORK_BLOCK,
                    match_value="LPs & Fundraising",
                    target_db_url="",
                    action=ACTION_AFFINITY_LP_FUNNEL,
                ),
            ],
        }
        routes = load_routes(client, "db-rules")
        page_props = {
            "Macro Work Block": {
                "type": "select",
                "select": {"name": "LPs & Fundraising"},
            },
        }
        matched = match_routes(routes, page_props)
        assert len(matched) == 1
        assert matched[0].action == ACTION_AFFINITY_LP_FUNNEL

    def test_select_value_mismatches(self):
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                _make_row(
                    match_property=MATCH_MACRO_WORK_BLOCK,
                    match_value="LPs & Fundraising",
                    target_db_url="",
                    action=ACTION_AFFINITY_LP_FUNNEL,
                ),
            ],
        }
        routes = load_routes(client, "db-rules")
        page_props = {
            "Macro Work Block": {
                "type": "select",
                "select": {"name": "Sourcing & Investing"},
            },
        }
        matched = match_routes(routes, page_props)
        assert matched == []
