"""Tests for src.hierarchy.detail_applier_sync (multi-select canonical-driven applier)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.config import SyncConfig
from src.hierarchy import detail_applier_sync
from src.hierarchy.detail_applier_sync import (
    _CanonicalDetailRow,
    _Mapping,
    _plan_member_db_update,
)
from src.meeting_db_registry import MeetingDB


def _make_config(**overrides) -> SyncConfig:
    defaults = {
        "notion_api_token": "secret_abc",
        "openai_api_key": "sk-abc",
        "team_tracker_db_id": "db-tracker",
        "merged_transcript_extraction_prompt_page_id": "page-merged",
        "org_chart_db_id": "db-org",
    }
    defaults.update(overrides)
    return SyncConfig(**defaults)


def _drow(
    page_id: str = "d-1",
    name: str = "Legal DD",
    color: str = "blue",
    active: bool = True,
    deleted_at: str | None = None,
) -> _CanonicalDetailRow:
    return _CanonicalDetailRow(
        notion_page_id=page_id, name=name, color=color,
        active=active, deleted_at=deleted_at,
    )


def _ds(options: list[dict]) -> dict:
    return {
        "properties": {
            "Detail": {"type": "multi_select", "multi_select": {"options": options}},
        },
    }


def _patch_response(options: list[dict]) -> dict:
    return _ds(options)


class TestPlanMemberDbUpdate:
    def test_noop_when_mapping_name_and_color_match(self):
        rows = [_drow("d-1", "Legal DD", "blue")]
        mappings = {"d-1": _Mapping("d-1", "mdb-1", "opt-1", "Legal DD")}
        current = [{"id": "opt-1", "name": "Legal DD", "color": "blue"}]
        plan = _plan_member_db_update(rows, mappings, current, "mdb-1")
        assert plan.changed is False
        assert plan.renamed == 0
        assert plan.created == 0

    def test_color_only_change_is_ignored_not_recolored(self):
        """Notion's API can't recolor an existing option (400). A color-only
        drift on an existing option must be left untouched — no recolor, no
        change — so we never emit a doomed PATCH."""
        rows = [_drow("d-1", "Legal DD", "green")]
        mappings = {"d-1": _Mapping("d-1", "mdb-1", "opt-1", "Legal DD")}
        current = [{"id": "opt-1", "name": "Legal DD", "color": "blue"}]
        plan = _plan_member_db_update(rows, mappings, current, "mdb-1")
        assert plan.changed is False
        assert plan.renamed == 0
        # Option keeps its existing (blue) color — canonical green is NOT forced.
        assert plan.new_options == [
            {"id": "opt-1", "name": "Legal DD", "color": "blue"},
        ]

    def test_name_change_via_mapping_preserves_id(self):
        rows = [_drow("d-1", "Legal Due Diligence", "blue")]
        mappings = {"d-1": _Mapping("d-1", "mdb-1", "opt-1", "Legal DD")}
        current = [{"id": "opt-1", "name": "Legal DD", "color": "blue"}]
        plan = _plan_member_db_update(rows, mappings, current, "mdb-1")
        assert plan.changed is True
        assert plan.renamed == 1
        assert plan.new_options[0]["id"] == "opt-1"
        assert plan.new_options[0]["name"] == "Legal Due Diligence"

    def test_bootstrap_create_with_color(self):
        rows = [_drow("d-1", "New Topic", "orange")]
        plan = _plan_member_db_update(rows, {}, [], "mdb-1")
        assert plan.created == 1
        assert plan.changed is True
        assert plan.new_options == [{"name": "New Topic", "color": "orange"}]
        assert plan.mapping_writes[0].option_id == ""

    def test_bootstrap_adopt_by_sanitized_name_ignores_color_drift(self):
        """Adopt an existing same-name option by id, but do NOT recolor it —
        Notion forbids recoloring an existing option."""
        rows = [_drow("d-1", "Legal DD", "blue")]
        current = [{"id": "opt-1", "name": "Legal DD", "color": "default"}]
        plan = _plan_member_db_update(rows, {}, current, "mdb-1")
        assert plan.changed is False
        assert plan.renamed == 0
        # Keeps its existing color; mapping is still recorded (adoption).
        assert plan.new_options[0]["color"] == "default"
        assert plan.mapping_writes[0].option_id == "opt-1"

    def test_archive_when_active_false(self):
        rows = [_drow("d-1", "Legal DD", "blue", active=False)]
        mappings = {"d-1": _Mapping("d-1", "mdb-1", "opt-1", "Legal DD")}
        current = [{"id": "opt-1", "name": "Legal DD", "color": "blue"}]
        plan = _plan_member_db_update(rows, mappings, current, "mdb-1")
        assert plan.renamed == 1
        assert plan.archived == 1
        assert plan.new_options[0]["name"] == "(archived) Legal DD"

    def test_tombstoned_canonical_drops_option_and_queues_mapping_delete(self):
        """Tombstoned (deleted_at NOT NULL) canonical → drop the multi-select
        option entirely (DropIntent) + queue mapping DELETE. The I/O drop
        saga clears every tagged page's array entry before the option is
        removed."""
        rows = [_drow("d-1", "Legal DD", "blue",
                      deleted_at="2026-05-21T07:00:00Z")]
        mappings = {"d-1": _Mapping("d-1", "mdb-1", "opt-1", "Legal DD")}
        current = [{"id": "opt-1", "name": "Legal DD", "color": "blue"}]
        plan = _plan_member_db_update(rows, mappings, current, "mdb-1")
        assert plan.deleted == 1
        assert plan.archived == 0
        assert plan.renamed == 0
        assert plan.new_options == []
        assert len(plan.drops) == 1
        assert plan.drops[0].old_option_id == "opt-1"
        assert plan.drops[0].old_name == "Legal DD"
        assert plan.mapping_deletes == ["d-1"]
        assert plan.mapping_writes == []

    def test_inactive_but_not_tombstoned_still_archives(self):
        """active=False (no deleted_at) → archive in place, not drop."""
        rows = [_drow("d-1", "Legal DD", "blue", active=False)]
        mappings = {"d-1": _Mapping("d-1", "mdb-1", "opt-1", "Legal DD")}
        current = [{"id": "opt-1", "name": "Legal DD", "color": "blue"}]
        plan = _plan_member_db_update(rows, mappings, current, "mdb-1")
        assert plan.archived == 1
        assert plan.renamed == 1
        assert plan.deleted == 0
        assert plan.drops == []
        assert plan.renames[0].desired_name == "(archived) Legal DD"

    def test_manual_order_preserved_for_existing_options(self):
        """Operator's manual ordering of existing options is never disturbed:
        renames / color updates / adoptions edit options in-place; no
        post-pass reorder reshuffles them."""
        rows = [
            _drow("d-1", "AI & Tech", "green"),
            _drow("d-2", "Legal DD",  "blue"),
            _drow("d-3", "Operations", "orange"),
        ]
        mappings = {
            "d-1": _Mapping("d-1", "mdb-1", "opt-1", "AI & Tech"),
            "d-2": _Mapping("d-2", "mdb-1", "opt-2", "Legal DD"),
            "d-3": _Mapping("d-3", "mdb-1", "opt-3", "Operations"),
        }
        # Operator placed green on top, orange in middle, blue at bottom —
        # deliberately NOT Notion palette order.
        current = [
            {"id": "opt-1", "name": "AI & Tech",  "color": "green"},
            {"id": "opt-3", "name": "Operations", "color": "orange"},
            {"id": "opt-2", "name": "Legal DD",   "color": "blue"},
        ]
        plan = _plan_member_db_update(rows, mappings, current, "mdb-1")
        assert plan.changed is False
        assert [o["id"] for o in plan.new_options] == ["opt-1", "opt-3", "opt-2"]

    def test_new_option_slots_into_existing_same_color_cluster(self):
        """A bootstrap-created option lands right after the last existing
        option of its color, not at the bottom of the array."""
        rows = [
            _drow("d-1", "AI & Tech",         "green"),
            _drow("d-2", "Finance & Reporting", "green"),
            _drow("d-3", "Investor Relations", "pink"),
            _drow("d-4", "HR Ops",             "green"),  # NEW
        ]
        mappings = {
            "d-1": _Mapping("d-1", "mdb-1", "opt-1", "AI & Tech"),
            "d-2": _Mapping("d-2", "mdb-1", "opt-2", "Finance & Reporting"),
            "d-3": _Mapping("d-3", "mdb-1", "opt-3", "Investor Relations"),
        }
        current = [
            {"id": "opt-1", "name": "AI & Tech",            "color": "green"},
            {"id": "opt-2", "name": "Finance & Reporting",  "color": "green"},
            {"id": "opt-3", "name": "Investor Relations",   "color": "pink"},
        ]
        plan = _plan_member_db_update(rows, mappings, current, "mdb-1")
        assert plan.created == 1
        names = [o.get("name") for o in plan.new_options]
        assert names == [
            "AI & Tech", "Finance & Reporting", "HR Ops",
            "Investor Relations",
        ]

    def test_new_option_appends_when_no_same_color_cluster(self):
        """First option of a never-seen color stays at the tail; it becomes
        the start of that color's cluster for future inserts."""
        rows = [
            _drow("d-1", "AI & Tech",  "green"),
            _drow("d-2", "Tech DD",    "blue"),  # NEW; no other blue yet
        ]
        mappings = {"d-1": _Mapping("d-1", "mdb-1", "opt-1", "AI & Tech")}
        current = [{"id": "opt-1", "name": "AI & Tech", "color": "green"}]
        plan = _plan_member_db_update(rows, mappings, current, "mdb-1")
        assert plan.created == 1
        assert [o.get("name") for o in plan.new_options] == [
            "AI & Tech", "Tech DD",
        ]


