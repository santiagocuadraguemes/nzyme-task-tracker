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


# Body of the "Full transcript" section in the fallback note posted when the
# full-length one is rejected by Affinity (most plausibly: too large).
_TRANSCRIPT_OMITTED_NOTICE = (
    "(omitted — the full note was rejected by Affinity, likely too large; "
    "read it in Notion via the link below)"
)


def _build_html_note(
    *, meeting_title: str, manual_notes: str, ai_summary: str, notion_url: str,
    meeting_owner: str = "", transcript: str = "",
) -> str:
    """Sectioned HTML note: manual notes, then the Notion summary, then —
    when provided — the raw meeting transcript.

    The title carries no date — Notion meeting titles already embed it, and
    repeating it looks bad. When the manual notes are empty (the user never
    touched the template), the first section reads "No manual notes". The
    "Summary" section is omitted entirely when Notion produced no summary,
    and the "Full transcript" section is omitted when ``transcript`` is empty
    (rule says no-transcript, or transcription was paused/not yet processed).

    ``meeting_owner`` (the Kibo member whose Meeting Notes DB this meeting
    lives in — i.e. who hosted/recorded it) is rendered as an "Owner" line
    right under the title, when provided. The note attaches every attendee as
    an Affinity person but doesn't otherwise say who owns the meeting; this
    line makes that explicit on the LP timeline.
    """
    t = html.escape(meeting_title or "Fundraising meeting")
    parts = [f"<p><strong>{t}</strong></p>"]

    owner = meeting_owner.strip()
    if owner:
        parts.append(
            f"<p><strong>Owner:</strong> {html.escape(owner)}</p>"
        )

    notes = manual_notes.strip()
    parts.append(_section("Manual notes", notes) if notes
                 else "<p><strong>Manual notes</strong></p><p>No manual notes</p>")

    summary = ai_summary.strip()
    if summary:
        parts.append(_section("Summary", summary))

    transcript_text = transcript.strip()
    if transcript_text:
        parts.append(_section("Full transcript", transcript_text))

    u = html.escape(notion_url or "")
    if u:
        parts.append(f'<p><a href="{u}">View full meeting notes in Notion</a></p>')
    # One blank line between top-level blocks (title, Owner, each section, the
    # link) — an empty paragraph is what an "extra Enter" produces in Affinity's
    # rich-text editor. Within a section the label and body stay tight.
    return "<p></p>".join(parts)


def post_meeting_note_to_lps(
    client: AffinityClient,
    *,
    opportunity_entity_ids: list[int],
    meeting_title: str,
    manual_notes: str,
    ai_summary: str,
    notion_url: str,
    person_ids: list[int] | None = None,
    meeting_owner: str = "",
    transcript: str = "",
) -> tuple[list[int], list[tuple[int, str]], list[int]]:
    """Post the same HTML meeting note to every matched LP opportunity.

    The note is attached to each opportunity AND to ``person_ids`` (the
    meeting's owner/host + attendees resolved to Affinity persons) so it shows
    on their timelines too — not just the LP organization's.

    ``transcript`` (raw, full length — deliberately uncapped) gets its own
    "Full transcript" section. Transcript notes can run very large, so each
    opportunity gets a fallback: if the full note is rejected by Affinity,
    retry once with the transcript section replaced by an omission notice (the
    Notion backlink still carries the full text). A fallback post counts as
    posted — retrying the whole batch would only duplicate the note — but the
    opportunity is reported in ``degraded`` and logged at WARNING.

    Returns ``(posted, failed, degraded)`` where ``posted`` is the list of
    opportunity ids that received a note, ``failed`` is a list of
    ``(opportunity_id, error_message)`` for ones where every attempt raised,
    and ``degraded`` ⊆ ``posted`` flags the ones that got the
    transcript-omitted fallback. The caller decides whether a partial success
    counts as success — at the page level, the policy is: any failure →
    outcome=Failed (whole batch retried; the user accepted occasional
    duplicate notes on retry).
    """
    posted: list[int] = []
    failed: list[tuple[int, str]] = []
    degraded: list[int] = []
    note_body = _build_html_note(
        meeting_title=meeting_title,
        manual_notes=manual_notes,
        ai_summary=ai_summary,
        notion_url=notion_url,
        meeting_owner=meeting_owner,
        transcript=transcript,
    )
    fallback_body = (
        _build_html_note(
            meeting_title=meeting_title,
            manual_notes=manual_notes,
            ai_summary=ai_summary,
            notion_url=notion_url,
            meeting_owner=meeting_owner,
            transcript=_TRANSCRIPT_OMITTED_NOTICE,
        )
        if transcript.strip()
        else ""
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
            if not fallback_body:
                logger.exception(
                    "Note post failed for opportunity=%d — continuing", opp_id,
                )
                failed.append((opp_id, str(e)))
                continue
            logger.warning(
                "Full-transcript note (%d chars) rejected for opportunity=%d "
                "(%s) — retrying without the transcript",
                len(note_body), opp_id, e,
            )
            try:
                client.create_note(
                    content=fallback_body,
                    content_type="html",
                    opportunity_ids=[opp_id],
                    person_ids=person_ids,
                )
                logger.warning(
                    "Posted transcript-omitted fallback note to "
                    "opportunity=%d (persons=%s)",
                    opp_id, person_ids or [],
                )
                posted.append(opp_id)
                degraded.append(opp_id)
            except AffinityError as e2:
                logger.exception(
                    "Fallback note post also failed for opportunity=%d — "
                    "continuing", opp_id,
                )
                failed.append(
                    (opp_id, f"with transcript: {e}; without transcript: {e2}")
                )
    return posted, failed, degraded


__all__ = [
    "post_meeting_note_to_lps",
]
