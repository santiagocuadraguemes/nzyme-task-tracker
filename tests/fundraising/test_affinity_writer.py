"""Tests for the append-safe Affinity writer."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.affinity_client import AffinityError
from src.fundraising.affinity_writer import (
    post_meeting_note_to_lp,
    post_meeting_note_to_lps,
)


@pytest.fixture()
def client() -> MagicMock:
    c = MagicMock()
    c.get_field_values_for_entry.return_value = []
    return c


class TestPostMeetingNoteToLp:
    def test_posts_html_note_attached_to_opportunity(self, client):
        post_meeting_note_to_lp(
            client,
            opportunity_entity_id=701,
            meeting_title="LP X fundraising",
            meeting_date="2026-04-17",
            summary="Send Fund I deck.",
            notion_url="https://www.notion.so/kibo/page-abc",
        )

        client.create_note.assert_called_once()
        kwargs = client.create_note.call_args.kwargs
        assert kwargs["content_type"] == "html"
        assert kwargs["opportunity_ids"] == [701]
        # Note should NOT use organization_ids — attaching to opportunity only
        assert "organization_ids" not in kwargs
        assert "person_ids" not in kwargs
        assert "LP X fundraising" in kwargs["content"]
        assert "Send Fund I deck." in kwargs["content"]
        assert "page-abc" in kwargs["content"]
        # No field-value writes in note-only mode
        client.create_field_value.assert_not_called()
        client.update_field_value.assert_not_called()

    def test_skips_when_opportunity_entity_id_missing(self, client):
        post_meeting_note_to_lp(
            client,
            opportunity_entity_id=None,
            meeting_title="LP X",
            meeting_date="2026-04-17",
            summary="x",
            notion_url="",
        )
        client.create_note.assert_not_called()

    def test_swallows_affinity_error(self, client):
        client.create_note.side_effect = AffinityError(500, "boom", "/notes")
        # Should NOT raise — note post failures are logged and swallowed
        post_meeting_note_to_lp(
            client,
            opportunity_entity_id=701,
            meeting_title="t",
            meeting_date="2026-04-17",
            summary="s",
            notion_url="",
        )
        client.create_note.assert_called_once()


class TestPostMeetingNoteToLps:
    def test_all_lps_receive_the_note(self, client):
        posted, failed = post_meeting_note_to_lps(
            client,
            opportunity_entity_ids=[701, 702, 703],
            meeting_title="LP X & Y intro",
            meeting_date="2026-04-17",
            summary="Both LPs are interested.",
            notion_url="https://notion.so/p",
        )
        assert posted == [701, 702, 703]
        assert failed == []
        assert client.create_note.call_count == 3
        # Each call attaches to exactly ONE opportunity (separate notes).
        for call in client.create_note.call_args_list:
            assert len(call.kwargs["opportunity_ids"]) == 1

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
            meeting_date="2026-04-17",
            summary="s",
            notion_url="",
        )
        assert seen == [701, 702, 703]
        assert posted == [701, 703]
        assert len(failed) == 1
        opp_id, err_msg = failed[0]
        assert opp_id == 702
        assert "boom" in err_msg

    def test_single_lp_path_still_works(self, client):
        posted, failed = post_meeting_note_to_lps(
            client,
            opportunity_entity_ids=[701],
            meeting_title="LP X",
            meeting_date="2026-04-17",
            summary="Send deck.",
            notion_url="https://notion.so/p",
        )
        assert posted == [701]
        assert failed == []
        client.create_note.assert_called_once()

    def test_empty_list_is_a_noop(self, client):
        posted, failed = post_meeting_note_to_lps(
            client,
            opportunity_entity_ids=[],
            meeting_title="t",
            meeting_date="2026-04-17",
            summary="s",
            notion_url="",
        )
        assert posted == []
        assert failed == []
        client.create_note.assert_not_called()
