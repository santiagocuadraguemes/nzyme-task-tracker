"""Tests for src.hierarchy.canonical_mirror_sync."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.config import SyncConfig
from src.hierarchy import canonical_mirror_sync
from src.hierarchy.canonical_mirror_sync import (
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
        "hierarchy_db_id": "db-hierarchy",
    }
    defaults.update(overrides)
    return SyncConfig(**defaults)


def _nrow(
    page_id: str = "h-1",
    name: str = "Sourcing",
    tier: str = "0. Macro Work Block",
    active: bool = True,
    parent: str | None = None,
    tracker: str | None = None,
    notes: str = "",
) -> _NotionRow:
    return _NotionRow(
        notion_page_id=page_id, name=name, tier=tier, active=active,
        parent_notion_page_id=parent, tracker_node_page_id=tracker, notes=notes,
    )


def _canonical(
    page_id: str = "h-1",
    name: str = "Sourcing",
    tier: str = "0. Macro Work Block",
    active: bool = True,
    parent: str | None = None,
    tracker: str | None = None,
    notes: str | None = None,
    deleted_at: str | None = None,
) -> dict:
    return {
        "notion_page_id": page_id,
        "name": name,
        "tier": tier,
        "active": active,
        "parent_notion_page_id": parent,
        "tracker_node_page_id": tracker,
        "notes": notes,
        "deleted_at": deleted_at,
    }


def _notion_page(
    page_id: str = "h-1",
    name: str = "Sourcing",
    tier: str = "0. Macro Work Block",
    active: bool = True,
    parent: str | None = None,
    tracker: str | None = None,
    notes: str = "",
) -> dict:
    parent_rel = [{"id": parent}] if parent else []
    tracker_rel = [{"id": tracker}] if tracker else []
    notes_rt = [{"plain_text": notes}] if notes else []
    return {
        "id": page_id,
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": name}]},
            "Tier": {"type": "select", "select": {"name": tier}},
            "Active": {"type": "checkbox", "checkbox": active},
            "Parent item": {"type": "relation", "relation": parent_rel},
            "Tracker Node": {"type": "relation", "relation": tracker_rel},
            "Notes": {"type": "rich_text", "rich_text": notes_rt},
        },
    }


class TestComputeChanges:
    def test_new_row_in_notion_is_created(self):
        changes = _compute_changes([_nrow("h-1", "A")], {})
        assert len(changes) == 1
        assert changes[0].op == "created"
        assert changes[0].after["name"] == "A"
        assert changes[0].before is None

    def test_identical_row_is_unchanged(self):
        changes = _compute_changes(
            [_nrow("h-1", "A")],
            {"h-1": _canonical(page_id="h-1", name="A")},
        )
        assert len(changes) == 1
        assert changes[0].op == "unchanged"
        assert changes[0].field_diff == {}

    def test_field_change_is_edited_with_field_level_diff(self):
        changes = _compute_changes(
            [_nrow("h-1", name="WWW Sourcing")],
            {"h-1": _canonical(page_id="h-1", name="Sourcing")},
        )
        assert len(changes) == 1
        ch = changes[0]
        assert ch.op == "edited"
        assert "name" in ch.field_diff
        assert ch.field_diff["name"] == {"before": "Sourcing", "after": "WWW Sourcing"}
        # Other fields unchanged → not in diff.
        assert "tier" not in ch.field_diff
        assert "active" not in ch.field_diff

    def test_multiple_field_changes_listed_separately(self):
        changes = _compute_changes(
            [_nrow("h-1", name="Renamed", active=False, parent="h-p")],
            {"h-1": _canonical(page_id="h-1", name="Original", active=True, parent=None)},
        )
        ch = changes[0]
        assert ch.op == "edited"
        assert set(ch.field_diff.keys()) == {"name", "active", "parent_notion_page_id"}

    def test_canonical_row_missing_from_notion_is_deleted(self):
        changes = _compute_changes(
            [],
            {"h-1": _canonical(page_id="h-1", name="A")},
        )
        assert len(changes) == 1
        assert changes[0].op == "deleted"
        assert changes[0].before["name"] == "A"
        assert changes[0].after is None

    def test_already_tombstoned_canonical_is_ignored(self):
        changes = _compute_changes(
            [],
            {"h-1": _canonical(page_id="h-1", deleted_at="2026-05-19T07:00:00Z")},
        )
        # Already tombstoned, still missing → no new event.
        assert changes == []

    def test_tombstoned_row_back_in_notion_is_reactivated(self):
        changes = _compute_changes(
            [_nrow("h-1", "A")],
            {"h-1": _canonical(page_id="h-1", name="A", deleted_at="2026-05-19T07:00:00Z")},
        )
        assert len(changes) == 1
        assert changes[0].op == "reactivated"
        assert changes[0].after["name"] == "A"

    def test_notes_none_in_canonical_vs_empty_string_in_notion_is_not_edited(self):
        """PostgREST returns None for never-set notes; Notion gives ''. Don't flag."""
        changes = _compute_changes(
            [_nrow("h-1", notes="")],
            {"h-1": _canonical(page_id="h-1", notes=None)},
        )
        assert len(changes) == 1
        assert changes[0].op == "unchanged"

    def test_mixed_workspace_produces_all_op_types(self):
        notion = [
            _nrow("h-keep", "Keep"),           # unchanged
            _nrow("h-edit", "Edited Now"),     # edited
            _nrow("h-new",  "Brand New"),      # created
            _nrow("h-back", "Returned"),       # reactivated
        ]
        canonical = {
            "h-keep": _canonical(page_id="h-keep", name="Keep"),
            "h-edit": _canonical(page_id="h-edit", name="Old Name"),
            "h-back": _canonical(page_id="h-back", name="Returned",
                                 deleted_at="2026-05-19T07:00:00Z"),
            "h-gone": _canonical(page_id="h-gone", name="Removed"),  # → deleted
        }
        changes = _compute_changes(notion, canonical)
        ops = {ch.notion_page_id: ch.op for ch in changes}
        assert ops == {
            "h-keep": "unchanged",
            "h-edit": "edited",
            "h-new": "created",
            "h-back": "reactivated",
            "h-gone": "deleted",
        }


