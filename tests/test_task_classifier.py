"""Tests for TaskClassifier — focus on meeting-context injection and the
external-assignee safety net. The OpenAI client is fully mocked."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.transcript_pipeline.task_classifier import TaskClassifier


def _mock_openai_response(payload: dict) -> SimpleNamespace:
    """Shape a minimal OpenAI ChatCompletion response object."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(payload))
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
    )


def _make_classifier(mock_create: MagicMock) -> TaskClassifier:
    with patch("src.transcript_pipeline.task_classifier.OpenAI") as mock_openai:
        mock_openai.return_value = MagicMock(
            chat=MagicMock(completions=MagicMock(create=mock_create))
        )
        return TaskClassifier(api_key="fake", model="gpt-test")


PROMPT_TEMPLATE = (
    "Categories: {{CATEGORIES}}\n"
    "Hierarchy: {{HIERARCHY}}\n"
    "Team: {{TEAM_MEMBERS}}\n"
    "Deals: {{DEAL_CONTEXT}}\n"
)


def test_meeting_context_is_included_in_user_message():
    mock_create = MagicMock(
        return_value=_mock_openai_response(
            {
                "tasks": [
                    {
                        "index": 0,
                        "category": "Value Creation (Portfolio)",
                        "parent_task_id": "wv-id",
                        "assignee_id": [],
                        "deal_page_id": None,
                    }
                ]
            }
        )
    )
    classifier = _make_classifier(mock_create)

    tasks = [{"title": "Review FDD", "assignee": "Sakhee"}]
    hierarchy = [{"id": "wv-id", "title": "White Vega", "children": []}]
    team_members = [{"id": "u1", "name": "Sakhee Joisher", "email": "sakhee@example.com"}]

    classifier.classify(
        tasks,
        PROMPT_TEMPLATE,
        categories=["Value Creation (Portfolio)"],
        hierarchy=hierarchy,
        team_members=team_members,
        meeting_title="Commercial Weekly - WV",
        meeting_date="2026-03-27T10:02:00+01:00",
        enriched_attendees="- Sakhee Joisher [Investment — Partner]\n- Miguel Serrano (external)",
        notes_text="This is a White Vega Meeting (external).",
    )

    user_msg = mock_create.call_args.kwargs["messages"][1]["content"]
    assert "=== MEETING CONTEXT ===" in user_msg
    assert "Commercial Weekly - WV" in user_msg
    assert "2026-03-27T10:02:00+01:00" in user_msg
    assert "=== ATTENDEES" in user_msg
    assert "Miguel Serrano (external)" in user_msg
    assert "=== HUMAN NOTES" in user_msg
    assert "White Vega Meeting" in user_msg
    assert "=== TASKS TO CLASSIFY ===" in user_msg


def test_external_only_task_drops_guessed_assignee_id():
    # Classifier's LLM returns a spurious internal ID for a task whose only
    # assignee is external (Miguel Serrano). The safety net should drop it.
    mock_create = MagicMock(
        return_value=_mock_openai_response(
            {
                "tasks": [
                    {
                        "index": 0,
                        "category": "Value Creation (Portfolio)",
                        "parent_task_id": None,
                        "assignee_id": ["internal-miguel-id"],
                        "deal_page_id": None,
                    }
                ]
            }
        )
    )
    classifier = _make_classifier(mock_create)

    tasks = [
        {
            "title": "Miguel to prepare sales deck",
            "assignee": "Miguel Serrano",
            "internal_assignees": [],
            "external_assignees": ["Miguel Serrano"],
        }
    ]
    team_members = [{"id": "internal-miguel-id", "name": "Miguel Kibo", "email": "miguel@kibo.com"}]

    result = classifier.classify(
        tasks,
        PROMPT_TEMPLATE,
        categories=["Value Creation (Portfolio)"],
        hierarchy=[],
        team_members=team_members,
    )

    assert result[0]["assignee_id"] == []
    assert result[0]["external_assignees"] == ["Miguel Serrano"]


def test_mixed_task_keeps_internal_id_and_preserves_external():
    mock_create = MagicMock(
        return_value=_mock_openai_response(
            {
                "tasks": [
                    {
                        "index": 0,
                        "category": "Value Creation (Portfolio)",
                        "parent_task_id": None,
                        "assignee_id": ["sakhee-id"],
                        "deal_page_id": None,
                    }
                ]
            }
        )
    )
    classifier = _make_classifier(mock_create)

    tasks = [
        {
            "title": "Sakhee + Miguel to align on FDD",
            "assignee": "Sakhee, Miguel Serrano",
            "internal_assignees": ["Sakhee Joisher"],
            "external_assignees": ["Miguel Serrano"],
        }
    ]
    team_members = [{"id": "sakhee-id", "name": "Sakhee Joisher", "email": "sakhee@x.com"}]

    result = classifier.classify(
        tasks,
        PROMPT_TEMPLATE,
        categories=["Value Creation (Portfolio)"],
        hierarchy=[],
        team_members=team_members,
    )

    assert result[0]["assignee_id"] == ["sakhee-id"]
    assert result[0]["external_assignees"] == ["Miguel Serrano"]


def test_task_input_includes_internal_and_external_arrays():
    mock_create = MagicMock(
        return_value=_mock_openai_response({"tasks": []})
    )
    classifier = _make_classifier(mock_create)

    tasks = [
        {
            "title": "t",
            "assignee": "X",
            "internal_assignees": ["Sakhee"],
            "external_assignees": ["Miguel Serrano"],
        }
    ]

    classifier.classify(
        tasks,
        PROMPT_TEMPLATE,
        categories=["Other"],
        hierarchy=[],
        team_members=[],
    )

    user_msg = mock_create.call_args.kwargs["messages"][1]["content"]
    # The tasks JSON block should contain both arrays
    assert "internal_assignees" in user_msg
    assert "external_assignees" in user_msg
    assert "Miguel Serrano" in user_msg
    assert "Sakhee" in user_msg


def test_invalid_category_falls_back_to_other():
    mock_create = MagicMock(
        return_value=_mock_openai_response(
            {
                "tasks": [
                    {
                        "index": 0,
                        "category": "Not A Real Category",
                        "parent_task_id": None,
                        "assignee_id": [],
                        "deal_page_id": None,
                    }
                ]
            }
        )
    )
    classifier = _make_classifier(mock_create)

    tasks = [{"title": "t", "assignee": "X"}]

    result = classifier.classify(
        tasks,
        PROMPT_TEMPLATE,
        categories=["Operations", "Other"],
        hierarchy=[],
        team_members=[],
    )

    assert result[0]["category"] == "Other"
