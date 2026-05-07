"""Tests for src.literal_notes_extractor — LLM-based notes extraction."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from src.literal_notes_extractor import (
    _build_user_message,
    _format_team_members,
    extract,
    fetch_notes_markdown,
)


# ---------- Block-construction helpers ----------

def _rt(text: str) -> dict:
    return {"type": "text", "plain_text": text}


def _bullet(rich_text: list[dict], block_id: str = "b0", has_children: bool = False) -> dict:
    return {
        "id": block_id,
        "type": "bulleted_list_item",
        "has_children": has_children,
        "bulleted_list_item": {"rich_text": rich_text},
    }


def _heading2(text: str) -> dict:
    return {
        "id": f"h2-{text}",
        "type": "heading_2",
        "has_children": False,
        "heading_2": {"rich_text": [_rt(text)]},
    }


def _meeting_notes_block(notes_block_id: str = "notes-container") -> dict:
    return {
        "id": "mn-1",
        "type": "meeting_notes",
        "has_children": False,
        "meeting_notes": {"children": {"notes_block_id": notes_block_id}},
    }


# ---------- LLM mock ----------

def _make_openai_mock(json_payload: dict, prompt_tokens: int = 100, completion_tokens: int = 50):
    """Return an OpenAI client mock that yields the given JSON object."""
    client = MagicMock()
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps(json_payload)
    response.usage = MagicMock(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
    )
    client.chat.completions.create.return_value = response
    return client


USERS = [
    {"id": "u-santiago", "name": "Santiago Cuadra", "email": "santiago@x.com"},
    {"id": "u-reyes", "name": "Reyes Rubio", "email": "reyes@x.com"},
]


# ---------- Helpers ----------

class TestFormatTeamMembers:
    def test_emits_one_line_per_user_with_aliases(self):
        out = _format_team_members(USERS)
        assert "Santiago Cuadra (ID: u-santiago)" in out
        assert "santiago" in out
        assert "Reyes Rubio (ID: u-reyes)" in out

    def test_empty_users_returns_placeholder(self):
        assert _format_team_members([]) == "No team members available"


class TestBuildUserMessage:
    def test_includes_meeting_context_attendees_and_notes(self):
        msg = _build_user_message(
            notes_markdown="## Action Items\n- Send the deck",
            metadata={
                "title": "Citadel weekly",
                "date": "2026-04-29",
                "created_by": {"id": "u-creator", "name": "Page Creator"},
            },
            attendees=[{"id": "u-santiago", "name": "Santiago Cuadra"}],
        )
        assert "=== MEETING CONTEXT ===" in msg
        assert "Citadel weekly" in msg
        assert "2026-04-29" in msg
        assert "Page Creator (ID: u-creator)" in msg
        assert "=== ATTENDEES ===" in msg
        assert "Santiago Cuadra" in msg
        assert "=== NOTES ===" in msg
        assert "## Action Items" in msg
        assert "Send the deck" in msg

    def test_no_attendees_yields_placeholder(self):
        msg = _build_user_message(
            notes_markdown="x", metadata={"title": "t", "date": "d"}, attendees=None,
        )
        assert "No attendees listed" in msg


# ---------- fetch_notes_markdown ----------

class TestFetchNotesMarkdown:
    def test_prefers_meeting_notes_container(self):
        client = MagicMock()
        client.get_block_children.return_value = [
            _heading2("Action Items"),
            _bullet([_rt("Send the deck (SC)")]),
        ]
        page_blocks = [_meeting_notes_block()]

        md = fetch_notes_markdown(client, page_blocks)

        assert "## Action Items" in md
        assert "Send the deck (SC)" in md
        client.get_block_children.assert_called_once_with("notes-container")

    def test_falls_back_to_page_blocks_when_no_meeting_notes(self):
        client = MagicMock()
        page_blocks = [
            _heading2("Action Items"),
            _bullet([_rt("Top-level bullet")]),
        ]

        md = fetch_notes_markdown(client, page_blocks)

        assert "## Action Items" in md
        assert "Top-level bullet" in md
        client.get_block_children.assert_not_called()

    def test_falls_back_when_notes_container_empty(self):
        client = MagicMock()
        client.get_block_children.return_value = []
        page_blocks = [
            _meeting_notes_block(),
            _bullet([_rt("Page-level bullet")]),
        ]

        md = fetch_notes_markdown(client, page_blocks)

        assert "Page-level bullet" in md


# ---------- extract (the LLM-driven entrypoint) ----------

class TestExtract:
    def _setup(self, llm_payload: dict, notes_blocks: list[dict] | None = None):
        client = MagicMock()
        if notes_blocks is None:
            notes_blocks = [
                _heading2("Action Items"),
                _bullet([_rt("Send the deck (SC)")]),
                _bullet([_rt("Follow up with Reyes")]),
            ]
        client.get_block_children.return_value = notes_blocks
        page_blocks = [_meeting_notes_block()]
        openai_client = _make_openai_mock(llm_payload)
        return client, page_blocks, openai_client

    def test_happy_path_returns_classifier_shape(self):
        payload = {
            "tasks": [
                {
                    "title": "Send the deck",
                    "assignee": "Santiago",
                    "internal_assignees": ["Santiago Cuadra"],
                    "external_assignees": [],
                    "supporting_quote": "Send the deck (SC)",
                },
                {
                    "title": "Follow up with Reyes",
                    "assignee": "Reyes",
                    "internal_assignees": ["Reyes Rubio"],
                    "external_assignees": [],
                    "supporting_quote": "Follow up with Reyes",
                },
            ]
        }
        client, page_blocks, openai_client = self._setup(payload)

        tasks = extract(
            client=client,
            page_blocks=page_blocks,
            metadata={"title": "Citadel sync", "date": "2026-04-29",
                      "created_by": {"id": "u-creator", "name": "Creator"}},
            attendees=[],
            all_users=USERS,
            system_prompt_template="Team: {{TEAM_MEMBERS}}",
            openai_client=openai_client,
            model="gpt-5-mini",
        )

        assert len(tasks) == 2
        assert tasks[0]["title"] == "Send the deck"
        assert tasks[0]["internal_assignees"] == ["Santiago Cuadra"]
        assert tasks[0]["external_assignees"] == []
        assert tasks[0]["supporting_quote"] == "Send the deck (SC)"
        # No assignee_id yet — that's the classifier's job downstream.
        assert "assignee_id" not in tasks[0]

    def test_team_members_substituted_into_system_prompt(self):
        client, page_blocks, openai_client = self._setup({"tasks": []})

        extract(
            client=client,
            page_blocks=page_blocks,
            metadata={"title": "t", "date": "d"},
            attendees=[],
            all_users=USERS,
            system_prompt_template="Workforce:\n{{TEAM_MEMBERS}}",
            openai_client=openai_client,
            model="gpt-5-mini",
        )

        call = openai_client.chat.completions.create.call_args
        messages = call.kwargs["messages"]
        system_msg = messages[0]["content"]
        assert "Santiago Cuadra (ID: u-santiago)" in system_msg
        assert "Reyes Rubio (ID: u-reyes)" in system_msg

    def test_uses_json_object_response_format(self):
        client, page_blocks, openai_client = self._setup({"tasks": []})

        extract(
            client=client, page_blocks=page_blocks,
            metadata={"title": "t", "date": "d"}, attendees=[],
            all_users=USERS, system_prompt_template="x",
            openai_client=openai_client, model="gpt-5-mini",
        )

        call = openai_client.chat.completions.create.call_args
        assert call.kwargs["response_format"] == {"type": "json_object"}
        assert call.kwargs["model"] == "gpt-5-mini"

    def test_returns_empty_when_no_notes_content(self):
        client = MagicMock()
        client.get_block_children.return_value = []  # no children in notes_block_id
        page_blocks = [_meeting_notes_block()]
        # No top-level bullets either.
        openai_client = _make_openai_mock({"tasks": []})

        tasks = extract(
            client=client, page_blocks=page_blocks,
            metadata={"title": "t", "date": "d"}, attendees=[],
            all_users=USERS, system_prompt_template="x",
            openai_client=openai_client, model="gpt-5-mini",
        )

        assert tasks == []
        # LLM should NOT be called when there's no content to extract from.
        openai_client.chat.completions.create.assert_not_called()

    def test_drops_tasks_with_empty_titles(self):
        payload = {
            "tasks": [
                {"title": "", "internal_assignees": [], "external_assignees": []},
                {"title": "  ", "internal_assignees": [], "external_assignees": []},
                {"title": "Real task", "internal_assignees": [],
                 "external_assignees": [], "supporting_quote": "Real task"},
            ]
        }
        client, page_blocks, openai_client = self._setup(payload)

        tasks = extract(
            client=client, page_blocks=page_blocks,
            metadata={"title": "t", "date": "d"}, attendees=[],
            all_users=USERS, system_prompt_template="x",
            openai_client=openai_client, model="gpt-5-mini",
        )

        assert len(tasks) == 1
        assert tasks[0]["title"] == "Real task"

    def test_handles_invalid_json_gracefully(self):
        client = MagicMock()
        client.get_block_children.return_value = [
            _heading2("Action Items"),
            _bullet([_rt("Anything")]),
        ]
        page_blocks = [_meeting_notes_block()]
        openai_client = MagicMock()
        bad_resp = MagicMock()
        bad_resp.choices = [MagicMock()]
        bad_resp.choices[0].message.content = "not json {{{"
        bad_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        openai_client.chat.completions.create.return_value = bad_resp

        tasks = extract(
            client=client, page_blocks=page_blocks,
            metadata={"title": "t", "date": "d"}, attendees=[],
            all_users=USERS, system_prompt_template="x",
            openai_client=openai_client, model="gpt-5-mini",
        )

        assert tasks == []

    def test_handles_non_list_tasks_field(self):
        client, page_blocks, openai_client = self._setup({"tasks": "not a list"})

        tasks = extract(
            client=client, page_blocks=page_blocks,
            metadata={"title": "t", "date": "d"}, attendees=[],
            all_users=USERS, system_prompt_template="x",
            openai_client=openai_client, model="gpt-5-mini",
        )

        assert tasks == []

    def test_supporting_quote_defaults_to_title_when_missing(self):
        payload = {
            "tasks": [
                {"title": "Just title", "internal_assignees": [], "external_assignees": []},
            ]
        }
        client, page_blocks, openai_client = self._setup(payload)

        tasks = extract(
            client=client, page_blocks=page_blocks,
            metadata={"title": "t", "date": "d"}, attendees=[],
            all_users=USERS, system_prompt_template="x",
            openai_client=openai_client, model="gpt-5-mini",
        )

        assert tasks[0]["supporting_quote"] == "Just title"