class TestSync:
    def test_aborts_when_hierarchy_db_unconfigured(self):
        config = _make_config(hierarchy_db_id=None)
        client = MagicMock()
        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = canonical_mirror_sync.sync(client, config)
        assert report.errors == 1
        assert "HIERARCHY_DB_ID" in report.details[0]
        client.query_database.assert_not_called()

    def test_aborts_when_supabase_unconfigured(self):
        config = _make_config()
        client = MagicMock()
        with patch.dict("os.environ", {}, clear=True):
            report = canonical_mirror_sync.sync(client, config)
        assert report.errors == 1
        assert "Supabase" in report.details[0]
        client.query_database.assert_not_called()

    @patch("src.hierarchy.canonical_mirror_sync._http")
    def test_bootstrap_first_run_creates_every_row(self, mock_http):
        config = _make_config()
        client = MagicMock()
        client.query_database.return_value = {
            "results": [_notion_page("h-1", "A"), _notion_page("h-2", "B")],
        }

        # Calls: GET (snapshot) → POST upsert → POST audit row.
        def http(method, path, body=None, prefer=""):
            if method == "GET" and "hierarchy_rows" in path:
                return []
            return None

        mock_http.side_effect = http

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = canonical_mirror_sync.sync(client, config)

        assert report.created == 2
        assert report.edited == 0
        assert report.deleted == 0
        assert report.errors == 0

        # Find the upsert call.
        upsert_calls = [
            c for c in mock_http.call_args_list
            if c.args[0] == "POST" and "hierarchy_rows" in c.args[1]
        ]
        assert len(upsert_calls) == 1
        upsert_body = upsert_calls[0].kwargs.get("body") or upsert_calls[0].args[2]
        assert len(upsert_body) == 2
        page_ids = {r["notion_page_id"] for r in upsert_body}
        assert page_ids == {"h-1", "h-2"}

        # Audit row should be written.
        audit_calls = [
            c for c in mock_http.call_args_list
            if c.args[0] == "POST" and "hierarchy_sync_runs" in c.args[1]
        ]
        assert len(audit_calls) == 1

    @patch("src.hierarchy.canonical_mirror_sync._http")
    def test_rename_in_notion_produces_one_edited_upsert(self, mock_http):
        config = _make_config()
        client = MagicMock()
        client.query_database.return_value = {
            "results": [_notion_page("h-1", "New Name")],
        }

        def http(method, path, body=None, prefer=""):
            if method == "GET" and "hierarchy_rows" in path:
                return [{"notion_page_id": "h-1", "name": "Old Name",
                         "tier": "0. Macro Work Block", "active": True,
                         "parent_notion_page_id": None, "tracker_node_page_id": None,
                         "notes": None, "deleted_at": None}]
            return None

        mock_http.side_effect = http

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = canonical_mirror_sync.sync(client, config)

        assert report.edited == 1
        assert report.created == 0
        assert report.errors == 0

        upsert_calls = [
            c for c in mock_http.call_args_list
            if c.args[0] == "POST" and "hierarchy_rows" in c.args[1]
        ]
        body = upsert_calls[0].kwargs.get("body") or upsert_calls[0].args[2]
        assert body[0]["notion_page_id"] == "h-1"
        assert body[0]["name"] == "New Name"
        assert "last_changed_at" in body[0]

    @patch("src.hierarchy.canonical_mirror_sync._http")
    def test_row_disappears_triggers_tombstone_patch(self, mock_http):
        config = _make_config()
        client = MagicMock()
        client.query_database.return_value = {"results": []}

        def http(method, path, body=None, prefer=""):
            if method == "GET" and "hierarchy_rows" in path:
                return [{"notion_page_id": "h-1", "name": "Gone",
                         "tier": "0. Macro Work Block", "active": True,
                         "parent_notion_page_id": None, "tracker_node_page_id": None,
                         "notes": None, "deleted_at": None}]
            return None

        mock_http.side_effect = http

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = canonical_mirror_sync.sync(client, config)

        assert report.deleted == 1
        # Find the PATCH on hierarchy_rows.
        patch_calls = [
            c for c in mock_http.call_args_list
            if c.args[0] == "PATCH" and "hierarchy_rows" in c.args[1]
        ]
        assert len(patch_calls) == 1
        assert "notion_page_id=in." in patch_calls[0].args[1]
        body = patch_calls[0].kwargs.get("body") or patch_calls[0].args[2]
        assert "deleted_at" in body

    @patch("src.hierarchy.canonical_mirror_sync._http")
    def test_dry_run_does_no_writes_but_counts(self, mock_http):
        config = _make_config(dry_run=True)
        client = MagicMock()
        client.query_database.return_value = {
            "results": [_notion_page("h-1", "New Name"), _notion_page("h-new", "Brand New")],
        }

        def http(method, path, body=None, prefer=""):
            if method == "GET" and "hierarchy_rows" in path:
                return [{"notion_page_id": "h-1", "name": "Old Name",
                         "tier": "0. Macro Work Block", "active": True,
                         "parent_notion_page_id": None, "tracker_node_page_id": None,
                         "notes": None, "deleted_at": None}]
            # In dry-run we should never see POST/PATCH writes.
            raise AssertionError(f"Unexpected write: {method} {path}")

        mock_http.side_effect = http

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = canonical_mirror_sync.sync(client, config)

        assert report.created == 1
        assert report.edited == 1
        # Only one HTTP call: the GET snapshot.
        assert mock_http.call_count == 1
        assert mock_http.call_args.args[0] == "GET"
        assert any("dry-run" in d for d in report.details)

    @patch("src.hierarchy.canonical_mirror_sync._http")
    def test_upsert_failure_bails_before_audit(self, mock_http):
        config = _make_config()
        client = MagicMock()
        client.query_database.return_value = {
            "results": [_notion_page("h-1", "A")],
        }

        def http(method, path, body=None, prefer=""):
            if method == "GET":
                return []
            if method == "POST" and "hierarchy_rows" in path:
                raise RuntimeError("upsert boom")
            raise AssertionError(f"Should not reach: {method} {path}")

        mock_http.side_effect = http

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = canonical_mirror_sync.sync(client, config)

        assert report.errors >= 1
        # Audit row should NOT have been written — next run retries cleanly.
        audit_calls = [
            c for c in mock_http.call_args_list
            if c.args[0] == "POST" and "hierarchy_sync_runs" in c.args[1]
        ]
        assert audit_calls == []

    @patch("src.hierarchy.canonical_mirror_sync._http")
    def test_audit_row_failure_does_not_fail_sync(self, mock_http):
        config = _make_config()
        client = MagicMock()
        client.query_database.return_value = {
            "results": [_notion_page("h-1", "A")],
        }

        def http(method, path, body=None, prefer=""):
            if method == "GET":
                return []
            if "hierarchy_sync_runs" in path:
                raise RuntimeError("audit boom")
            return None

        mock_http.side_effect = http

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = canonical_mirror_sync.sync(client, config)

        # Upsert succeeded, audit failed → no hard error, only a detail.
        assert report.errors == 0
        assert any("audit row insert failed" in d for d in report.details)

    @patch("src.hierarchy.canonical_mirror_sync._http")
    def test_empty_name_notion_page_is_skipped(self, mock_http):
        config = _make_config()
        client = MagicMock()
        page = _notion_page("h-1", "")
        page["properties"]["Name"]["title"] = []
        client.query_database.return_value = {"results": [page]}

        def http(method, path, body=None, prefer=""):
            if method == "GET":
                return []
            return None

        mock_http.side_effect = http

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = canonical_mirror_sync.sync(client, config)

        # Page was skipped → nothing to create.
        assert report.created == 0
        upsert_calls = [
            c for c in mock_http.call_args_list
            if c.args[0] == "POST" and "hierarchy_rows" in c.args[1]
        ]
        assert upsert_calls == []
