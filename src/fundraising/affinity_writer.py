"""Append-safe writer that posts a meeting note linking back to the Notion
page on the matched LP row(s).
"""
from __future__ import annotations

import html
import logging

from src.affinity_client import AffinityClient, AffinityError

logger = logging.getLogger(__name__)


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


__all__ = [
    "post_meeting_note_to_lp",
    "post_meeting_note_to_lps",
]
