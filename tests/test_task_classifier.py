"""Tests for TaskClassifier — focus on token compression, category
derivation from Tier-0 ancestor, meeting-taxonomy injection, structured
outputs (Pydantic-typed `.parsed`), and the external-assignee safety
net. The OpenAI client is fully mocked."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.transcript_pipeline.schemas import (
    ClassificationOutput,
    ClassifiedTask,
)
from src.transcript_pipeline.task_classifier import TaskClassifier


def _mock_parse_response(parsed: ClassificationOutput) -> SimpleNamespace:
    """Shape a minimal ``client.beta.chat.completions.parse`` response object."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(parsed=parsed, refusal=None)
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
    )


def _mock_refusal_response(message: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(parsed=None, refusal=message)
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
    )


def _make_classifier(mock_parse: MagicMock) -> TaskClassifier:
    with patch("src.transcript_pipeline.task_classifier.OpenAI") as mock_openai:
        mock_openai.return_value = MagicMock(
            beta=MagicMock(
                chat=MagicMock(completions=MagicMock(parse=mock_parse))
            )
        )
        return TaskClassifier(api_key="fake", model="gpt-test")


PROMPT_TEMPLATE = (
    "Hierarchy: {{HIERARCHY}}\n"
    "Team: {{TEAM_MEMBERS}}\n"
)


# A small two-tier hierarchy used by most tests: VC root with one
# Workstream child (Citadel). The classifier should walk down to Citadel
# (token 2) and derive category "Value Creation (Portfolio)" from the
# root.
_HIERARCHY_VC_CITADEL = [
    {
        "id": "vc-root-id",
        "title": "Value Creation for Portfolio",
        "category": "Value Creation (Portfolio)",
        "children": [
            {
                "id": "citadel-id",
                "title": "Citadel",
                "category": "",
                "children": [],
            },
        ],
    },
]


def test_hierarchy_placeholder_uses_token_compressed_tree():
    """{{HIERARCHY}} must be rendered as the compact {n,t,c} tree, never UUIDs."""
    mock_parse = MagicMock(
        return_value=_mock_parse_response(ClassificationOutput(tasks=[])),
    )
    classifier = _make_classifier(mock_parse)

    classifier.classify(
        [{"title": "t", "internal_assignees": [], "external_assignees": []}],
        PROMPT_TEMPLATE,
        hierarchy=_HIERARCHY_VC_CITADEL,
        team_members=[],
    )

    system_msg = mock_parse.call_args.kwargs["messages"][0]["content"]
    assert '"n": 1' in system_msg
    assert '"t": "Value Creation for Portfolio"' in system_msg
    assert '"n": 2' in system_msg
    assert '"t": "Citadel"' in system_msg
    assert "vc-root-id" not in system_msg
    assert "citadel-id" not in system_msg


def test_team_members_placeholder_uses_bracketed_integer_tokens():
    mock_parse = MagicMock(
        return_value=_mock_parse_response(ClassificationOutput(tasks=[])),
    )
    classifier = _make_classifier(mock_parse)

    classifier.classify(
        [{"title": "t", "internal_assignees": [], "external_assignees": []}],
        PROMPT_TEMPLATE,
        hierarchy=[],
        team_members=[
            {"id": "sakhee-uuid", "name": "Sakhee Joisher", "email": "sakhee@x.com"},
            {"id": "santi-uuid", "name": "Santiago Cuadra", "email": "santiago@x.com"},
        ],
    )

    system_msg = mock_parse.call_args.kwargs["messages"][0]["content"]
    assert "[1] Sakhee Joisher (sakhee)" in system_msg
    assert "[2] Santiago Cuadra (santiago)" in system_msg
    assert "sakhee-uuid" not in system_msg
    assert "santi-uuid" not in system_msg


def test_response_format_is_classification_output_pydantic_model():
    """The call must pin response_format to the Pydantic schema (Structured Outputs)."""
    mock_parse = MagicMock(
        return_value=_mock_parse_response(ClassificationOutput(tasks=[])),
    )
    classifier = _make_classifier(mock_parse)

    classifier.classify(
        [{"title": "t", "internal_assignees": [], "external_assignees": []}],
        PROMPT_TEMPLATE,
        hierarchy=[],
        team_members=[],
    )

    assert mock_parse.call_args.kwargs["response_format"] is ClassificationOutput


