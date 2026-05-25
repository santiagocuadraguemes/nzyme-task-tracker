"""Tests for src.hierarchy.macro_block_sync (Supabase-canonical-driven applier)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.config import SyncConfig
from src.hierarchy import macro_block_sync
from src.hierarchy.macro_block_sync import (
    _CanonicalTier0Row,
    _Mapping,
    _plan_member_db_update,
    _sanitize_option_name,
)
from src.meeting_db_registry import MeetingDB


def _make_config(**overrides) -> SyncConfig:
    defaults = {
        "notion_api_token": "secret_abc",
        "openai_api_key": "sk-abc",
        "team_tracker_db_id": "db-tracker",
        "org_chart_db_id": "db-org",
        "hierarchy_db_id": "db-hierarchy",
    }
    defaults.update(overrides)
    return SyncConfig(**defaults)


def _crow(
    page_id: str = "h-1",
    name: str = "Sourcing",
    active: bool = True,
    deleted_at: str | None = None,
) -> _CanonicalTier0Row:
    return _CanonicalTier0Row(
        notion_page_id=page_id, name=name, active=active, deleted_at=deleted_at,
    )


def _ds(options: list[dict]) -> dict:
    return {
        "properties": {
            "Work area": {"type": "select", "select": {"options": options}},
        },
    }


def _patch_response(options: list[dict]) -> dict:
    """Notion's update_data_source response: same shape as retrieve_data_source."""
    return _ds(options)


# ---------------------------------------------------------------------------
# TestSanitize
# ---------------------------------------------------------------------------


class TestSanitize:
    def test_dealflow_block_strips_comma(self):
        assert (
            _sanitize_option_name("Sourcing, Investing & Divesting (Dealflow)")
            == "Sourcing Investing & Divesting (Dealflow)"
        )

    def test_ab_inserts_space_never_concatenates(self):
        assert _sanitize_option_name("A,B") == "A B"

    def test_multiple_commas_become_spaces(self):
        assert _sanitize_option_name("A, B, C") == "A B C"

    def test_leading_comma(self):
        assert _sanitize_option_name(",X") == "X"

    def test_trailing_comma(self):
        assert _sanitize_option_name("X,") == "X"

    def test_whitespace_trim(self):
        assert _sanitize_option_name("  trailing  ") == "trailing"

    def test_no_comma_is_idempotent(self):
        assert _sanitize_option_name("no comma") == "no comma"

    def test_empty_string(self):
        assert _sanitize_option_name("") == ""


# ---------------------------------------------------------------------------
# TestPlanMemberDbUpdate
# ---------------------------------------------------------------------------


