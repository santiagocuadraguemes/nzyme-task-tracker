"""Append-safe writer that updates the four Nzyme Next Step fields on a
matched LP row and drops a meeting note linking back to the Notion page.

All field ids are pre-known on list ``168609`` (Nzyme - LP Funnel); see
``next_step_summarizer`` for the constants. V1 ``/fields`` returns bare
numeric ids, so conversions are handled here.
"""
from __future__ import annotations

import html
import logging
from datetime import date
from typing import Any

from src.affinity_client import AffinityClient, AffinityError
from src.fundraising.next_step_summarizer import (
    FIELD_ID_DETAILS,
    FIELD_ID_FOLLOW_UP_DATE,
    FIELD_ID_NEXT_STEP_DROPDOWN,
    FIELD_ID_OWNER,
)

logger = logging.getLogger(__name__)


def _numeric_field_id(field_str_id: str) -> int:
    """Convert V2-style ``field-123`` → bare int ``123`` for V1 writes."""
    return int(field_str_id.split("-", 1)[1])


def _find_existing_value(
    field_values: list[dict[str, Any]], field_str_id: str,
) -> dict[str, Any] | None:
    """Find the existing field-value record (if any) by field id.

    V1 ``/field-values`` returns entries with a bare integer ``field_id``.
    """
    target = _numeric_field_id(field_str_id)
    for fv in field_values:
        if fv.get("field_id") == target:
            return fv
    return None


def _build_append_block(
    *, meeting_title: str, meeting_date: str, summary: str,
) -> str:
    today = meeting_date or date.today().isoformat()
    safe_title = meeting_title.replace("\n", " ").strip()
    return f"[{today} — {safe_title!r}]\n{summary.strip()}"


def _build_html_note(
    *, meeting_title: str, meeting_date: str, summary: str, notion_url: str,
) -> str:
    t = html.escape(meeting_title or "Fundraising meeting")
    d = html.escape(meeting_date or "")
    s = html.escape(summary.strip()).replace("\n", "<br>")
    u = html.escape(notion_url or "")
    link = (
        f'<p><a href="{u}">View full meeting notes in Notion</a></p>' if u else ""
    )
    body = f"<p>{s}</p>" if s else ""
    return (
        f"<p><strong>{t}</strong> — {d}</p>"
        f"{body}"
        f"{link}"
    )


def write_next_step_to_lp(
    client: AffinityClient,
    *,
    list_entry_id: int,
    opportunity_entity_id: int | None,
    organization_ids: list[int],
    fields: list[dict[str, Any]],
    summary_payload: dict[str, Any],
    meeting_title: str,
    meeting_date: str,
    notion_url: str,
    owner_affinity_user_id: int | None,
) -> None:
    """Apply the summarizer output to the matched LP row, append-safe.

    Individual step failures are logged with full context but do not raise —
    a note-post failure must not undo field updates, and vice versa.
    """
    current = client.get_field_values_for_entry(list_entry_id)

    # 1) DETAILS — append
    append_block = _build_append_block(
        meeting_title=meeting_title,
        meeting_date=meeting_date,
        summary=summary_payload.get("details_text", ""),
    )
    existing_details = _find_existing_value(current, FIELD_ID_DETAILS)
    existing_text = ""
    if existing_details is not None:
        val = existing_details.get("value")
        if isinstance(val, str):
            existing_text = val
        elif isinstance(val, dict):
            existing_text = val.get("text") or ""
    new_text = (existing_text + "\n\n" + append_block).strip() if existing_text else append_block
    _apply(
        client,
        existing=existing_details,
        field_str_id=FIELD_ID_DETAILS,
        entity_id=opportunity_entity_id,
        list_entry_id=list_entry_id,
        new_value=new_text,
    )

    # 2) DROP-DOWN (ranked) — only write if summarizer picked one
    if (option_id := summary_payload.get("drop_down_option_id")) is not None:
        existing = _find_existing_value(current, FIELD_ID_NEXT_STEP_DROPDOWN)
        _apply(
            client,
            existing=existing,
            field_str_id=FIELD_ID_NEXT_STEP_DROPDOWN,
            entity_id=opportunity_entity_id,
            list_entry_id=list_entry_id,
            new_value=option_id,
        )

    # 3) FOLLOW-UP QUARTER (ranked) — only write if summarizer picked one
    if (option_id := summary_payload.get("follow_up_option_id")) is not None:
        existing = _find_existing_value(current, FIELD_ID_FOLLOW_UP_DATE)
        _apply(
            client,
            existing=existing,
            field_str_id=FIELD_ID_FOLLOW_UP_DATE,
            entity_id=opportunity_entity_id,
            list_entry_id=list_entry_id,
            new_value=option_id,
        )

    # 4) OWNER (person-multi) — single-value write is fine, Affinity accepts list
    if owner_affinity_user_id is not None:
        existing = _find_existing_value(current, FIELD_ID_OWNER)
        _apply(
            client,
            existing=existing,
            field_str_id=FIELD_ID_OWNER,
            entity_id=opportunity_entity_id,
            list_entry_id=list_entry_id,
            new_value=owner_affinity_user_id,
        )

    # 5) HTML meeting note attached to opportunity + organizations
    try:
        note_body = _build_html_note(
            meeting_title=meeting_title,
            meeting_date=meeting_date,
            summary=summary_payload.get("details_text", ""),
            notion_url=notion_url,
        )
        # Affinity V1 does not support opportunity_ids on notes for all accounts;
        # attaching to organizations is universally supported.
        if organization_ids:
            client.create_note(
                content=note_body,
                content_type="html",
                organization_ids=organization_ids,
            )
            logger.info(
                "Posted Affinity note to orgs=%s for list_entry=%d",
                organization_ids, list_entry_id,
            )
        else:
            logger.warning(
                "No organization_ids attached to LP entry %d — skipping note",
                list_entry_id,
            )
    except AffinityError:
        logger.exception(
            "Note post failed for list_entry=%d — continuing", list_entry_id,
        )


