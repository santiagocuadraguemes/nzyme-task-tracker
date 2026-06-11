from unittest.mock import MagicMock, patch

from src.config import SyncConfig
from src.deal_context import DealInfo, DealWorkstream
from src.pipeline import (
    run_inject_templates, run_sync,
    _inject_templates, _flatten_hierarchy, _load_existing_tasks,
    _substitute_placeholders, _format_existing_tasks, _format_team_members,
    _format_deal_context, _detect_deals_from_title, _run_semantic_dedup,
    _meeting_fingerprint, _resolve_stage_creds, OPENAI_DEFAULT_BASE_URL,
)


def _make_config(**overrides) -> SyncConfig:
    defaults = {
        "notion_api_token": "secret_abc",
        "openai_api_key": "sk-abc",
        "meeting_notes_db_id": "db-meetings",
        "team_tracker_db_id": "db-tracker",
        "merged_transcript_extraction_prompt_page_id": "page-merged",
        "buffer_hours": 2,
        "dry_run": False,
    }
    defaults.update(overrides)
    return SyncConfig(**defaults)


def _make_page(page_id: str, title: str) -> dict:
    return {
        "id": page_id,
        "created_by": {"id": "u1", "name": "Santiago"},
        "properties": {
            "Meeting": {"type": "title", "title": [{"plain_text": title}]},
            "Date": {"type": "date", "date": {"start": "2026-03-28"}},
            "Meeting type": {"type": "select", "select": {"name": "Team sync"}},
            "Attendees": {"type": "people", "people": [{"id": "u1", "name": "Santiago"}]},
            "Processed": {"type": "checkbox", "checkbox": False},
        },
    }


class TestInjectTemplates:
    @patch("src.pipeline.inject_notes_section")
    def test_injects_into_unprocessed_pages(self, mock_inject):
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                _make_page("p1", "Meeting 1"),
                _make_page("p2", "Meeting 2"),
            ]
        }
        mock_inject.side_effect = [True, False]  # p1 injected, p2 already had it
        template_blocks = [{"type": "heading_2"}]
        marker = ("heading_2", "action items")

        injected = _inject_templates(client, "db-meetings", template_blocks, marker)

        assert injected == 1
        assert mock_inject.call_count == 2

    @patch("src.pipeline.inject_notes_section")
    def test_dry_run_does_not_inject(self, mock_inject):
        client = MagicMock()
        client.query_database.return_value = {
            "results": [_make_page("p1", "Meeting 1")]
        }
        template_blocks = [{"type": "heading_2"}]
        marker = ("heading_2", "action items")

        injected = _inject_templates(
            client, "db-meetings", template_blocks, marker, dry_run=True,
        )

        assert injected == 0
        mock_inject.assert_not_called()

    @patch("src.pipeline.inject_notes_section")
    def test_continues_on_individual_failure(self, mock_inject):
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                _make_page("p1", "Meeting 1"),
                _make_page("p2", "Meeting 2"),
            ]
        }
        mock_inject.side_effect = [Exception("API error"), True]
        template_blocks = [{"type": "heading_2"}]
        marker = ("heading_2", "action items")

        injected = _inject_templates(client, "db-meetings", template_blocks, marker)

        assert injected == 1
        assert mock_inject.call_count == 2


class TestRunInjectTemplates:
    @patch("src.pipeline._inject_templates")
    @patch("src.pipeline.fetch_template")
    def test_fetches_and_injects(self, mock_fetch, mock_inject):
        config = _make_config(meeting_template_page_id="tmpl-123")
        client = MagicMock()
        mock_fetch.return_value = ([{"type": "heading_2"}], ("heading_2", "action items"))
        mock_inject.return_value = 2

        run_inject_templates(config, client)

        mock_fetch.assert_called_once_with(client, "tmpl-123")
        mock_inject.assert_called_once()

    @patch("src.pipeline.fetch_template")
    def test_skips_when_no_template_configured(self, mock_fetch):
        config = _make_config()  # no meeting_template_page_id
        client = MagicMock()

        run_inject_templates(config, client)

        mock_fetch.assert_not_called()


class TestFlattenHierarchy:
    def test_flat(self):
        nodes = [
            {"id": "a", "title": "Root A", "children": []},
            {"id": "b", "title": "Root B", "children": []},
        ]
        assert _flatten_hierarchy(nodes) == {"a": "Root A", "b": "Root B"}

    def test_nested(self):
        nodes = [
            {"id": "a", "title": "Root", "children": [
                {"id": "b", "title": "Child", "children": [
                    {"id": "c", "title": "Grandchild", "children": []},
                ]},
            ]},
        ]
        result = _flatten_hierarchy(nodes)
        assert result == {"a": "Root", "b": "Child", "c": "Grandchild"}

    def test_empty(self):
        assert _flatten_hierarchy([]) == {}