class TestPlanMemberDbUpdate:
    def test_noop_when_mapping_and_name_match(self):
        rows = [_crow("h-1", "Sourcing")]
        mappings = {
            "h-1": _Mapping("h-1", "mdb-1", "opt-1", "Sourcing"),
        }
        current = [{"id": "opt-1", "name": "Sourcing"}]
        plan = _plan_member_db_update(rows, mappings, current, "mdb-1")
        assert plan.changed is False
        assert plan.created == 0
        assert plan.renamed == 0
        # Mapping refreshed for last_synced_at freshness.
        assert len(plan.mapping_writes) == 1
        assert plan.mapping_writes[0].option_id == "opt-1"

    def test_bootstrap_create_no_mapping_no_existing(self):
        rows = [_crow("h-1", "Sourcing")]
        plan = _plan_member_db_update(rows, {}, [], "mdb-1")
        assert plan.changed is True
        assert plan.created == 1
        assert plan.renamed == 0
        # Placeholder mapping with empty option_id, to be back-filled.
        assert len(plan.mapping_writes) == 1
        assert plan.mapping_writes[0].option_id == ""
        assert plan.mapping_writes[0].option_name == "Sourcing"
        assert plan.new_options == [{"name": "Sourcing"}]

    def test_bootstrap_adopt_clean_no_rename(self):
        rows = [_crow("h-1", "Sourcing")]
        current = [{"id": "opt-1", "name": "Sourcing"}]
        plan = _plan_member_db_update(rows, {}, current, "mdb-1")
        # Adopted the existing option; no rename.
        assert plan.changed is False
        assert plan.renamed == 0
        assert plan.created == 0
        assert len(plan.mapping_writes) == 1
        assert plan.mapping_writes[0].option_id == "opt-1"
        assert plan.mapping_writes[0].option_name == "Sourcing"

    def test_bootstrap_adopt_with_comma_cleanup(self):
        rows = [_crow("h-1", "Sourcing, Investing & Divesting (Dealflow)")]
        current = [{
            "id": "opt-1",
            "name": "Sourcing, Investing & Divesting (Dealflow)",
        }]
        plan = _plan_member_db_update(rows, {}, current, "mdb-1")
        # Match by sanitized name, then emit a rename intent for the saga
        # to strip the comma. The planner leaves the OLD id on the entry;
        # the I/O layer swaps it to the saga's new id after PATCH 1 runs.
        assert plan.changed is True
        assert plan.renamed == 1
        assert plan.created == 0
        assert plan.new_options == [{
            "id": "opt-1",
            "name": "Sourcing Investing & Divesting (Dealflow)",
        }]
        # Mapping placeholder — option_id back-filled after the saga.
        assert plan.mapping_writes[0].option_id == ""
        assert (
            plan.mapping_writes[0].option_name
            == "Sourcing Investing & Divesting (Dealflow)"
        )
        # Rename intent emitted for the saga to execute.
        assert len(plan.renames) == 1
        intent = plan.renames[0]
        assert intent.old_option_id == "opt-1"
        assert intent.old_name == "Sourcing, Investing & Divesting (Dealflow)"
        assert intent.desired_name == "Sourcing Investing & Divesting (Dealflow)"
        assert intent.canonical_id == "h-1"

    def test_rename_preserves_existing_option_color(self):
        """Work area color is NOT canonical-driven — the saga must carry the
        existing option's color through to the new option, otherwise the
        tag's color visually resets on rename."""
        rows = [_crow("h-1", "WWW Sourcing")]
        mappings = {"h-1": _Mapping("h-1", "mdb-1", "opt-1", "Sourcing")}
        current = [{"id": "opt-1", "name": "Sourcing", "color": "orange"}]
        plan = _plan_member_db_update(rows, mappings, current, "mdb-1")
        assert len(plan.renames) == 1
        assert plan.renames[0].desired_color == "orange"

    def test_adopt_and_rename_preserves_existing_option_color(self):
        """Bootstrap-adopt + rename (comma cleanup) carries the adopted
        option's color into the saga."""
        rows = [_crow("h-1", "Sourcing, Investing & Divesting (Dealflow)")]
        current = [{
            "id": "opt-1",
            "name": "Sourcing, Investing & Divesting (Dealflow)",
            "color": "blue",
        }]
        plan = _plan_member_db_update(rows, {}, current, "mdb-1")
        assert len(plan.renames) == 1
        assert plan.renames[0].desired_color == "blue"

    def test_rename_via_mapping_emits_intent_with_placeholder_mapping(self):
        """Name change emits a saga intent — Notion's PATCH cannot rename in
        place. The planner leaves the OLD id on the entry; the I/O layer
        swaps it for the saga-assigned new id after PATCH 1 runs."""
        rows = [_crow("h-1", "WWW Sourcing")]
        mappings = {
            "h-1": _Mapping("h-1", "mdb-1", "opt-1", "Sourcing"),
        }
        current = [{"id": "opt-1", "name": "Sourcing"}]
        plan = _plan_member_db_update(rows, mappings, current, "mdb-1")
        assert plan.changed is True
        assert plan.renamed == 1
        assert plan.created == 0
        assert plan.new_options == [{"id": "opt-1", "name": "WWW Sourcing"}]
        # Placeholder mapping; back-filled with the saga's new id post-run.
        assert plan.mapping_writes[0].option_id == ""
        assert plan.mapping_writes[0].option_name == "WWW Sourcing"
        # Rename intent emitted.
        assert len(plan.renames) == 1
        intent = plan.renames[0]
        assert intent.old_option_id == "opt-1"
        assert intent.old_name == "Sourcing"
        assert intent.desired_name == "WWW Sourcing"
        assert intent.canonical_id == "h-1"

    def test_archive_live_inactive(self):
        rows = [_crow("h-1", "Sourcing", active=False)]
        mappings = {
            "h-1": _Mapping("h-1", "mdb-1", "opt-1", "Sourcing"),
        }
        current = [{"id": "opt-1", "name": "Sourcing"}]
        plan = _plan_member_db_update(rows, mappings, current, "mdb-1")
        assert plan.changed is True
        assert plan.renamed == 1
        assert plan.archived == 1
        assert plan.new_options == [
            {"id": "opt-1", "name": "(archived) Sourcing"},
        ]

    def test_reactivate_strips_archived_prefix(self):
        rows = [_crow("h-1", "Sourcing", active=True)]
        mappings = {
            "h-1": _Mapping("h-1", "mdb-1", "opt-1", "(archived) Sourcing"),
        }
        current = [{"id": "opt-1", "name": "(archived) Sourcing"}]
        plan = _plan_member_db_update(rows, mappings, current, "mdb-1")
        assert plan.changed is True
        assert plan.renamed == 1
        # archived counter NOT incremented when going (archived) → bare.
        assert plan.archived == 0
        assert plan.new_options == [{"id": "opt-1", "name": "Sourcing"}]

    def test_mapping_stale_falls_through_to_bootstrap(self):
        """option_id in mapping but no longer in current options."""
        rows = [_crow("h-1", "Sourcing")]
        mappings = {
            "h-1": _Mapping("h-1", "mdb-1", "opt-ghost", "Sourcing"),
        }
        plan = _plan_member_db_update(rows, mappings, [], "mdb-1")
        # Falls through to bootstrap-create; stale mapping NOT carried forward.
        assert plan.created == 1
        assert plan.changed is True
        # Mapping_writes carries ONLY the placeholder for the bootstrap create.
        assert len(plan.mapping_writes) == 1
        assert plan.mapping_writes[0].option_id == ""
        assert any("falling back to bootstrap" in d for d in plan.details)

    def test_legacy_options_pass_through(self):
        """Standup / 1:1 must not be touched by Tier 0 planning."""
        rows = [_crow("h-1", "Sourcing")]
        current = [
            {"id": "opt-a", "name": "Standup", "color": "default"},
            {"id": "opt-b", "name": "1:1", "color": "default"},
        ]
        plan = _plan_member_db_update(rows, {}, current, "mdb-1")
        assert plan.changed is True
        # Legacy options first (carried through verbatim), new Sourcing appended.
        assert plan.new_options == [
            {"id": "opt-a", "name": "Standup", "color": "default"},
            {"id": "opt-b", "name": "1:1", "color": "default"},
            {"name": "Sourcing"},
        ]

    def test_collision_skip_excludes_rows(self):
        """skip_page_ids excludes colliding rows from per-member planning."""
        rows = [
            _crow("h-1", "Same"),
            _crow("h-2", "Same"),
        ]
        plan = _plan_member_db_update(
            rows, {}, [], "mdb-1", skip_page_ids={"h-1", "h-2"},
        )
        assert plan.created == 0
        assert plan.changed is False

    def test_tombstoned_row_drops_option_and_queues_mapping_delete(self):
        """A tombstoned canonical row should REMOVE the option from the
        member DB (DropIntent), not archive it. Mapping is also queued for
        Supabase DELETE so the next tick doesn't re-process the row."""
        rows = [_crow("h-1", "Sourcing", deleted_at="2026-05-19T07:00:00Z")]
        mappings = {
            "h-1": _Mapping("h-1", "mdb-1", "opt-1", "Sourcing"),
        }
        current = [{"id": "opt-1", "name": "Sourcing"}]
        plan = _plan_member_db_update(rows, mappings, current, "mdb-1")
        assert plan.deleted == 1
        assert plan.archived == 0
        assert plan.renamed == 0
        # The option is dropped from out (the I/O drop saga removes it from
        # Notion).
        assert plan.new_options == []
        # DropIntent emitted with the right metadata.
        assert len(plan.drops) == 1
        drop = plan.drops[0]
        assert drop.old_option_id == "opt-1"
        assert drop.old_name == "Sourcing"
        assert drop.canonical_id == "h-1"
        # Mapping row queued for DELETE.
        assert plan.mapping_deletes == ["h-1"]
        # No mapping_writes for tombstoned rows.
        assert plan.mapping_writes == []

    def test_tombstoned_row_with_stale_mapping_just_deletes_mapping(self):
        """Tombstoned canonical + mapping pointing at gone option →
        no drop saga needed, just clean up the stale mapping row."""
        rows = [_crow("h-1", "Sourcing", deleted_at="2026-05-19T07:00:00Z")]
        mappings = {
            "h-1": _Mapping("h-1", "mdb-1", "opt-gone", "Sourcing"),
        }
        plan = _plan_member_db_update(rows, mappings, [], "mdb-1")
        assert plan.drops == []
        assert plan.mapping_deletes == ["h-1"]
        assert plan.deleted == 0  # nothing to drop on Notion side
        assert plan.changed is False

    def test_tombstoned_row_with_no_mapping_is_noop(self):
        """Tombstoned canonical that was never mapped → no work."""
        rows = [_crow("h-1", "Sourcing", deleted_at="2026-05-19T07:00:00Z")]
        plan = _plan_member_db_update(rows, {}, [], "mdb-1")
        assert plan.drops == []
        assert plan.mapping_deletes == []
        assert plan.deleted == 0
        # CRITICAL: no bootstrap-create for a tombstoned row.
        assert plan.created == 0
        assert plan.changed is False

    def test_inactive_but_not_tombstoned_still_archives(self):
        """Inactive (active=False, deleted_at=None) → still archive in place
        via saga rename to '(archived) X'. Drop behavior is reserved for
        truly tombstoned rows."""
        rows = [_crow("h-1", "Sourcing", active=False, deleted_at=None)]
        mappings = {"h-1": _Mapping("h-1", "mdb-1", "opt-1", "Sourcing")}
        current = [{"id": "opt-1", "name": "Sourcing"}]
        plan = _plan_member_db_update(rows, mappings, current, "mdb-1")
        # Rename intent (the saga renames to "(archived) Sourcing"),
        # NOT a drop intent.
        assert plan.archived == 1
        assert plan.renamed == 1
        assert plan.deleted == 0
        assert len(plan.renames) == 1
        assert plan.drops == []
        assert plan.renames[0].desired_name == "(archived) Sourcing"


