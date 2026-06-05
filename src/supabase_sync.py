"""Incremental + full Notion → Supabase sync.

`sync_incremental`: per-tick (5 min cron). For each Meeting Notes DB, pulls
pages whose `last_edited_time` is past the per-DB checkpoint stored in
Supabase, and upserts them. Cheap when nothing has changed.

`sync_full`: weekly safety net. Re-syncs every page modified within the
last `lookback_days`, regardless of checkpoint. Catches any edits that
slipped past the incremental tick (Lambda outages, transient errors, the
rare case where Notion advances `last_edited_time` after our query window).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from src.meeting_db_registry import MeetingDB, load_registry
from src.meeting_row import extract_row
from src.notion_client_wrapper import NotionClientWrapper
from src.supabase_writer import fetch_max_last_edited, upsert_meetings

logger = logging.getLogger(__name__)

# Batch size for Supabase upsert POSTs. Rows can be ~50KB each (transcript +
# summary); 10 keeps a single POST under ~500KB which PostgREST handles cleanly.
_UPSERT_BATCH = 10


def _query_pages_since(
    client: NotionClientWrapper,
    db_id: str,
    since: str | None,
) -> list[dict[str, Any]]:
    """Return pages from `db_id` with last_edited_time > `since` (ISO 8601).

    When `since` is None, returns every page in the DB.
    """
    kwargs: dict[str, Any] = {"database_id": db_id}
    if since:
        kwargs["filter"] = {
            "timestamp": "last_edited_time",
            "last_edited_time": {"after": since},
        }
    kwargs["sorts"] = [{
        "timestamp": "last_edited_time",
        "direction": "ascending",
    }]
    resp = client.query_database(**kwargs)
    return resp.get("results", [])


def _sync_db(
    db: MeetingDB,
    client: NotionClientWrapper,
    since: str | None,
    *,
    config=None,
) -> int:
    """Sync one DB; return number of rows upserted.

    When ``config`` is provided, GCal attendee resolution runs for every
    extracted page (`attendee_emails`) — the mirror is the complete record.
    """
    try:
        pages = _query_pages_since(client, db.db_id, since)
    except Exception:
        logger.exception(
            "Failed to query DB %s (%s) — skipping",
            db.db_id, db.owner_name or "?",
        )
        return 0

    if not pages:
        logger.debug("  %s: no pages newer than %s", db.owner_name or "?", since)
        return 0

    logger.info(
        "  %s: %d page(s) to sync (since %s)",
        db.owner_name or "?", len(pages), since or "epoch",
    )

    batch: list[dict[str, Any]] = []
    written = 0
    for page in pages:
        try:
            row = extract_row(
                page, db, client,
                config=config,
                resolve_attendees=config is not None,
            )
            # Never downgrade: a None here (resolution off / GCal failure /
            # no emails found) must not NULL out previously stored emails.
            if row.get("attendee_emails") is None:
                row.pop("attendee_emails", None)
        except Exception:
            logger.exception(
                "Failed to extract row for page %s — skipping",
                page.get("id"),
            )
            continue
        batch.append(row)
        if len(batch) >= _UPSERT_BATCH:
            try:
                upsert_meetings(batch)
                written += len(batch)
            except Exception:
                logger.exception(
                    "Upsert batch failed (%d rows) — will retry next tick",
                    len(batch),
                )
            batch.clear()
    if batch:
        try:
            upsert_meetings(batch)
            written += len(batch)
        except Exception:
            logger.exception(
                "Final upsert batch failed (%d rows) — will retry next tick",
                len(batch),
            )

    return written


def sync_incremental(
    client: NotionClientWrapper,
    registry: list[MeetingDB],
    config=None,
) -> int:
    """For each DB, pull pages edited since our checkpoint and upsert."""
    db_ids = [db.db_id for db in registry]
    try:
        checkpoints = fetch_max_last_edited(db_ids)
    except Exception:
        logger.exception(
            "Failed to read checkpoints from Supabase — defaulting to "
            "since=None (will sync every page; idempotent so safe).",
        )
        checkpoints = {}

    total = 0
    for db in registry:
        total += _sync_db(db, client, checkpoints.get(db.db_id), config=config)
    return total


def sync_full(
    client: NotionClientWrapper,
    registry: list[MeetingDB],
    lookback_days: int = 14,
    config=None,
) -> int:
    """Re-sync every page modified in the last `lookback_days`, all DBs."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=lookback_days)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    logger.info("full sweep: lookback=%dd cutoff=%s", lookback_days, cutoff)

    total = 0
    for db in registry:
        total += _sync_db(db, client, cutoff, config=config)
    return total


def run_incremental(config, client: NotionClientWrapper) -> int:
    """CLI/Lambda entry — discover registry and run incremental sync."""
    # include_inactive: the mirror covers EVERY member DB, not just active
    # members — fundraising meetings live in inactive partners' DBs and the
    # standalone fundraising consumer reads its candidates from the mirror.
    registry = load_registry(config, client, include_inactive=True)
    if not registry:
        logger.warning("No Meeting Notes DBs in registry; nothing to sync.")
        return 0
    return sync_incremental(client, registry, config=config)


def run_full(
    config,
    client: NotionClientWrapper,
    lookback_days: int = 14,
) -> int:
    """CLI/Lambda entry — discover registry and run full safety-net sweep."""
    registry = load_registry(config, client, include_inactive=True)
    if not registry:
        logger.warning("No Meeting Notes DBs in registry; nothing to sync.")
        return 0
    return sync_full(client, registry, lookback_days=lookback_days, config=config)
