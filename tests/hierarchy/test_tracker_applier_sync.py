"""Tests for src.hierarchy.tracker_applier_sync."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.config import SyncConfig
from src.hierarchy import tracker_applier_sync
from src.hierarchy.tracker_applier_sync import (
    _CanonicalRow,
    _plan_tracker_updates,
)


def _make_config(**overrides) -> SyncConfig:
    defaults = {
        "notion_api_token": "secret_abc",
        "openai_api_key": "sk-abc",
        "team_tracker_db_id": "db-tracker",
        "system_prompt_page_id": "page-system-prompt",
        "user_prompt_page_id": "page-user-prompt",
        "org_chart_db_id": "db-org",
        "hierarchy_db_id": "db-hierarchy",
    }
    defaults.update(overrides)
    return SyncConfig(**defaults)


def _crow(
    page_id: str = "h-1",
    name: str = "Sourcing",
    tier: str = "0. Macro Work Block",
    active: bool = True,
    parent: str | None = None,
    tracker: str | None = "trk-1",
    deleted_at: str | None = None,
) -> _CanonicalRow:
    return _CanonicalRow(
        notion_page_id=page_id, name=name, tier=tier, active=active,
        parent_notion_page_id=parent, tracker_node_page_id=tracker,
        deleted_at=deleted_at,
    )


def _tracker_page(
    page_id: str,
    title: str,
    parent_id: str | None = None,
    archived: bool = False,
) -> dict:
    parent_rel = [{"id": parent_id}] if parent_id else []
    return {
        "id": page_id,
        "archived": archived,
        "properties": {
            "Task": {"type": "title", "title": [{"plain_text": title}]},
            "Parent item": {"type": "relation", "relation": parent_rel},
            "Priority": {"type": "select", "select": {"name": "[DETAILS INSIDE]"}},
        },
    }


# ---------------------------------------------------------------------------
# Pure planner
# ---------------------------------------------------------------------------


class TestPlanTrackerUpdates:
    def test_live_active_matching_tracker_is_noop(self):
        rows = [_crow("h-1", "Sourcing", tracker="trk-1")]
        snap = {"trk-1": {"title": "Sourcing", "parent_id": None}}
        plan = _plan_tracker_updates(rows, snap)
        assert plan.to_create == []
        assert plan.to_update == []
        assert (plan.created, plan.renamed, plan.parent_fixed, plan.archived) == (0, 0, 0, 0)

    def test_live_active_with_stale_title_updates_task_only(self):
        rows = [_crow("h-1", "Sourcing", tracker="trk-1")]
        snap = {"trk-1": {"title": "Old", "parent_id": None}}
        plan = _plan_tracker_updates(rows, snap)
        assert len(plan.to_update) == 1
        tid, payload = plan.to_update[0]
        assert tid == "trk-1"
        assert payload["Task"]["title"][0]["text"]["content"] == "Sourcing"
        assert "Parent item" not in payload
        assert (plan.renamed, plan.parent_fixed, plan.archived) == (1, 0, 0)

    def test_live_active_with_wrong_parent_updates_parent_only(self):
        rows = [
            _crow("h-p", "Macro", tracker="trk-p"),
            _crow("h-1", "Sourcing", parent="h-p", tracker="trk-1"),
        ]
        snap = {
            "trk-p": {"title": "Macro", "parent_id": None},
            "trk-1": {"title": "Sourcing", "parent_id": None},
        }
        plan = _plan_tracker_updates(rows, snap)
        assert len(plan.to_update) == 1
        tid, payload = plan.to_update[0]
        assert tid == "trk-1"
        assert "Task" not in payload
        assert payload["Parent item"]["relation"] == [{"id": "trk-p"}]
        assert (plan.parent_fixed, plan.renamed) == (1, 0)

    def test_both_diverged_one_combined_patch(self):
        rows = [
            _crow("h-p", "Macro", tracker="trk-p"),
            _crow("h-1", "Sourcing", parent="h-p", tracker="trk-1"),
        ]
        snap = {
            "trk-p": {"title": "Macro", "parent_id": None},
            "trk-1": {"title": "Wrong", "parent_id": None},
        }
        plan = _plan_tracker_updates(rows, snap)
        assert len(plan.to_update) == 1
        _, payload = plan.to_update[0]
        assert "Task" in payload
        assert "Parent item" in payload
        assert (plan.renamed, plan.parent_fixed) == (1, 1)

    def test_live_inactive_archives_title(self):
        rows = [_crow("h-1", "Sourcing", active=False, tracker="trk-1")]
        snap = {"trk-1": {"title": "Sourcing", "parent_id": None}}
        plan = _plan_tracker_updates(rows, snap)
        assert len(plan.to_update) == 1
        _, payload = plan.to_update[0]
        assert payload["Task"]["title"][0]["text"]["content"] == "(archived) Sourcing"
        assert (plan.renamed, plan.archived) == (1, 1)

    def test_live_inactive_already_archived_is_noop(self):
        rows = [_crow("h-1", "Sourcing", active=False, tracker="trk-1")]
        snap = {"trk-1": {"title": "(archived) Sourcing", "parent_id": None}}
        plan = _plan_tracker_updates(rows, snap)
        assert plan.to_update == []

    def test_reactivated_strips_archived_prefix(self):
        rows = [_crow("h-1", "Sourcing", active=True, tracker="trk-1")]
        snap = {"trk-1": {"title": "(archived) Sourcing", "parent_id": None}}
        plan = _plan_tracker_updates(rows, snap)
        _, payload = plan.to_update[0]
        assert payload["Task"]["title"][0]["text"]["content"] == "Sourcing"
        assert (plan.renamed, plan.archived) == (1, 0)

    def test_tombstoned_canonical_archives_tracker_page(self):
        """Tombstoned canonical → tracker page Notion-archived (not renamed
        to `(archived) X` as before). Verifies the new drop behavior."""
        rows = [_crow("h-1", "Sourcing", active=True,
                      tracker="trk-1", deleted_at="2026-05-19T07:00:00Z")]
        snap = {"trk-1": {"title": "Sourcing", "parent_id": None}}
        plan = _plan_tracker_updates(rows, snap)
        # Queued for archive, not for title-rename.
        assert plan.to_update == []
        assert plan.to_create == []
        assert len(plan.to_archive) == 1
        archived_row, archived_tracker_id = plan.to_archive[0]
        assert archived_row.notion_page_id == "h-1"
        assert archived_tracker_id == "trk-1"
        assert plan.deleted == 1
        assert plan.archived == 0  # archive counter reserved for inactive title-prefix

    def test_tombstoned_canonical_with_stale_tracker_id_queues_canonical_clear(self):
        """Tombstoned canonical + tracker_node_page_id pointing at a page
        that's missing from the snapshot (already manually archived) →
        no Notion call, just clear the Supabase mapping."""
        rows = [_crow("h-1", "Sourcing",
                      tracker="trk-gone", deleted_at="2026-05-19T07:00:00Z")]
        plan = _plan_tracker_updates(rows, {})
        assert plan.to_create == []
        assert plan.to_update == []
        assert plan.to_archive == []
        assert len(plan.to_clear_canonical) == 1
        assert plan.to_clear_canonical[0].notion_page_id == "h-1"
        assert any("page already gone" in d for d in plan.details)

    def test_tombstoned_canonical_without_tracker_id_is_noop(self):
        """Tombstoned canonical that was never mapped → no work."""
        rows = [_crow("h-1", "Sourcing", tracker=None,
                      deleted_at="2026-05-19T07:00:00Z")]
        plan = _plan_tracker_updates(rows, {})
        assert plan.to_create == []
        assert plan.to_update == []
        assert plan.to_archive == []
        assert plan.to_clear_canonical == []
        # CRITICAL: NO bootstrap-create for a tombstoned row.
        assert plan.created == 0

    def test_missing_tracker_id_queues_create(self):
        rows = [_crow("h-1", "Sourcing", tracker=None)]
        plan = _plan_tracker_updates(rows, {})
        assert len(plan.to_create) == 1
        assert plan.created == 1
        assert plan.archived == 0

    def test_stale_tracker_id_treated_as_create_with_warning(self):
        rows = [_crow("h-1", "Sourcing", tracker="ghost-trk")]
        plan = _plan_tracker_updates(rows, {})
        assert len(plan.to_create) == 1
        assert plan.created == 1
        assert any("not found in Tracker DB" in d for d in plan.details)

    def test_tombstoned_new_row_does_not_create_tracker(self):
        """Pre-PR behavior created a fresh '(archived) X' row for a
        tombstoned canonical with no tracker mapping yet. New behavior:
        tombstoned rows are never created on the tracker side."""
        rows = [_crow("h-1", "Sourcing", active=True,
                      tracker=None, deleted_at="2026-05-19T07:00:00Z")]
        plan = _plan_tracker_updates(rows, {})
        assert plan.created == 0
        assert plan.archived == 0
        assert plan.deleted == 0
        assert plan.to_create == []
        assert plan.to_archive == []

    def test_inactive_new_row_counts_both_created_and_archived(self):
        rows = [_crow("h-1", "Sourcing", active=False, tracker=None)]
        plan = _plan_tracker_updates(rows, {})
        assert (plan.created, plan.archived) == (1, 1)

    def test_duplicate_tracker_fan_in_planned_once(self):
        rows = [
            _crow("h-1", "A", tracker="trk-1"),
            _crow("h-2", "A", tracker="trk-1"),
        ]
        snap = {"trk-1": {"title": "Z", "parent_id": None}}
        plan = _plan_tracker_updates(rows, snap)
        assert len(plan.to_update) == 1
        assert plan.renamed == 1
        assert any("duplicate fan-in" in d for d in plan.details)

    def test_root_row_with_existing_parent_clears_it(self):
        rows = [_crow("h-1", "Sourcing", parent=None, tracker="trk-1")]
        snap = {"trk-1": {"title": "Sourcing", "parent_id": "trk-old"}}
        plan = _plan_tracker_updates(rows, snap)
        _, payload = plan.to_update[0]
        assert payload["Parent item"] == {"relation": []}
        assert plan.parent_fixed == 1

    def test_parent_missing_from_canonical_leaves_parent_empty(self):
        rows = [_crow("h-1", "Sourcing", parent="h-ghost", tracker="trk-1")]
        snap = {"trk-1": {"title": "Sourcing", "parent_id": None}}
        plan = _plan_tracker_updates(rows, snap)
        # Parent unresolvable; Tracker already has no parent → no update.
        assert plan.to_update == []
        assert any("not in canonical snapshot" in d for d in plan.details)

    def test_parent_without_tracker_id_yet_leaves_parent_empty(self):
        """Mid-run state: parent canonical row exists but tracker_id is None."""
        rows = [
            _crow("h-p", "Macro", tracker=None),  # not yet created
            _crow("h-1", "Sourcing", parent="h-p", tracker="trk-1"),
        ]
        snap = {"trk-1": {"title": "Sourcing", "parent_id": None}}
        plan = _plan_tracker_updates(rows, snap)
        # Parent row → to_create. Child has Tracker, desired parent target
        # is None (parent's tracker_id is None), current is None → no update.
        assert len(plan.to_create) == 1
        assert plan.to_update == []

    def test_live_child_clears_link_to_tombstoned_parent(self):
        """New behavior: tombstoned parent's tracker page is Notion-archived,
        so children clear their Parent item to avoid a relation pointing at
        an archived/greyed-out page. Inactive-but-not-tombstoned parents
        still keep the link (a separate test covers that)."""
        rows = [
            _crow("h-p", "Macro", tracker="trk-p",
                  deleted_at="2026-05-19T07:00:00Z"),
            _crow("h-1", "Sourcing", parent="h-p", tracker="trk-1"),
        ]
        snap = {
            "trk-p": {"title": "Macro", "parent_id": None},
            "trk-1": {"title": "Sourcing", "parent_id": "trk-p"},
        }
        plan = _plan_tracker_updates(rows, snap)
        # Parent queued for archive.
        assert len(plan.to_archive) == 1
        assert plan.to_archive[0][1] == "trk-p"
        # Child's Parent item cleared.
        assert len(plan.to_update) == 1
        child_tracker_id, payload = plan.to_update[0]
        assert child_tracker_id == "trk-1"
        assert payload["Parent item"] == {"relation": []}
        assert plan.parent_fixed == 1
        assert any("is tombstoned" in d for d in plan.details)

    def test_live_child_keeps_link_to_inactive_but_not_tombstoned_parent(self):
        """Inactive (active=False, deleted_at=None) parent → tracker row
        renamed in place to `(archived) X` (still exists). Child keeps the
        link, since the relation still resolves to a visible page."""
        rows = [
            _crow("h-p", "Macro", tracker="trk-p", active=False),
            _crow("h-1", "Sourcing", parent="h-p", tracker="trk-1"),
        ]
        snap = {
            "trk-p": {"title": "(archived) Macro", "parent_id": None},
            "trk-1": {"title": "Sourcing", "parent_id": "trk-p"},
        }
        plan = _plan_tracker_updates(rows, snap)
        # Parent NOT queued for archive (inactive ≠ tombstoned).
        assert plan.to_archive == []
        # Child already correctly linked → no Parent item patch.
        assert plan.to_update == []


# ---------------------------------------------------------------------------
# I/O sync
# ---------------------------------------------------------------------------


class TestSync:
    def test_aborts_when_team_tracker_db_empty(self):
        config = _make_config(team_tracker_db_id="")
        client = MagicMock()
        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = tracker_applier_sync.sync(client, config)
        assert report.errors == 1
        assert "TEAM_TRACKER_DB_ID" in report.details[0]

    def test_aborts_when_supabase_env_missing(self):
        config = _make_config()
        client = MagicMock()
        with patch.dict("os.environ", {}, clear=True):
            report = tracker_applier_sync.sync(client, config)
        assert report.errors == 1
        assert "Supabase" in report.details[0]

    @patch("src.hierarchy.tracker_applier_sync._http")
    def test_canonical_query_failure_records_error(self, mock_http):
        config = _make_config()
        client = MagicMock()
        mock_http.side_effect = RuntimeError("canonical boom")
        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = tracker_applier_sync.sync(client, config)
        assert report.errors == 1
        assert "canonical snapshot failed" in report.details[0]
        client.create_page.assert_not_called()
        client.update_page.assert_not_called()

    @patch("src.hierarchy.tracker_applier_sync._http")
    def test_empty_canonical_returns_with_warning_not_error(self, mock_http):
        """PR1 mirror hasn't run yet → benign skip, NOT an error."""
        config = _make_config()
        client = MagicMock()
        mock_http.return_value = []
        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = tracker_applier_sync.sync(client, config)
        assert report.errors == 0
        assert any("canonical empty" in d for d in report.details)
        client.query_database.assert_not_called()
        client.create_page.assert_not_called()

    @patch("src.hierarchy.tracker_applier_sync._http")
    def test_tracker_query_failure_records_error(self, mock_http):
        config = _make_config()
        client = MagicMock()
        mock_http.return_value = [{
            "notion_page_id": "h-1", "name": "A", "tier": "0. Macro Work Block",
            "active": True, "parent_notion_page_id": None,
            "tracker_node_page_id": "trk-1", "deleted_at": None,
        }]
        client.query_database.side_effect = RuntimeError("tracker boom")
        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = tracker_applier_sync.sync(client, config)
        assert report.errors == 1
        assert any("tracker snapshot failed" in d for d in report.details)
        client.create_page.assert_not_called()
        client.update_page.assert_not_called()

    @patch("src.hierarchy.tracker_applier_sync._http")
    def test_in_sync_workspace_makes_no_writes(self, mock_http):
        config = _make_config()
        client = MagicMock()
        mock_http.return_value = [{
            "notion_page_id": "h-1", "name": "A", "tier": "0. Macro Work Block",
            "active": True, "parent_notion_page_id": None,
            "tracker_node_page_id": "trk-1", "deleted_at": None,
        }]
        client.query_database.return_value = {
            "results": [_tracker_page("trk-1", "A")],
        }
        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = tracker_applier_sync.sync(client, config)
        assert (report.created, report.renamed, report.parent_fixed,
                report.archived, report.errors) == (0, 0, 0, 0, 0)
        client.create_page.assert_not_called()
        client.update_page.assert_not_called()
        # Only one _http call: the canonical GET.
        assert mock_http.call_count == 1

    @patch("src.hierarchy.tracker_applier_sync._http")
    def test_tombstoned_canonical_archives_tracker_and_clears_mapping(
        self, mock_http,
    ):
        """End-to-end: tombstoned canonical → archive the tracker page +
        PATCH Supabase to NULL the tracker_node_page_id. Subsequent ticks
        treat the row as unmapped (CASE T short-circuit), not re-created."""
        config = _make_config()
        client = MagicMock()
        http_calls: list[tuple] = []

        def http(method, path, body=None, prefer="return=minimal"):
            http_calls.append((method, path, body))
            if method == "GET" and "hierarchy_rows" in path:
                return [{
                    "notion_page_id": "h-1", "name": "Sourcing",
                    "tier": "0. Macro Work Block", "active": True,
                    "parent_notion_page_id": None,
                    "tracker_node_page_id": "trk-doomed",
                    "deleted_at": "2026-05-20T07:00:00Z",
                }]
            return None

        mock_http.side_effect = http
        client.query_database.return_value = {
            "results": [_tracker_page("trk-doomed", "Sourcing")],
        }

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = tracker_applier_sync.sync(client, config)

        # Tracker page archived; NOT renamed.
        client.archive_page.assert_called_once_with("trk-doomed")
        client.update_page.assert_not_called()
        client.create_page.assert_not_called()
        assert report.deleted == 1
        assert report.errors == 0
        # Supabase: PATCH cleared tracker_node_page_id to None.
        clears = [
            c for c in http_calls
            if c[0] == "PATCH"
            and "hierarchy_rows" in c[1]
            and c[2].get("tracker_node_page_id") is None
        ]
        assert len(clears) == 1
        assert "h-1" in clears[0][1]

    @patch("src.hierarchy.tracker_applier_sync._http")
    def test_tombstoned_canonical_skips_canonical_clear_on_archive_failure(
        self, mock_http,
    ):
        """If archive_page fails, the canonical mapping must NOT be cleared
        — otherwise next tick treats the row as unmapped and the operator
        loses the recovery path."""
        config = _make_config()
        client = MagicMock()
        http_calls: list[tuple] = []

        def http(method, path, body=None, prefer="return=minimal"):
            http_calls.append((method, path))
            if method == "GET" and "hierarchy_rows" in path:
                return [{
                    "notion_page_id": "h-1", "name": "Sourcing",
                    "tier": "0. Macro Work Block", "active": True,
                    "parent_notion_page_id": None,
                    "tracker_node_page_id": "trk-doomed",
                    "deleted_at": "2026-05-20T07:00:00Z",
                }]
            return None

        mock_http.side_effect = http
        client.query_database.return_value = {
            "results": [_tracker_page("trk-doomed", "Sourcing")],
        }
        client.archive_page.side_effect = RuntimeError("archive boom")

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = tracker_applier_sync.sync(client, config)

        assert report.errors == 1
        # No PATCH to clear the canonical mapping.
        assert not any(c[0] == "PATCH" for c in http_calls)

    @patch("src.hierarchy.tracker_applier_sync._http")
    def test_diverged_title_issues_one_task_patch(self, mock_http):
        config = _make_config()
        client = MagicMock()
        mock_http.return_value = [{
            "notion_page_id": "h-1", "name": "New", "tier": "0. Macro Work Block",
            "active": True, "parent_notion_page_id": None,
            "tracker_node_page_id": "trk-1", "deleted_at": None,
        }]
        client.query_database.return_value = {
            "results": [_tracker_page("trk-1", "Old")],
        }
        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = tracker_applier_sync.sync(client, config)
        client.create_page.assert_not_called()
        client.update_page.assert_called_once()
        kwargs = client.update_page.call_args.kwargs
        assert kwargs["page_id"] == "trk-1"
        assert "Task" in kwargs["properties"]
        assert "Parent item" not in kwargs["properties"]
        assert report.renamed == 1

    @patch("src.hierarchy.tracker_applier_sync._http")
    def test_diverged_parent_issues_one_parent_patch(self, mock_http):
        config = _make_config()
        client = MagicMock()
        mock_http.return_value = [
            {"notion_page_id": "h-p", "name": "Macro", "tier": "0. Macro Work Block",
             "active": True, "parent_notion_page_id": None,
             "tracker_node_page_id": "trk-p", "deleted_at": None},
            {"notion_page_id": "h-1", "name": "Sourcing", "tier": "1. Project",
             "active": True, "parent_notion_page_id": "h-p",
             "tracker_node_page_id": "trk-1", "deleted_at": None},
        ]
        client.query_database.return_value = {
            "results": [
                _tracker_page("trk-p", "Macro"),
                _tracker_page("trk-1", "Sourcing"),
            ],
        }
        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = tracker_applier_sync.sync(client, config)
        client.update_page.assert_called_once()
        kwargs = client.update_page.call_args.kwargs
        assert kwargs["page_id"] == "trk-1"
        assert kwargs["properties"]["Parent item"] == {"relation": [{"id": "trk-p"}]}
        assert "Task" not in kwargs["properties"]
        assert report.parent_fixed == 1

    @patch("src.hierarchy.tracker_applier_sync._http")
    def test_bootstrap_creates_tracker_and_backfills_supabase_and_notion(self, mock_http):
        config = _make_config()
        client = MagicMock()
        client.query_database.return_value = {"results": []}  # empty Tracker
        client.create_page.return_value = {"id": "trk-new", "object": "page"}

        gets = [{
            "notion_page_id": "h-1", "name": "Sourcing", "tier": "0. Macro Work Block",
            "active": True, "parent_notion_page_id": None,
            "tracker_node_page_id": None, "deleted_at": None,
        }]
        http_calls: list[tuple] = []

        def http(method, path, body=None, prefer="return=minimal"):
            http_calls.append((method, path, body))
            if method == "GET":
                return gets
            return None

        mock_http.side_effect = http

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = tracker_applier_sync.sync(client, config)

        # 1 create_page on the Tracker DB.
        client.create_page.assert_called_once()
        c_args, _ = client.create_page.call_args
        assert c_args[0] == "db-tracker"
        assert c_args[1]["Priority"]["select"]["name"] == "[DETAILS INSIDE]"
        assert c_args[1]["Task"]["title"][0]["text"]["content"] == "Sourcing"

        # 1 Supabase PATCH (back-fill tracker_node_page_id).
        patch_calls = [c for c in http_calls if c[0] == "PATCH"]
        assert len(patch_calls) == 1
        assert "notion_page_id=eq.h-1" in patch_calls[0][1]
        assert patch_calls[0][2]["tracker_node_page_id"] == "trk-new"

        # 1 Notion cache writeback on the Hierarchy DB page.
        client.update_page.assert_called_once()
        u_kwargs = client.update_page.call_args.kwargs
        assert u_kwargs["page_id"] == "h-1"
        assert u_kwargs["properties"] == {
            "Tracker Node": {"relation": [{"id": "trk-new"}]},
        }

        assert report.created == 1
        assert report.errors == 0

    @patch("src.hierarchy.tracker_applier_sync._http")
    def test_dry_run_issues_no_writes_but_counts(self, mock_http):
        config = _make_config(dry_run=True)
        client = MagicMock()
        client.query_database.return_value = {
            "results": [_tracker_page("trk-1", "Old Name")],
        }

        def http(method, path, body=None, prefer="return=minimal"):
            if method == "GET":
                return [
                    # needs rename
                    {"notion_page_id": "h-1", "name": "New Name",
                     "tier": "0. Macro Work Block", "active": True,
                     "parent_notion_page_id": None, "tracker_node_page_id": "trk-1",
                     "deleted_at": None},
                    # needs create
                    {"notion_page_id": "h-new", "name": "Brand New",
                     "tier": "0. Macro Work Block", "active": True,
                     "parent_notion_page_id": None, "tracker_node_page_id": None,
                     "deleted_at": None},
                ]
            raise AssertionError(f"Unexpected write: {method} {path}")

        mock_http.side_effect = http

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = tracker_applier_sync.sync(client, config)

        client.create_page.assert_not_called()
        client.update_page.assert_not_called()
        # Only one _http call: the canonical GET.
        assert mock_http.call_count == 1
        assert report.created == 1
        assert report.renamed == 1
        assert any("dry-run" in d for d in report.details)

    @patch("src.hierarchy.tracker_applier_sync._http")
    def test_supabase_backfill_failure_leaks_tracker_and_records_error(self, mock_http):
        config = _make_config()
        client = MagicMock()
        client.query_database.return_value = {"results": []}
        client.create_page.return_value = {"id": "trk-leaked", "object": "page"}

        def http(method, path, body=None, prefer="return=minimal"):
            if method == "GET":
                return [{
                    "notion_page_id": "h-1", "name": "Sourcing",
                    "tier": "0. Macro Work Block", "active": True,
                    "parent_notion_page_id": None,
                    "tracker_node_page_id": None, "deleted_at": None,
                }]
            if method == "PATCH":
                raise RuntimeError("supabase patch boom")
            return None

        mock_http.side_effect = http

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = tracker_applier_sync.sync(client, config)

        client.create_page.assert_called_once()
        # Notion cache writeback MUST NOT happen when Supabase back-fill failed
        # (skip the cache to avoid a pointless write on a broken canonical).
        client.update_page.assert_not_called()
        assert report.errors >= 1
        assert any("trk-leak" in d and "orphan" in d for d in report.details)

    @patch("src.hierarchy.tracker_applier_sync._http")
    def test_notion_cache_writeback_failure_is_warning_not_error(self, mock_http):
        config = _make_config()
        client = MagicMock()
        client.query_database.return_value = {"results": []}
        client.create_page.return_value = {"id": "trk-new", "object": "page"}
        client.update_page.side_effect = RuntimeError("notion patch boom")

        def http(method, path, body=None, prefer="return=minimal"):
            if method == "GET":
                return [{
                    "notion_page_id": "h-1", "name": "Sourcing",
                    "tier": "0. Macro Work Block", "active": True,
                    "parent_notion_page_id": None,
                    "tracker_node_page_id": None, "deleted_at": None,
                }]
            return None

        mock_http.side_effect = http

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = tracker_applier_sync.sync(client, config)

        # Notion cache failed but Supabase canonical write succeeded → no error,
        # only a warning detail. created still counts.
        assert report.created == 1
        assert report.errors == 0
        assert any("Notion Tracker Node cache writeback failed" in d for d in report.details)

    @patch("src.hierarchy.tracker_applier_sync._http")
    def test_one_update_failure_does_not_block_others(self, mock_http):
        config = _make_config()
        client = MagicMock()
        mock_http.return_value = [
            {"notion_page_id": "h-1", "name": "A", "tier": "0. Macro Work Block",
             "active": True, "parent_notion_page_id": None,
             "tracker_node_page_id": "trk-1", "deleted_at": None},
            {"notion_page_id": "h-2", "name": "B", "tier": "0. Macro Work Block",
             "active": True, "parent_notion_page_id": None,
             "tracker_node_page_id": "trk-2", "deleted_at": None},
        ]
        client.query_database.return_value = {
            "results": [
                _tracker_page("trk-1", "Old A"),
                _tracker_page("trk-2", "Old B"),
            ],
        }

        call_count = {"n": 0}

        def update(page_id, properties):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("first PATCH boom")
            return {"id": page_id}

        client.update_page.side_effect = update

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = tracker_applier_sync.sync(client, config)

        assert client.update_page.call_count == 2
        assert report.errors == 1
        assert report.renamed == 1  # only the second one counted
