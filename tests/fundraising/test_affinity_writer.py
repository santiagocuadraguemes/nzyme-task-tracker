"""Tests for the append-safe Affinity writer."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.affinity_client import AffinityError
from src.fundraising.affinity_writer import post_meeting_note_to_lps


@pytest.fixture()
def client() -> MagicMock:
    c = MagicMock()
    c.get_field_values_for_entry.return_value = []
    return c


class TestNoteBody:
    def test_posts_html_note_attached_to_opportunity(self, client):
        post_meeting_note_to_lps(
            client,
            opportunity_entity_ids=[701],
            meeting_title="LP X fundraising 2026-04-17T10:00:00",
            manual_notes="Send Fund I deck.",
            ai_summary="They are interested in Fund I.",
            notion_url="https://www.notion.so/kibo/page-abc",
        )

        client.create_note.assert_called_once()
        kwargs = client.create_note.call_args.kwargs
        content = kwargs["content"]
        assert kwargs["content_type"] == "html"
        assert kwargs["opportunity_ids"] == [701]
        # Note attaches to opportunity only, never organization.
        assert "organization_ids" not in kwargs
        # Title appears verbatim (date in the title is NOT stripped/duplicated).
        assert "LP X fundraising 2026-04-17T10:00:00" in content
        # Both sections present with their labels.
        assert "Manual notes" in content
        assert "Send Fund I deck." in content
        assert "Summary" in content
        assert "They are interested in Fund I." in content
        assert "page-abc" in content
        # No field-value writes in note-only mode.
        client.create_field_value.assert_not_called()
        client.update_field_value.assert_not_called()

    def test_no_manual_notes_placeholder_when_notes_empty(self, client):
        post_meeting_note_to_lps(
            client,
            opportunity_entity_ids=[701],
            meeting_title="LP X",
            manual_notes="",
            ai_summary="Notion summary text.",
            notion_url="",
        )
        content = client.create_note.call_args.kwargs["content"]
        assert "No manual notes" in content
        assert "Notion summary text." in content

    def test_summary_section_omitted_when_no_ai_summary(self, client):
        post_meeting_note_to_lps(
            client,
            opportunity_entity_ids=[701],
            meeting_title="LP X",
            manual_notes="Real notes.",
            ai_summary="",
            notion_url="",
        )
        content = client.create_note.call_args.kwargs["content"]
        assert "Real notes." in content
        assert "Summary" not in content

    def test_owner_line_rendered_on_top_when_provided(self, client):
        post_meeting_note_to_lps(
            client,
            opportunity_entity_ids=[701],
            meeting_title="LP X",
            manual_notes="Real notes.",
            ai_summary="",
            notion_url="",
            meeting_owner="Vicente",
        )
        content = client.create_note.call_args.kwargs["content"]
        assert "Owner:" in content
        assert "Vicente" in content
        # Owner line sits above the manual notes section.
        assert content.index("Vicente") < content.index("Real notes.")

    def test_owner_line_omitted_when_blank(self, client):
        post_meeting_note_to_lps(
            client,
            opportunity_entity_ids=[701],
            meeting_title="LP X",
            manual_notes="Real notes.",
            ai_summary="",
            notion_url="",
            meeting_owner="",
        )
        content = client.create_note.call_args.kwargs["content"]
        assert "Owner:" not in content

    def test_person_ids_forwarded_to_create_note(self, client):
        post_meeting_note_to_lps(
            client,
            opportunity_entity_ids=[701],
            meeting_title="LP X",
            manual_notes="",
            ai_summary="",
            notion_url="",
            person_ids=[11, 22, 33],
        )
        assert client.create_note.call_args.kwargs["person_ids"] == [11, 22, 33]

    def test_transcript_section_rendered_below_summary_above_link(self, client):
        post_meeting_note_to_lps(
            client,
            opportunity_entity_ids=[701],
            meeting_title="LP X",
            manual_notes="Real notes.",
            ai_summary="The summary.",
            notion_url="https://www.notion.so/kibo/page-abc",
            transcript="Speaker 1: hello\nSpeaker 2: hi there",
        )
        content = client.create_note.call_args.kwargs["content"]
        assert "Full transcript" in content
        assert "Speaker 1: hello" in content
        # Below the summary, above the Notion backlink.
        assert content.index("The summary.") < content.index("Full transcript")
        assert content.index("Full transcript") < content.index("page-abc")

    def test_transcript_section_omitted_when_empty(self, client):
        post_meeting_note_to_lps(
            client,
            opportunity_entity_ids=[701],
            meeting_title="LP X",
            manual_notes="Real notes.",
            ai_summary="The summary.",
            notion_url="",
            transcript="",
        )
        content = client.create_note.call_args.kwargs["content"]
        assert "Full transcript" not in content


class TestPostMeetingNoteToLps:
    def test_all_lps_receive_the_note(self, client):
        posted, failed, degraded = post_meeting_note_to_lps(
            client,
            opportunity_entity_ids=[701, 702, 703],
            meeting_title="LP X & Y intro",
            manual_notes="",
            ai_summary="Both LPs are interested.",
            notion_url="https://notion.so/p",
            person_ids=[5],
        )
        assert posted == [701, 702, 703]
        assert failed == []
        assert degraded == []
        assert client.create_note.call_count == 3
        # Each call attaches to exactly ONE opportunity (separate notes), and
        # carries the same person attachments.
        for call in client.create_note.call_args_list:
            assert len(call.kwargs["opportunity_ids"]) == 1
            assert call.kwargs["person_ids"] == [5]

    def test_partial_failure_records_error_per_lp(self, client):
        seen: list[int] = []

        def flaky(content, content_type, opportunity_ids, **kwargs):
            opp = opportunity_ids[0]
            seen.append(opp)
            if opp == 702:
                raise AffinityError(500, "boom", "/notes")
            return {"id": opp + 9000}

        client.create_note.side_effect = flaky
        posted, failed, degraded = post_meeting_note_to_lps(
            client,
            opportunity_entity_ids=[701, 702, 703],
            meeting_title="t",
            manual_notes="",
            ai_summary="s",
            notion_url="",
        )
        assert seen == [701, 702, 703]
        assert posted == [701, 703]
        assert degraded == []
        assert len(failed) == 1
        opp_id, err_msg = failed[0]
        assert opp_id == 702
        assert "boom" in err_msg

    def test_empty_list_is_a_noop(self, client):
        posted, failed, degraded = post_meeting_note_to_lps(
            client,
            opportunity_entity_ids=[],
            meeting_title="t",
            manual_notes="",
            ai_summary="s",
            notion_url="",
        )
        assert posted == []
        assert failed == []
        assert degraded == []
        client.create_note.assert_not_called()


class TestTranscriptFallback:
    """Failing handler: a rejected full-transcript note retries once without
    the transcript before counting as failed."""

    def test_rejected_transcript_note_falls_back_without_transcript(self, client):
        calls: list[str] = []

        def reject_large(content, content_type, opportunity_ids, **kwargs):
            calls.append(content)
            if "Speaker 1" in content:
                raise AffinityError(413, "payload too large", "/notes")
            return {"id": 9000}

        client.create_note.side_effect = reject_large
        posted, failed, degraded = post_meeting_note_to_lps(
            client,
            opportunity_entity_ids=[701],
            meeting_title="LP X",
            manual_notes="Real notes.",
            ai_summary="The summary.",
            notion_url="https://notion.so/p",
            transcript="Speaker 1: hello\n" * 1000,
        )
        assert posted == [701]
        assert degraded == [701]
        assert failed == []
        assert len(calls) == 2
        # Fallback keeps the other sections + the backlink, swaps the
        # transcript body for an omission notice.
        fallback = calls[1]
        assert "Speaker 1" not in fallback
        assert "Full transcript" in fallback
        assert "omitted" in fallback
        assert "Real notes." in fallback
        assert "The summary." in fallback
        assert "notion.so/p" in fallback

    def test_fallback_failure_records_both_errors(self, client):
        client.create_note.side_effect = AffinityError(500, "boom", "/notes")
        posted, failed, degraded = post_meeting_note_to_lps(
            client,
            opportunity_entity_ids=[701],
            meeting_title="LP X",
            manual_notes="",
            ai_summary="",
            notion_url="",
            transcript="Speaker 1: hello",
        )
        assert posted == []
        assert degraded == []
        assert len(failed) == 1
        opp_id, err_msg = failed[0]
        assert opp_id == 701
        assert "with transcript" in err_msg
        assert "without transcript" in err_msg
        # Exactly two attempts: full, then fallback.
        assert client.create_note.call_count == 2

    def test_no_fallback_attempt_without_transcript(self, client):
        """A failure on a transcript-less note keeps the single-attempt
        behavior — nothing to strip, so retrying the same body is pointless."""
        client.create_note.side_effect = AffinityError(500, "boom", "/notes")
        posted, failed, degraded = post_meeting_note_to_lps(
            client,
            opportunity_entity_ids=[701],
            meeting_title="LP X",
            manual_notes="",
            ai_summary="",
            notion_url="",
        )
        assert posted == []
        assert degraded == []
        assert len(failed) == 1
        assert client.create_note.call_count == 1

    def test_fallback_is_per_opportunity(self, client):
        """One opportunity rejecting the large note doesn't degrade the
        others' posts."""

        def reject_701_full(content, content_type, opportunity_ids, **kwargs):
            if opportunity_ids[0] == 701 and "Speaker 1" in content:
                raise AffinityError(413, "too large", "/notes")
            return {"id": 9000}

        client.create_note.side_effect = reject_701_full
        posted, failed, degraded = post_meeting_note_to_lps(
            client,
            opportunity_entity_ids=[701, 702],
            meeting_title="LP X",
            manual_notes="",
            ai_summary="",
            notion_url="",
            transcript="Speaker 1: hello",
        )
        assert posted == [701, 702]
        assert degraded == [701]
        assert failed == []
        # 701 got the fallback body; 702 still got the full transcript.
        last_call_content = client.create_note.call_args_list[-1].kwargs["content"]
        assert "Speaker 1" in last_call_content
