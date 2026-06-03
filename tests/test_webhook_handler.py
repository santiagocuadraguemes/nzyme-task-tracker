"""Tests for the Notion automation webhook handler."""
from unittest.mock import MagicMock, patch

from src.config import SyncConfig
from src.meeting_db_registry import MeetingDB
from src.webhook.handler import handle_automation_webhook


def _make_config(**overrides) -> SyncConfig:
    defaults = {
        "notion_api_token": "secret_abc",
        "openai_api_key": "sk-abc",
        "meeting_notes_db_id": "db-meetings-1234",
        "team_tracker_db_id": "db-tracker",
        "merged_transcript_extraction_prompt_page_id": "page-merged",
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


def _client_with_page(created_time: str = "2026-04-28T10:30:00.000Z") -> MagicMock:
    client = MagicMock()
    client.get_page.return_value = {"id": "page-1", "created_time": created_time}
    return client


class TestHandleAutomationWebhook:
    @patch("src.webhook.handler.run_inject_templates_for_page")
    def test_injects_template_for_matching_db(self, mock_inject):
        config = _make_config()
        client = _client_with_page()
        mock_inject.return_value = True
        payload = _make_automation_payload("page-1")

        result = handle_automation_webhook(payload, config, client)

        assert result["status"] == "injected"
        assert result["page_id"] == "page-1"
        mock_inject.assert_called_once_with(config, client, "page-1")

    @patch("src.webhook.handler.run_inject_templates_for_page")
    def test_sets_date_to_created_time(self, mock_inject):
        config = _make_config()
        client = _client_with_page("2026-04-28T10:30:00.000Z")
        mock_inject.return_value = True
        payload = _make_automation_payload("page-1")

        handle_automation_webhook(payload, config, client)

        client.update_page.assert_called_once_with(
            page_id="page-1",
            properties={"Date": {"date": {"start": "2026-04-28T10:30:00.000Z"}}},
        )

    @patch("src.webhook.handler.run_inject_templates_for_page")
    def test_sets_date_even_when_injection_skipped(self, mock_inject):
        config = _make_config()
        client = _client_with_page("2026-04-28T10:30:00.000Z")
        mock_inject.return_value = False
        payload = _make_automation_payload("page-1")

        handle_automation_webhook(payload, config, client)

        client.update_page.assert_called_once()

    @patch("src.webhook.handler.run_inject_templates_for_page")
    def test_skipped_when_template_already_present(self, mock_inject):
        config = _make_config()
        client = _client_with_page()
        mock_inject.return_value = False
        payload = _make_automation_payload("page-1")

        result = handle_automation_webhook(payload, config, client)

        assert result["status"] == "skipped"

    @patch("src.webhook.handler._REGISTRY_LOOKUP_RETRY_DELAY_SECONDS", 0)
    def test_ignores_wrong_database(self):
        config = _make_config()
        client = _client_with_page()
        payload = _make_automation_payload("page-1", database_id="other-db-5678")

        result = handle_automation_webhook(payload, config, client)

        assert result["status"] == "ignored"
        assert result["reason"] == "unknown database"
        client.update_page.assert_not_called()

    @patch("src.webhook.handler._REGISTRY_LOOKUP_RETRY_DELAY_SECONDS", 0)
    @patch("src.webhook.handler.run_inject_templates_for_page")
    @patch("src.webhook.handler.load_registry")
    def test_recovers_when_registry_load_is_partial(self, mock_load, mock_inject):
        """A partial registry read must not wrongly reject a real member DB.

        Regression for the prod bug where load_registry() intermittently
        returned 1-3 of 13 member DBs, so the page's DB looked unknown and
        template injection was skipped. The handler reloads on a miss.
        """
        config = _make_config()
        client = _client_with_page()
        mock_inject.return_value = True
        # First read omits the target DB (partial); reload includes it.
        mock_load.side_effect = [
            [MeetingDB(db_id="some-other-db", owner_name="X", owner_email="")],
            [MeetingDB(db_id="db-meetings-1234", owner_name="Guille", owner_email="")],
        ]
        payload = _make_automation_payload("page-1", database_id="db-meetings-1234")

        result = handle_automation_webhook(payload, config, client)

        assert result["status"] == "injected"
        assert mock_load.call_count == 2
        mock_inject.assert_called_once_with(config, client, "page-1")

    def test_ignores_non_automation_payload(self):
        config = _make_config()
        client = _client_with_page()
        payload = {"source": {"type": "webhook"}, "data": {"id": "page-1"}}

        result = handle_automation_webhook(payload, config, client)

        assert result["status"] == "ignored"
        assert "not an automation" in result["reason"]
        client.update_page.assert_not_called()

    def test_error_on_missing_page_id(self):
        config = _make_config()
        client = _client_with_page()
        payload = {"source": {"type": "automation"}, "data": {}}

        result = handle_automation_webhook(payload, config, client)

        assert result["status"] == "error"
        assert "missing" in result["reason"]
        client.update_page.assert_not_called()

    @patch("src.webhook.handler.run_inject_templates_for_page")
    def test_matches_db_id_ignoring_hyphens(self, mock_inject):
        """DB IDs from Notion payloads have hyphens; config may not."""
        config = _make_config(meeting_notes_db_id="dbmeetings1234")
        client = _client_with_page()
        mock_inject.return_value = True
        payload = _make_automation_payload("page-1", database_id="db-meetings-1234")

        result = handle_automation_webhook(payload, config, client)

        assert result["status"] == "injected"
