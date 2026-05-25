"""Tests for single-page pipeline entry points."""
from unittest.mock import MagicMock, patch

import httpx
from notion_client import APIResponseError

from src.config import SyncConfig
from src.pipeline import run_inject_templates_for_page, run_sync_for_page


def _make_404_error(message: str = "Could not find block") -> APIResponseError:
    """Build a 404 APIResponseError matching the shape Notion's SDK raises."""
    return APIResponseError(
        code="object_not_found",
        status=404,
        message=message,
        headers=httpx.Headers(),
        raw_body_text="",
    )


def _make_config(**overrides) -> SyncConfig:
    defaults = {
        "notion_api_token": "secret_abc",
        "openai_api_key": "sk-abc",
        "meeting_notes_db_id": "db-meetings",
        "team_tracker_db_id": "db-tracker",
        "meeting_template_page_id": "tmpl-123",
    }
    defaults.update(overrides)
    return SyncConfig(**defaults)


class TestRunInjectTemplatesForPage:
    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline.inject_notes_section")
    @patch("src.pipeline.fetch_template")
    def test_injects_and_marks(self, mock_fetch, mock_inject, mock_source_cls):
        config = _make_config()
        client = MagicMock()
        mock_fetch.return_value = ([{"type": "heading_2"}], ("heading_2", "notes"))
        mock_inject.return_value = True

        result = run_inject_templates_for_page(config, client, "page-1")

        assert result is True
        mock_inject.assert_called_once_with(client, "page-1", [{"type": "heading_2"}], ("heading_2", "notes"))
        mock_source_cls.return_value.mark_template_injected.assert_called_once_with("page-1")

    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline.inject_notes_section")
    @patch("src.pipeline.fetch_template")
    def test_skips_when_already_present(self, mock_fetch, mock_inject, mock_source_cls):
        config = _make_config()
        client = MagicMock()
        mock_fetch.return_value = ([{"type": "heading_2"}], ("heading_2", "notes"))
        mock_inject.return_value = False  # already present

        result = run_inject_templates_for_page(config, client, "page-1")

        assert result is False
        mock_source_cls.return_value.mark_template_injected.assert_not_called()

    @patch("src.pipeline.fetch_template")
    def test_skips_when_no_template_configured(self, mock_fetch):
        config = _make_config(meeting_template_page_id=None)
        client = MagicMock()

        result = run_inject_templates_for_page(config, client, "page-1")

        assert result is False
        mock_fetch.assert_not_called()

    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline.inject_notes_section")
    @patch("src.pipeline.fetch_template")
    def test_dry_run_does_not_mark(self, mock_fetch, mock_inject, mock_source_cls):
        config = _make_config(dry_run=True)
        client = MagicMock()
        mock_fetch.return_value = ([{"type": "heading_2"}], ("heading_2", "notes"))
        mock_inject.return_value = True

        result = run_inject_templates_for_page(config, client, "page-1")

        assert result is True
        mock_source_cls.return_value.mark_template_injected.assert_not_called()

    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline.inject_notes_section")
    @patch("src.pipeline.fetch_template")
    def test_skips_when_page_archived(self, mock_fetch, mock_inject, mock_source_cls):
        """Archived pages return 404 from blocks.children.list — skip cleanly."""
        config = _make_config()
        client = MagicMock()
        client.get_page.return_value = {
            "id": "page-1",
            "archived": True,
            "parent": {"type": "database_id", "database_id": "db-meetings"},
        }
        mock_fetch.return_value = ([{"type": "heading_2"}], ("heading_2", "notes"))

        result = run_inject_templates_for_page(config, client, "page-1")

        assert result is False
        mock_inject.assert_not_called()

    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline.inject_notes_section")
    @patch("src.pipeline.fetch_template")
    def test_skips_when_page_in_trash(self, mock_fetch, mock_inject, mock_source_cls):
        config = _make_config()
        client = MagicMock()
        client.get_page.return_value = {
            "id": "page-1",
            "in_trash": True,
            "parent": {"type": "database_id", "database_id": "db-meetings"},
        }
        mock_fetch.return_value = ([{"type": "heading_2"}], ("heading_2", "notes"))

        result = run_inject_templates_for_page(config, client, "page-1")

        assert result is False
        mock_inject.assert_not_called()

    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline.inject_notes_section")
    @patch("src.pipeline.fetch_template")
    def test_skips_when_get_page_404(self, mock_fetch, mock_inject, mock_source_cls):
        """Page deleted before webhook arrived — pages.retrieve returns 404."""
        config = _make_config()
        client = MagicMock()
        client.get_page.side_effect = _make_404_error()
        mock_fetch.return_value = ([{"type": "heading_2"}], ("heading_2", "notes"))

        result = run_inject_templates_for_page(config, client, "page-1")

        assert result is False
        mock_inject.assert_not_called()

    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline.inject_notes_section")
    @patch("src.pipeline.fetch_template")
    def test_skips_when_inject_404_race(self, mock_fetch, mock_inject, mock_source_cls):
        """Page deleted between get_page and inject — 404 on blocks.children.list."""
        config = _make_config()
        client = MagicMock()
        client.get_page.return_value = {
            "id": "page-1",
            "parent": {"type": "database_id", "database_id": "db-meetings"},
        }
        mock_fetch.return_value = ([{"type": "heading_2"}], ("heading_2", "notes"))
        mock_inject.side_effect = _make_404_error()

        result = run_inject_templates_for_page(config, client, "page-1")

        assert result is False
        mock_source_cls.return_value.mark_template_injected.assert_not_called()


