"""Build `meeting_transcripts` row dicts from Notion pages.

Shared kernel for both the one-time backfill script and the recurring
Supabase sync Lambda handler. Pure extraction — no I/O beyond the Notion
calls already wrapped by NotionClientWrapper.
"""
from __future__ import annotations

import logging
from typing import Any

from src.meeting_db_registry import MeetingDB
from src.notion_client_wrapper import NotionClientWrapper
from src.transcript_pipeline.fetch_transcript import (
    extract_transcript_block_id,
    fetch_notes_text,
    find_meeting_notes_block,
)
from src.utils.blocks_to_text import blocks_to_text

logger = logging.getLogger(__name__)


def _hex_to_uuid(s: str | None) -> str | None:
    """Normalize a 32-hex (with or without dashes) into canonical UUID form."""
    if not s:
        return None
    h = s.replace("-", "").lower()
    if len(h) != 32:
        return None
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _select_value(prop: dict[str, Any] | None) -> str | None:
    if not prop or prop.get("type") != "select":
        return None
    sel = prop.get("select") or {}
    return sel.get("name") or None


def _title_text(prop: dict[str, Any] | None) -> str:
    if not prop or prop.get("type") != "title":
        return ""
    return "".join(p.get("plain_text", "") for p in prop.get("title", []) or [])


def _date_fields(
    prop: dict[str, Any] | None,
) -> tuple[str | None, str | None, bool | None]:
    """Return (start, end, is_datetime) from a Notion date property."""
    if not prop or prop.get("type") != "date":
        return (None, None, None)
    d = prop.get("date") or {}
    start = d.get("start")
    end = d.get("end")
    is_datetime = None
    if start:
        # Notion encodes date-only as "YYYY-MM-DD" and datetime with "T".
        is_datetime = "T" in start
    return (start, end, is_datetime)


def _relation_ids(prop: dict[str, Any] | None) -> list[str]:
    if not prop or prop.get("type") != "relation":
        return []
    ids = []
    for r in prop.get("relation", []) or []:
        uid = _hex_to_uuid(r.get("id"))
        if uid:
            ids.append(uid)
    return ids


def _fetch_block_summary(
    mn_block: dict[str, Any],
    client: NotionClientWrapper,
) -> str:
    """Pull the in-block AI summary from meeting_notes.children, if present.

    Probes the keys Notion exposes for the summary sub-block. Observed key:
    `summary_block_id`. Falls back to walking every `*_block_id` under
    children that isn't transcript/notes, in case Notion renames things.
    """
    children = (mn_block or {}).get("meeting_notes", {}).get("children") or {}

    for key in ("summary_block_id", "ai_summary_block_id"):
        bid = children.get(key)
        if isinstance(bid, str) and bid:
            try:
                blocks = client.get_block_children(bid)
            except Exception as exc:
                logger.warning("Couldn't read summary block %s: %s", bid, exc)
                return ""
            return blocks_to_text(blocks, client) if blocks else ""

    skip = {"transcript_block_id", "notes_block_id"}
    for key, bid in children.items():
        if key in skip or not key.endswith("_block_id"):
            continue
        if isinstance(bid, str) and bid:
            logger.info(
                "Unrecognised meeting_notes child key %r — treating as summary.",
                key,
            )
            try:
                blocks = client.get_block_children(bid)
            except Exception as exc:
                logger.warning("Couldn't read fallback block %s: %s", bid, exc)
                return ""
            return blocks_to_text(blocks, client) if blocks else ""

    return ""


def extract_row(
    page: dict[str, Any],
    owner: MeetingDB,
    client: NotionClientWrapper,
) -> dict[str, Any]:
    """Build one `meeting_transcripts` row from a Notion meeting page."""
    page_id = _hex_to_uuid(page["id"])
    db_id = _hex_to_uuid(owner.db_id)
    props = page.get("properties", {})

    start, end, is_dt = _date_fields(props.get("Date"))

    blocks = client.get_block_children(page["id"])
    mn_block = find_meeting_notes_block(blocks)

    transcript = ""
    notes_text = ""
    summary_text = ""
    if mn_block is not None:
        tbid = extract_transcript_block_id(mn_block)
        if tbid:
            tblocks = client.get_block_children(tbid)
            transcript = blocks_to_text(tblocks, client) if tblocks else ""
        notes_text = fetch_notes_text(mn_block, client)
        summary_text = _fetch_block_summary(mn_block, client)

    return {
        "page_id": page_id,
        "db_id": db_id,
        "owner_name": owner.owner_name or None,
        "title": _title_text(props.get("Meeting")) or "(untitled)",
        "meeting_start": start,
        "meeting_end": end,
        "meeting_is_datetime": is_dt,
        "created_time": page.get("created_time"),
        "last_edited_time": page.get("last_edited_time"),
        "meeting_type": _select_value(props.get("Meeting type")),
        "detail": _select_value(props.get("Detail")),
        "transcript": transcript or None,
        "notes": notes_text or None,
        "notion_summary": summary_text or None,
        "attendee_emails": None,  # GCal not run in sync (separate concern)
        "task_page_ids": _relation_ids(props.get("Task - Relation")) or None,
        "raw": None,
    }
