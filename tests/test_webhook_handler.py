"""Tests for the Notion automation webhook handler."""
from unittest.mock import MagicMock, patch

from src.config import SyncConfig
from src.webhook.handler import handle_automation_webhook


def _make_config(**overrides) -> SyncConfig:
    defaults = {
        "notion_api_token": "secret_abc",
        "openai_api_key": "sk-abc",
        "meeting_notes_db_id": "db-meetings-1234",
        "team_tracker_db_id": "db-tracker",
        "playbook_page_id": "page-playbook",
    }
    defaults.update(overrides)
    return SyncConfig(**defaults)


def _make_automation_payload(page_id: str, database_id: str = "db-meetings-1234") -> dict:
    return {
        "source": {
            "type": "automation",
            "automation_id": "auto-1",
            "action_id": "action-1",
            "event_id": "event-1",
            "attempt": 1,
        },
        "data": {
            "object": "page",
            "id": page_id,
            "parent": {
                "type": "data_source_id",
                "data_source_id": "ds-1",
                "database_id": database_id,
            },
            "properties": {
                "Processed": {"type": "checkbox", "checkbox": False},
                "Meeting": {"type": "title", "title": [{"plain_text": "Standup"}]},
            },
        },
    }


class TestHandleAutomationWebhook:
    @patch("src.webhook.handler.run_inject_templates_for_page")
    def test_injects_template_for_matching_db(self, mock_inject):
        config = _make_config()
        client = MagicMock()
        mock_inject.return_value = True
        payload = _make_automation_payload("page-1")

        result = handle_automation_webhook(payload, config, client)

        assert result["status"] == "injected"
        assert result["page_id"] == "page-1"
        mock_inject.assert_called_once_with(config, client, "page-1")

    @patch("src.webhook.handler.run_inject_templates_for_page")
    def test_skipped_when_template_already_present(self, mock_inject):
        config = _make_config()
        client = MagicMock()
        mock_inject.return_value = False
        payload = _make_automation_payload("page-1")

        result = handle_automation_webhook(payload, config, client)

        assert result["status"] == "skipped"

    def test_ignores_wrong_database(self):
        config = _make_config()
        client = MagicMock()
        payload = _make_automation_payload("page-1", database_id="other-db-5678")

        result = handle_automation_webhook(payload, config, client)

        assert result["status"] == "ignored"
        assert result["reason"] == "wrong database"

    def test_ignores_non_automation_payload(self):
        config = _make_config()
        client = MagicMock()
        payload = {"source": {"type": "webhook"}, "data": {"id": "page-1"}}

        result = handle_automation_webhook(payload, config, client)

        assert result["status"] == "ignored"
        assert "not an automation" in result["reason"]

    def test_error_on_missing_page_id(self):
        config = _make_config()
        client = MagicMock()
        payload = {"source": {"type": "automation"}, "data": {}}

        result = handle_automation_webhook(payload, config, client)

        assert result["status"] == "error"
        assert "missing" in result["reason"]

    @patch("src.webhook.handler.run_inject_templates_for_page")
    def test_matches_db_id_ignoring_hyphens(self, mock_inject):
        """DB IDs from Notion payloads have hyphens; config may not."""
        config = _make_config(meeting_notes_db_id="dbmeetings1234")
        client = MagicMock()
        mock_inject.return_value = True
        payload = _make_automation_payload("page-1", database_id="db-meetings-1234")

        result = handle_automation_webhook(payload, config, client)

        assert result["status"] == "injected"