class TestLoadExistingTasks:
    def test_loads_recent_tasks_with_parent_titles(self):
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                {
                    "properties": {
                        "Task": {"type": "title", "title": [{"plain_text": "Call investor"}]},
                        "Parent item": {"relation": [{"id": "parent-1"}]},
                    }
                },
                {
                    "properties": {
                        "Task": {"type": "title", "title": [{"plain_text": "Review doc"}]},
                        "Parent item": {"relation": []},
                    }
                },
            ]
        }
        hierarchy = [{"id": "parent-1", "title": "Fundraising", "children": []}]

        tasks = _load_existing_tasks(client, "db-tracker", hierarchy)

        assert len(tasks) == 2
        assert tasks[0] == {"title": "Call investor", "parent_title": "Fundraising"}
        assert tasks[1] == {"title": "Review doc", "parent_title": ""}

    def test_returns_empty_on_api_failure(self):
        client = MagicMock()
        client.query_database.side_effect = Exception("API error")

        tasks = _load_existing_tasks(client, "db-tracker", [])

        assert tasks == []

    def test_skips_blank_titles(self):
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                {"properties": {"Task": {"type": "title", "title": []}}},
            ]
        }

        tasks = _load_existing_tasks(client, "db-tracker", [])

        assert tasks == []


class TestSubstitutePlaceholders:
    def test_replaces_placeholders(self):
        template = "Hello {{NAME}}, your category is {{CATEGORY}}."
        result = _substitute_placeholders(template, NAME="Santiago", CATEGORY="Ops")
        assert result == "Hello Santiago, your category is Ops."

    def test_leaves_unknown_placeholders(self):
        template = "{{KNOWN}} and {{UNKNOWN}}"
        result = _substitute_placeholders(template, KNOWN="yes")
        assert result == "yes and {{UNKNOWN}}"

    def test_empty_value(self):
        result = _substitute_placeholders("before{{X}}after", X="")
        assert result == "beforeafter"


class TestFormatExistingTasks:
    def test_formats_tasks_with_parents(self):
        tasks = [
            {"title": "Call X", "parent_title": "Fundraising"},
            {"title": "Review Y", "parent_title": ""},
        ]
        result = _format_existing_tasks(tasks)
        assert "DO NOT duplicate" in result
        assert "Call X (under: Fundraising)" in result
        assert "- Review Y\n" in result

    def test_empty_returns_empty_string(self):
        assert _format_existing_tasks([]) == ""


class TestFormatTeamMembers:
    def test_includes_aliases(self):
        users = [
            {"id": "u1", "name": "Jose Gasalla", "email": "jmg@kiboventures.com"},
            {"id": "u2", "name": "Vicente", "email": "vicente@kiboventures.com"},
        ]
        result = _format_team_members(users)
        assert "Jose Gasalla (ID: u1) (aliases: jmg, Jose)" in result
        assert "Vicente (ID: u2) (aliases: vicente)" in result

    def test_empty_users(self):
        assert _format_team_members([]) == "No team members available — use attendees only"

    def test_no_email(self):
        users = [{"id": "u1", "name": "Santiago", "email": ""}]
        result = _format_team_members(users)
        assert "Santiago (ID: u1)" in result


class TestFormatDealContext:
    def test_formats_deals_with_workstreams(self):
        deals = [DealInfo(
            name="Citadel",
            deal_page_id="deal-1",
            tracker_page_id="tracker-1",
            workplan_db_id="wp-1",
            workstreams=[
                DealWorkstream(id="ws-1", title="FDD", status="In progress",
                               workstream_type=["DD"], adviser=["A&M"]),
                DealWorkstream(id="ws-2", title="Legal DD", status="Not started",
                               workstream_type=["DD"], adviser=["DLA"]),
            ],
        )]
        result = _format_deal_context(deals)
        assert "Citadel" in result
        assert "deal_page_id: deal-1" in result
        assert "parent_task_id (Tracker page ID): tracker-1" in result
        assert "FDD (Status: In progress, Type: DD, Adviser: A&M)" in result
        assert "Legal DD" in result

    def test_empty_deals_returns_empty(self):
        assert _format_deal_context([]) == ""

    def test_deal_without_workstreams(self):
        deals = [DealInfo(
            name="SimpleDeal",
            deal_page_id="deal-2",
            tracker_page_id=None,
        )]
        result = _format_deal_context(deals)
        assert "SimpleDeal" in result
        assert "parent_task_id (Tracker page ID): not linked" in result
        assert "Workstreams" not in result


