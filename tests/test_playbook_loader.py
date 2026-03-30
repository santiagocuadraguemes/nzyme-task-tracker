from unittest.mock import MagicMock

from src.playbook_loader import PlaybookLoader


class TestPlaybookLoader:
    def _make_client(self, blocks: list[dict]) -> MagicMock:
        client = MagicMock()
        client.get_block_children.return_value = blocks
        return client

    def test_load_returns_text(self):
        blocks = [
            {
                "id": "b1",
                "type": "heading_1",
                "has_children": False,
                "heading_1": {"rich_text": [{"plain_text": "Rules"}]},
            },
            {
                "id": "b2",
                "type": "bulleted_list_item",
                "has_children": False,
                "bulleted_list_item": {
                    "rich_text": [{"plain_text": "Extract tasks from to-do blocks"}]
                },
            },
        ]
        client = self._make_client(blocks)
        loader = PlaybookLoader(client, "page-123")

        result = loader.load()

        assert "# Rules" in result
        assert "Extract tasks from to-do blocks" in result
        client.get_block_children.assert_called_once_with("page-123")

    def test_load_caches_result(self):
        blocks = [
            {
                "id": "b1",
                "type": "paragraph",
                "has_children": False,
                "paragraph": {"rich_text": [{"plain_text": "Cached"}]},
            },
        ]
        client = self._make_client(blocks)
        loader = PlaybookLoader(client, "page-123")

        loader.load()
        loader.load()

        client.get_block_children.assert_called_once()

    def test_load_empty_page(self):
        client = self._make_client([])
        loader = PlaybookLoader(client, "page-123")
        assert loader.load() == ""
