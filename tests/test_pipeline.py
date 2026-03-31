from unittest.mock import MagicMock, patch, call

from src.config import SyncConfig
from src.pipeline import run_sync


def _make_config(**overrides) -> SyncConfig:
    defaults = {
        "notion_api_token": "secret_abc",
        "openai_api_key": "sk-abc",
        "meeting_notes_db_id": "db-meetings",
        "team_tracker_db_id": "db-tracker",
        "playbook_page_id": "page-playbook",
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
    @patch("src.pipeline.PlaybookLoader")
    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline._load_categories")
    def test_full_cycle(
        self, mock_load_cats, mock_source_cls, mock_playbook_cls,
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

        mock_playbook_cls.return_value.load.return_value = "playbook rules"
        mock_hierarchy_cls.return_value.load.return_value = [{"id": "cat1", "title": "Ops"}]
        mock_extractor_cls.return_value.extract.return_value = [
            {"title": "Review doc", "assignee_id": "u1", "priority": "High", "category": "Operations"}
        ]
        mock_writer_cls.return_value.write_batch.return_value = [{"id": "new-task-1"}]

        run_sync(config, client)

        mock_source.get_unprocessed_pages.assert_called_once_with(2)
        extract_kwargs = mock_extractor_cls.return_value.extract.call_args.kwargs
        assert extract_kwargs["team_members"] == [
            {"id": "u1", "name": "Santiago"},
            {"id": "u2", "name": "Reyes"},
        ]
        write_call_args = mock_writer_cls.return_value.write_batch.call_args
        tasks_written = write_call_args.args[0]
        assert tasks_written[0]["meeting_page_id"] == "p1"
        mock_writer_cls.return_value.write_batch.assert_called_once()
        mock_source.mark_page_processed.assert_called_once_with("p1")

    @patch("src.pipeline.TeamTaskTrackerWriter")
    @patch("src.pipeline.AIExtractor")
    @patch("src.pipeline.HierarchyLoader")
    @patch("src.pipeline.PlaybookLoader")
    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline._load_categories")
    def test_no_pages_does_nothing(
        self, mock_load_cats, mock_source_cls, mock_playbook_cls,
        mock_hierarchy_cls, mock_extractor_cls, mock_writer_cls,
    ):
        config = _make_config()
        client = MagicMock()
        client.list_users.return_value = []

        mock_load_cats.return_value = ["Other"]
        mock_source_cls.return_value.get_unprocessed_pages.return_value = []
        mock_playbook_cls.return_value.load.return_value = "rules"
        mock_hierarchy_cls.return_value.load.return_value = []

        run_sync(config, client)

        mock_extractor_cls.return_value.extract.assert_not_called()

    @patch("src.pipeline.TeamTaskTrackerWriter")
    @patch("src.pipeline.AIExtractor")
    @patch("src.pipeline.HierarchyLoader")
    @patch("src.pipeline.PlaybookLoader")
    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline._load_categories")
    def test_page_failure_continues_to_next(
        self, mock_load_cats, mock_source_cls, mock_playbook_cls,
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

        mock_playbook_cls.return_value.load.return_value = "rules"
        mock_hierarchy_cls.return_value.load.return_value = []
        mock_extractor_cls.return_value.extract.return_value = []

        run_sync(config, client)

        assert mock_source.mark_page_processed.call_count == 1
        mock_source.mark_page_processed.assert_called_with("p2")