def test_unpacks_parent_token_and_derives_category_from_tier0_ancestor():
    """Model emits p=2 (Citadel); unpacker resolves UUID + walks to root category."""
    mock_parse = MagicMock(
        return_value=_mock_parse_response(
            ClassificationOutput(tasks=[ClassifiedTask(i=0, p=2, a=[1])])
        ),
    )
    classifier = _make_classifier(mock_parse)

    tasks = [
        {
            "title": "Review FDD",
            "internal_assignees": ["Sakhee Joisher"],
            "external_assignees": [],
        },
    ]

    result = classifier.classify(
        tasks,
        PROMPT_TEMPLATE,
        hierarchy=_HIERARCHY_VC_CITADEL,
        team_members=[
            {"id": "sakhee-uuid", "name": "Sakhee Joisher", "email": "sakhee@x.com"},
        ],
    )

    assert result[0]["parent_task_id"] == "citadel-id"
    assert result[0]["category"] == "Value Creation (Portfolio)"
    assert result[0]["assignee_id"] == ["sakhee-uuid"]
    assert result[0]["deal_page_id"] is None


def test_null_p_yields_null_parent_and_category_other():
    mock_parse = MagicMock(
        return_value=_mock_parse_response(
            ClassificationOutput(tasks=[ClassifiedTask(i=0, p=None, a=[])])
        ),
    )
    classifier = _make_classifier(mock_parse)

    tasks = [{"title": "t", "internal_assignees": [], "external_assignees": []}]
    result = classifier.classify(
        tasks, PROMPT_TEMPLATE,
        hierarchy=_HIERARCHY_VC_CITADEL,
        team_members=[],
    )
    assert result[0]["parent_task_id"] is None
    assert result[0]["category"] == "Other"
    assert result[0]["assignee_id"] == []


def test_invalid_parent_token_drops_to_other():
    mock_parse = MagicMock(
        return_value=_mock_parse_response(
            ClassificationOutput(tasks=[ClassifiedTask(i=0, p=999, a=[])])
        ),
    )
    classifier = _make_classifier(mock_parse)

    tasks = [{"title": "t", "internal_assignees": [], "external_assignees": []}]
    result = classifier.classify(
        tasks, PROMPT_TEMPLATE,
        hierarchy=_HIERARCHY_VC_CITADEL,
        team_members=[],
    )
    assert result[0]["parent_task_id"] is None
    assert result[0]["category"] == "Other"


def test_external_only_task_drops_assignee_via_safety_net():
    """Even if the model emits an internal token for a first-name collision
    with an external person, the safety net should drop it."""
    mock_parse = MagicMock(
        return_value=_mock_parse_response(
            ClassificationOutput(tasks=[ClassifiedTask(i=0, p=None, a=[1])])
        ),
    )
    classifier = _make_classifier(mock_parse)

    tasks = [
        {
            "title": "Miguel to prepare sales deck",
            "internal_assignees": [],
            "external_assignees": ["Miguel Serrano"],
        }
    ]
    result = classifier.classify(
        tasks, PROMPT_TEMPLATE,
        hierarchy=[],
        team_members=[
            {"id": "internal-miguel-id", "name": "Miguel Kibo", "email": "miguel@x.com"},
        ],
    )
    assert result[0]["assignee_id"] == []
    assert result[0]["external_assignees"] == ["Miguel Serrano"]


def test_mixed_task_keeps_internal_assignee():
    mock_parse = MagicMock(
        return_value=_mock_parse_response(
            ClassificationOutput(tasks=[ClassifiedTask(i=0, p=None, a=[1])])
        ),
    )
    classifier = _make_classifier(mock_parse)

    tasks = [
        {
            "title": "Sakhee + Miguel to align on FDD",
            "internal_assignees": ["Sakhee Joisher"],
            "external_assignees": ["Miguel Serrano"],
        }
    ]
    result = classifier.classify(
        tasks, PROMPT_TEMPLATE,
        hierarchy=[],
        team_members=[
            {"id": "sakhee-id", "name": "Sakhee Joisher", "email": "sakhee@x.com"},
        ],
    )
    assert result[0]["assignee_id"] == ["sakhee-id"]
    assert result[0]["external_assignees"] == ["Miguel Serrano"]


def test_meeting_taxonomy_block_injected_when_provided():
    mock_parse = MagicMock(
        return_value=_mock_parse_response(ClassificationOutput(tasks=[])),
    )
    classifier = _make_classifier(mock_parse)

    classifier.classify(
        [{"title": "t", "internal_assignees": [], "external_assignees": []}],
        PROMPT_TEMPLATE,
        hierarchy=[],
        team_members=[],
        meeting_taxonomy={
            "macro_work_block": "Value Creation for Portfolio",
            "detail": ["Commercial", "FDD"],
            "external_org": "Citadel",
        },
    )

    user_msg = mock_parse.call_args.kwargs["messages"][1]["content"]
    assert "=== MEETING TAXONOMY" in user_msg
    assert "Macro Work Block: Value Creation for Portfolio" in user_msg
    assert "Detail: Commercial, FDD" in user_msg
    assert "External Org: Citadel" in user_msg


