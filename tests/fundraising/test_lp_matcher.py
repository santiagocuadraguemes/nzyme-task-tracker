"""Tests for the LP matcher — every matched LP gets a note."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.fundraising.lp_matcher import (
    build_lp_entity_index,
    extract_external_emails,
    resolve_lp_list_entries,
)


@pytest.fixture()
def fake_affinity() -> MagicMock:
    return MagicMock()


def test_build_lp_entity_index_maps_opportunity_to_entry(fake_affinity):
    fake_affinity.list_list_entries.return_value = [
        {"id": 1001, "entity": {"id": 900, "name": "051 Capital"}},
        {"id": 1002, "entity": {"id": 901, "name": "Renta4"}},
        {"id": 1003, "entity_id": 902, "entity": {}},
    ]
    index = build_lp_entity_index(fake_affinity, list_id=168609)
    assert index == {900: 1001, 901: 1002, 902: 1003}


def test_extract_external_emails_drops_kibo():
    emails = extract_external_emails([
        {"id": "santiago@kiboventures.com", "name": "Santiago"},
        {"id": "lp@example.com", "name": "LP"},
        {"id": "notion-uid-123", "name": "Notion User"},
    ])
    assert emails == ["lp@example.com"]


def test_matcher_returns_empty_when_no_external_emails(fake_affinity):
    attendees = [
        {"id": "santiago@kiboventures.com", "name": "Santiago"},
        {"id": "notion-uid-123", "name": "Someone"},
    ]
    result = resolve_lp_list_entries(
        fake_affinity, attendees=attendees, lp_entity_index={900: 999},
    )
    assert result == []
    fake_affinity.search_persons.assert_not_called()


def test_matcher_returns_single_match_via_email(fake_affinity):
    fake_affinity.search_persons.return_value = [
        {"id": 1, "opportunity_ids": [90683048]},
    ]
    result = resolve_lp_list_entries(
        fake_affinity,
        attendees=[{"id": "luciano@051capital.com", "name": "Luciano"}],
        lp_entity_index={90683048: 234863309, 100223544: 225958888},
    )
    assert result == [234863309]


def test_matcher_ignores_opportunities_outside_funnel(fake_affinity):
    fake_affinity.search_persons.return_value = [
        {"id": 1, "opportunity_ids": [100899015, 92861014, 90683048]},
    ]
    result = resolve_lp_list_entries(
        fake_affinity,
        attendees=[{"id": "luciano@051capital.com", "name": "Luciano"}],
        lp_entity_index={90683048: 999},
    )
    assert result == [999]


def test_matcher_returns_all_matches_when_multiple_lps_in_meeting(fake_affinity):
    """User decision: post the same note to every matched LP, don't skip."""
    def persons_side_effect(term):
        if term == "a@fundx.com":
            return [{"id": 1, "opportunity_ids": [100]}]
        return [{"id": 2, "opportunity_ids": [200]}]

    fake_affinity.search_persons.side_effect = persons_side_effect
    result = resolve_lp_list_entries(
        fake_affinity,
        attendees=[
            {"id": "a@fundx.com", "name": "A"},
            {"id": "b@fundy.com", "name": "B"},
        ],
        lp_entity_index={100: 701, 200: 702},
    )
    # Sorted dedup of all matches — both LPs get the note.
    assert result == [701, 702]


def test_matcher_domain_fallback(fake_affinity):
    call_log = []

    def persons_side_effect(term):
        call_log.append(term)
        if term == "unknown@example.com":
            return []
        if term == "@example.com":
            return [{"id": 2, "opportunity_ids": [500]}]
        return []

    fake_affinity.search_persons.side_effect = persons_side_effect
    result = resolve_lp_list_entries(
        fake_affinity,
        attendees=[{"id": "unknown@example.com", "name": "New Contact"}],
        lp_entity_index={500: 999},
    )
    assert result == [999]
    assert call_log == ["unknown@example.com", "@example.com"]


def test_matcher_returns_empty_when_no_candidate(fake_affinity):
    fake_affinity.search_persons.return_value = [
        {"id": 1, "opportunity_ids": [77]},  # 77 not on funnel
    ]
    result = resolve_lp_list_entries(
        fake_affinity,
        attendees=[{"id": "lp@example.com", "name": "LP Rep"}],
        lp_entity_index={900: 999},
    )
    assert result == []


def test_matcher_reads_email_key_when_present(fake_affinity):
    fake_affinity.search_persons.return_value = [
        {"id": 1, "opportunity_ids": [900]},
    ]
    result = resolve_lp_list_entries(
        fake_affinity,
        attendees=[{"id": "uid-abc", "email": "lp@example.com", "name": "LP"}],
        lp_entity_index={900: 999},
    )
    assert result == [999]
    fake_affinity.search_persons.assert_called_with("lp@example.com")
