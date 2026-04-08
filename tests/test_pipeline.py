from unittest.mock import MagicMock, patch, call

from src.config import SyncConfig
from src.deal_context import DealInfo, DealWorkstream
from src.pipeline import (
    run_inject_templates, run_sync, _archive_done_tasks,
    _inject_templates, _flatten_hierarchy, _load_existing_tasks,
    _substitute_placeholders, _format_existing_tasks, _format_team_members,
    _format_deal_context, _detect_deals_from_title, _run_semantic_dedup,
)


def _make_config(**overrides) -> SyncConfig:
    defaults = {
        "notion_api_token": "secret_abc",
        "openai_api_key": "sk-abc",
        "meeting_notes_db_id": "db-meetings",
        "team_tracker_db_id": "db-tracker",
        "system_prompt_page_id": "page-system-prompt",
        "user_prompt_page_id": "page-user-prompt",
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


class TestRunSync:
    @patch("src.pipeline.SemanticDedup")
    @patch("src.pipeline.OpenAI")
    @patch("src.pipeline.TeamTaskTrackerWriter")
    @patch("src.pipeline.AIExtractor")
    @patch("src.pipeline.HierarchyLoader")
    @patch("src.pipeline._fetch_page_text")
    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline._load_categories")
    def test_full_cycle(
        self, mock_load_cats, mock_source_cls, mock_fetch_text,
        mock_hierarchy_cls, mock_extractor_cls, mock_writer_cls,
        mock_openai_cls, mock_dedup_cls,
    ):
        config = _make_config()
        client = MagicMock()
        client.list_users.return_value = [
            {"id": "u1", "name": "Santiago", "type": "person", "person": {"email": "santiago@kiboventures.com"}},
            {"id": "u2", "name": "Reyes", "type": "person", "person": {"email": "reyes@kiboventures.com"}},
            {"id": "bot-1", "name": "Integration", "type": "bot"},
        ]

        mock_load_cats.return_value = ["Operations", "Other"]
        mock_source = mock_source_cls.return_value
        mock_source.get_unprocessed_pages.return_value = [_make_page("p1", "Standup")]
        mock_source.get_page_content.return_value = "Action: @Santiago review doc"
        mock_source.get_page_metadata.return_value = {
            "title": "Standup",
            "date": "2026-03-28",
            "meeting_type": "Team sync",
            "attendees": [{"id": "u1", "name": "Santiago"}],
            "created_by": {"id": "u1", "name": "Santiago"},
        }

        mock_fetch_text.return_value = "prompt template with {{CATEGORIES}} {{HIERARCHY}} {{EXISTING_TASKS}} {{TEAM_MEMBERS}} {{ATTENDEES}} {{MEETING_CREATOR}}"
        mock_hierarchy_cls.return_value.load.return_value = [{"id": "cat1", "title": "Ops"}]
        mock_extractor_cls.return_value.extract.return_value = [
            {"title": "Review doc", "assignee_id": "u1", "priority": "High", "category": "Operations"}
        ]
        mock_writer_cls.return_value.write_batch.return_value = [{"id": "new-task-1"}]
        mock_writer_cls.return_value._existing_titles = set()
        mock_dedup_cls.return_value.is_duplicate.return_value = (False, None, 0.0)
        # No recently-created tasks for dedup
        client.query_database.return_value = {"results": []}

        run_sync(config, client)

        mock_source.get_unprocessed_pages.assert_called_once_with(2)
        extract_kwargs = mock_extractor_cls.return_value.extract.call_args.kwargs
        assert "system_prompt" in extract_kwargs
        assert "user_prompt" in extract_kwargs
        assert "categories" in extract_kwargs
        # Verify placeholders were substituted
        assert "{{CATEGORIES}}" not in extract_kwargs["system_prompt"]
        assert "Santiago" in extract_kwargs["system_prompt"]  # team member
        write_call_args = mock_writer_cls.return_value.write_batch.call_args
        tasks_written = write_call_args.args[0]
        assert tasks_written[0]["meeting_page_id"] == "p1"
        mock_writer_cls.return_value.write_batch.assert_called_once()
        mock_source.mark_page_processed.assert_called_once_with("p1")

    @patch("src.pipeline.SemanticDedup")
    @patch("src.pipeline.OpenAI")
    @patch("src.pipeline.TeamTaskTrackerWriter")
    @patch("src.pipeline.AIExtractor")
    @patch("src.pipeline.HierarchyLoader")
    @patch("src.pipeline._fetch_page_text")
    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline._load_categories")
    def test_no_pages_does_nothing(
        self, mock_load_cats, mock_source_cls, mock_fetch_text,
        mock_hierarchy_cls, mock_extractor_cls, mock_writer_cls,
        mock_openai_cls, mock_dedup_cls,
    ):
        config = _make_config()
        client = MagicMock()
        client.list_users.return_value = []

        mock_load_cats.return_value = ["Other"]
        mock_source_cls.return_value.get_unprocessed_pages.return_value = []
        mock_fetch_text.return_value = "template"
        mock_hierarchy_cls.return_value.load.return_value = []
        mock_writer_cls.return_value._existing_titles = set()
        client.query_database.return_value = {"results": []}

        run_sync(config, client)

        mock_extractor_cls.return_value.extract.assert_not_called()

    @patch("src.pipeline.SemanticDedup")
    @patch("src.pipeline.OpenAI")
    @patch("src.pipeline.TeamTaskTrackerWriter")
    @patch("src.pipeline.AIExtractor")
    @patch("src.pipeline.HierarchyLoader")
    @patch("src.pipeline._fetch_page_text")
    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline._load_categories")
    def test_page_failure_continues_to_next(
        self, mock_load_cats, mock_source_cls, mock_fetch_text,
        mock_hierarchy_cls, mock_extractor_cls, mock_writer_cls,
        mock_openai_cls, mock_dedup_cls,
    ):
        config = _make_config()
        client = MagicMock()
        client.list_users.return_value = []

        mock_load_cats.return_value = ["Other"]
        mock_source = mock_source_cls.return_value
        mock_source.get_unprocessed_pages.return_value = [
            _make_page("p1", "Meeting 1"),
            _make_page("p2", "Meeting 2"),
        ]
        mock_source.get_page_content.side_effect = [Exception("API error"), "Content"]
        mock_source.get_page_metadata.return_value = {
            "title": "Meeting", "date": "2026-03-28",
            "meeting_type": "Other", "attendees": [],
            "created_by": {"id": "u1", "name": "Santiago"},
        }

        mock_fetch_text.return_value = "template"
        mock_hierarchy_cls.return_value.load.return_value = []
        mock_extractor_cls.return_value.extract.return_value = []
        mock_writer_cls.return_value._existing_titles = set()
        client.query_database.return_value = {"results": []}

        run_sync(config, client)

        assert mock_source.mark_page_processed.call_count == 1
        mock_source.mark_page_processed.assert_called_with("p2")


class TestArchiveDoneTasks:
    def test_archives_done_tasks(self):
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                {
                    "id": "task-1",
                    "properties": {"Task": {"type": "title", "title": [{"plain_text": "Old done task"}]}},
                },
                {
                    "id": "task-2",
                    "properties": {"Task": {"type": "title", "title": [{"plain_text": "Another done"}]}},
                },
            ]
        }

        archived = _archive_done_tasks(client, "db-tracker", grace_days=3)

        assert archived == 2
        assert client.archive_page.call_count == 2
        client.archive_page.assert_any_call("task-1")
        client.archive_page.assert_any_call("task-2")

    def test_dry_run_does_not_archive(self):
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                {"id": "task-1", "properties": {"Task": {"type": "title", "title": [{"plain_text": "Done task"}]}}},
            ]
        }

        archived = _archive_done_tasks(client, "db-tracker", grace_days=3, dry_run=True)

        assert archived == 0
        client.archive_page.assert_not_called()

    def test_no_done_tasks(self):
        client = MagicMock()
        client.query_database.return_value = {"results": []}

        archived = _archive_done_tasks(client, "db-tracker", grace_days=3)

        assert archived == 0
        client.archive_page.assert_not_called()

    def test_continues_on_individual_failure(self):
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                {"id": "task-1", "properties": {"Task": {"type": "title", "title": [{"plain_text": "Fail"}]}}},
                {"id": "task-2", "properties": {"Task": {"type": "title", "title": [{"plain_text": "OK"}]}}},
            ]
        }
        client.archive_page.side_effect = [Exception("API error"), {"id": "task-2"}]

        archived = _archive_done_tasks(client, "db-tracker", grace_days=3)

        assert archived == 1
        assert client.archive_page.call_count == 2


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


