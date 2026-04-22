"""Fundraising → Affinity branch.

Entry point called by ``src.pipeline.run_sync_for_page`` after the primary
Team Task Tracker write succeeds. Skips itself cleanly when the branch is
not enabled, attendees have no resolvable emails (typical Lambda state), or
no single LP can be confidently matched.
"""
from __future__ import annotations

import logging
from typing import Any

from openai import OpenAI

from src.affinity_client import AffinityClient
from src.config import SyncConfig
from src.fundraising.affinity_writer import (
    post_meeting_note_to_lp,
    write_next_step_to_lp,  # noqa: F401 — retained for future field-write re-enable
)
from src.fundraising.lp_matcher import build_lp_entity_index, resolve_lp_list_entry
from src.fundraising.next_step_summarizer import summarize_next_step
from src.fundraising.user_map import KiboUserMap  # noqa: F401 — used by _resolve_owner

logger = logging.getLogger(__name__)


def write_to_affinity(
    *,
    config: SyncConfig,
    tasks: list[dict[str, Any]],
    metadata: dict[str, Any],
    attendees: list[dict[str, Any]],
    notion_url: str,
) -> None:
    """Main fundraising-branch entry point — soft-fails on any error.

    Args:
        config: Runtime config (supplies affinity_api_key, list id, user map path).
        tasks: Classified task dicts produced by the transcript/notes pipeline.
        metadata: Meeting metadata dict (title, date, created_by, meeting_type).
        attendees: Resolved attendees from ``pipeline._resolve_attendees``.
            Each dict has ``id`` and ``name``; ``id`` may be a Notion user id
            (Lambda) or an email (CLI+GCal).
        notion_url: URL of the Notion meeting page for the Affinity note link.
    """
    if not config.affinity_api_key:
        logger.error("AFFINITY_API_KEY unset — fundraising branch cannot run")
        return

    with AffinityClient(config.affinity_api_key) as client:
        lp_index = build_lp_entity_index(client, config.affinity_lp_funnel_list_id)
        list_entry_id = resolve_lp_list_entry(
            client, attendees=attendees, lp_entity_index=lp_index,
        )
        if list_entry_id is None:
            return  # matcher already logged the reason

        # The index is {opportunity_id: list_entry_id}; reverse-lookup the
        # opportunity id so we can attach the note to the opportunity directly.
        opportunity_entity_id = next(
            (opp for opp, entry in lp_index.items() if entry == list_entry_id),
            None,
        )

        fields = client.get_fields(config.affinity_lp_funnel_list_id)

        # Fundraising summary is a LIGHT call → OpenAI. Force base_url so the
        # SDK can't pick up a stale OPENAI_BASE_URL env var pointing at Gemini.
        openai_client = OpenAI(
            api_key=config.openai_api_key,
            base_url="https://api.openai.com/v1",
        )
        summary_payload = summarize_next_step(
            openai_client=openai_client,
            model=config.openai_model,
            classified_tasks=tasks,
            affinity_fields=fields,
            meeting_title=metadata.get("title", ""),
            meeting_date=metadata.get("date", ""),
            creator_name=(metadata.get("created_by") or {}).get("name", ""),
        )

        # Temporary note-only mode: skip the four field writes (DETAILS /
        # DROPDOWN / FOLLOW-UP / OWNER) while validating the end-to-end flow.
        # `write_next_step_to_lp` + `_resolve_owner` remain available and can
        # be re-wired here once we're ready to resume field updates.
        post_meeting_note_to_lp(
            client,
            opportunity_entity_id=opportunity_entity_id,
            meeting_title=metadata.get("title", ""),
            meeting_date=metadata.get("date", ""),
            summary=summary_payload.get("details_text", ""),
            notion_url=notion_url,
        )


def _resolve_owner(
    *,
    user_map: KiboUserMap,
    summary_payload: dict[str, Any],
    tasks: list[dict[str, Any]],
    creator: dict[str, Any],
) -> int | None:
    """Owner precedence: summarizer → any classified internal assignee → creator.

    Returns an Affinity user id, or None if no mapping is available (in which
    case the writer will skip the OWNER field so we never overwrite with null).
    """
    summarizer_pick = summary_payload.get("owner_notion_user_id")
    if summarizer_pick:
        if (aid := user_map.affinity_id_for_notion_user(summarizer_pick)) is not None:
            return aid
        logger.info(
            "Summarizer chose Notion user %s but no Affinity mapping exists",
            summarizer_pick,
        )

    for task in tasks:
        for notion_id in task.get("assignee_id") or []:
            if (aid := user_map.affinity_id_for_notion_user(notion_id)) is not None:
                return aid

    creator_id = creator.get("id")
    if creator_id and (aid := user_map.affinity_id_for_notion_user(creator_id)) is not None:
        return aid

    logger.warning(
        "No Affinity owner could be resolved — OWNER field will be left unchanged",
    )
    return None


__all__ = ["write_to_affinity"]
