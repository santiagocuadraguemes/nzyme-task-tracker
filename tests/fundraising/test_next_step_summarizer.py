"""Tests for the enum-constrained next-step summarizer."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.fundraising.next_step_summarizer import (
    FIELD_ID_FOLLOW_UP_DATE,
    FIELD_ID_NEXT_STEP_DROPDOWN,
    summarize_next_step,
)


def _stub_openai(payload: dict) -> MagicMock:
    client = MagicMock()
    msg = MagicMock()
    msg.content = json.dumps(payload)
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    client.chat.completions.create.return_value = resp
    return client


# V1 fields response shape: bare integer ids + dropdown_options
FIELDS_FIXTURE = [
    {
        "id": 5175722,
        "name": "Nzyme next step [DROP-DOWN]",
        "dropdown_options": [
            {"id": 22150286, "text": "Send Germany Fund documentation"},
            {"id": 22152063, "text": "Reach out for first contact"},
        ],
    },
    {
        "id": 5171600,
        "name": "Nzyme next step: Follow Up Date",
        "dropdown_options": [
            {"id": 22345081, "text": "2026 Q3"},
            {"id": 22344941, "text": "2026 Q1"},
        ],
    },
]


def test_summarizer_returns_valid_payload():
    openai = _stub_openai({
        "drop_down_option_id": 22152063,
        "follow_up_option_id": 22344941,
        "details_text": "Santiago to resend Fund I deck and follow up in Q1.",
        "owner_notion_user_id": "uid-santiago",
    })
    result = summarize_next_step(
        openai_client=openai,
        model="gpt-5-mini",
        classified_tasks=[{"title": "Send deck", "assignee_id": ["uid-santiago"]}],
        affinity_fields=FIELDS_FIXTURE,
        meeting_title="Fundraising — LP X",
        meeting_date="2026-04-17",
        creator_name="Santiago Cuadra",
    )
    assert result["drop_down_option_id"] == 22152063
    assert result["follow_up_option_id"] == 22344941
    assert "Santiago" in result["details_text"]
    assert result["owner_notion_user_id"] == "uid-santiago"


def test_summarizer_nulls_invalid_enum_ids():
    openai = _stub_openai({
        "drop_down_option_id": 99999999,  # not in enum
        "follow_up_option_id": 88888888,  # not in enum
        "details_text": "Meeting happened.",
        "owner_notion_user_id": None,
    })
    result = summarize_next_step(
        openai_client=openai,
        model="gpt-5-mini",
        classified_tasks=[],
        affinity_fields=FIELDS_FIXTURE,
        meeting_title="t",
        meeting_date="2026-04-17",
        creator_name="S",
    )
    assert result["drop_down_option_id"] is None
    assert result["follow_up_option_id"] is None


def test_summarizer_raises_on_non_json():
    openai = MagicMock()
    msg = MagicMock()
    msg.content = "not json at all"
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    openai.chat.completions.create.return_value = resp

    with pytest.raises(json.JSONDecodeError):
        summarize_next_step(
            openai_client=openai,
            model="gpt-5-mini",
            classified_tasks=[],
            affinity_fields=FIELDS_FIXTURE,
            meeting_title="t",
            meeting_date="2026-04-17",
            creator_name="S",
        )


def test_summarizer_tolerates_missing_options():
    """When a field has no dropdown_options, enum validation accepts only None."""
    openai = _stub_openai({
        "drop_down_option_id": None,
        "follow_up_option_id": None,
        "details_text": "",
        "owner_notion_user_id": None,
    })
    fields_empty = [
        {"id": 5175722, "dropdown_options": []},
        {"id": 5171600, "dropdown_options": []},
    ]
    result = summarize_next_step(
        openai_client=openai,
        model="gpt-5-mini",
        classified_tasks=[],
        affinity_fields=fields_empty,
        meeting_title="t",
        meeting_date="2026-04-17",
        creator_name="S",
    )
    assert result["drop_down_option_id"] is None
    assert result["follow_up_option_id"] is None


def test_field_id_constants_match_known_list_schema():
    """Guardrail: these ids are hard-wired to list 168609. If they change,
    the summarizer/writer both need updating."""
    assert FIELD_ID_NEXT_STEP_DROPDOWN == "field-5175722"
    assert FIELD_ID_FOLLOW_UP_DATE == "field-5171600"
