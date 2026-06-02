"""Tests for src.hierarchy.external_org_applier_sync (Supabase-only source).

The hierarchy is now owned by ``deal_hierarchy_sync`` (keyed on a ``Deal ID``
property), so this applier no longer maintains ``deal_hierarchy_links`` — it
just fans the tracked deal set out to each member-DB ``External Org`` select.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.config import SyncConfig
from src.hierarchy import external_org_applier_sync
from src.hierarchy.external_org_applier_sync import (
    _ALLOWED_STAGES,
    _CanonicalDeal,
    _Mapping,
    _STAGE_TO_COLOR,
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


def _deal(deal_id: str = "00000000-0000-0000-0000-000000000001",
          name: str = "Project Lavare",
          stage: str = "Portfolio") -> _CanonicalDeal:
    return _CanonicalDeal(deal_id=deal_id, name=name, stage=stage)


def _ds(options: list[dict]) -> dict:
    return {
        "properties": {
            "External Org": {"type": "select", "select": {"options": options}},
        },
    }


def _patch_response(options: list[dict]) -> dict:
    return _ds(options)


class TestStageRules:
    def test_allowed_stages_are_the_four_specified(self):
        assert _ALLOWED_STAGES == (
            "Portfolio",
            "DD phase",
            "Working on a deal (significant effort)",
            "Under analysis (team assigned, moderate effort)",
        )

    def test_color_portfolio_is_orange(self):
        assert _STAGE_TO_COLOR["Portfolio"] == "orange"

    def test_color_dealflow_stages_are_blue(self):
        for s in (
            "DD phase",
            "Working on a deal (significant effort)",
            "Under analysis (team assigned, moderate effort)",
        ):
            assert _STAGE_TO_COLOR[s] == "blue"

    def test_canonical_deal_color_property(self):
        assert _deal(stage="Portfolio").color == "orange"
        assert _deal(stage="DD phase").color == "blue"

    def test_canonical_deal_sort_key_priority(self):
        a = _deal("d-1", "Aaa", "Portfolio")
        b = _deal("d-2", "Bbb", "DD phase")
        c = _deal("d-3", "Aaa", "Under analysis (team assigned, moderate effort)")
        ordered = sorted([c, b, a], key=lambda d: d.sort_key)
        # Portfolio Aaa first, then DD phase Bbb, then Under analysis Aaa.
        assert [d.deal_id for d in ordered] == ["d-1", "d-2", "d-3"]


class TestPlanMemberDbUpdate:
    def test_bootstrap_create_carries_stage_color(self):
        deals = [_deal("d-1", "Project Lavare", "Portfolio")]
        plan = _plan_member_db_update(deals, {}, [], "mdb-1")
        assert plan.created == 1
        assert plan.new_options == [
            {"name": "Project Lavare", "color": "orange"},
        ]

    def test_bootstrap_adopt_by_sanitized_name_with_color_recolor(self):
        """Existing option matches by name but wrong color → adopt + recolor."""
        deals = [_deal("d-1", "Project Lavare", "Portfolio")]
        current = [{"id": "opt-1", "name": "Project Lavare", "color": "blue"}]
        plan = _plan_member_db_update(deals, {}, current, "mdb-1")
        assert plan.changed is True
        assert plan.renamed == 1
        assert plan.new_options[0]["color"] == "orange"
        assert plan.new_options[0]["id"] == "opt-1"

    def test_stage_transition_out_archives_existing_option(self):
        """Deal NOT in canonical but mapping exists → option archived."""
        deals: list[_CanonicalDeal] = []
        mappings = {"d-1": _Mapping("d-1", "mdb-1", "opt-1", "Old Deal")}
        current = [{"id": "opt-1", "name": "Old Deal", "color": "blue"}]
        plan = _plan_member_db_update(deals, mappings, current, "mdb-1")
        assert plan.archived == 1
        assert plan.renamed == 1
        assert plan.new_options[0]["name"] == "(archived) Old Deal"

    def test_re_entry_un_archives_in_place(self):
        """Deal back in canonical → existing (archived) option renamed to bare."""
        deals = [_deal("d-1", "Project Lavare", "Portfolio")]
        mappings = {
            "d-1": _Mapping("d-1", "mdb-1", "opt-1", "(archived) Project Lavare"),
        }
        current = [{
            "id": "opt-1", "name": "(archived) Project Lavare", "color": "default",
        }]
        plan = _plan_member_db_update(deals, mappings, current, "mdb-1")
        assert plan.renamed == 1
        assert plan.archived == 0
        assert plan.new_options[0]["name"] == "Project Lavare"
        assert plan.new_options[0]["color"] == "orange"

    def test_noop_in_sync(self):
        deals = [_deal("d-1", "Project Lavare", "Portfolio")]
        mappings = {"d-1": _Mapping("d-1", "mdb-1", "opt-1", "Project Lavare")}
        current = [{"id": "opt-1", "name": "Project Lavare", "color": "orange"}]
        plan = _plan_member_db_update(deals, mappings, current, "mdb-1")
        assert plan.changed is False
        assert plan.created == 0
        assert plan.renamed == 0

    def test_already_archived_option_not_re_archived(self):
        """Idempotent archive — option already carrying the prefix is left alone."""
        deals: list[_CanonicalDeal] = []
        mappings = {"d-1": _Mapping("d-1", "mdb-1", "opt-1", "(archived) Old")}
        current = [{
            "id": "opt-1", "name": "(archived) Old", "color": "default",
        }]
        plan = _plan_member_db_update(deals, mappings, current, "mdb-1")
        assert plan.archived == 0
        assert plan.renamed == 0
        assert plan.changed is False


class TestLegacyCleanupAndReorder:
    def test_legacy_option_with_tags_kept_at_bottom(self):
        deals = [_deal("d-1", "Project Lavare", "Working on a deal (significant effort)")]
        current = [
            {"id": "opt-legacy", "name": "Old Random Org", "color": "blue"},
            {"id": "opt-1", "name": "Project Lavare", "color": "blue"},
        ]
        plan = _plan_member_db_update(
            deals, {}, current, "mdb-1",
            legacy_options_with_tags={"opt-legacy"},
        )
        # Canonical first, legacy at the bottom.
        assert plan.new_options[0]["name"] == "Project Lavare"
        assert plan.new_options[-1]["name"] == "Old Random Org"
        # No drop counter.
        assert getattr(plan, "_dropped_legacy_count", 0) == 0

    def test_legacy_option_without_tags_is_dropped(self):
        deals = [_deal("d-1", "Project Lavare", "Working on a deal (significant effort)")]
        current = [
            {"id": "opt-orphan", "name": "Never Tagged", "color": "blue"},
            {"id": "opt-1", "name": "Project Lavare", "color": "blue"},
        ]
        plan = _plan_member_db_update(
            deals, {}, current, "mdb-1",
            legacy_options_with_tags=set(),  # empty → drop every legacy
        )
        # The orphan should be GONE.
        names = [o["name"] for o in plan.new_options]
        assert "Never Tagged" not in names
        assert "Project Lavare" in names
        assert getattr(plan, "_dropped_legacy_count", 0) == 1
        assert plan.changed is True

    def test_legacy_with_tags_none_keeps_everything_still_reorders(self):
        """legacy_options_with_tags=None → skip cleanup but still send legacy
        options to the bottom of the list."""
        deals = [_deal("d-1", "Project Lavare", "Working on a deal (significant effort)")]
        current = [
            {"id": "opt-legacy", "name": "Old Random Org", "color": "blue"},
            {"id": "opt-1", "name": "Project Lavare", "color": "blue"},
        ]
        plan = _plan_member_db_update(
            deals, {}, current, "mdb-1",
            legacy_options_with_tags=None,  # tag-check unavailable
        )
        # Both options still present, but reordered.
        names = [o["name"] for o in plan.new_options]
        assert names == ["Project Lavare", "Old Random Org"]
        assert getattr(plan, "_dropped_legacy_count", 0) == 0

    def test_canonical_active_sorted_by_stage_priority_and_alpha(self):
        deals = [
            _deal("d-1", "Sertyf", "Under analysis (team assigned, moderate effort)"),
            _deal("d-2", "Azenea", "Portfolio"),
            _deal("d-3", "Civislend", "DD phase"),
            _deal("d-4", "Project Lavare", "Working on a deal (significant effort)"),
        ]
        current = [
            {"id": "opt-c", "name": "Civislend", "color": "blue"},
            {"id": "opt-s", "name": "Sertyf", "color": "blue"},
            {"id": "opt-a", "name": "Azenea", "color": "orange"},
            {"id": "opt-pl", "name": "Project Lavare", "color": "blue"},
        ]
        plan = _plan_member_db_update(
            deals, {}, current, "mdb-1",
            legacy_options_with_tags=set(),
        )
        # Expected order: Portfolio Azenea, DD phase Civislend, Working Project Lavare, Under analysis Sertyf.
        assert [o["name"] for o in plan.new_options] == [
            "Azenea", "Civislend", "Project Lavare", "Sertyf",
        ]

    def test_archived_and_legacy_kept_sink_to_bottom_alpha(self):
        """Stage-out archived options + legacy-kept options both sort to the
        bottom; non-archived first within that section, archived after."""
        deals = [_deal("d-1", "Project Lavare", "Working on a deal (significant effort)")]
        mappings = {
            "d-1": _Mapping("d-1", "mdb-1", "opt-1", "Project Lavare"),
            # d-2 had a mapping but is no longer in canonical → archive.
            "d-2": _Mapping("d-2", "mdb-1", "opt-2", "Out Of Filter"),
        }
        current = [
            {"id": "opt-legacy-a", "name": "Aardvark Legacy", "color": "blue"},
            {"id": "opt-1", "name": "Project Lavare", "color": "blue"},
            {"id": "opt-2", "name": "Out Of Filter", "color": "blue"},
            {"id": "opt-legacy-z", "name": "Zebra Legacy", "color": "blue"},
        ]
        plan = _plan_member_db_update(
            deals, mappings, current, "mdb-1",
            legacy_options_with_tags={"opt-legacy-a", "opt-legacy-z"},
        )
        names = [o["name"] for o in plan.new_options]
        # Canonical first, then non-archived legacy alpha, then archived alpha.
        assert names == [
            "Project Lavare",
            "Aardvark Legacy",
            "Zebra Legacy",
            "(archived) Out Of Filter",
        ]


class TestSync:
    def test_aborts_when_org_chart_unset(self):
        config = _make_config(org_chart_db_id=None)
        client = MagicMock()
        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = external_org_applier_sync.sync(client, config)
        assert report.errors == 1
        assert "ORG_CHART_DB_ID" in report.details[0]

    @patch("src.hierarchy.external_org_applier_sync.discover_meeting_dbs")
    @patch("src.hierarchy.external_org_applier_sync._http")
    def test_filters_to_allowed_stages_only(self, mock_http, mock_discover):
        """Rows outside the 4 allowed stages must NOT become options."""
        config = _make_config()
        client = MagicMock()

        def http(method, path, body=None, prefer="return=minimal"):
            if method == "GET" and "ReportingNz_deals" in path:
                return [
                    {"id": "d-1", "name": "Project Lavare", "stage": "Portfolio"},
                    {"id": "d-2", "name": "Discarded Deal", "stage": "Discarded"},
                    {"id": "d-3", "name": "Idea Deal", "stage": "Ideas"},
                ]
            if method == "GET" and "external_org_option_mappings" in path:
                return []
            return None

        mock_http.side_effect = http
        client.retrieve_data_source.return_value = _ds([])
        client.update_data_source.return_value = _patch_response(
            [{"id": "opt-new", "name": "Project Lavare", "color": "orange"}],
        )
        mock_discover.return_value = [
            MeetingDB(db_id="mdb-1", owner_name="A", owner_email=""),
        ]

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = external_org_applier_sync.sync(client, config)

        assert report.created == 1  # only Portfolio deal made it through
        args, _ = client.update_data_source.call_args
        opts = args[1]["External Org"]["select"]["options"]
        assert opts == [{"name": "Project Lavare", "color": "orange"}]

    @patch("src.hierarchy.external_org_applier_sync.discover_meeting_dbs")
    @patch("src.hierarchy.external_org_applier_sync._http")
    def test_adopted_in_sync_still_persists_mapping(self, mock_http, mock_discover):
        """A member whose options already match by name+color (adopted, no
        mapping yet) must still get its external_org_option_mappings row
        written — otherwise the stage-out archive pass can never find it."""
        config = _make_config()
        client = MagicMock()
        http_calls: list[tuple] = []

        def http(method, path, body=None, prefer="return=minimal"):
            http_calls.append((method, path, body))
            if method == "GET" and "ReportingNz_deals" in path:
                return [{"id": "d-1", "name": "Project Lavare", "stage": "Portfolio"}]
            if method == "GET" and "external_org_option_mappings" in path:
                return []  # no mappings yet
            return None

        mock_http.side_effect = http
        # Option already present with the correct name AND color → adopt-clean,
        # no schema change, but a mapping must be recorded.
        client.retrieve_data_source.return_value = _ds([
            {"id": "opt-1", "name": "Project Lavare", "color": "orange"},
        ])
        client.query_database.return_value = {
            "results": [], "has_more": False, "next_cursor": None,
        }
        mock_discover.return_value = [
            MeetingDB(db_id="mdb-1", owner_name="A", owner_email=""),
        ]

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = external_org_applier_sync.sync(client, config)

        assert report.errors == 0
        # No schema PATCH needed (options already correct + ordered).
        client.update_data_source.assert_not_called()
        # But the mapping IS persisted.
        external_posts = [
            c for c in http_calls
            if c[0] == "POST" and "external_org_option_mappings" in c[1]
        ]
        assert len(external_posts) == 1
        assert external_posts[0][2][0]["deal_id"] == "d-1"
        assert external_posts[0][2][0]["option_id"] == "opt-1"

    @patch("src.hierarchy.external_org_applier_sync.discover_meeting_dbs")
    @patch("src.hierarchy.external_org_applier_sync._http")
    def test_stage_out_archive_runs_saga(self, mock_http, mock_discover):
        """Deal fell out of allowed stages → saga renames option to
        (archived) X (since Notion's PATCH can't rename in place).
        """
        config = _make_config()
        client = MagicMock()
        http_calls: list[tuple] = []

        # Seed one allowed-stage deal (different deal_id) so the per-member loop
        # runs, plus a mapping for the deal that fell out of the filter.
        def http(method, path, body=None, prefer="return=minimal"):
            http_calls.append((method, path, body))
            if method == "GET" and "ReportingNz_deals" in path:
                return [{"id": "d-keep", "name": "Project Lavare",
                         "stage": "Portfolio"}]
            if method == "GET" and "external_org_option_mappings" in path:
                return [{
                    "deal_id": "d-1", "member_db_id": "mdb-1",
                    "option_id": "opt-old", "option_name": "Old Deal",
                }]
            return None

        mock_http.side_effect = http
        client.retrieve_data_source.return_value = _ds([
            {"id": "opt-old", "name": "Old Deal", "color": "blue"},
        ])
        # PATCH 1 (add archived Old Deal) → PATCH 2 (drop opt-old) → final PATCH
        # (add Project Lavare bootstrap-create + reorder).
        client.update_data_source.side_effect = [
            _patch_response([
                {"id": "opt-old", "name": "Old Deal", "color": "blue"},
                {"id": "opt-arch",
                 "name": "(archived) Old Deal", "color": "blue"},
            ]),
            _patch_response([
                {"id": "opt-arch",
                 "name": "(archived) Old Deal", "color": "blue"},
            ]),
            _patch_response([
                {"id": "opt-lavare", "name": "Project Lavare", "color": "orange"},
                {"id": "opt-arch",
                 "name": "(archived) Old Deal", "color": "blue"},
            ]),
        ]
        client.query_database.return_value = {
            "results": [], "has_more": False, "next_cursor": None,
        }
        mock_discover.return_value = [
            MeetingDB(db_id="mdb-1", owner_name="A", owner_email=""),
        ]

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = external_org_applier_sync.sync(client, config)

        assert report.archived == 1
        assert report.renamed == 1  # the archive rename counts
        assert report.errors == 0
        # Saga PATCH 1 + PATCH 2 + final PATCH (creates).
        assert client.update_data_source.call_count == 3
        # Mapping upsert for the archived deal carries the saga's new id.
        external_posts = [
            c for c in http_calls
            if c[0] == "POST" and "external_org_option_mappings" in c[1]
        ]
        assert len(external_posts) == 1
        d1_entry = next(
            e for e in external_posts[0][2] if e["deal_id"] == "d-1"
        )
        assert d1_entry["option_id"] == "opt-arch"
        assert d1_entry["option_name"] == "(archived) Old Deal"

    @patch("src.hierarchy.external_org_applier_sync.discover_meeting_dbs")
    @patch("src.hierarchy.external_org_applier_sync._http")
    def test_re_entry_un_archive_runs_saga(self, mock_http, mock_discover):
        """Deal back in canonical → existing (archived) X option saga'd to X.
        """
        config = _make_config()
        client = MagicMock()
        http_calls: list[tuple] = []

        def http(method, path, body=None, prefer="return=minimal"):
            http_calls.append((method, path, body))
            if method == "GET" and "ReportingNz_deals" in path:
                return [{"id": "d-1", "name": "Project Lavare",
                         "stage": "Portfolio"}]
            if method == "GET" and "external_org_option_mappings" in path:
                return [{
                    "deal_id": "d-1", "member_db_id": "mdb-1",
                    "option_id": "opt-arch",
                    "option_name": "(archived) Project Lavare",
                }]
            return None

        mock_http.side_effect = http
        client.retrieve_data_source.return_value = _ds([
            {"id": "opt-arch",
             "name": "(archived) Project Lavare", "color": "default"},
        ])
        client.update_data_source.side_effect = [
            _patch_response([
                {"id": "opt-arch",
                 "name": "(archived) Project Lavare", "color": "default"},
                {"id": "opt-new", "name": "Project Lavare", "color": "orange"},
            ]),
            _patch_response([
                {"id": "opt-new", "name": "Project Lavare", "color": "orange"},
            ]),
        ]
        client.query_database.return_value = {
            "results": [], "has_more": False, "next_cursor": None,
        }
        mock_discover.return_value = [
            MeetingDB(db_id="mdb-1", owner_name="A", owner_email=""),
        ]

        with patch.dict("os.environ", {"SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
            report = external_org_applier_sync.sync(client, config)

        assert report.renamed == 1
        assert report.archived == 0  # un-archive doesn't increment archived
        assert report.errors == 0
        # Mapping carries the saga's new id.
        external_posts = [
            c for c in http_calls
            if c[0] == "POST" and "external_org_option_mappings" in c[1]
        ]
        assert external_posts[0][2][0]["option_id"] == "opt-new"
        assert external_posts[0][2][0]["option_name"] == "Project Lavare"