class TestAssigneeFallback:
    @patch("src.pipeline.SemanticDedup")
    @patch("src.pipeline.OpenAI")
    @patch("src.pipeline.TeamTaskTrackerWriter")
    @patch("src.pipeline.AIExtractor")
    @patch("src.pipeline.HierarchyLoader")
    @patch("src.pipeline._fetch_page_text")
    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline._load_categories")
    def test_fallback_to_meeting_creator(
        self, mock_load_cats, mock_source_cls, mock_fetch_text,
        mock_hierarchy_cls, mock_extractor_cls, mock_writer_cls,
        mock_openai_cls, mock_dedup_cls,
    ):
        """Tasks without assignee_id get the meeting creator as fallback."""
        config = _make_config()
        client = MagicMock()
        client.list_users.return_value = [
            {"id": "u1", "name": "Santiago", "type": "person", "person": {"email": "santiago@kibo.com"}},
        ]

        mock_load_cats.return_value = ["Operations", "Other"]
        mock_source = mock_source_cls.return_value
        mock_source.get_unprocessed_pages.return_value = [_make_page("p1", "Standup")]
        mock_source.get_page_content.return_value = "Vicente - contacto con Clikalia"
        mock_source.get_page_metadata.return_value = {
            "title": "Standup",
            "date": "2026-03-28",
            "meeting_type": "Team sync",
            "attendees": [],
            "created_by": {"id": "creator-1", "name": "Reyes"},
        }

        mock_fetch_text.return_value = "template {{CATEGORIES}} {{HIERARCHY}} {{EXISTING_TASKS}} {{TEAM_MEMBERS}} {{ATTENDEES}} {{MEETING_CREATOR}}"
        mock_hierarchy_cls.return_value.load.return_value = []
        # AI returns a task WITHOUT assignee_id
        mock_extractor_cls.return_value.extract.return_value = [
            {"title": "Vicente - contacto con Clikalia", "priority": "Medium", "category": "Other"}
        ]
        mock_writer_cls.return_value.write_batch.return_value = [{"id": "new-task-1"}]
        mock_writer_cls.return_value._existing_titles = set()
        mock_dedup_cls.return_value.is_duplicate.return_value = (False, None, 0.0)
        client.query_database.return_value = {"results": []}

        run_sync(config, client)

        tasks_written = mock_writer_cls.return_value.write_batch.call_args.args[0]
        assert tasks_written[0]["assignee_id"] == "creator-1"


