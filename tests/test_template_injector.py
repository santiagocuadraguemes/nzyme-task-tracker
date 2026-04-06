from unittest.mock import MagicMock

from src.template_injector import (
    _block_to_create_format,
    _extract_heading_marker,
    fetch_template,
    inject_notes_section,
    page_has_template,
)


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


class TestInjectNotesSection:
    def test_injects_when_template_missing(self):
        client = MagicMock()
        client.get_block_children.return_value = []  # empty page

        template_blocks = [{"object": "block", "type": "heading_2", "heading_2": {}}]
        marker = ("heading_2", "action items")

        result = inject_notes_section(client, "page-123", template_blocks, marker)

        assert result is True
        client.append_block_children.assert_called_once()

    def test_skips_when_template_exists(self):
        client = MagicMock()
        client.get_block_children.return_value = [_heading_block(2, "Action Items")]

        template_blocks = [{"object": "block", "type": "heading_2", "heading_2": {}}]
        marker = ("heading_2", "action items")

        result = inject_notes_section(client, "page-123", template_blocks, marker)

        assert result is False
        client.append_block_children.assert_not_called()