def post_meeting_note_to_lps(
    client: AffinityClient,
    *,
    opportunity_entity_ids: list[int],
    meeting_title: str,
    meeting_date: str,
    summary: str,
    notion_url: str,
) -> tuple[list[int], list[tuple[int, str]]]:
    """Post the same HTML meeting note to every matched LP opportunity.

    Returns ``(posted, failed)`` where ``posted`` is the list of opportunity
    ids that received the note and ``failed`` is a list of
    ``(opportunity_id, error_message)`` for ones that raised. The caller
    decides whether a partial success counts as success — at the page level,
    the policy is: any failure → outcome=Failed (whole batch retried; the
    user accepted occasional duplicate notes on retry).
    """
    posted: list[int] = []
    failed: list[tuple[int, str]] = []
    note_body = _build_html_note(
        meeting_title=meeting_title,
        meeting_date=meeting_date,
        summary=summary,
        notion_url=notion_url,
    )
    for opp_id in opportunity_entity_ids:
        try:
            client.create_note(
                content=note_body,
                content_type="html",
                opportunity_ids=[opp_id],
            )
            logger.info("Posted Affinity note to opportunity=%d", opp_id)
            posted.append(opp_id)
        except AffinityError as e:
            logger.exception(
                "Note post failed for opportunity=%d — continuing", opp_id,
            )
            failed.append((opp_id, str(e)))
    return posted, failed


def post_meeting_note_to_lp(
    client: AffinityClient,
    *,
    opportunity_entity_id: int | None,
    meeting_title: str,
    meeting_date: str,
    summary: str,
    notion_url: str,
) -> None:
    """Backward-compat single-LP wrapper. Swallows errors; no return.

    New code should call ``post_meeting_note_to_lps`` and inspect the result.
    """
    if opportunity_entity_id is None:
        logger.warning(
            "Cannot post meeting note: opportunity_entity_id is None — skipping",
        )
        return
    post_meeting_note_to_lps(
        client,
        opportunity_entity_ids=[opportunity_entity_id],
        meeting_title=meeting_title,
        meeting_date=meeting_date,
        summary=summary,
        notion_url=notion_url,
    )


def _apply(
    client: AffinityClient,
    *,
    existing: dict[str, Any] | None,
    field_str_id: str,
    entity_id: int | None,
    list_entry_id: int,
    new_value: Any,
) -> None:
    """PUT when a field-value exists, else POST to create.

    Logs and swallows per-field errors so that one broken field doesn't
    abort the remaining writes.
    """
    try:
        if existing and existing.get("id") is not None:
            client.update_field_value(existing["id"], new_value)
            logger.info(
                "PUT field-value id=%s for %s on list_entry=%d",
                existing["id"], field_str_id, list_entry_id,
            )
            return
        if entity_id is None:
            logger.warning(
                "Cannot POST field-value for %s: missing entity_id (list_entry=%d)",
                field_str_id, list_entry_id,
            )
            return
        client.create_field_value(
            field_id=_numeric_field_id(field_str_id),
            entity_id=entity_id,
            list_entry_id=list_entry_id,
            value=new_value,
        )
        logger.info(
            "POST field-value for %s on list_entry=%d", field_str_id, list_entry_id,
        )
    except AffinityError:
        logger.exception(
            "Field write failed: %s on list_entry=%d — continuing",
            field_str_id, list_entry_id,
        )


__all__ = [
    "post_meeting_note_to_lp",
    "post_meeting_note_to_lps",
    "write_next_step_to_lp",
]