class TestCrossMeetingDedupContext:
    @patch("src.pipeline.SemanticDedup")
    @patch("src.pipeline.OpenAI")
    @patch("src.pipeline.TeamTaskTrackerWriter")
    @patch("src.pipeline.AIExtractor")
    @patch("src.pipeline.HierarchyLoader")
    @patch("src.pipeline._fetch_page_text")
    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline._load_categories")
    def test_second_meeting_prompt_includes_first_meeting_tasks(
        self, mock_load_cats, mock_source_cls, mock_fetch_text,
        mock_hierarchy_cls, mock_extractor_cls, mock_writer_cls,
        mock_openai_cls, mock_dedup_cls,
    ):
        """After processing meeting 1, meeting 2's prompt should include
        meeting 1's tasks in {{EXISTING_TASKS}}."""
        config = _make_config()
        client = MagicMock()
        client.list_users.return_value = []

        mock_load_cats.return_value = ["Operations"]
        mock_source = mock_source_cls.return_value
        mock_source.get_unprocessed_pages.return_value = [
            _make_page("p1", "Meeting A"),
            _make_page("p2", "Meeting B"),
        ]
        mock_source.get_page_content.return_value = "some content"

        # Each meeting returns different metadata (different titles for fingerprinting)
        mock_source.get_page_metadata.side_effect = [
            {"title": "Meeting A", "date": "2026-03-28", "meeting_type": "Other",
             "attendees": [], "created_by": {"id": "u1", "name": "Santiago"}},
            {"title": "Meeting B", "date": "2026-03-28", "meeting_type": "Other",
             "attendees": [], "created_by": {"id": "u1", "name": "Santiago"}},
        ]

        mock_fetch_text.return_value = "template {{CATEGORIES}} {{HIERARCHY}} {{EXISTING_TASKS}} {{TEAM_MEMBERS}} {{ATTENDEES}} {{MEETING_CREATOR}}"
        mock_hierarchy_cls.return_value.load.return_value = [
            {"id": "cat1", "title": "Ops", "children": []}
        ]
        # Meeting A extracts 1 task; Meeting B extracts 1 task
        mock_extractor = mock_extractor_cls.return_value
        mock_extractor.extract.side_effect = [
            [{"title": "Task from Meeting A", "priority": "High", "category": "Operations",
              "parent_task_id": "cat1"}],
            [{"title": "Task from Meeting B", "priority": "Medium", "category": "Operations"}],
        ]
        mock_writer_cls.return_value.write_batch.return_value = [{"id": "new-1"}]
        mock_writer_cls.return_value._existing_titles = set()
        mock_dedup_cls.return_value.is_duplicate.return_value = (False, None, 0.0)
        client.query_database.return_value = {"results": []}

        run_sync(config, client)

        # The second call to extract should have the first meeting's task in prompt
        assert mock_extractor.extract.call_count == 2
        second_call_kwargs = mock_extractor.extract.call_args_list[1].kwargs
        system_prompt_2 = second_call_kwargs["system_prompt"]
        assert "Task from Meeting A" in system_prompt_2
        assert "(under: Ops)" in system_prompt_2
