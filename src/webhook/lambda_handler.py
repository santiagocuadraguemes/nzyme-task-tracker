"""AWS Lambda handler — webhook (template injection) + Notion → Supabase sync.

Task extraction was carved out to the standalone ``nzyme-task-extraction``
project (2026-06-15); this handler no longer runs any extraction.
"""
from __future__ import annotations

import json
import logging
import os

from notion_client import Client as NotionClient

from src.config import load_config
from src.notion_client_wrapper import NotionClientWrapper
from src.supabase_sync import run_full as supabase_run_full
from src.supabase_sync import run_incremental as supabase_run_incremental
from src.utils.llm_logging import configure_logfire
from src.utils.logger import setup_logging
from src.webhook.handler import handle_automation_webhook

# Configure logging at cold start so noisy 3rd-party loggers (httpx, botocore,
# googleapiclient, etc.) are capped at WARNING. AWS owns the root handler in
# Lambda; setup_logging only adjusts levels there.
setup_logging(os.environ.get("LOG_LEVEL", "INFO"))

# Wire logfire so OpenAI + native Gemini token usage / spans show up there
# (was previously only configured in CLI, leaving Lambda emitting
# LogfireNotConfiguredWarning). configure_logfire also instruments the
# native google-genai SDK (used for gemini-* models, which bypasses the
# OpenAI client) and enables GenAI message-content capture. In Lambda,
# scrubbing always stays on regardless of NZYME_DEBUG_LLM.
configure_logfire(os.environ.get("LOGFIRE_TOKEN"), service_name="nzyme-lambda")

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
    - CloudWatch Events with {"job": "supabase_sync"} → Notion → Supabase mirror
    - CloudWatch Events with {"job": "supabase_sync_full"} → weekly safety sweep

    NOTE: task extraction was carved out to the standalone nzyme-task-extraction
    project (2026-06-15); hierarchy_sync (daily) + weekly_archive (Sunday) were
    carved out to nzyme-housekeeping (org account, 2026-06-11).
    """
    # CloudWatch Events cron — silent unless there's actual work
    if event.get("source") == "aws.events":
        job = event.get("job")
        if job == "supabase_sync":
            return _handle_supabase_sync(event, context)
        if job == "supabase_sync_full":
            return _handle_supabase_sync_full(event, context)
        logger.warning("Unknown cron job: %s", job)
        return {"statusCode": 400, "body": json.dumps({"error": "unknown job"})}

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


def _handle_supabase_sync(event, context):
    """5-min cron: incremental Notion → Supabase sync.

    For each Meeting Notes DB, pull pages whose `last_edited_time` is past
    the per-DB checkpoint stored in Supabase and upsert them. Catches new
    meetings, async transcript completion, and any later edits.
    """
    config, client = _init()
    try:
        upserted = supabase_run_incremental(config, client)
        # Heartbeat — ALWAYS at INFO, even on no-change ticks. The mirror is
        # the read surface for the consumer Lambdas (fundraising/extraction/
        # topic-mirror), so a CloudWatch metric filter counts this exact line
        # and the SupabaseSyncStalled alarm fires when it goes missing.
        # Don't reword without updating the filter in template.yaml.
        logger.info("supabase sync heartbeat: upserted=%d", upserted)
        return {"statusCode": 200, "body": json.dumps({"upserted": upserted})}
    except Exception:
        logger.exception("Supabase incremental sync failed")
        return {"statusCode": 500, "body": json.dumps({"error": "sync failed"})}


def _handle_supabase_sync_full(event, context):
    """Weekly Sunday sweep: re-sync every page edited in the last 14 days.

    Safety net for the 5-min incremental — catches edits that slipped past
    via Lambda outages, transient failures, or any case where Notion
    advances `last_edited_time` outside the incremental's filter window.
    """
    config, client = _init()
    try:
        upserted = supabase_run_full(config, client, lookback_days=14)
        logger.info("supabase weekly sweep: upserted=%d", upserted)
        return {"statusCode": 200, "body": json.dumps({"upserted": upserted})}
    except Exception:
        logger.exception("Supabase weekly sweep failed")
        return {"statusCode": 500, "body": json.dumps({"error": "sweep failed"})}
