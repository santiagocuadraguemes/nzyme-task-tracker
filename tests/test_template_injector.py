from unittest.mock import MagicMock, patch

from src.template_injector import (
    _block_to_create_format,
    _extract_heading_marker,
    _find_notes_block_id,
    fetch_template,
    inject_notes_section,
    page_has_template,
)


def _meeting_notes_block(notes_block_id: str = "notes-1") -> dict:
    return {
        "id": "mn-1",
        "type": "meeting_notes",
        "has_children": True,
        "meeting_notes": {
            "children": {
                "transcript_block_id": "tx-1",
                "notes_block_id": notes_block_id,
            },
            "calendar_event": {"attendees": []},
        },
    }


def _children_map(client: MagicMock, mapping: dict) -> None:
    """Wire ``client.get_block_children`` to dispatch by block id."""
    client.get_block_children.side_effect = lambda block_id: list(mapping.get(block_id, []))


def _heading_block(level: int, text: str, block_id: str = "b1") -> dict:
    """Helper to build a read-format heading block."""
    bt = f"heading_{level}"
    return {
        "id": block_id,
        "type": bt,
        "has_children": False,
        bt: {
            "rich_text": [{"plain_text": text}],
            "color": "blue_background",
            "is_toggleable": False,
        },
    }


def _todo_block(text: str, block_id: str = "t1") -> dict:
    return {
        "id": block_id,
        "type": "to_do",
        "has_children": False,
        "to_do": {
            "rich_text": [{"plain_text": text}] if text else [],
            "checked": False,
        },
    }


class TestExtractHeadingMarker:
    def test_finds_first_heading(self):
        blocks = [
            _todo_block("something"),
            _heading_block(2, "Action Items"),
            _heading_block(3, "Alternative 1"),
        ]
        assert _extract_heading_marker(blocks) == ("heading_2", "action items")

    def test_returns_none_for_no_headings(self):
        blocks = [_todo_block("something")]
        assert _extract_heading_marker(blocks) is None

    def test_returns_none_for_empty(self):
        assert _extract_heading_marker([]) is None


class TestBlockToCreateFormat:
    def test_converts_heading(self):
        client = MagicMock()
        block = _heading_block(2, "Action Items")

        result = _block_to_create_format(block, client)

        assert result["type"] == "heading_2"
        assert result["object"] == "block"
        assert "is_toggleable" not in result["heading_2"]
        assert result["heading_2"]["color"] == "blue_background"

    def test_strips_read_only_fields(self):
        client = MagicMock()
        block = _heading_block(2, "Test")

        result = _block_to_create_format(block, client)

        assert "id" not in result
        assert "has_children" not in result

    def test_skips_non_human_block(self):
        client = MagicMock()
        block = {"id": "ai1", "type": "ai_block", "has_children": True, "ai_block": {}}

        assert _block_to_create_format(block, client) is None

    def test_drops_null_valued_fields(self):
        """Notion read returns ``icon: null`` on paragraphs; create API rejects null."""
        client = MagicMock()
        block = {
            "id": "p1",
            "type": "paragraph",
            "has_children": False,
            "paragraph": {
                "rich_text": [{"plain_text": "hi", "type": "text"}],
                "color": "default",
                "icon": None,
                "caption": None,
            },
        }

        result = _block_to_create_format(block, client)

        assert result is not None
        assert "icon" not in result["paragraph"]
        assert "caption" not in result["paragraph"]
        assert result["paragraph"]["color"] == "default"

    def test_recursively_converts_children(self):
        client = MagicMock()
        child_row = {
            "id": "row1",
            "type": "table_row",
            "has_children": False,
            "table_row": {"cells": [[{"plain_text": "Task"}]]},
        }
        client.get_block_children.return_value = [child_row]

        block = {
            "id": "tbl1",
            "type": "table",
            "has_children": True,
            "table": {
                "table_width": 3,
                "has_column_header": True,
                "has_row_header": False,
            },
        }

        result = _block_to_create_format(block, client)

        assert result["type"] == "table"
        assert len(result["table"]["children"]) == 1
        assert result["table"]["children"][0]["type"] == "table_row"
        client.get_block_children.assert_called_once_with("tbl1")


