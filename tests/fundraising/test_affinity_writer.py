"""Tests for the append-safe Affinity writer."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.affinity_client import AffinityError
from src.fundraising.affinity_writer import (
    post_meeting_note_to_lp,
    post_meeting_note_to_lps,
    write_next_step_to_lp,
)


@pytest.fixture()
def client() -> MagicMock:
    c = MagicMock()
    c.get_field_values_for_entry.return_value = []
    return c


def _minimal_payload(**overrides):
    base = {
        "drop_down_option_id": 22152063,
        "follow_up_option_id": 22344941,
        "details_text": "Send Fund I deck; revisit Q1.",
        "owner_notion_user_id": None,
    }
    base.update(overrides)
    return base


def test_writer_posts_when_no_existing_values(client):
    write_next_step_to_lp(
        client,
        list_entry_id=999,
        opportunity_entity_id=701,
        organization_ids=[50],
        fields=[],
        summary_payload=_minimal_payload(),
        meeting_title="Fundraising — LP X",
        meeting_date="2026-04-17",
        notion_url="https://notion.so/page-id",
        owner_affinity_user_id=41826372,
    )
    # 4 POSTs (details + dropdown + follow-up + owner) since no existing values
    assert client.create_field_value.call_count == 4
    client.update_field_value.assert_not_called()
    client.create_note.assert_called_once()


def test_writer_puts_when_existing_values_found(client):
    client.get_field_values_for_entry.return_value = [
        {"id": 11, "field_id": 5437596, "value": "Previous entry"},
        {"id": 12, "field_id": 5175722, "value": 22150286},
        {"id": 13, "field_id": 5171600, "value": 22345081},
        {"id": 14, "field_id": 5432855, "value": 100000},
    ]
    write_next_step_to_lp(
        client,
        list_entry_id=999,
        opportunity_entity_id=701,
        organization_ids=[50],
        fields=[],
        summary_payload=_minimal_payload(),
        meeting_title="Fundraising — LP X",
        meeting_date="2026-04-17",
        notion_url="https://notion.so/page-id",
        owner_affinity_user_id=41826372,
    )
    assert client.update_field_value.call_count == 4
    client.create_field_value.assert_not_called()


def test_writer_appends_details_to_existing_text(client):
    client.get_field_values_for_entry.return_value = [
        {"id": 11, "field_id": 5437596, "value": "Previous meeting note."},
    ]
    write_next_step_to_lp(
        client,
        list_entry_id=999,
        opportunity_entity_id=701,
        organization_ids=[50],
        fields=[],
        summary_payload=_minimal_payload(details_text="New update."),
        meeting_title="LP X meeting",
        meeting_date="2026-04-17",
        notion_url="",
        owner_affinity_user_id=None,
    )
    # DETAILS PUT should receive appended text
    details_call = next(
        call for call in client.update_field_value.call_args_list
        if call.args[0] == 11
    )
    _fv_id, new_value = details_call.args
    assert "Previous meeting note." in new_value
    assert "New update." in new_value
    assert new_value.index("Previous meeting note.") < new_value.index("New update.")


def test_writer_skips_enum_writes_when_summary_has_null(client):
    payload = _minimal_payload(drop_down_option_id=None, follow_up_option_id=None)
    write_next_step_to_lp(
        client,
        list_entry_id=999,
        opportunity_entity_id=701,
        organization_ids=[50],
        fields=[],
        summary_payload=payload,
        meeting_title="t",
        meeting_date="2026-04-17",
        notion_url="",
        owner_affinity_user_id=None,
    )
    # Only DETAILS should be written (owner is None, dropdowns are None)
    assert client.create_field_value.call_count == 1


def test_writer_continues_after_individual_field_failure(client):
    def flaky_create(**kwargs):
        if kwargs.get("field_id") == 5437596:  # DETAILS POST fails
            raise AffinityError(500, "boom", "/field-values")
        return {"id": 1}

    client.create_field_value.side_effect = flaky_create
    # Should NOT raise — individual failures are swallowed
    write_next_step_to_lp(
        client,
        list_entry_id=999,
        opportunity_entity_id=701,
        organization_ids=[50],
        fields=[],
        summary_payload=_minimal_payload(),
        meeting_title="t",
        meeting_date="2026-04-17",
        notion_url="",
        owner_affinity_user_id=41826372,
    )
    # The other three fields still attempted
    assert client.create_field_value.call_count == 4


def test_writer_skips_note_when_no_organization_ids(client):
    write_next_step_to_lp(
        client,
        list_entry_id=999,
        opportunity_entity_id=701,
        organization_ids=[],
        fields=[],
        summary_payload=_minimal_payload(),
        meeting_title="t",
        meeting_date="2026-04-17",
        notion_url="https://notion.so/x",
        owner_affinity_user_id=None,
    )
    client.create_note.assert_not_called()


def test_writer_builds_html_note_with_link(client):
    write_next_step_to_lp(
        client,
        list_entry_id=999,
        opportunity_entity_id=701,
        organization_ids=[50, 51],
        fields=[],
        summary_payload=_minimal_payload(details_text="Plan next steps."),
        meeting_title="LP X fundraising",
        meeting_date="2026-04-17",
        notion_url="https://www.notion.so/kibo/page-abc",
        owner_affinity_user_id=None,
    )
    client.create_note.assert_called_once()
    kwargs = client.create_note.call_args.kwargs
    assert kwargs["content_type"] == "html"
    assert kwargs["organization_ids"] == [50, 51]
    assert "LP X fundraising" in kwargs["content"]
    assert "page-abc" in kwargs["content"]


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
