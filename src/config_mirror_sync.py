"""Org Chart + Meeting Rules → Supabase canonical mirrors.

Two small sub-syncs riding the 5-min Notion → Supabase sync job
(``supabase_sync.run_incremental`` / ``run_full``):

- ``sync_org_chart`` → ``public.org_chart_rows`` — EVERY Org Chart row,
  including inactive members and members without a Meeting Notes DB (unlike
  ``discover_meeting_dbs``, which only returns pollable DBs).
- ``sync_meeting_rules`` → ``public.meeting_rule_rows`` — every parseable
  rule, including inactive ones (``active`` is a column, not a filter, so a
  consumer can distinguish "rule turned off" from "rule deleted").

Together with ``meeting_transcripts`` these make Supabase the complete read
surface for the consumer Lambdas (fundraising / extraction / topic-mirror):
member config and routing rules no longer require a Notion query at
consumption time. Notion stays the editing UI; edits land here on the next
sync tick (≤5 min).

Same conventions as the hierarchy canonical mirrors: ``notion_page_id`` is
the stable identity, ``deleted_at`` is a tombstone for rows that vanished
from Notion (set on disappearance, cleared if the row reappears), and all
I/O goes through the stdlib ``_http`` helper (service-role key).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.hierarchy.canonical_mirror_sync import _http
from src.meeting_db_registry import _extract_db_id
from src.meeting_row import _hex_to_uuid
from src.notion_client_wrapper import NotionClientWrapper
from src.topic_mirror.route_registry import (
    ACTION_AFFINITY_LP_FUNNEL,
    ACTION_MIRROR_TO_DB,
    _LEGACY_AFFINITY_ACTION,
    _VALID_ACTIONS,
    _VALID_MATCH_PROPERTIES,
    _extract_db_id_from_url,
)

logger = logging.getLogger(__name__)

_ORG_TABLE = "/rest/v1/org_chart_rows"
_RULES_TABLE = "/rest/v1/meeting_rule_rows"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_title(props: dict[str, Any]) -> str:
    for prop in props.values():
        if prop.get("type") == "title":
            return "".join(
                p.get("plain_text", "") for p in prop.get("title", []) or []
            ).strip()
    return ""


def _read_select(props: dict[str, Any], name: str) -> str | None:
    prop = props.get(name)
    if not isinstance(prop, dict) or prop.get("type") != "select":
        return None
    return ((prop.get("select") or {}).get("name") or "").strip() or None


def _read_checkbox(props: dict[str, Any], name: str, default: bool = False) -> bool:
    prop = props.get(name)
    if isinstance(prop, dict) and prop.get("type") == "checkbox":
        return bool(prop.get("checkbox"))
    return default


def _read_rich_text(props: dict[str, Any], name: str) -> str:
    items = (props.get(name) or {}).get("rich_text") or []
    return "".join(rt.get("plain_text", "") for rt in items).strip()


def _upsert(table: str, rows: list[dict[str, Any]]) -> None:
    """Merge-upsert on ``notion_page_id``. Rows carry ``deleted_at: None`` so
    a tombstoned row that reappears in Notion is automatically revived."""
    if not rows:
        return
    _http(
        "POST",
        f"{table}?on_conflict=notion_page_id",
        body=rows,
        prefer="resolution=merge-duplicates,return=minimal",
    )


def _tombstone_missing(table: str, present_ids: set[str], label: str) -> int:
    """Set ``deleted_at`` on live Supabase rows that vanished from Notion."""
    live = _http(
        "GET", f"{table}?select=notion_page_id&deleted_at=is.null&limit=10000",
    ) or []
    missing = [
        r["notion_page_id"] for r in live
        if r.get("notion_page_id") and r["notion_page_id"] not in present_ids
    ]
    if not missing:
        return 0
    in_list = ",".join(missing)
    _http(
        "PATCH",
        f"{table}?notion_page_id=in.({in_list})&deleted_at=is.null",
        body={"deleted_at": _now_iso()},
    )
    logger.info("%s mirror: tombstoned %d row(s): %s", label, len(missing), missing)
    return len(missing)


# ---------------------------------------------------------------------------
# Org Chart → org_chart_rows
# ---------------------------------------------------------------------------


def sync_org_chart(client: NotionClientWrapper, org_chart_db_id: str | None) -> int:
    """Mirror every Org Chart row to ``public.org_chart_rows``.

    Returns the number of rows upserted. Benign no-op (warning) when the
    Org Chart DB id isn't configured (single-DB dev override).
    """
    if not org_chart_db_id:
        logger.warning("org_chart mirror: ORG_CHART_DB_ID unset — skipping")
        return 0

    response = client.query_database(database_id=org_chart_db_id)
    now = _now_iso()
    rows: list[dict[str, Any]] = []
    for page in response.get("results", []):
        page_id = _hex_to_uuid(page.get("id"))
        if not page_id:
            continue
        props = page.get("properties", {})
        name = _read_title(props)
        if not name:
            logger.warning(
                "org_chart mirror: row %s has empty name — skipping",
                str(page.get("id"))[:8],
            )
            continue
        url = (props.get("Meeting Notes DB") or {}).get("url") or ""
        email_prop = props.get("Email") or {}
        rows.append({
            "notion_page_id": page_id,
            "name": name,
            "email": (email_prop.get("email") or "").strip().lower() or None,
            "meeting_notes_db_id": _extract_db_id(url),
            "active": _read_checkbox(props, "Active"),
            "auto_extract_tasks": _read_checkbox(props, "Auto-extract Tasks"),
            # Raw value (NULL when unset) — consumers apply the "Shared"
            # default, same as MeetingDB.default_mirror_visibility does.
            "default_mirror_visibility": _read_select(
                props, "Default Mirror Visibility",
            ),
            "seniority": _read_select(props, "Seniority"),
            "synced_at": now,
            "deleted_at": None,
        })

    _upsert(_ORG_TABLE, rows)
    _tombstone_missing(_ORG_TABLE, {r["notion_page_id"] for r in rows}, "org_chart")
    logger.info("org_chart mirror: upserted %d row(s)", len(rows))
    return len(rows)


# ---------------------------------------------------------------------------
# Meeting Rules → meeting_rule_rows
# ---------------------------------------------------------------------------


def sync_meeting_rules(client: NotionClientWrapper, rules_db_id: str | None) -> int:
    """Mirror every parseable Meeting Rules row to ``public.meeting_rule_rows``.

    Validation matches ``route_registry.load_routes`` (unknown Match
    Property/Action, empty Match Value, missing Target DB for Mirror-to-DB
    rows are skipped with a log line) EXCEPT the Active filter: inactive
    rules are mirrored with ``active=false`` so consumers can tell "off"
    from "deleted". The legacy pre-split Affinity tag is normalized here so
    consumers never need to know it existed.
    """
    if not rules_db_id:
        logger.warning("meeting_rules mirror: MEETING_RULES_DB_ID unset — skipping")
        return 0

    response = client.query_database(database_id=rules_db_id)
    now = _now_iso()
    rows: list[dict[str, Any]] = []
    for page in response.get("results", []):
        page_id = _hex_to_uuid(page.get("id"))
        if not page_id:
            continue
        props = page.get("properties", {})
        short = str(page.get("id"))[:8]

        match_property = _read_select(props, "Match Property") or ""
        if match_property not in _VALID_MATCH_PROPERTIES:
            logger.info(
                "meeting_rules mirror: row %s skipped: Match Property %r invalid",
                short, match_property,
            )
            continue

        match_value = _read_rich_text(props, "Match Value")
        if not match_value:
            logger.info(
                "meeting_rules mirror: row %s skipped: Match Value empty", short,
            )
            continue

        action = _read_select(props, "Action") or ACTION_MIRROR_TO_DB
        if action == _LEGACY_AFFINITY_ACTION:
            action = ACTION_AFFINITY_LP_FUNNEL
        if action not in _VALID_ACTIONS:
            logger.info(
                "meeting_rules mirror: row %s skipped: Action %r invalid",
                short, action,
            )
            continue

        target_url = ((props.get("Target DB") or {}).get("url") or "").strip()
        target_db_id = _hex_to_uuid(_extract_db_id_from_url(target_url))
        if action == ACTION_MIRROR_TO_DB and not target_db_id:
            logger.warning(
                "meeting_rules mirror: row %s skipped: Mirror-to-DB rule "
                "without a parseable Target DB URL (got %r)", short, target_url,
            )
            continue

        title_prop = props.get("Route") or {}
        label = "".join(
            rt.get("plain_text", "") for rt in title_prop.get("title", []) or []
        ).strip() or f"{match_property}:{match_value}"

        rows.append({
            "notion_page_id": page_id,
            "label": label,
            "match_property": match_property,
            "match_value": match_value,
            "action": action,
            "target_db_id": target_db_id,
            "active": _read_checkbox(props, "Active"),
            "synced_at": now,
            "deleted_at": None,
        })

    _upsert(_RULES_TABLE, rows)
    _tombstone_missing(
        _RULES_TABLE, {r["notion_page_id"] for r in rows}, "meeting_rules",
    )
    logger.info("meeting_rules mirror: upserted %d row(s)", len(rows))
    return len(rows)


__all__ = ["sync_meeting_rules", "sync_org_chart"]
