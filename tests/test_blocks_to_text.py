from src.utils.blocks_to_text import blocks_to_text


class TestBlocksToText:
    def test_paragraph(self):
        blocks = [
            {
                "id": "b1",
                "type": "paragraph",
                "has_children": False,
                "paragraph": {
                    "rich_text": [{"plain_text": "Hello world"}]
                },
            }
        ]
        assert blocks_to_text(blocks) == "Hello world"

    def test_headings(self):
        blocks = [
            {
                "id": "b1",
                "type": "heading_1",
                "has_children": False,
                "heading_1": {"rich_text": [{"plain_text": "Title"}]},
            },
            {
                "id": "b2",
                "type": "heading_2",
                "has_children": False,
                "heading_2": {"rich_text": [{"plain_text": "Subtitle"}]},
            },
        ]
        result = blocks_to_text(blocks)
        assert "# Title" in result
        assert "## Subtitle" in result

    def test_bulleted_list(self):
        blocks = [
            {
                "id": "b1",
                "type": "bulleted_list_item",
                "has_children": False,
                "bulleted_list_item": {
                    "rich_text": [{"plain_text": "Item one"}]
                },
            },
        ]
        assert blocks_to_text(blocks) == "- Item one"

    def test_to_do(self):
        blocks = [
            {
                "id": "b1",
                "type": "to_do",
                "has_children": False,
                "to_do": {
                    "rich_text": [{"plain_text": "Buy milk"}],
                    "checked": False,
                },
            },
        ]
        assert blocks_to_text(blocks) == "- [ ] Buy milk"

    def test_to_do_checked(self):
        blocks = [
            {
                "id": "b1",
                "type": "to_do",
                "has_children": False,
                "to_do": {
                    "rich_text": [{"plain_text": "Done task"}],
                    "checked": True,
                },
            },
        ]
        assert blocks_to_text(blocks) == "- [x] Done task"

    def test_nested_children(self):
        from unittest.mock import MagicMock

        mock_client = MagicMock()
        mock_client.get_block_children.return_value = [
            {
                "id": "child1",
                "type": "paragraph",
                "has_children": False,
                "paragraph": {"rich_text": [{"plain_text": "Nested text"}]},
            }
        ]
        blocks = [
            {
                "id": "parent1",
                "type": "toggle",
                "has_children": True,
                "toggle": {"rich_text": [{"plain_text": "Toggle header"}]},
            },
        ]
        result = blocks_to_text(blocks, client=mock_client)
        assert "Toggle header" in result
        assert "Nested text" in result

    def test_empty_blocks(self):
        assert blocks_to_text([]) == ""

    def test_empty_paragraph_skipped(self):
        blocks = [
            {
                "id": "b1",
                "type": "paragraph",
                "has_children": False,
                "paragraph": {"rich_text": []},
            },
        ]
        assert blocks_to_text(blocks) == ""
