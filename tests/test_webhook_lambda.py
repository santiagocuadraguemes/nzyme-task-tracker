"""Tests for the unified Lambda handler."""
import json
from unittest.mock import MagicMock, patch

from src.webhook.lambda_handler import handler


class TestWebhookRoute:
    @patch("src.webhook.lambda_handler.handle_automation_webhook")
    @patch("src.webhook.lambda_handler._init")
    def test_valid_token_processes_payload(self, mock_init, mock_handle):
        config = MagicMock()
        config.webhook_path_token = "my-secret"
        client = MagicMock()
        mock_init.return_value = (config, client)
        mock_handle.return_value = {"status": "injected", "page_id": "p1"}

        payload = {"source": {"type": "automation"}, "data": {"id": "p1"}}
        event = {
            "requestContext": {"http": {"method": "POST"}},
            "pathParameters": {"token": "my-secret"},
            "body": json.dumps(payload),
        }

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["status"] == "injected"
        mock_handle.assert_called_once_with(payload, config, client)

    @patch("src.webhook.lambda_handler._init")
    def test_invalid_token_returns_401(self, mock_init):
        config = MagicMock()
        config.webhook_path_token = "my-secret"
        mock_init.return_value = (config, MagicMock())

        event = {
            "requestContext": {"http": {"method": "POST"}},
            "pathParameters": {"token": "wrong-token"},
            "body": "{}",
        }

        response = handler(event, None)

        assert response["statusCode"] == 401

    @patch("src.webhook.lambda_handler._init")
    def test_invalid_json_returns_400(self, mock_init):
        config = MagicMock()
        config.webhook_path_token = None
        mock_init.return_value = (config, MagicMock())

        event = {
            "requestContext": {"http": {"method": "POST"}},
            "pathParameters": {},
            "body": "not valid json{{{",
        }

        response = handler(event, None)

        assert response["statusCode"] == 400

    @patch("src.webhook.lambda_handler.handle_automation_webhook")
    @patch("src.webhook.lambda_handler._init")
    def test_no_token_configured_allows_any(self, mock_init, mock_handle):
        config = MagicMock()
        config.webhook_path_token = None
        client = MagicMock()
        mock_init.return_value = (config, client)
        mock_handle.return_value = {"status": "skipped"}

        event = {
            "requestContext": {"http": {"method": "POST"}},
            "pathParameters": {"token": "anything"},
            "body": json.dumps({"source": {"type": "automation"}, "data": {}}),
        }

        response = handler(event, None)

        assert response["statusCode"] == 200


class TestExtractionRoute:
    @patch("src.webhook.lambda_handler._archive_done_tasks")
    @patch("src.webhook.lambda_handler.run_sync_for_page")
    @patch("src.webhook.lambda_handler.SingleSource")
    @patch("src.webhook.lambda_handler._init")
    def test_processes_ready_pages(self, mock_init, mock_source_cls, mock_sync, mock_archive):
        config = MagicMock()
        config.idle_minutes = 3
        config.meeting_notes_db_id = "db-meetings"
        config.team_tracker_db_id = "db-tracker"
        config.dry_run = False
        client = MagicMock()
        mock_init.return_value = (config, client)

        mock_source = mock_source_cls.return_value
        mock_source.get_ready_pages.return_value = [
            {"id": "page-1"},
            {"id": "page-2"},
        ]

        event = {"source": "aws.events", "detail-type": "Scheduled Event"}
        response = handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["processed"] == 2
        assert mock_sync.call_count == 2
        mock_source.get_ready_pages.assert_called_once_with(idle_minutes=3)

    @patch("src.webhook.lambda_handler.SingleSource")
    @patch("src.webhook.lambda_handler._init")
    def test_no_ready_pages(self, mock_init, mock_source_cls):
        config = MagicMock()
        config.idle_minutes = 3
        config.meeting_notes_db_id = "db-meetings"
        mock_init.return_value = (config, MagicMock())

        mock_source_cls.return_value.get_ready_pages.return_value = []

        event = {"source": "aws.events", "detail-type": "Scheduled Event"}
        response = handler(event, None)

        body = json.loads(response["body"])
        assert body["processed"] == 0

    @patch("src.webhook.lambda_handler._archive_done_tasks")
    @patch("src.webhook.lambda_handler.run_sync_for_page")
    @patch("src.webhook.lambda_handler.SingleSource")
    @patch("src.webhook.lambda_handler._init")
    def test_continues_on_page_failure(self, mock_init, mock_source_cls, mock_sync, mock_archive):
        config = MagicMock()
        config.idle_minutes = 3
        config.meeting_notes_db_id = "db-meetings"
        config.team_tracker_db_id = "db-tracker"
        config.dry_run = False
        client = MagicMock()
        mock_init.return_value = (config, client)

        mock_source_cls.return_value.get_ready_pages.return_value = [
            {"id": "page-1"},
            {"id": "page-2"},
        ]
        mock_sync.side_effect = [Exception("API error"), None]

        event = {"source": "aws.events", "detail-type": "Scheduled Event"}
        response = handler(event, None)

        body = json.loads(response["body"])
        assert body["processed"] == 1  # second page succeeded


class TestEventRouting:
    def test_unknown_event_returns_400(self):
        event = {"something": "unexpected"}
        response = handler(event, None)
        assert response["statusCode"] == 400