class TestDetectDealsFromTitle:
    def test_matches_deal_name_in_title(self):
        deals = [
            DealInfo(name="Citadel", deal_page_id="d1", tracker_page_id="t1"),
            DealInfo(name="Phoenix", deal_page_id="d2", tracker_page_id="t2"),
        ]
        result = _detect_deals_from_title("Citadel Weekly Sync", deals)
        assert len(result) == 1
        assert result[0].name == "Citadel"

    def test_case_insensitive(self):
        deals = [DealInfo(name="Citadel", deal_page_id="d1", tracker_page_id="t1")]
        result = _detect_deals_from_title("citadel dd review", deals)
        assert len(result) == 1

    def test_no_match(self):
        deals = [DealInfo(name="Citadel", deal_page_id="d1", tracker_page_id="t1")]
        result = _detect_deals_from_title("Weekly Standup", deals)
        assert len(result) == 0

    def test_multiple_deals_in_title(self):
        deals = [
            DealInfo(name="Citadel", deal_page_id="d1", tracker_page_id="t1"),
            DealInfo(name="Phoenix", deal_page_id="d2", tracker_page_id="t2"),
        ]
        result = _detect_deals_from_title("Citadel + Phoenix review", deals)
        assert len(result) == 2


class TestRunSemanticDedup:
    def test_filters_duplicates(self):
        dedup = MagicMock()
        dedup.is_duplicate.side_effect = [
            (False, None, 0.3),   # task 1: not a dup
            (True, "existing", 0.92),  # task 2: duplicate
            (False, None, 0.1),   # task 3: not a dup
        ]
        tasks = [
            {"title": "unique task"},
            {"title": "duplicate task"},
            {"title": "another unique"},
        ]

        result = _run_semantic_dedup(tasks, dedup)

        assert len(result) == 2
        assert result[0]["title"] == "unique task"
        assert result[1]["title"] == "another unique"
        # add_title called for kept tasks only
        assert dedup.add_title.call_count == 2

    def test_none_dedup_passes_all(self):
        tasks = [{"title": "a"}, {"title": "b"}]
        result = _run_semantic_dedup(tasks, None)
        assert result == tasks

    def test_empty_tasks_returns_empty(self):
        dedup = MagicMock()
        result = _run_semantic_dedup([], dedup)
        assert result == []


class TestMeetingFingerprint:
    """The (db_id, title, date) fingerprint is what makes the multi-DB model safe."""

    def test_same_meeting_in_different_dbs_does_not_collapse(self):
        """Two team members capturing the same meeting must NOT dedup."""
        santiago_db = "34583e67-e2e7-8081-b515-f5e33926f153"
        reyes_db = "b0797647-2620-499f-a4b8-9be7b03c07d0"

        fp_santiago = _meeting_fingerprint(santiago_db, "Reyes <> Santiago", "2026-04-24")
        fp_reyes = _meeting_fingerprint(reyes_db, "Reyes <> Santiago", "2026-04-24")

        assert fp_santiago != fp_reyes

    def test_notion_dup_suffix_within_same_db_collapses(self):
        """Within one DB, Notion's ' (1)' suffix on duplicate pages still collapses."""
        db_id = "34583e67-e2e7-8081-b515-f5e33926f153"

        fp_a = _meeting_fingerprint(db_id, "Standup", "2026-04-24")
        fp_b = _meeting_fingerprint(db_id, "Standup (1)", "2026-04-24")
        fp_c = _meeting_fingerprint(db_id, "  STANDUP  ", "2026-04-24")

        assert fp_a == fp_b == fp_c

    def test_db_id_normalized_independent_of_dashes(self):
        """Same DB ID with/without dashes produces the same fingerprint."""
        with_dashes = "34583e67-e2e7-8081-b515-f5e33926f153"
        no_dashes = "34583e67e2e78081b515f5e33926f153"

        assert (
            _meeting_fingerprint(with_dashes, "Standup", "2026-04-24")
            == _meeting_fingerprint(no_dashes, "Standup", "2026-04-24")
        )




class TestResolveStageCreds:
    """`gemini-` prefix routes through Gemini creds; everything else â†’ OpenAI."""

    def test_gemini_prefix_returns_gemini_creds(self):
        config = _make_config(
            gemini_api_key="gem-secret",
            gemini_base_url="https://gemini.example/v1/",
        )
        api_key, base_url = _resolve_stage_creds("gemini-3-flash-preview", config)
        assert api_key == "gem-secret"
        assert base_url == "https://gemini.example/v1/"

    def test_non_gemini_prefix_returns_openai_creds(self):
        config = _make_config(openai_api_key="sk-openai-secret")
        api_key, base_url = _resolve_stage_creds("gpt-5-mini", config)
        assert api_key == "sk-openai-secret"
        assert base_url == OPENAI_DEFAULT_BASE_URL

    def test_gemini_prefix_without_key_raises(self):
        config = _make_config(gemini_api_key=None)
        try:
            _resolve_stage_creds("gemini-3-flash-preview", config)
        except RuntimeError as exc:
            assert "GEMINI_API_KEY" in str(exc)
        else:
            raise AssertionError("expected RuntimeError when gemini_api_key is missing")


