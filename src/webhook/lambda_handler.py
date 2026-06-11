"""AWS Lambda handler — single function for both webhook and scheduled extraction."""
from __future__ import annotations

import json
import logging
import os

from notion_client import APIResponseError, Client as NotionClient

from src.config import load_config
from src.meeting_db_registry import load_registry
from src.notion_client_wrapper import NotionClientWrapper
from src.pipeline import run_sync_for_page
from src.sources.single_source import SingleSource
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
    - CloudWatch Events (default) → scheduled extraction

    NOTE: hierarchy_sync (daily) + weekly_archive (Sunday) were carved out to the
    standalone nzyme-housekeeping Lambda (org account, 2026-06-11).
    """
    # CloudWatch Events cron — silent unless there's actual work
    if event.get("source") == "aws.events":
        job = event.get("job")
        if job == "supabase_sync":
            return _handle_supabase_sync(event, context)
        if job == "supabase_sync_full":
            return _handle_supabase_sync_full(event, context)
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

    # include_inactive: poll inactive members' DBs too so their meetings
    # still reach the Supabase mirror. Inactive members' pages skip task
    # extraction (gated in process_meeting by owner.active).
    try:
        registry = load_registry(config, client, include_inactive=True)
    except Exception:
        logger.exception("Failed to load Meeting Notes DB registry — aborting tick")
        return {"statusCode": 500, "body": json.dumps({"error": "registry load failed"})}

    # Gather ready pages across all per-member DBs.
    ready: list[tuple[dict, str]] = []  # (page, owner_name)
    for member_db in registry:
        try:
            source = SingleSource(client, member_db.db_id)
            for page in source.get_ready_pages(idle_minutes=config.idle_minutes):
                ready.append((page, member_db.owner_name or "?"))
        except APIResponseError as e:
            # Notion transient outage (429 / 5xx) after retries exhausted —
            # next 5-min cron tick will pick up this DB, so log as WARNING
            # without a stack trace rather than alarming as ERROR.
            if e.status in (429, 500, 502, 503, 504):
                logger.warning(
                    "Notion transient %s for db=%s (%s) — will retry next cycle",
                    e.status, member_db.db_id, member_db.owner_name or "?",
                )
            else:
                logger.exception(
                    "Failed to query ready pages for db=%s (%s) — skipping this DB",
                    member_db.db_id, member_db.owner_name or "?",
                )
        except Exception:
            logger.exception(
                "Failed to query ready pages for db=%s (%s) — skipping this DB",
                member_db.db_id, member_db.owner_name or "?",
            )

    processed = 0
    if ready:
        logger.info(
            "cron tick: %d page(s) ready for extraction across %d DB(s)",
            len(ready), len(registry),
        )
        for page, owner in ready:
            page_id = page["id"]
            try:
                run_sync_for_page(config, client, page_id)
                processed += 1
            except Exception:
                logger.exception(
                    "Failed to process page %s (db_owner=%s) — will retry next cycle",
                    page_id, owner,
                )
    else:
        logger.debug("cron tick: 0 pages ready across %d DB(s)", len(registry))

    logger.info("cron tick complete: processed=%d", processed)
    return {"statusCode": 200, "body": json.dumps({"processed": processed})}


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
