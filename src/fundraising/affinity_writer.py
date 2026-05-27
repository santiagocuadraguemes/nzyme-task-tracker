"""Append-safe writer that posts a meeting note linking back to the Notion
page on the matched LP row(s).
"""
from __future__ import annotations

import html
import logging

from src.affinity_client import AffinityClient, AffinityError

logger = logging.getLogger(__name__)


def _section(label: str, text: str) -> str:
    """A bolded label paragraph + a body paragraph (newlines → <br>)."""
    body = html.escape(text.strip()).replace("\n", "<br>")
    return f"<p><strong>{html.escape(label)}</strong></p><p>{body}</p>"


def _build_html_note(
    *, meeting_title: str, manual_notes: str, ai_summary: str, notion_url: str,
) -> str:
    """Two-section HTML note: manual notes first, then the Notion summary.

    The title carries no date — Notion meeting titles already embed it, and
    repeating it looks bad. When the manual notes are empty (the user never
    touched the template), the first section reads "No manual notes". The
    "Summary" section is omitted entirely when Notion produced no summary.
    """
    t = html.escape(meeting_title or "Fundraising meeting")
    parts = [f"<p><strong>{t}</strong></p>"]

    notes = manual_notes.strip()
    parts.append(_section("Manual notes", notes) if notes
                 else "<p><strong>Manual notes</strong></p><p>No manual notes</p>")

    summary = ai_summary.strip()
    if summary:
        parts.append(_section("Summary", summary))

    u = html.escape(notion_url or "")
    if u:
        parts.append(f'<p><a href="{u}">View full meeting notes in Notion</a></p>')
    return "".join(parts)


def post_meeting_note_to_lps(
    client: AffinityClient,
    *,
    opportunity_entity_ids: list[int],
    meeting_title: str,
    manual_notes: str,
    ai_summary: str,
    notion_url: str,
    person_ids: list[int] | None = None,
) -> tuple[list[int], list[tuple[int, str]]]:
    """Post the same HTML meeting note to every matched LP opportunity.

    The note is attached to each opportunity AND to ``person_ids`` (the
    meeting's owner/host + attendees resolved to Affinity persons) so it shows
    on their timelines too — not just the LP organization's.

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
        manual_notes=manual_notes,
        ai_summary=ai_summary,
        notion_url=notion_url,
    )
    for opp_id in opportunity_entity_ids:
        try:
            client.create_note(
                content=note_body,
                content_type="html",
                opportunity_ids=[opp_id],
                person_ids=person_ids,
            )
            logger.info(
                "Posted Affinity note to opportunity=%d (persons=%s)",
                opp_id, person_ids or [],
            )
            posted.append(opp_id)
        except AffinityError as e:
            logger.exception(
                "Note post failed for opportunity=%d — continuing", opp_id,
            )
            failed.append((opp_id, str(e)))
    return posted, failed


__all__ = [
    "post_meeting_note_to_lps",
]
