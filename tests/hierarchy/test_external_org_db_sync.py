"""Tests for src.hierarchy.external_org_db_sync (Supabase → single Settings DB)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.config import SyncConfig
from src.hierarchy import external_org_db_sync
from src.hierarchy.external_org_db_sync import (
    _Deal,
    _Row,
    _plan,
    _props_for,
)

_DB_ID = "db-external-orgs"


def _make_config(**overrides) -> SyncConfig:
    defaults = {
        "notion_api_token": "secret_abc",
        "openai_api_key": "sk-abc",
        "team_tracker_db_id": "db-tracker",
        "merged_transcript_extraction_prompt_page_id": "page-merged",
        "external_orgs_db_id": _DB_ID,
    }
    defaults.update(overrides)
    return SyncConfig(**defaults)


def _page(deal_id: str, name: str, stage: str | None) -> dict:
    """Build a Notion page dict as query_database would return it."""
    sel = {"select": {"name": stage}} if stage else {"select": None}
    return {
        "id": f"page-{deal_id}",
        "properties": {
            "Name": {"title": [{"plain_text": name}]},
            "Stage": sel,
            "Deal ID": {"rich_text": [{"plain_text": deal_id}]},
        },
    }


# ---------------------------------------------------------------------------
# TestPlan — pure planner
# ---------------------------------------------------------------------------


class TestPlan:
    def test_tracked_deal_with_no_row_is_created(self):
        deals = [_Deal("d1", "Project Lavare", "Portfolio")]
        plan = _plan(deals, existing={})
        assert len(plan) == 1
        assert plan[0].kind == "create"
        assert plan[0].deal.deal_id == "d1"

    def test_untracked_deal_with_no_row_is_skipped(self):
        deals = [_Deal("d1", "Dead Deal", "Discarded")]
        plan = _plan(deals, existing={})
        assert plan == []

    def test_existing_row_outside_tracked_stages_is_updated_not_deleted(self):
        # Deal left the tracked stages (Portfolio → Discarded) but has a row.
        deals = [_Deal("d1", "Project Lavare", "Discarded")]
        existing = {
            "d1": _Row(
                page_id="page-d1", deal_id="d1",
                name="Project Lavare", stage="Portfolio",
            ),
        }
        plan = _plan(deals, existing)
        assert len(plan) == 1
        assert plan[0].kind == "update"
        assert plan[0].page_id == "page-d1"
        assert plan[0].deal.stage == "Discarded"

    def test_unchanged_row_is_idempotent(self):
        deals = [_Deal("d1", "Project Lavare", "Portfolio")]
        existing = {
            "d1": _Row(
                page_id="page-d1", deal_id="d1",
                name="Project Lavare", stage="Portfolio",
            ),
        }
        assert _plan(deals, existing) == []

    def test_name_drift_triggers_update(self):
        deals = [_Deal("d1", "Project Lavare II", "Portfolio")]
        existing = {
            "d1": _Row(
                page_id="page-d1", deal_id="d1",
                name="Project Lavare", stage="Portfolio",
            ),
        }
        plan = _plan(deals, existing)
        assert len(plan) == 1
        assert plan[0].kind == "update"

    def test_name_with_comma_is_stored_verbatim(self):
        props = _props_for(_Deal("d1", "Acme, Inc.", "Portfolio"))
        assert props["Name"]["title"][0]["text"]["content"] == "Acme, Inc."
        assert props["Deal ID"]["rich_text"][0]["text"]["content"] == "d1"
        assert props["Stage"]["select"] == {"name": "Portfolio"}

    def test_stage_with_comma_is_sanitized(self):
        props = _props_for(
            _Deal("d1", "Kuma", "Under analysis (team assigned, moderate effort)"),
        )
        # Notion forbids commas in select option names → comma stripped.
        assert props["Stage"]["select"] == {
            "name": "Under analysis (team assigned moderate effort)",
        }

    def test_sanitized_stage_does_not_loop(self):
        # Row stores the already-sanitized stage; desired is the raw comma form.
        deals = [_Deal("d1", "Kuma", "Under analysis (team assigned, moderate effort)")]
        existing = {
            "d1": _Row(
                page_id="page-d1", deal_id="d1", name="Kuma",
                stage="Under analysis (team assigned moderate effort)",
            ),
        }
        assert _plan(deals, existing) == []


# ---------------------------------------------------------------------------
# TestSync — I/O
# ---------------------------------------------------------------------------


class TestSync:
    @patch("src.hierarchy.external_org_db_sync._supabase_creds")
    @patch("src.hierarchy.external_org_db_sync._http")
    def test_creates_and_updates(self, mock_http, mock_creds):
        mock_creds.return_value = ("https://x.supabase.co", "key")
        mock_http.return_value = [
            {"id": "d1", "name": "Project Lavare", "stage": "Portfolio"},
            {"id": "d2", "name": "Civislend", "stage": "DD phase"},
            {"id": "d3", "name": "Dead", "stage": "Discarded"},  # untracked, no row
        ]
        client = MagicMock()
        # d2 already exists with a stale stage → update; d1 is new → create.
        client.query_database.return_value = {
            "results": [_page("d2", "Civislend", "Under analysis (team assigned, moderate effort)")],
        }
        client.create_page.return_value = {}
        client.update_page.return_value = {}

        report = external_org_db_sync.sync(client, _make_config())

        assert report.errors == 0
        assert report.created == 1  # d1
        assert report.edited == 1   # d2 stage drift
        client.create_page.assert_called_once()
        created_props = client.create_page.call_args[0][1]
        assert created_props["Deal ID"]["rich_text"][0]["text"]["content"] == "d1"
        assert created_props["Stage"]["select"] == {"name": "Portfolio"}
        client.update_page.assert_called_once_with(
            "page-d2",
            _props_for(_Deal("d2", "Civislend", "DD phase")),
        )

    @patch("src.hierarchy.external_org_db_sync._supabase_creds")
    @patch("src.hierarchy.external_org_db_sync._http")
    def test_dry_run_writes_nothing(self, mock_http, mock_creds):
        mock_creds.return_value = ("https://x.supabase.co", "key")
        mock_http.return_value = [
            {"id": "d1", "name": "Project Lavare", "stage": "Portfolio"},
        ]
        client = MagicMock()
        client.query_database.return_value = {"results": []}

        report = external_org_db_sync.sync(client, _make_config(dry_run=True))

        assert report.created == 1
        client.create_page.assert_not_called()
        client.update_page.assert_not_called()

    def test_missing_db_id_reports_error_and_skips(self):
        client = MagicMock()
        report = external_org_db_sync.sync(client, _make_config(external_orgs_db_id=None))
        assert report.errors == 1
        client.query_database.assert_not_called()

    @patch("src.hierarchy.external_org_db_sync._supabase_creds")
    @patch("src.hierarchy.external_org_db_sync._http")
    def test_rows_without_deal_id_are_ignored(self, mock_http, mock_creds):
        mock_creds.return_value = ("https://x.supabase.co", "key")
        mock_http.return_value = [
            {"id": "d1", "name": "Project Lavare", "stage": "Portfolio"},
        ]
        client = MagicMock()
        # A manual row with no Deal ID and a matching name must NOT block creation.
        manual = {
            "id": "page-manual",
            "properties": {
                "Name": {"title": [{"plain_text": "Project Lavare"}]},
                "Stage": {"select": None},
                "Deal ID": {"rich_text": []},
            },
        }
        client.query_database.return_value = {"results": [manual]}

        report = external_org_db_sync.sync(client, _make_config())

        assert report.created == 1  # keyed by Deal ID, so still created
        client.update_page.assert_not_called()