class TestSync:
    def test_aborts_when_org_chart_unset(self):
        config = _make_config(org_chart_db_id=None)
        client = MagicMock()
        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = detail_applier_sync.sync(client, config)
        assert report.errors == 1
        assert "ORG_CHART_DB_ID" in report.details[0]

    @patch("src.hierarchy.detail_applier_sync.discover_meeting_dbs")
    @patch("src.hierarchy.detail_applier_sync._http")
    def test_empty_canonical_returns_with_warning_not_error(
        self, mock_http, mock_discover,
    ):
        config = _make_config()
        client = MagicMock()
        mock_http.return_value = []  # detail_rows empty
        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = detail_applier_sync.sync(client, config)
        assert report.errors == 0
        assert any("detail_rows empty" in d for d in report.details)
        mock_discover.assert_not_called()

    @patch("src.hierarchy.detail_applier_sync.discover_meeting_dbs")
    @patch("src.hierarchy.detail_applier_sync._http")
    def test_name_change_runs_multi_select_saga_preserving_other_tags(
        self, mock_http, mock_discover,
    ):
        """Detail rename: saga PATCH 1 + per-page multi-select swap (drops
        old id, appends new id, preserves every other tagged option) + PATCH 2.
        """
        config = _make_config()
        client = MagicMock()
        http_calls: list[tuple] = []

        def http(method, path, body=None, prefer="return=minimal"):
            http_calls.append((method, path, body))
            if method == "GET" and "detail_rows" in path:
                return [{
                    "notion_page_id": "d-1", "name": "Legal Due Diligence",
                    "color": "blue", "active": True, "deleted_at": None,
                }]
            if method == "GET" and "detail_option_mappings" in path:
                return [{
                    "detail_notion_page_id": "d-1", "member_db_id": "mdb-1",
                    "option_id": "opt-old", "option_name": "Legal DD",
                }]
            return None

        mock_http.side_effect = http
        client.retrieve_data_source.return_value = _ds([
            {"id": "opt-old", "name": "Legal DD", "color": "blue"},
            {"id": "opt-other", "name": "Tech DD", "color": "default"},
        ])
        # PATCH 1 add new + PATCH 2 drop old.
        client.update_data_source.side_effect = [
            _patch_response([
                {"id": "opt-old", "name": "Legal DD", "color": "blue"},
                {"id": "opt-other", "name": "Tech DD", "color": "default"},
                {"id": "opt-new", "name": "Legal Due Diligence", "color": "blue"},
            ]),
            _patch_response([
                {"id": "opt-other", "name": "Tech DD", "color": "default"},
                {"id": "opt-new", "name": "Legal Due Diligence", "color": "blue"},
            ]),
        ]
        # Tagged page has BOTH the old option and another tag.
        client.query_database.return_value = {
            "results": [
                {"id": "page-A", "properties": {
                    "Detail": {"multi_select": [
                        {"id": "opt-old", "name": "Legal DD"},
                        {"id": "opt-other", "name": "Tech DD"},
                    ]},
                }},
            ],
            "has_more": False, "next_cursor": None,
        }
        mock_discover.return_value = [
            MeetingDB(db_id="mdb-1", owner_name="A", owner_email=""),
        ]

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = detail_applier_sync.sync(client, config)

        assert report.renamed == 1
        assert report.errors == 0
        # Page migrated — array preserves opt-other, drops opt-old, adds opt-new.
        client.update_page.assert_called_once()
        kwargs = client.update_page.call_args.kwargs
        new_array = kwargs["properties"]["Detail"]["multi_select"]
        ids = [e["id"] for e in new_array]
        assert "opt-other" in ids
        assert "opt-new" in ids
        assert "opt-old" not in ids
        # Mapping carries saga's new id.
        posts = [c for c in http_calls if c[0] == "POST"]
        assert posts[0][2][0]["option_id"] == "opt-new"

    @patch("src.hierarchy.detail_applier_sync.discover_meeting_dbs")
    @patch("src.hierarchy.detail_applier_sync._http")
    def test_successful_create_writes_multi_select_with_color(
        self, mock_http, mock_discover,
    ):
        config = _make_config()
        client = MagicMock()

        def http(method, path, body=None, prefer="return=minimal"):
            if method == "GET" and "detail_rows" in path:
                return [{
                    "notion_page_id": "d-1", "name": "Legal DD",
                    "color": "blue", "active": True, "deleted_at": None,
                }]
            if method == "GET" and "detail_option_mappings" in path:
                return []
            return None

        mock_http.side_effect = http
        client.retrieve_data_source.return_value = _ds([])
        client.update_data_source.return_value = _patch_response(
            [{"id": "opt-new", "name": "Legal DD", "color": "blue"}],
        )
        mock_discover.return_value = [
            MeetingDB(db_id="mdb-1", owner_name="A", owner_email=""),
        ]

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = detail_applier_sync.sync(client, config)

        assert report.created == 1
        assert report.errors == 0
        # Verify PATCH payload uses multi_select (not select) AND carries color.
        args, _ = client.update_data_source.call_args
        opts = args[1]["Detail"]["multi_select"]["options"]
        assert opts == [{"name": "Legal DD", "color": "blue"}]
