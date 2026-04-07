from unittest.mock import MagicMock, patch, call

from src.config import SyncConfig
from src.pipeline import (
    run_inject_templates, run_sync, _archive_done_tasks,
    _inject_templates, _flatten_hierarchy, _load_existing_tasks,
    _substitute_placeholders, _format_existing_tasks,
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
        "properties": {
            "Meeting": {"type": "title", "title": [{"plain_text": title}]},
            "Date": {"type": "date", "date": {"start": "2026-03-28"}},
            "Meeting type": {"type": "select", "select": {"name": "Team sync"}},
            "Attendees": {"type": "people", "people": [{"id": "u1", "name": "Santiago"}]},
            "Processed": {"type": "checkbox", "checkbox": False},
        },
    }


class TestRunSync:
    @patch("src.pipeline.TeamTaskTrackerWriter")
    @patch("src.pipeline.AIExtractor")
    @patch("src.pipeline.HierarchyLoader")
    @patch("src.pipeline._fetch_page_text")
    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline._load_categories")
    def test_full_cycle(
        self, mock_load_cats, mock_source_cls, mock_fetch_text,
        mock_hierarchy_cls, mock_extractor_cls, mock_writer_cls,
    ):
        config = _make_config()
        client = MagicMock()
        client.list_users.return_value = [
            {"id": "u1", "name": "Santiago", "type": "person"},
            {"id": "u2", "name": "Reyes", "type": "person"},
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
        }

        mock_fetch_text.return_value = "prompt template with {{CATEGORIES}} {{HIERARCHY}} {{EXISTING_TASKS}} {{TEAM_MEMBERS}} {{ATTENDEES}}"
        mock_hierarchy_cls.return_value.load.return_value = [{"id": "cat1", "title": "Ops"}]
        mock_extractor_cls.return_value.extract.return_value = [
            {"title": "Review doc", "assignee_id": "u1", "priority": "High", "category": "Operations"}
        ]
        mock_writer_cls.return_value.write_batch.return_value = [{"id": "new-task-1"}]
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

    @patch("src.pipeline.TeamTaskTrackerWriter")
    @patch("src.pipeline.AIExtractor")
    @patch("src.pipeline.HierarchyLoader")
    @patch("src.pipeline._fetch_page_text")
    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline._load_categories")
    def test_no_pages_does_nothing(
        self, mock_load_cats, mock_source_cls, mock_fetch_text,
        mock_hierarchy_cls, mock_extractor_cls, mock_writer_cls,
    ):
        config = _make_config()
        client = MagicMock()
        client.list_users.return_value = []

        mock_load_cats.return_value = ["Other"]
        mock_source_cls.return_value.get_unprocessed_pages.return_value = []
        mock_fetch_text.return_value = "template"
        mock_hierarchy_cls.return_value.load.return_value = []
        client.query_database.return_value = {"results": []}

        run_sync(config, client)

        mock_extractor_cls.return_value.extract.assert_not_called()

    @patch("src.pipeline.TeamTaskTrackerWriter")
    @patch("src.pipeline.AIExtractor")
    @patch("src.pipeline.HierarchyLoader")
    @patch("src.pipeline._fetch_page_text")
    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline._load_categories")
    def test_page_failure_continues_to_next(
        self, mock_load_cats, mock_source_cls, mock_fetch_text,
        mock_hierarchy_cls, mock_extractor_cls, mock_writer_cls,
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
        }

        mock_fetch_text.return_value = "template"
        mock_hierarchy_cls.return_value.load.return_value = []
        mock_extractor_cls.return_value.extract.return_value = []
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