class TestRunSyncForPage:
    def test_skips_already_processed_page(self):
        config = _make_config()
        client = MagicMock()
        client.get_page.return_value = {
            "id": "page-1",
            "parent": {"type": "database_id", "database_id": "db-meetings"},
            "properties": {
                "Processed": {"type": "checkbox", "checkbox": True},
                "Date": {"type": "date", "date": {"start": "2026-01-01"}},
            },
        }

        run_sync_for_page(config, client, "page-1")

        # Should return early without calling any extraction
        client.list_users.assert_not_called()

    @patch("src.pipeline._load_sync_context")
    @patch("src.pipeline._build_seen_fingerprints")
    @patch("src.pipeline.SingleSource")
    def test_marks_empty_page_as_processed(
        self, mock_source_cls, mock_fingerprints, mock_ctx,
    ):
        config = _make_config()
        client = MagicMock()
        client.get_page.return_value = {
            "id": "page-1",
            "parent": {"type": "database_id", "database_id": "db-meetings"},
            "properties": {
                "Processed": {"type": "checkbox", "checkbox": False},
                "Date": {"type": "date", "date": {"start": "2026-01-01"}},
                "Meeting": {"type": "title", "title": [{"plain_text": "Empty meeting"}]},
                "Meeting type": {"type": "select", "select": None},
                "Attendees": {"type": "people", "people": []},
            },
        }

        mock_fingerprints.return_value = set()
        mock_source = mock_source_cls.return_value
        mock_source.get_page_content.return_value = "   "  # empty content
        mock_source.get_page_metadata.return_value = {
            "title": "Empty meeting", "date": "2026-01-01",
            "meeting_type": "", "attendees": [],
        }

        mock_ctx.return_value = {
            "system_prompt_template": "template", "user_prompt_template": "template",
            "hierarchy": [], "categories": ["Other"],
            "all_users": [], "existing_tasks": [], "deals": [],
            "extractor": MagicMock(), "writer": MagicMock(), "semantic_dedup": None,
        }

        run_sync_for_page(config, client, "page-1")

        mock_source.mark_page_processed.assert_called_once_with("page-1")
        mock_ctx.return_value["extractor"].extract.assert_not_called()

    @patch("src.pipeline._load_sync_context")
    @patch("src.pipeline._build_seen_fingerprints")
    @patch("src.pipeline.SingleSource")
    def test_dedup_marks_processed_and_skips(
        self, mock_source_cls, mock_fingerprints, mock_ctx,
    ):
        config = _make_config()
        client = MagicMock()
        client.get_page.return_value = {
            "id": "page-1",
            "parent": {"type": "database_id", "database_id": "db-meetings"},
            "properties": {
                "Processed": {"type": "checkbox", "checkbox": False},
                "Date": {"type": "date", "date": {"start": "2026-01-01"}},
                "Meeting": {"type": "title", "title": [{"plain_text": "Standup"}]},
                "Meeting type": {"type": "select", "select": None},
                "Attendees": {"type": "people", "people": []},
            },
        }

        # Fingerprint is now (db_id|title|date) — db prefix is the page's parent DB.
        mock_fingerprints.return_value = {"dbmeetings|standup|2026-01-01"}
        mock_source = mock_source_cls.return_value
        mock_source.get_page_metadata.return_value = {
            "title": "Standup", "date": "2026-01-01",
            "meeting_type": "", "attendees": [],
        }

        mock_ctx.return_value = {
            "system_prompt_template": "template", "user_prompt_template": "template",
            "hierarchy": [], "categories": ["Other"],
            "all_users": [], "existing_tasks": [], "deals": [],
            "extractor": MagicMock(), "writer": MagicMock(), "semantic_dedup": None,
        }

        run_sync_for_page(config, client, "page-1")

        mock_source.mark_page_processed.assert_called_once_with("page-1")
        mock_ctx.return_value["extractor"].extract.assert_not_called()
