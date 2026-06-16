"""Tests for the two Lambda entry points.

``webhook_handler`` backs the ``nzyme-webhook`` Lambda (API Gateway route
``POST /webhook/{token}``); ``cron_handler`` backs ``nzyme-task-tracker`` (the
two Supabase-sync Schedule events). Split 2026-06-16 — see the module docstring
in ``src/webhook/lambda_handler.py``.
"""
import json
from unittest.mock import MagicMock, patch

from src.webhook.lambda_handler import cron_handler, webhook_handler


class TestWebhookHandler:
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

        response = webhook_handler(event, None)

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

        response = webhook_handler(event, None)

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

        response = webhook_handler(event, None)

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

        response = webhook_handler(event, None)

        assert response["statusCode"] == 200

    def test_non_api_gateway_event_returns_400(self):
        # webhook_handler is wired only to the HttpApi route; a stray event
        # with neither requestContext nor pathParameters must not mis-route.
        response = webhook_handler({"source": "aws.events", "job": "supabase_sync"}, None)
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"] == "unknown event source"


class TestCronHandler:
    @patch("src.webhook.lambda_handler.supabase_run_incremental")
    @patch("src.webhook.lambda_handler._init")
    def test_supabase_sync_job(self, mock_init, mock_sync):
        config = MagicMock()
        mock_init.return_value = (config, MagicMock())
        mock_sync.return_value = 3

        event = {"source": "aws.events", "job": "supabase_sync"}
        response = cron_handler(event, None)

        assert response["statusCode"] == 200
        assert json.loads(response["body"])["upserted"] == 3

    @patch("src.webhook.lambda_handler.supabase_run_full")
    @patch("src.webhook.lambda_handler._init")
    def test_supabase_sync_full_job(self, mock_init, mock_sweep):
        config = MagicMock()
        mock_init.return_value = (config, MagicMock())
        mock_sweep.return_value = 7

        event = {"source": "aws.events", "job": "supabase_sync_full"}
        response = cron_handler(event, None)

        assert response["statusCode"] == 200
        assert json.loads(response["body"])["upserted"] == 7

    def test_unknown_cron_job_returns_400(self):
        # Extraction was carved out — a cron event with no recognized job
        # must NOT trigger extraction; it returns a clear 400.
        event = {"source": "aws.events", "detail-type": "Scheduled Event"}
        response = cron_handler(event, None)
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"] == "unknown job"

    def test_webhook_event_on_cron_handler_returns_400(self):
        # A webhook event misrouted to the cron handler has no `job` → 400,
        # never silently treated as a sync.
        event = {
            "requestContext": {"http": {"method": "POST"}},
            "pathParameters": {"token": "x"},
        }
        response = cron_handler(event, None)
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"] == "unknown job"
