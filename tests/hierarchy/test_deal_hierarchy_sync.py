"""Tests for src.hierarchy.deal_hierarchy_sync (ReportingNz_deals → Hierarchy DB)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.config import SyncConfig
from src.hierarchy import deal_hierarchy_sync
from src.hierarchy.deal_hierarchy_sync import (
    _ANCHOR_DEALFLOW,
    _ANCHOR_PORTFOLIO,
    _TIER_PROJECT,
    _TIER_WORKSTREAM,
    _Deal,
    _load_existing_rows,
    _norm,
    _plan,
    _Row,
)


def _make_config(**overrides) -> SyncConfig:
    defaults = {
        "notion_api_token": "secret_abc",
        "openai_api_key": "sk-abc",
        "team_tracker_db_id": "db-tracker",
        "merged_transcript_extraction_prompt_page_id": "page-merged",
        "hierarchy_db_id": "db-hierarchy",
    }
    defaults.update(overrides)
    return SyncConfig(**defaults)


def _deal(deal_id="d-1", name="Project Lavare", stage="Portfolio") -> _Deal:
    return _Deal(deal_id=deal_id, name=name, stage=stage)


def _row(deal_id="d-1", name="Project Lavare", tier=_TIER_PROJECT,
         active=True, parent_id=_ANCHOR_PORTFOLIO, page_id="page-1") -> _Row:
    return _Row(page_id=page_id, deal_id=deal_id, name=name, tier=tier,
                active=active, parent_id=parent_id)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class TestPlan:
    def test_portfolio_deal_creates_when_no_existing_row(self):
        plan = _plan([_deal(stage="Portfolio")], {}, {})
        assert len(plan) == 1
        item = plan[0]
        assert item.kind == "create"
        assert item.page_id is None
        assert item.properties["Tier"] == {"select": {"name": _TIER_PROJECT}}
        assert item.properties["Parent item"] == {
            "relation": [{"id": _ANCHOR_PORTFOLIO}],
        }
        assert item.properties["Active"] == {"checkbox": True}
        assert item.properties["Deal ID"]["rich_text"][0]["text"]["content"] == "d-1"

    def test_dealflow_deal_creates_tier_2_under_dealflow_anchor(self):
        plan = _plan([_deal(stage="DD phase")], {}, {})
        assert plan[0].properties["Tier"] == {"select": {"name": _TIER_WORKSTREAM}}
        assert plan[0].properties["Parent item"] == {
            "relation": [{"id": _ANCHOR_DEALFLOW}],
        }

    def test_adopts_existing_handmade_row_by_name(self):
        """The fix: a tracked deal with a matching hand-made row ADOPTS it
        (stamps Deal ID) instead of creating a duplicate."""
        deals = [_deal(name="Azenea", stage="Portfolio")]
        handmade = _row(deal_id="", name="Azenea", page_id="handmade-1")
        plan = _plan(deals, {}, {"azenea": handmade})
        assert len(plan) == 1
        assert plan[0].kind == "adopted"
        assert plan[0].page_id == "handmade-1"
        # Only the Deal ID is written — curation (name/tier/parent) untouched.
        assert plan[0].properties == {
            "Deal ID": {"rich_text": [{"text": {"content": "d-1"}}]},
        }

    def test_adoption_is_case_and_whitespace_insensitive(self):
        deals = [_deal(name="Project  Lavare", stage="DD phase")]
        handmade = _row(deal_id="", name="project lavare", page_id="hm")
        plan = _plan(deals, {}, {_norm("project lavare"): handmade})
        assert plan[0].kind == "adopted"

    def test_no_name_match_creates(self):
        deals = [_deal(name="Brand New Deal", stage="DD phase")]
        plan = _plan(deals, {}, {"azenea": _row(deal_id="", name="Azenea")})
        assert plan[0].kind == "create"

    def test_owned_in_sync_no_plan_item(self):
        deals = [_deal(stage="Portfolio")]
        owned = {"d-1": _row()}
        assert _plan(deals, owned, {}) == []

    def test_owned_row_never_renamed_or_rehomed_on_drift(self):
        # Deal moved Portfolio → DD phase but the owned row keeps its curated
        # placement: no plan item (we only toggle Active, never re-home).
        deals = [_deal(stage="DD phase")]
        owned = {"d-1": _row(tier=_TIER_PROJECT, parent_id=_ANCHOR_PORTFOLIO, active=True)}
        assert _plan(deals, owned, {}) == []

    def test_untracked_deal_no_row_is_skipped(self):
        assert _plan([_deal(stage="Discarded")], {}, {}) == []

    def test_deal_left_tracked_stages_soft_archives(self):
        deals = [_deal(stage="Discarded")]
        owned = {"d-1": _row(active=True)}
        plan = _plan(deals, owned, {})
        assert len(plan) == 1
        assert plan[0].kind == "archived"
        assert plan[0].page_id == "page-1"
        assert plan[0].properties == {"Active": {"checkbox": False}}

    def test_already_archived_left_stage_is_idempotent(self):
        deals = [_deal(stage="Discarded")]
        owned = {"d-1": _row(active=False)}
        assert _plan(deals, owned, {}) == []

    def test_re_entry_reactivates(self):
        deals = [_deal(stage="Portfolio")]
        owned = {"d-1": _row(active=False)}
        plan = _plan(deals, owned, {})
        assert len(plan) == 1
        assert plan[0].kind == "reactivated"
        assert plan[0].properties["Active"] == {"checkbox": True}

    def test_deal_absent_from_snapshot_soft_archives_owned_row(self):
        owned = {"d-1": _row(active=True)}
        plan = _plan([], owned, {})
        assert len(plan) == 1
        assert plan[0].kind == "archived"
        assert plan[0].properties == {"Active": {"checkbox": False}}

    def test_deal_absent_from_snapshot_already_inactive_noop(self):
        owned = {"d-1": _row(active=False)}
        assert _plan([], owned, {}) == []


# ---------------------------------------------------------------------------
# _load_existing_rows
# ---------------------------------------------------------------------------


class TestLoadExistingRows:
    def test_splits_owned_from_adoptable(self):
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                {  # owned — already carries a Deal ID
                    "id": "page-owned",
                    "properties": {
                        "Name": {"type": "title",
                                 "title": [{"plain_text": "Project Lavare"}]},
                        "Deal ID": {"rich_text": [{"plain_text": "d-1"}]},
                        "Tier": {"select": {"name": _TIER_PROJECT}},
                        "Active": {"checkbox": True},
                        "Parent item": {"relation": [{"id": _ANCHOR_PORTFOLIO}]},
                    },
                },
                {  # adoptable — hand-made, under an anchor, no Deal ID
                    "id": "page-handmade",
                    "properties": {
                        "Name": {"type": "title",
                                 "title": [{"plain_text": "Azenea"}]},
                        "Deal ID": {"rich_text": []},
                        "Tier": {"select": {"name": _TIER_PROJECT}},
                        "Active": {"checkbox": True},
                        "Parent item": {"relation": [{"id": _ANCHOR_PORTFOLIO}]},
                    },
                },
                {  # NOT adoptable — hand-made but NOT under an anchor
                    "id": "page-elsewhere",
                    "properties": {
                        "Name": {"type": "title",
                                 "title": [{"plain_text": "Marketing"}]},
                        "Deal ID": {"rich_text": []},
                        "Tier": {"select": {"name": _TIER_PROJECT}},
                        "Active": {"checkbox": True},
                        "Parent item": {"relation": [{"id": "some-other-parent"}]},
                    },
                },
            ],
        }
        owned, adoptable = _load_existing_rows(client, "db-hierarchy")
        assert set(owned) == {"d-1"}
        assert owned["d-1"].page_id == "page-owned"
        # Only the anchor-child hand-made row is adoptable; "Marketing" excluded.
        assert set(adoptable) == {_norm("Azenea")}
        assert adoptable[_norm("Azenea")].page_id == "page-handmade"


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


class TestSync:
    def test_aborts_when_hierarchy_db_unset(self):
        config = _make_config(hierarchy_db_id=None)
        client = MagicMock()
        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = deal_hierarchy_sync.sync(client, config)
        assert report.errors == 1
        assert "HIERARCHY_DB_ID" in report.details[0]

    @patch("src.hierarchy.deal_hierarchy_sync._http")
    def test_dry_run_issues_no_writes(self, mock_http):
        config = _make_config(dry_run=True)
        client = MagicMock()

        def http(method, path, body=None, prefer="return=minimal"):
            if method == "GET" and "ReportingNz_deals" in path:
                return [{"id": "d-1", "name": "Project Lavare", "stage": "Portfolio"}]
            return None

        mock_http.side_effect = http
        client.query_database.return_value = {"results": []}

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = deal_hierarchy_sync.sync(client, config)

        assert report.created == 1
        client.create_page.assert_not_called()
        client.update_page.assert_not_called()

    @patch("src.hierarchy.deal_hierarchy_sync._http")
    def test_live_creates_row_for_tracked_deal(self, mock_http):
        config = _make_config()
        client = MagicMock()

        def http(method, path, body=None, prefer="return=minimal"):
            if method == "GET" and "ReportingNz_deals" in path:
                return [{"id": "d-1", "name": "Project Lavare", "stage": "DD phase"}]
            return None

        mock_http.side_effect = http
        client.query_database.return_value = {"results": []}

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = deal_hierarchy_sync.sync(client, config)

        assert report.created == 1
        assert report.errors == 0
        client.create_page.assert_called_once()
        db_id, props = client.create_page.call_args.args
        assert db_id == "db-hierarchy"
        assert props["Tier"] == {"select": {"name": _TIER_WORKSTREAM}}
        assert props["Parent item"] == {"relation": [{"id": _ANCHOR_DEALFLOW}]}