class TestPageHasTemplate:
    def test_returns_true_when_marker_found(self):
        page_blocks = [_heading_block(2, "Action Items")]
        marker = ("heading_2", "action items")
        assert page_has_template(page_blocks, marker) is True

    def test_returns_true_case_insensitive(self):
        page_blocks = [_heading_block(2, "ACTION ITEMS")]
        marker = ("heading_2", "action items")
        assert page_has_template(page_blocks, marker) is True

    def test_returns_false_when_missing(self):
        page_blocks = [_todo_block("something")]
        marker = ("heading_2", "action items")
        assert page_has_template(page_blocks, marker) is False

    def test_returns_false_for_empty_page(self):
        marker = ("heading_2", "action items")
        assert page_has_template([], marker) is False

    def test_returns_false_when_no_marker(self):
        page_blocks = [_heading_block(2, "Action Items")]
        assert page_has_template(page_blocks, None) is False

    def test_wrong_heading_level_not_matched(self):
        page_blocks = [_heading_block(1, "Action Items")]
        marker = ("heading_2", "action items")
        assert page_has_template(page_blocks, marker) is False


class TestFetchTemplate:
    def test_fetches_and_converts(self):
        client = MagicMock()
        client.get_block_children.return_value = [
            _heading_block(2, "Action Items"),
            _todo_block(""),
            {"id": "ai1", "type": "ai_block", "has_children": False, "ai_block": {}},
        ]

        blocks, marker = fetch_template(client, "template-page-id")

        assert len(blocks) == 2  # ai_block filtered out
        assert blocks[0]["type"] == "heading_2"
        assert blocks[1]["type"] == "to_do"
        assert marker == ("heading_2", "action items")
        client.get_block_children.assert_called_once_with("template-page-id")


class TestFindNotesBlockId:
    def test_returns_notes_block_id(self):
        client = MagicMock()
        _children_map(client, {"page-1": [_meeting_notes_block("notes-xyz")]})

        assert _find_notes_block_id(client, "page-1") == "notes-xyz"

    def test_returns_none_when_no_meeting_notes_block(self):
        client = MagicMock()
        _children_map(client, {"page-1": [_todo_block("standalone")]})

        with patch("src.template_injector.time.sleep"):
            assert _find_notes_block_id(client, "page-1") is None

    def test_retries_then_succeeds(self):
        client = MagicMock()
        # First two calls: no meeting_notes block; third call: present.
        client.get_block_children.side_effect = [
            [],
            [],
            [_meeting_notes_block("notes-late")],
        ]

        with patch("src.template_injector.time.sleep") as mock_sleep:
            result = _find_notes_block_id(client, "page-1")

        assert result == "notes-late"
        assert client.get_block_children.call_count == 3
        assert mock_sleep.call_count == 2

    def test_returns_none_when_notes_block_id_missing(self):
        client = MagicMock()
        broken = {
            "id": "mn-1",
            "type": "meeting_notes",
            "has_children": True,
            "meeting_notes": {"children": {}},
        }
        _children_map(client, {"page-1": [broken]})

        assert _find_notes_block_id(client, "page-1") is None


class TestInjectNotesSection:
    def test_injects_inside_meeting_notes_block(self):
        client = MagicMock()
        _children_map(client, {
            "page-1": [_meeting_notes_block("notes-1")],
            "notes-1": [],  # empty notes section
        })

        template_blocks = [{"object": "block", "type": "heading_2", "heading_2": {}}]
        marker = ("heading_2", "action items")

        result = inject_notes_section(client, "page-1", template_blocks, marker)

        assert result is True
        client.append_block_children.assert_called_once()
        kwargs = client.append_block_children.call_args.kwargs
        assert kwargs["block_id"] == "notes-1"
        assert kwargs["children"] == template_blocks
        assert kwargs["position"] == {"type": "start"}

    def test_skips_when_template_already_in_notes_block(self):
        client = MagicMock()
        _children_map(client, {
            "page-1": [_meeting_notes_block("notes-1")],
            "notes-1": [_heading_block(2, "Action Items")],
        })

        template_blocks = [{"object": "block", "type": "heading_2", "heading_2": {}}]
        marker = ("heading_2", "action items")

        result = inject_notes_section(client, "page-1", template_blocks, marker)

        assert result is False
        client.append_block_children.assert_not_called()

    def test_skips_when_meeting_notes_block_missing(self):
        client = MagicMock()
        _children_map(client, {"page-1": [_todo_block("orphan")]})

        template_blocks = [{"object": "block", "type": "heading_2", "heading_2": {}}]
        marker = ("heading_2", "action items")

        with patch("src.template_injector.time.sleep"):
            result = inject_notes_section(client, "page-1", template_blocks, marker)

        assert result is False
        client.append_block_children.assert_not_called()
