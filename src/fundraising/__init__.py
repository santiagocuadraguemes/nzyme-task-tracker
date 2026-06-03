"""Fundraising → Affinity branch.

Entry point called by ``src.pipeline.run_sync_for_page`` after the primary
Team Task Tracker write succeeds, and by the cron retry sweep in
``lambda_handler`` for pages stuck at ``Failed: API error`` or ``Pending``.

Returns a structured ``FundraisingOutcome`` so the caller can map onto the
``Affinity Status`` Notion property — silent skips are no longer possible.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from src.affinity_client import AffinityClient, AffinityError
from src.config import SyncConfig
from src.fundraising.affinity_writer import post_meeting_note_to_lps
from src.fundraising.lp_matcher import (
    build_lp_entity_index,
    extract_external_emails,
    resolve_attendee_person_ids,
    resolve_lp_list_entries,
)
from src.fundraising.outcome import FundraisingOutcome, FundraisingStatus
from src.meeting_row import _fetch_block_summary
from src.notion_client_wrapper import NotionClientWrapper
from src.transcript_pipeline.fetch_transcript import (
    extract_ai_summary,
    fetch_notes_text,
    find_meeting_notes_block,
    strip_title_datetime,
)

logger = logging.getLogger(__name__)


# Markdown list / checkbox marker at the start of a notes line.
_LIST_MARKER_RE = re.compile(r"^[-*]\s*(\[[ xX]\]\s*)?")


def _strip_template_scaffolding(raw_notes: str) -> str:
    """Return the user's real notes, or "" when the section is just the
    untouched meeting template.

    The injected template seeds the notes with section headings
    (``## Action Items``, ``### Notes``), empty checklist bullets, and a
    bracketed ``[placeholder]``. Stripping all of that leaves nothing when the
    user never typed anything — the caller then renders "No manual notes".
    Lines that carry real content are kept verbatim (markers and all).
    """
    kept: list[str] = []
    for line in raw_notes.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):                 # section-heading scaffolding
            continue
        body = _LIST_MARKER_RE.sub("", stripped).strip()
        if not body:                                 # bare "-" / empty checkbox
            continue
        if body.startswith("[") and body.endswith("]"):  # bracketed placeholder
            continue
        kept.append(stripped)
    return "\n".join(kept).strip()


def _compose_outcome_summary(*, manual_notes: str, ai_summary: str) -> str:
    """Plain-text rendering of the note's two sections, for the
    ``FundraisingOutcome.summary`` debug field (not what Affinity receives)."""
    notes = manual_notes.strip() or "(none)"
    parts = [f"Manual notes:\n{notes}"]
    if ai_summary.strip():
        parts.append(f"Summary:\n{ai_summary.strip()}")
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
    meeting_owner: str = "",
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

            # Pull the note body from Notion: user-written notes + the Notion
            # AI summary. The summary lives inside the meeting_notes block
            # (`summary_block_id`) — what the user sees in the block — so read
            # that first and only fall back to the legacy "AI Summary" page
            # property when the block has none. No LLM call here.
            page = notion_client.get_page(page_id)

            user_notes = ""
            block_summary = ""
            try:
                blocks = notion_client.get_block_children(page_id)
                mn_block = find_meeting_notes_block(blocks)
                if mn_block is not None:
                    user_notes = fetch_notes_text(mn_block, notion_client)
                    block_summary = _fetch_block_summary(mn_block, notion_client)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to fetch notes/summary from page %s — continuing "
                    "with whatever was retrieved",
                    page_id,
                )

            ai_summary = block_summary or extract_ai_summary(page)

            manual_notes = _strip_template_scaffolding(user_notes)
            summary_text = _compose_outcome_summary(
                manual_notes=manual_notes, ai_summary=ai_summary,
            )

            # Attach the note to the meeting's people (owner/host + attendees
            # that exist in Affinity) so it lands on their timelines, not just
            # the LP opportunity.
            person_ids = resolve_attendee_person_ids(client, attendees)

            posted, failed = post_meeting_note_to_lps(
                client,
                opportunity_entity_ids=opportunity_ids,
                meeting_title=strip_title_datetime(metadata.get("title", "")),
                manual_notes=manual_notes,
                ai_summary=ai_summary,
                notion_url=notion_url,
                person_ids=person_ids,
                meeting_owner=meeting_owner,
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
