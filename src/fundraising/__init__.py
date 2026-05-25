"""Fundraising → Affinity branch.

Entry point called by ``src.pipeline.run_sync_for_page`` after the primary
Team Task Tracker write succeeds, and by the cron retry sweep in
``lambda_handler`` for pages stuck at ``Failed: API error`` or ``Pending``.

Returns a structured ``FundraisingOutcome`` so the caller can map onto the
``Affinity Status`` Notion property — silent skips are no longer possible.
"""
from __future__ import annotations

import logging
from typing import Any

from src.affinity_client import AffinityClient, AffinityError
from src.config import SyncConfig
from src.fundraising.affinity_writer import post_meeting_note_to_lps
from src.fundraising.lp_matcher import (
    build_lp_entity_index,
    extract_external_emails,
    resolve_lp_list_entries,
)
from src.fundraising.outcome import FundraisingOutcome, FundraisingStatus
from src.notion_client_wrapper import NotionClientWrapper
from src.transcript_pipeline.fetch_transcript import (
    extract_ai_summary,
    fetch_notes_text,
    find_meeting_notes_block,
)

logger = logging.getLogger(__name__)


def _compose_note_body(*, user_notes: str, ai_summary: str) -> str:
    """Compose the Affinity note body from user notes + Notion AI summary.

    Returns a multi-line string; ``_build_html_note`` converts ``\\n`` → ``<br>``.
    Sections appear only when their source has content, with a plain-text label
    so the reader can tell which is which. When neither is present, returns ""
    and the writer falls back to a link-only note.
    """
    parts: list[str] = []
    if user_notes:
        parts.append(f"Notes:\n{user_notes.strip()}")
    if ai_summary:
        parts.append(f"Notion AI summary:\n{ai_summary.strip()}")
    return "\n\n".join(parts)


def write_to_affinity(
    *,
    config: SyncConfig,
    tasks: list[dict[str, Any]],
    metadata: dict[str, Any],
    attendees: list[dict[str, Any]],
    notion_url: str,
    page_id: str,
    notion_client: NotionClientWrapper,
) -> FundraisingOutcome:
    """Main fundraising-branch entry point.

    Never raises — every failure mode maps to a ``FundraisingOutcome``. The
    caller (the pipeline) logs a structured ``fundraising outcome:`` line so
    the result is grep-able in CloudWatch.
    """
    if not config.affinity_api_key:
        logger.error("AFFINITY_API_KEY unset — fundraising branch cannot run")
        return FundraisingOutcome(
            status=FundraisingStatus.FAILED_API_ERROR,
            detail="AFFINITY_API_KEY not configured",
        )

    # Fast pre-check: bail out on the no-external-attendees case before
    # spending a network round-trip on the LP Funnel index. The matcher
    # would log this anyway, but the early return makes the outcome
    # accurate (and saves a list_list_entries call).
    if not extract_external_emails(attendees):
        return FundraisingOutcome(
            status=FundraisingStatus.SKIPPED_NO_EXTERNAL_ATTENDEES,
            detail="No external attendee emails on the meeting page",
        )

    try:
        with AffinityClient(config.affinity_api_key) as client:
            lp_index = build_lp_entity_index(client, config.affinity_lp_funnel_list_id)
            list_entry_ids = resolve_lp_list_entries(
                client, attendees=attendees, lp_entity_index=lp_index,
            )
            if not list_entry_ids:
                return FundraisingOutcome(
                    status=FundraisingStatus.SKIPPED_NO_LP_MATCH,
                    detail=(
                        "External attendees present but none mapped to an LP "
                        "Funnel opportunity"
                    ),
                )

            # Reverse-lookup opportunity ids from the index so we can attach
            # the note directly to each opportunity.
            entry_to_opp = {entry: opp for opp, entry in lp_index.items()}
            opportunity_ids: list[int] = [
                entry_to_opp[entry] for entry in list_entry_ids
                if entry in entry_to_opp
            ]
            if not opportunity_ids:
                # Defensive: lp_matcher returned entries we can't reverse-map.
                return FundraisingOutcome(
                    status=FundraisingStatus.FAILED_API_ERROR,
                    detail=(
                        f"Matched LP entries {list_entry_ids} but could not "
                        "reverse-map to opportunity ids — LP Funnel index "
                        "shape changed?"
                    ),
                )

            # Pull the note body from Notion: user-written notes (inside the
            # meeting_notes block) + Notion's auto-generated "AI Summary" page
            # property. No LLM call here.
            page = notion_client.get_page(page_id)
            ai_summary = extract_ai_summary(page)

            user_notes = ""
            try:
                blocks = notion_client.get_block_children(page_id)
                mn_block = find_meeting_notes_block(blocks)
                if mn_block is not None:
                    user_notes = fetch_notes_text(mn_block, notion_client)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to fetch user notes from page %s — continuing with "
                    "summary-only body",
                    page_id,
                )

            summary_text = _compose_note_body(
                user_notes=user_notes, ai_summary=ai_summary,
            )

            posted, failed = post_meeting_note_to_lps(
                client,
                opportunity_entity_ids=opportunity_ids,
                meeting_title=metadata.get("title", ""),
                meeting_date=metadata.get("date", ""),
                summary=summary_text,
                notion_url=notion_url,
            )

            if failed:
                err_summary = "; ".join(f"opp={opp}: {msg}" for opp, msg in failed)
                detail = (
                    f"Posted to opportunities {posted}; failed for "
                    f"{[opp for opp, _ in failed]}. Errors: {err_summary}"
                )
                return FundraisingOutcome(
                    status=FundraisingStatus.FAILED_API_ERROR,
                    detail=detail,
                    summary=summary_text,
                )

            return FundraisingOutcome(
                status=FundraisingStatus.POSTED,
                detail=f"posted_to=[{','.join(map(str, posted))}]",
                summary=summary_text,
            )
    except AffinityError as e:
        return FundraisingOutcome(
            status=FundraisingStatus.FAILED_API_ERROR,
            detail=f"Affinity API error during setup: {e}",
        )
    except Exception as e:  # noqa: BLE001
        # Catch-all so the branch never raises into the caller.
        logger.exception("Unexpected error in fundraising branch")
        return FundraisingOutcome(
            status=FundraisingStatus.FAILED_API_ERROR,
            detail=f"Unexpected error: {type(e).__name__}: {e}",
        )


__all__ = ["FundraisingOutcome", "FundraisingStatus", "write_to_affinity"]
