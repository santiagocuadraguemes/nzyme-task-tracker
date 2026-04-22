"""AWS Lambda handler — single function for both webhook and scheduled extraction."""
from __future__ import annotations

import json
import logging
import os

import logfire
from notion_client import Client as NotionClient

from src.config import load_config
from src.notion_client_wrapper import NotionClientWrapper
from src.pipeline import run_sync_for_page, _archive_done_tasks
from src.sources.single_source import SingleSource
from src.utils.logger import setup_logging
from src.webhook.handler import handle_automation_webhook

# Configure logging at cold start so noisy 3rd-party loggers (httpx, botocore,
# googleapiclient, etc.) are capped at WARNING. AWS owns the root handler in
# Lambda; setup_logging only adjusts levels there.
setup_logging(os.environ.get("LOG_LEVEL", "INFO"))

# Wire logfire so OpenAI token usage / spans show up there (was previously
# only configured in CLI, leaving Lambda emitting LogfireNotConfiguredWarning).
logfire.configure(
    token=os.environ.get("LOGFIRE_TOKEN"),
    service_name="nzyme-lambda",
    send_to_logfire="if-token-present",
)
logfire.instrument_openai()

logger = logging.getLogger(__name__)


def _init():
    """Shared initialisation."""
    config = load_config()
    notion = NotionClient(auth=config.notion_api_token, notion_version="2026-03-11")
    client = NotionClientWrapper(notion)
    return config, client


def handler(event, context):
    """Unified Lambda entry point.

    Routes based on event source:
    - API Gateway (has "requestContext") → template injection via webhook
    - CloudWatch Events (has "source": "aws.events") → scheduled extraction
    """
    # CloudWatch Events cron — silent unless there's actual work
    if event.get("source") == "aws.events":
        return _handle_extraction(event, context)

    # API Gateway (has requestContext or pathParameters)
    if event.get("requestContext") or event.get("pathParameters"):
        logger.info("webhook received")
        return _handle_webhook(event, context)

    logger.warning("Unknown event source: %s", json.dumps(event)[:200])
    return {"statusCode": 400, "body": json.dumps({"error": "unknown event source"})}


def _handle_webhook(event, context):
    """Process a Notion automation webhook via API Gateway.

    API Gateway route: POST /webhook/{token}
    """
    config, client = _init()

    # Validate path token
    path_params = event.get("pathParameters") or {}
    token = path_params.get("token", "")
    if config.webhook_path_token and token != config.webhook_path_token:
        logger.warning("Invalid webhook path token")
        return {"statusCode": 401, "body": json.dumps({"error": "unauthorized"})}

    # Parse body
    body = event.get("body", "{}")
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode()
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        logger.error("Failed to parse webhook body")
        return {"statusCode": 400, "body": json.dumps({"error": "invalid json"})}

    # Handle the automation event
    try:
        result = handle_automation_webhook(payload, config, client)
        return {"statusCode": 200, "body": json.dumps(result)}
    except Exception:
        logger.exception("Webhook handler failed")
        return {"statusCode": 500, "body": json.dumps({"error": "internal error"})}


def _handle_extraction(event, context):
    """Scheduled extraction: find idle meeting pages and run AI extraction.

    Triggered by CloudWatch Events rule (every 1 minute). Stays silent at
    INFO when there's no work — only logs once a page is found.
    """
    config, client = _init()
    source = SingleSource(client, config.meeting_notes_db_id)

    pages = source.get_ready_pages(idle_minutes=config.idle_minutes)
    if not pages:
        logger.debug("cron tick: 0 pages ready")
        return {"statusCode": 200, "body": json.dumps({"processed": 0})}

    logger.info("cron tick: %d page(s) ready for extraction", len(pages))
    processed = 0
    for page in pages:
        page_id = page["id"]
        try:
            run_sync_for_page(config, client, page_id)
            processed += 1
        except Exception:
            logger.exception("Failed to process page %s — will retry next cycle", page_id)

    # Archive done tasks (3-day grace period)
    try:
        _archive_done_tasks(
            client, config.team_tracker_db_id, grace_days=3, dry_run=config.dry_run,
        )
    except Exception:
        logger.exception("Failed to archive done tasks")

    logger.info("cron tick complete: %d page(s) processed", processed)
    return {"statusCode": 200, "body": json.dumps({"processed": processed})}