# ---------------------------------------------------------------------------
# TestSync
# ---------------------------------------------------------------------------


class TestSync:
    def test_aborts_when_org_chart_unset(self):
        config = _make_config(org_chart_db_id=None)
        client = MagicMock()
        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = macro_block_sync.sync(client, config)
        assert report.errors == 1
        assert "ORG_CHART_DB_ID" in report.details[0]
        client.query_database.assert_not_called()

    def test_aborts_when_supabase_env_missing(self):
        config = _make_config()
        client = MagicMock()
        with patch.dict("os.environ", {}, clear=True):
            report = macro_block_sync.sync(client, config)
        assert report.errors == 1
        assert "Supabase" in report.details[0]
        client.retrieve_data_source.assert_not_called()

    @patch("src.hierarchy.macro_block_sync.discover_meeting_dbs")
    @patch("src.hierarchy.macro_block_sync._http")
    def test_empty_canonical_returns_with_warning_not_error(
        self, mock_http, mock_discover,
    ):
        """PR1 mirror hasn't run yet → benign skip, NOT an error.
        discover_meeting_dbs MUST NOT be called."""
        config = _make_config()
        client = MagicMock()
        mock_http.return_value = []  # canonical empty
        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = macro_block_sync.sync(client, config)
        assert report.errors == 0
        assert any("canonical Tier 0 empty" in d for d in report.details)
        mock_discover.assert_not_called()
        client.retrieve_data_source.assert_not_called()
        client.update_data_source.assert_not_called()

    @patch("src.hierarchy.macro_block_sync.discover_meeting_dbs")
    @patch("src.hierarchy.macro_block_sync._http")
    def test_retrieve_data_source_failure_records_error_others_proceed(
        self, mock_http, mock_discover,
    ):
        config = _make_config()
        client = MagicMock()

        def http(method, path, body=None, prefer="return=minimal"):
            if method == "GET" and "hierarchy_rows" in path:
                return [{
                    "notion_page_id": "h-1", "name": "Sourcing",
                    "active": True, "deleted_at": None,
                }]
            if method == "GET" and "work_area_option_mappings" in path:
                return []
            return None

        mock_http.side_effect = http

        # First member explodes; second succeeds.
        client.retrieve_data_source.side_effect = [
            RuntimeError("boom"),
            _ds([]),
        ]
        client.update_data_source.return_value = _patch_response(
            [{"id": "opt-new", "name": "Sourcing"}],
        )
        mock_discover.return_value = [
            MeetingDB(db_id="mdb-a", owner_name="A", owner_email=""),
            MeetingDB(db_id="mdb-b", owner_name="B", owner_email=""),
        ]

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = macro_block_sync.sync(client, config)

        assert report.errors == 1
        # Second member got the PATCH.
        client.update_data_source.assert_called_once()
        args, _ = client.update_data_source.call_args
        assert args[0] == "mdb-b"

    @patch("src.hierarchy.macro_block_sync.discover_meeting_dbs")
    @patch("src.hierarchy.macro_block_sync._http")
    def test_update_data_source_failure_records_error_skip_mapping_upsert(
        self, mock_http, mock_discover,
    ):
        config = _make_config()
        client = MagicMock()

        http_calls: list[tuple[str, str]] = []

        def http(method, path, body=None, prefer="return=minimal"):
            http_calls.append((method, path))
            if method == "GET" and "hierarchy_rows" in path:
                return [{
                    "notion_page_id": "h-1", "name": "Sourcing",
                    "active": True, "deleted_at": None,
                }]
            if method == "GET" and "work_area_option_mappings" in path:
                return []
            raise AssertionError(f"Unexpected: {method} {path}")

        mock_http.side_effect = http

        client.retrieve_data_source.return_value = _ds([])
        client.update_data_source.side_effect = RuntimeError("patch boom")
        mock_discover.return_value = [
            MeetingDB(db_id="mdb-1", owner_name="A", owner_email=""),
        ]

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = macro_block_sync.sync(client, config)

        assert report.errors == 1
        # Mapping POST MUST NOT have happened — only the two GETs.
        posts = [c for c in http_calls if c[0] == "POST"]
        assert posts == []

    @patch("src.hierarchy.macro_block_sync.discover_meeting_dbs")
    @patch("src.hierarchy.macro_block_sync._http")
    def test_successful_patch_then_mapping_upsert(
        self, mock_http, mock_discover,
    ):
        config = _make_config()
        client = MagicMock()

        http_calls: list[tuple] = []

        def http(method, path, body=None, prefer="return=minimal"):
            http_calls.append((method, path, body))
            if method == "GET" and "hierarchy_rows" in path:
                return [{
                    "notion_page_id": "h-1", "name": "Sourcing",
                    "active": True, "deleted_at": None,
                }]
            if method == "GET" and "work_area_option_mappings" in path:
                return []
            return None

        mock_http.side_effect = http

        client.retrieve_data_source.return_value = _ds([])
        # Notion assigns id "opt-new" to the freshly-created option.
        client.update_data_source.return_value = _patch_response(
            [{"id": "opt-new", "name": "Sourcing"}],
        )
        mock_discover.return_value = [
            MeetingDB(db_id="mdb-1", owner_name="A", owner_email=""),
        ]

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = macro_block_sync.sync(client, config)

        assert report.created == 1
        assert report.errors == 0
        # Exactly one POST to the mapping table.
        posts = [c for c in http_calls if c[0] == "POST"]
        assert len(posts) == 1
        assert "work_area_option_mappings" in posts[0][1]
        # Back-fill carried the real Notion-assigned id.
        body = posts[0][2]
        assert body[0]["option_id"] == "opt-new"
        assert body[0]["option_name"] == "Sourcing"
        assert body[0]["hierarchy_page_id"] == "h-1"
        assert body[0]["member_db_id"] == "mdb-1"

    @patch("src.hierarchy.macro_block_sync.discover_meeting_dbs")
    @patch("src.hierarchy.macro_block_sync._http")
    def test_mapping_upsert_failure_records_error_with_recovery_detail(
        self, mock_http, mock_discover,
    ):
        config = _make_config()
        client = MagicMock()

        def http(method, path, body=None, prefer="return=minimal"):
            if method == "GET" and "hierarchy_rows" in path:
                return [{
                    "notion_page_id": "h-1", "name": "Sourcing",
                    "active": True, "deleted_at": None,
                }]
            if method == "GET" and "work_area_option_mappings" in path:
                return []
            if method == "POST" and "work_area_option_mappings" in path:
                raise RuntimeError("mapping boom")
            return None

        mock_http.side_effect = http

        client.retrieve_data_source.return_value = _ds([])
        client.update_data_source.return_value = _patch_response(
            [{"id": "opt-new", "name": "Sourcing"}],
        )
        mock_discover.return_value = [
            MeetingDB(db_id="mdb-1", owner_name="A", owner_email=""),
        ]

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = macro_block_sync.sync(client, config)

        # The Notion PATCH succeeded but the back-fill upsert failed.
        client.update_data_source.assert_called_once()
        assert report.errors == 1
        assert any(
            "mapping back-fill failed" in d and "next run will adopt" in d
            for d in report.details
        )

    @patch("src.hierarchy.macro_block_sync.discover_meeting_dbs")
    @patch("src.hierarchy.macro_block_sync._http")
    def test_sanitized_name_collision_skips_both_rows(
        self, mock_http, mock_discover,
    ):
        config = _make_config()
        client = MagicMock()

        def http(method, path, body=None, prefer="return=minimal"):
            if method == "GET" and "hierarchy_rows" in path:
                return [
                    {"notion_page_id": "h-1", "name": "A,B",
                     "active": True, "deleted_at": None},
                    {"notion_page_id": "h-2", "name": "A B",
                     "active": True, "deleted_at": None},
                ]
            if method == "GET" and "work_area_option_mappings" in path:
                return []
            raise AssertionError(f"Unexpected write: {method} {path}")

        mock_http.side_effect = http

        client.retrieve_data_source.return_value = _ds([])
        mock_discover.return_value = [
            MeetingDB(db_id="mdb-1", owner_name="A", owner_email=""),
        ]

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = macro_block_sync.sync(client, config)

        # Exactly one collision error; both rows skipped → no PATCH.
        assert report.errors == 1
        client.update_data_source.assert_not_called()
        assert any("sanitized-name collision" in d for d in report.details)

    @patch("src.hierarchy.macro_block_sync.discover_meeting_dbs")
    @patch("src.hierarchy.macro_block_sync._http")
    def test_dry_run_issues_no_writes_but_counts(self, mock_http, mock_discover):
        config = _make_config(dry_run=True)
        client = MagicMock()

        def http(method, path, body=None, prefer="return=minimal"):
            if method == "GET" and "hierarchy_rows" in path:
                return [
                    {"notion_page_id": "h-1", "name": "Sourcing",
                     "active": True, "deleted_at": None},
                    {"notion_page_id": "h-2", "name": "Inactive",
                     "active": False, "deleted_at": None},
                ]
            if method == "GET" and "work_area_option_mappings" in path:
                return [{
                    "hierarchy_page_id": "h-2", "member_db_id": "mdb-1",
                    "option_id": "opt-i", "option_name": "Inactive",
                }]
            raise AssertionError(f"Unexpected write: {method} {path}")

        mock_http.side_effect = http

        client.retrieve_data_source.return_value = _ds(
            [{"id": "opt-i", "name": "Inactive"}],
        )
        mock_discover.return_value = [
            MeetingDB(db_id="mdb-1", owner_name="A", owner_email=""),
        ]

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = macro_block_sync.sync(client, config)

        client.update_data_source.assert_not_called()
        assert report.created == 1
        assert report.renamed == 1  # Inactive → (archived) Inactive
        assert report.archived == 1
        assert any("dry-run" in d for d in report.details)

    @patch("src.hierarchy.macro_block_sync.discover_meeting_dbs")
    @patch("src.hierarchy.macro_block_sync._http")
    def test_one_member_in_sync_only_other_gets_patch(
        self, mock_http, mock_discover,
    ):
        """Member A is in-sync; member B needs a create → exactly one PATCH."""
        config = _make_config()
        client = MagicMock()

        def http(method, path, body=None, prefer="return=minimal"):
            if method == "GET" and "hierarchy_rows" in path:
                return [{
                    "notion_page_id": "h-1", "name": "Sourcing",
                    "active": True, "deleted_at": None,
                }]
            if method == "GET" and "work_area_option_mappings" in path:
                # Only mdb-a has the option mapped.
                return [{
                    "hierarchy_page_id": "h-1", "member_db_id": "mdb-a",
                    "option_id": "opt-a", "option_name": "Sourcing",
                }]
            return None

        mock_http.side_effect = http

        client.retrieve_data_source.side_effect = [
            _ds([{"id": "opt-a", "name": "Sourcing"}]),
            _ds([]),
        ]
        client.update_data_source.return_value = _patch_response(
            [{"id": "opt-new", "name": "Sourcing"}],
        )
        mock_discover.return_value = [
            MeetingDB(db_id="mdb-a", owner_name="A", owner_email=""),
            MeetingDB(db_id="mdb-b", owner_name="B", owner_email=""),
        ]

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = macro_block_sync.sync(client, config)

        client.update_data_source.assert_called_once()
        args, _ = client.update_data_source.call_args
        assert args[0] == "mdb-b"
        assert report.created == 1
        assert report.errors == 0

    @patch("src.hierarchy.macro_block_sync.discover_meeting_dbs")
    @patch("src.hierarchy.macro_block_sync._http")
    def test_bootstrap_adopt_with_comma_cleanup_runs_saga(
        self, mock_http, mock_discover,
    ):
        """Canonical has commas; current option has commas; no mapping →
        after run, the saga has executed (PATCH 1 add new + drop old) and
        the mapping carries the saga's new id."""
        config = _make_config()
        client = MagicMock()

        http_calls: list[tuple] = []

        def http(method, path, body=None, prefer="return=minimal"):
            http_calls.append((method, path, body))
            if method == "GET" and "hierarchy_rows" in path:
                return [{
                    "notion_page_id": "h-1",
                    "name": "Sourcing, Investing & Divesting (Dealflow)",
                    "active": True, "deleted_at": None,
                }]
            if method == "GET" and "work_area_option_mappings" in path:
                return []
            return None

        mock_http.side_effect = http

        client.retrieve_data_source.return_value = _ds([{
            "id": "opt-1",
            "name": "Sourcing, Investing & Divesting (Dealflow)",
        }])
        # Saga PATCH 1: response carries new option with assigned id "opt-new".
        # Saga PATCH 2: response after drop. No final PATCH needed (post-saga
        # state matches the planner's desired state).
        client.update_data_source.side_effect = [
            _patch_response([
                {"id": "opt-1",
                 "name": "Sourcing, Investing & Divesting (Dealflow)"},
                {"id": "opt-new",
                 "name": "Sourcing Investing & Divesting (Dealflow)"},
            ]),
            _patch_response([
                {"id": "opt-new",
                 "name": "Sourcing Investing & Divesting (Dealflow)"},
            ]),
        ]
        # No pages tagged on the old option name.
        client.query_database.return_value = {
            "results": [], "has_more": False, "next_cursor": None,
        }
        mock_discover.return_value = [
            MeetingDB(db_id="mdb-1", owner_name="A", owner_email=""),
        ]

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = macro_block_sync.sync(client, config)

        assert report.renamed == 1
        assert report.created == 0
        assert report.errors == 0
        # The saga issued PATCH 1 (add new) then PATCH 2 (drop old); no final
        # PATCH needed because post-saga state already matches plan.new_options
        # after the id swap.
        assert client.update_data_source.call_count == 2
        # PATCH 1 payload appends the comma-free name (no id) to the existing
        # options.
        patch1_args = client.update_data_source.call_args_list[0].args
        patch1_opts = patch1_args[1]["Work area"]["select"]["options"]
        assert patch1_opts[-1] == {
            "name": "Sourcing Investing & Divesting (Dealflow)",
        }
        # PATCH 2 payload omits the old id.
        patch2_args = client.update_data_source.call_args_list[1].args
        patch2_opts = patch2_args[1]["Work area"]["select"]["options"]
        assert "opt-1" not in [o.get("id") for o in patch2_opts]
        # Mapping upsert carries the saga's new id (back-filled from PATCH 1
        # response).
        posts = [c for c in http_calls if c[0] == "POST"]
        assert len(posts) == 1
        assert posts[0][2][0]["option_id"] == "opt-new"
        assert (
            posts[0][2][0]["option_name"]
            == "Sourcing Investing & Divesting (Dealflow)"
        )

    @patch("src.hierarchy.macro_block_sync.discover_meeting_dbs")
    @patch("src.hierarchy.macro_block_sync._http")
    def test_rename_via_saga_swaps_option_id_and_back_fills_mapping(
        self, mock_http, mock_discover,
    ):
        """Live rename: PATCH 1 adds new option → migrate tagged pages →
        PATCH 2 drops old → mapping back-fill carries the saga's new id."""
        config = _make_config()
        client = MagicMock()
        http_calls: list[tuple] = []

        def http(method, path, body=None, prefer="return=minimal"):
            http_calls.append((method, path, body))
            if method == "GET" and "hierarchy_rows" in path:
                return [{
                    "notion_page_id": "h-1", "name": "WWW Sourcing",
                    "active": True, "deleted_at": None,
                }]
            if method == "GET" and "work_area_option_mappings" in path:
                return [{
                    "hierarchy_page_id": "h-1", "member_db_id": "mdb-1",
                    "option_id": "opt-old", "option_name": "Sourcing",
                }]
            return None

        mock_http.side_effect = http

        client.retrieve_data_source.return_value = _ds(
            [{"id": "opt-old", "name": "Sourcing"}],
        )
        # PATCH 1: append new option (Notion assigns "opt-new"). PATCH 2: drop
        # opt-old. No final PATCH (post-saga state matches plan.new_options).
        client.update_data_source.side_effect = [
            _patch_response([
                {"id": "opt-old", "name": "Sourcing"},
                {"id": "opt-new", "name": "WWW Sourcing"},
            ]),
            _patch_response([{"id": "opt-new", "name": "WWW Sourcing"}]),
        ]
        # Two pages tagged on the old option.
        client.query_database.return_value = {
            "results": [
                {"id": "page-A", "properties": {
                    "Work area": {"select": {"id": "opt-old", "name": "Sourcing"}},
                }},
                {"id": "page-B", "properties": {
                    "Work area": {"select": {"id": "opt-old", "name": "Sourcing"}},
                }},
            ],
            "has_more": False, "next_cursor": None,
        }
        mock_discover.return_value = [
            MeetingDB(db_id="mdb-1", owner_name="A", owner_email=""),
        ]

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = macro_block_sync.sync(client, config)

        assert report.renamed == 1
        assert report.errors == 0
        # Both pages migrated to the new option id.
        assert client.update_page.call_count == 2
        call_args = [c.kwargs for c in client.update_page.call_args_list]
        assert all(
            ca["properties"]["Work area"]["select"]["id"] == "opt-new"
            for ca in call_args
        )
        # Mapping upsert carries the new id (NOT the old one).
        posts = [c for c in http_calls if c[0] == "POST"]
        assert len(posts) == 1
        assert posts[0][2][0]["option_id"] == "opt-new"
        assert posts[0][2][0]["option_name"] == "WWW Sourcing"

    @patch("src.hierarchy.macro_block_sync.discover_meeting_dbs")
    @patch("src.hierarchy.macro_block_sync._http")
    def test_saga_resume_finishes_partial_rename_without_creating_duplicates(
        self, mock_http, mock_discover,
    ):
        """Mid-saga state: prior tick ran PATCH 1; both old + new options
        exist. Next tick must finish (migrate + PATCH 2) without re-adding."""
        config = _make_config()
        client = MagicMock()
        http_calls: list[tuple] = []

        def http(method, path, body=None, prefer="return=minimal"):
            http_calls.append((method, path, body))
            if method == "GET" and "hierarchy_rows" in path:
                return [{
                    "notion_page_id": "h-1", "name": "WWW Sourcing",
                    "active": True, "deleted_at": None,
                }]
            if method == "GET" and "work_area_option_mappings" in path:
                return [{
                    "hierarchy_page_id": "h-1", "member_db_id": "mdb-1",
                    "option_id": "opt-old", "option_name": "Sourcing",
                }]
            return None

        mock_http.side_effect = http

        # Current state: both old AND new option are present (mid-saga).
        client.retrieve_data_source.return_value = _ds([
            {"id": "opt-old", "name": "Sourcing"},
            {"id": "opt-new", "name": "WWW Sourcing"},
        ])
        # Only PATCH 2 (drop old) should be issued — PATCH 1 is resumed.
        client.update_data_source.return_value = _patch_response(
            [{"id": "opt-new", "name": "WWW Sourcing"}],
        )
        # No pages still tagged on the old (already migrated last tick).
        client.query_database.return_value = {
            "results": [], "has_more": False, "next_cursor": None,
        }
        mock_discover.return_value = [
            MeetingDB(db_id="mdb-1", owner_name="A", owner_email=""),
        ]

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = macro_block_sync.sync(client, config)

        assert report.errors == 0
        # Exactly ONE update_data_source call (PATCH 2 only — PATCH 1 skipped).
        assert client.update_data_source.call_count == 1
        # Mapping back-filled with the resume-detected new id.
        posts = [c for c in http_calls if c[0] == "POST"]
        assert posts[0][2][0]["option_id"] == "opt-new"

    @patch("src.hierarchy.macro_block_sync.discover_meeting_dbs")
    @patch("src.hierarchy.macro_block_sync._http")
    def test_tombstoned_row_drops_option_clears_tags_and_deletes_mapping(
        self, mock_http, mock_discover,
    ):
        """End-to-end: tombstoned canonical → drop saga clears tagged pages
        → drops the option on Notion → DELETEs the mapping in Supabase."""
        config = _make_config()
        client = MagicMock()
        http_calls: list[tuple] = []

        def http(method, path, body=None, prefer="return=minimal"):
            http_calls.append((method, path))
            if method == "GET" and "hierarchy_rows" in path:
                return [{
                    "notion_page_id": "h-1", "name": "Sourcing",
                    "active": True, "deleted_at": "2026-05-20T07:00:00Z",
                }]
            if method == "GET" and "work_area_option_mappings" in path:
                return [{
                    "hierarchy_page_id": "h-1", "member_db_id": "mdb-1",
                    "option_id": "opt-gone", "option_name": "Sourcing",
                }]
            return None

        mock_http.side_effect = http
        client.retrieve_data_source.return_value = _ds([
            {"id": "opt-gone", "name": "Sourcing"},
            {"id": "opt-keep", "name": "Standup"},
        ])
        # Drop saga PATCH: array minus opt-gone.
        client.update_data_source.return_value = _patch_response(
            [{"id": "opt-keep", "name": "Standup"}],
        )
        # One page tagged on the doomed option.
        client.query_database.return_value = {
            "results": [{
                "id": "page-1",
                "properties": {
                    "Work area": {"select": {"id": "opt-gone", "name": "Sourcing"}},
                },
            }],
            "has_more": False, "next_cursor": None,
        }
        mock_discover.return_value = [
            MeetingDB(db_id="mdb-1", owner_name="A", owner_email=""),
        ]

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = macro_block_sync.sync(client, config)

        assert report.deleted == 1
        assert report.archived == 0
        assert report.errors == 0
        # Page was cleared (select → None).
        client.update_page.assert_called_once()
        kwargs = client.update_page.call_args.kwargs
        assert kwargs["page_id"] == "page-1"
        assert kwargs["properties"] == {"Work area": {"select": None}}
        # PATCH dropped opt-gone.
        client.update_data_source.assert_called_once()
        patch_opts = (
            client.update_data_source.call_args.args[1]
            ["Work area"]["select"]["options"]
        )
        assert "opt-gone" not in [o.get("id") for o in patch_opts]
        # Supabase: DELETE on work_area_option_mappings was issued.
        deletes = [c for c in http_calls if c[0] == "DELETE"]
        assert len(deletes) == 1
        assert "work_area_option_mappings" in deletes[0][1]
        assert "mdb-1" in deletes[0][1]
        assert "h-1" in deletes[0][1]

    @patch("src.hierarchy.macro_block_sync.discover_meeting_dbs")
    @patch("src.hierarchy.macro_block_sync._http")
    def test_member_without_work_area_property_records_error(
        self, mock_http, mock_discover,
    ):
        config = _make_config()
        client = MagicMock()

        def http(method, path, body=None, prefer="return=minimal"):
            if method == "GET" and "hierarchy_rows" in path:
                return [{
                    "notion_page_id": "h-1", "name": "Sourcing",
                    "active": True, "deleted_at": None,
                }]
            if method == "GET" and "work_area_option_mappings" in path:
                return []
            raise AssertionError(f"Unexpected write: {method} {path}")

        mock_http.side_effect = http

        client.retrieve_data_source.return_value = {"properties": {}}
        mock_discover.return_value = [
            MeetingDB(db_id="mdb-1", owner_name="A", owner_email=""),
        ]

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = macro_block_sync.sync(client, config)

        assert report.errors == 1
        client.update_data_source.assert_not_called()
