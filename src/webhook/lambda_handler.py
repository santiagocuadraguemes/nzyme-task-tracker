"""AWS Lambda handler — single function for both webhook and scheduled extraction."""
from __future__ import annotations

import json
import logging
import os

from notion_client import Client as NotionClient

from src.config import load_config
from src.notion_client_wrapper import NotionClientWrapper
from src.pipeline import run_sync_for_page, _archive_done_tasks
from src.sources.single_source import SingleSource
from src.webhook.handler import handle_automation_webhook

logger = logging.getLogger(__name__)

# Lambda pre-configures the root logger. Set level on root so all our
# loggers (src.webhook, src.sources, src.pipeline, etc.) actually emit.
_log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO"), logging.INFO)
logging.getLogger().setLevel(_log_level)
logging.getLogger("src").setLevel(_log_level)


def _init():
    """Shared initialisation."""
    config = load_config()
    notion = NotionClient(auth=config.notion_api_token)
    client = NotionClientWrapper(notion)
    return config, client


def handler(event, context):
    """Unified Lambda entry point.

    Routes based on event source:
    - API Gateway (has "requestContext") → template injection via webhook
    - CloudWatch Events (has "source": "aws.events") → scheduled extraction
    """
    # CloudWatch Events cron
    if event.get("source") == "aws.events":
        print("[nzyme] Event routed to: extraction (CloudWatch cron)")
        logger.info("Event routed to: extraction (CloudWatch cron)")
        return _handle_extraction(event, context)

    # API Gateway (has requestContext or pathParameters)
    if event.get("requestContext") or event.get("pathParameters"):
        logger.info("Event routed to: webhook (API Gateway)")
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

    Triggered by CloudWatch Events rule (every 1 minute).
    """
    config, client = _init()
    logger.info("Extraction tick — db=%s, idle_minutes=%s", config.meeting_notes_db_id, config.idle_minutes)
    source = SingleSource(client, config.meeting_notes_db_id)

    pages = source.get_ready_pages(idle_minutes=config.idle_minutes)
    if not pages:
        logger.info("No pages ready for extraction")
        return {"statusCode": 200, "body": json.dumps({"processed": 0})}

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

    logger.info("Extraction cycle complete: %d pages processed", processed)
    return {"statusCode": 200, "body": json.dumps({"processed": processed})}