def _meeting_notes_block(
    transcript_block_id: str | None = None,
    notes_block_id: str | None = "notes-block-1",
) -> dict:
    """Build a fake meeting_notes block. Transcription paused â†’ no transcript_block_id."""
    children: dict = {}
    if notes_block_id:
        children["notes_block_id"] = notes_block_id
    if transcript_block_id:
        children["transcript_block_id"] = transcript_block_id
    return {
        "type": "meeting_notes",
        "id": "mn-block-1",
        "meeting_notes": {
            "title": [{"plain_text": "Meeting", "type": "text"}],
            "status": "transcribed" if transcript_block_id else "transcription_paused",
            "children": children,
            "calendar_event": {"attendees": [], "start_time": "", "end_time": ""},
        },
    }


class TestBufferAutoDisable:
    """`--db-id` (i.e. `meeting_notes_db_id` set) â†’ buffer auto-disabled."""

    @patch("src.pipeline.SemanticDedup")
    @patch("src.pipeline.OpenAI")
    @patch("src.pipeline.TeamTaskTrackerWriter")
    @patch("src.pipeline.HierarchyLoader")
    @patch("src.pipeline._fetch_page_text")
    @patch("src.pipeline.SingleSource")
    def test_db_id_set_passes_none_buffer(
        self, mock_source_cls, mock_fetch_text,
        mock_hierarchy_cls, mock_writer_cls,
        mock_openai_cls, mock_dedup_cls,
    ):
        config = _make_config(meeting_notes_db_id="db-meetings", buffer_hours=2)
        client = MagicMock()
        client.list_users.return_value = []

        mock_source_cls.return_value.get_unprocessed_pages.return_value = []
        mock_fetch_text.return_value = "tpl"
        mock_hierarchy_cls.return_value.load.return_value = []
        mock_writer_cls.return_value._existing_titles = set()
        client.query_database.return_value = {"results": []}

        run_sync(config, client)

        mock_source_cls.return_value.get_unprocessed_pages.assert_called_once_with(None)

    @patch("src.pipeline.load_registry")
    @patch("src.pipeline.SemanticDedup")
    @patch("src.pipeline.OpenAI")
    @patch("src.pipeline.TeamTaskTrackerWriter")
    @patch("src.pipeline.HierarchyLoader")
    @patch("src.pipeline._fetch_page_text")
    @patch("src.pipeline.SingleSource")
    def test_no_db_id_keeps_buffer(
        self, mock_source_cls, mock_fetch_text,
        mock_hierarchy_cls, mock_writer_cls,
        mock_openai_cls, mock_dedup_cls, mock_load_registry,
    ):
        from src.meeting_db_registry import MeetingDB

        config = _make_config(
            meeting_notes_db_id=None, org_chart_db_id="db-org", buffer_hours=2,
        )
        client = MagicMock()
        client.list_users.return_value = []

        mock_load_registry.return_value = [
            MeetingDB(db_id="db-discovered", owner_name="Reyes", owner_email="r@x.com"),
        ]
        mock_source_cls.return_value.get_unprocessed_pages.return_value = []
        mock_fetch_text.return_value = "tpl"
        mock_hierarchy_cls.return_value.load.return_value = []
        mock_writer_cls.return_value._existing_titles = set()
        client.query_database.return_value = {"results": []}

        run_sync(config, client)

        # Production-like multi-DB run keeps the configured buffer.
        mock_source_cls.return_value.get_unprocessed_pages.assert_called_once_with(2)


class TestExtractTranscriptBlockIdOptional:
    """The helper now returns None instead of raising on missing transcript."""

    def test_returns_id_when_present(self):
        from src.transcript_pipeline.fetch_transcript import extract_transcript_block_id

        block = {
            "meeting_notes": {
                "children": {"transcript_block_id": "t-123", "notes_block_id": "n-1"},
            },
        }
        assert extract_transcript_block_id(block) == "t-123"

    def test_returns_none_when_missing(self):
        from src.transcript_pipeline.fetch_transcript import extract_transcript_block_id

        block = {
            "meeting_notes": {
                "status": "transcription_paused",
                "children": {"notes_block_id": "n-1"},
            },
        }
        assert extract_transcript_block_id(block) is None

    def test_returns_none_when_children_missing(self):
        from src.transcript_pipeline.fetch_transcript import extract_transcript_block_id

        assert extract_transcript_block_id({"meeting_notes": {}}) is None
        assert extract_transcript_block_id({}) is None
