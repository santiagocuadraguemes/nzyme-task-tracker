"""Unit tests for extract_governance_attendees (pure, no network)."""

from __future__ import annotations

from src.transcript_pipeline.fetch_transcript import extract_governance_attendees


def _page_with_governance(people: list[dict]) -> dict:
    return {
        "properties": {
            "Governance: Edit & View Access": {
                "type": "people",
                "people": people,
            }
        }
    }


def test_empty_when_property_missing():
    assert extract_governance_attendees({"properties": {}}) == []


def test_empty_when_people_list_empty():
    assert extract_governance_attendees(_page_with_governance([])) == []


def test_resolves_inline_name_and_id():
    page = _page_with_governance(
        [
            {
                "object": "user",
                "id": "user-1",
                "name": "Sakhee Joisher",
                "type": "person",
                "person": {"email": "sakhee@example.com"},
            }
        ]
    )
    assert extract_governance_attendees(page) == [
        {"id": "user-1", "name": "Sakhee Joisher"}
    ]


def test_falls_back_to_email_when_name_missing():
    page = _page_with_governance(
        [
            {
                "object": "user",
                "id": "user-2",
                "type": "person",
                "person": {"email": "nameless@example.com"},
            }
        ]
    )
    assert extract_governance_attendees(page) == [
        {"id": "user-2", "name": "nameless@example.com"}
    ]


def test_skips_duplicate_ids():
    dup = {
        "object": "user",
        "id": "user-3",
        "name": "Dup User",
        "type": "person",
        "person": {"email": "dup@example.com"},
    }
    page = _page_with_governance([dup, dup])
    assert extract_governance_attendees(page) == [
        {"id": "user-3", "name": "Dup User"}
    ]


def test_skips_entries_without_id():
    page = _page_with_governance(
        [
            {"object": "user", "name": "No ID User"},
            {
                "object": "user",
                "id": "user-4",
                "name": "Valid",
                "type": "person",
                "person": {"email": "v@x.com"},
            },
        ]
    )
    assert extract_governance_attendees(page) == [
        {"id": "user-4", "name": "Valid"}
    ]


def test_ignores_property_of_wrong_type():
    page = {
        "properties": {
            "Governance: Edit & View Access": {
                "type": "rich_text",
                "rich_text": [{"plain_text": "not people"}],
            }
        }
    }
    assert extract_governance_attendees(page) == []