def test_empty_meeting_taxonomy_omits_block():
    mock_parse = MagicMock(
        return_value=_mock_parse_response(ClassificationOutput(tasks=[])),
    )
    classifier = _make_classifier(mock_parse)

    classifier.classify(
        [{"title": "t", "internal_assignees": [], "external_assignees": []}],
        PROMPT_TEMPLATE,
        hierarchy=[],
        team_members=[],
        meeting_taxonomy={"macro_work_block": "", "detail": [], "external_org": ""},
    )

    user_msg = mock_parse.call_args.kwargs["messages"][1]["content"]
    assert "=== MEETING TAXONOMY" not in user_msg


def test_meeting_context_attendees_and_notes_blocks():
    mock_parse = MagicMock(
        return_value=_mock_parse_response(ClassificationOutput(tasks=[])),
    )
    classifier = _make_classifier(mock_parse)

    classifier.classify(
        [{"title": "t", "internal_assignees": [], "external_assignees": []}],
        PROMPT_TEMPLATE,
        hierarchy=[],
        team_members=[],
        meeting_title="Commercial Weekly - Citadel",
        meeting_date="2026-05-25T10:00:00+02:00",
        enriched_attendees="- Sakhee Joisher [Investment — Partner]",
        notes_text="External meeting with Citadel CFO.",
    )

    user_msg = mock_parse.call_args.kwargs["messages"][1]["content"]
    assert "=== MEETING CONTEXT ===" in user_msg
    assert "Commercial Weekly - Citadel" in user_msg
    assert "2026-05-25T10:00:00+02:00" in user_msg
    assert "=== ATTENDEES" in user_msg
    assert "Sakhee Joisher" in user_msg
    assert "=== HUMAN NOTES" in user_msg
    assert "External meeting with Citadel CFO." in user_msg
    assert "=== TASKS TO CLASSIFY ===" in user_msg


def test_hierarchy_notes_included_inline_when_present():
    """Nodes with a `notes` field carry it through to the compact tree."""
    mock_parse = MagicMock(
        return_value=_mock_parse_response(ClassificationOutput(tasks=[])),
    )
    classifier = _make_classifier(mock_parse)

    hierarchy = [
        {
            "id": "ops-root",
            "title": "Operations & AI enablement",
            "category": "Operations",
            "notes": "Internal ops, AI/Tech, Marketing, Finance, HR.",
            "children": [
                {
                    "id": "marketing-id",
                    "title": "Marketing",
                    "category": "",
                    "notes": "Brand and content marketing for the firm.",
                    "children": [],
                },
            ],
        },
    ]

    classifier.classify(
        [{"title": "t", "internal_assignees": [], "external_assignees": []}],
        PROMPT_TEMPLATE,
        hierarchy=hierarchy,
        team_members=[],
    )

    system_msg = mock_parse.call_args.kwargs["messages"][0]["content"]
    assert "Brand and content marketing for the firm." in system_msg
    assert "Internal ops, AI/Tech, Marketing, Finance, HR." in system_msg


def test_task_input_includes_internal_and_external_arrays():
    mock_parse = MagicMock(
        return_value=_mock_parse_response(ClassificationOutput(tasks=[])),
    )
    classifier = _make_classifier(mock_parse)

    tasks = [
        {
            "title": "t",
            "internal_assignees": ["Sakhee"],
            "external_assignees": ["Miguel Serrano"],
        }
    ]

    classifier.classify(
        tasks, PROMPT_TEMPLATE, hierarchy=[], team_members=[],
    )

    user_msg = mock_parse.call_args.kwargs["messages"][1]["content"]
    assert "internal_assignees" in user_msg
    assert "external_assignees" in user_msg
    assert "Miguel Serrano" in user_msg
    assert "Sakhee" in user_msg


def test_empty_task_list_returns_immediately():
    mock_parse = MagicMock(
        return_value=_mock_parse_response(ClassificationOutput(tasks=[])),
    )
    classifier = _make_classifier(mock_parse)

    result = classifier.classify(
        [], PROMPT_TEMPLATE, hierarchy=[], team_members=[],
    )
    assert result == []
    mock_parse.assert_not_called()


def test_refusal_response_falls_back_to_other_for_every_task():
    """If the model refuses (safety filter, etc.), every task gets the
    default Other / no-parent / no-assignee shape."""
    mock_parse = MagicMock(
        return_value=_mock_refusal_response("Cannot comply."),
    )
    classifier = _make_classifier(mock_parse)

    tasks = [
        {"title": "t1", "internal_assignees": [], "external_assignees": []},
        {"title": "t2", "internal_assignees": ["Sakhee"], "external_assignees": []},
    ]
    result = classifier.classify(
        tasks, PROMPT_TEMPLATE,
        hierarchy=_HIERARCHY_VC_CITADEL,
        team_members=[
            {"id": "sakhee-id", "name": "Sakhee Joisher", "email": "sakhee@x.com"},
        ],
    )
    for t in result:
        assert t["category"] == "Other"
        assert t["parent_task_id"] is None
        assert t["assignee_id"] == []
        assert t["deal_page_id"] is None
