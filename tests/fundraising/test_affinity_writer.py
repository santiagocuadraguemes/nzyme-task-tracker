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


class TestPostMeetingNoteToLps:
    def test_all_lps_receive_the_note(self, client):
        posted, failed = post_meeting_note_to_lps(
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
        posted, failed = post_meeting_note_to_lps(
            client,
            opportunity_entity_ids=[701, 702, 703],
            meeting_title="t",
            manual_notes="",
            ai_summary="s",
            notion_url="",
        )
        assert seen == [701, 702, 703]
        assert posted == [701, 703]
        assert len(failed) == 1
        opp_id, err_msg = failed[0]
        assert opp_id == 702
        assert "boom" in err_msg

    def test_empty_list_is_a_noop(self, client):
        posted, failed = post_meeting_note_to_lps(
            client,
            opportunity_entity_ids=[],
            meeting_title="t",
            manual_notes="",
            ai_summary="s",
            notion_url="",
        )
        assert posted == []
        assert failed == []
        client.create_note.assert_not_called()
