"""Tests for src.hierarchy.detail_canonical_mirror_sync."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.config import SyncConfig
from src.hierarchy import detail_canonical_mirror_sync
from src.hierarchy.detail_canonical_mirror_sync import (
    _NotionRow,
    _compute_changes,
)


def _make_config(**overrides) -> SyncConfig:
    defaults = {
        "notion_api_token": "secret_abc",
        "openai_api_key": "sk-abc",
        "team_tracker_db_id": "db-tracker",
        "merged_transcript_extraction_prompt_page_id": "page-merged",
        "org_chart_db_id": "db-org",
        "detail_options_db_id": "db-detail",
    }
    defaults.update(overrides)
    return SyncConfig(**defaults)


def _nrow(
    page_id: str = "d-1",
    name: str = "Legal DD",
    color: str = "blue",
    parent: str | None = "tier0-sourcing",
    active: bool = True,
) -> _NotionRow:
    return _NotionRow(
        notion_page_id=page_id, name=name, color=color,
        parent_hierarchy_page_id=parent, active=active,
    )


def _canonical(
    page_id: str = "d-1",
    name: str = "Legal DD",
    color: str = "blue",
    parent: str | None = "tier0-sourcing",
    active: bool = True,
    deleted_at: str | None = None,
) -> dict:
    return {
        "notion_page_id": page_id,
        "name": name,
        "color": color,
        "parent_hierarchy_page_id": parent,
        "active": active,
        "deleted_at": deleted_at,
    }


class TestComputeChanges:
    def test_new_row_is_created(self):
        changes = _compute_changes([_nrow("d-1", "Legal DD")], {})
        assert len(changes) == 1
        assert changes[0].op == "created"

    def test_identical_row_is_unchanged(self):
        changes = _compute_changes(
            [_nrow("d-1")], {"d-1": _canonical()},
        )
        assert changes[0].op == "unchanged"

    def test_color_only_change_is_edited(self):
        changes = _compute_changes(
            [_nrow("d-1", color="green")],
            {"d-1": _canonical(color="blue")},
        )
        assert changes[0].op == "edited"
        assert "color" in changes[0].field_diff
        assert changes[0].field_diff["color"] == {"before": "blue", "after": "green"}

    def test_parent_change_is_edited(self):
        changes = _compute_changes(
            [_nrow("d-1", parent="tier0-other")],
            {"d-1": _canonical(parent="tier0-sourcing")},
        )
        assert changes[0].op == "edited"
        assert "parent_hierarchy_page_id" in changes[0].field_diff

    def test_active_toggled_is_edited(self):
        changes = _compute_changes(
            [_nrow("d-1", active=False)],
            {"d-1": _canonical(active=True)},
        )
        assert changes[0].op == "edited"
        assert "active" in changes[0].field_diff

    def test_missing_canonical_with_color_none_normalises_to_default(self):
        """PostgREST None color should normalise to 'default' — no spurious edit."""
        changes = _compute_changes(
            [_nrow("d-1", color="default")],
            {"d-1": _canonical(color=None)},
        )
        assert changes[0].op == "unchanged"

    def test_canonical_missing_from_notion_is_deleted(self):
        changes = _compute_changes([], {"d-1": _canonical()})
        assert changes[0].op == "deleted"

    def test_tombstoned_canonical_back_in_notion_is_reactivated(self):
        changes = _compute_changes(
            [_nrow("d-1")],
            {"d-1": _canonical(deleted_at="2026-05-21T07:00:00Z")},
        )
        assert changes[0].op == "reactivated"

    def test_tombstoned_canonical_absent_from_notion_is_ignored(self):
        changes = _compute_changes(
            [],
            {"d-1": _canonical(deleted_at="2026-05-21T07:00:00Z")},
        )
        assert changes == []


class TestSync:
    def test_skips_with_warning_when_settings_db_unconfigured(self):
        """Optional feature → skip with errors=0 when DETAIL_OPTIONS_DB_ID unset."""
        config = _make_config(detail_options_db_id=None)
        client = MagicMock()
        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = detail_canonical_mirror_sync.sync(client, config)
        assert report.errors == 0  # benign warning, NOT an error
        assert any("DETAIL_OPTIONS_DB_ID" in d for d in report.details)
        client.query_database.assert_not_called()

    def test_aborts_when_supabase_env_missing(self):
        config = _make_config()
        client = MagicMock()
        with patch.dict("os.environ", {}, clear=True):
            report = detail_canonical_mirror_sync.sync(client, config)
        assert report.errors == 1
        assert "Supabase" in report.details[0]
        client.query_database.assert_not_called()
