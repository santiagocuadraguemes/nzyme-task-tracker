from unittest.mock import MagicMock

from src.hierarchy_loader import HierarchyLoader


def _make_page(page_id: str, title: str, category: str = "", parent_id: str | None = None) -> dict:
    """Helper to build a minimal Notion page object."""
    props: dict = {
        "Task": {"type": "title", "title": [{"plain_text": title}]},
        "Status": {"type": "status", "status": {"name": "Not Started"}},
    }
    if category:
        props["Category"] = {"type": "select", "select": {"name": category}}
    else:
        props["Category"] = {"type": "select", "select": None}
    if parent_id:
        props["Parent item"] = {"type": "relation", "relation": [{"id": parent_id}]}
    else:
        props["Parent item"] = {"type": "relation", "relation": []}

    return {"id": page_id, "properties": props}


class TestHierarchyLoader:
    def _make_client(self, pages: list[dict]) -> MagicMock:
        client = MagicMock()
        client.query_database.return_value = {"results": pages}
        return client

    def test_flat_pages_become_roots(self):
        pages = [
            _make_page("p1", "Operations", "Operations"),
            _make_page("p2", "Other", "Other"),
        ]
        client = self._make_client(pages)
        loader = HierarchyLoader(client, "db-tracker")

        result = loader.load()

        assert len(result) == 2
        assert result[0]["title"] == "Operations"
        assert result[0]["children"] == []

    def test_parent_child_tree(self):
        pages = [
            _make_page("cat1", "Dealflow", "Dealflow"),
            _make_page("entity1", "Acme Corp", "", parent_id="cat1"),
        ]
        client = self._make_client(pages)
        loader = HierarchyLoader(client, "db-tracker")

        result = loader.load()

        assert len(result) == 1
        assert result[0]["title"] == "Dealflow"
        assert len(result[0]["children"]) == 1
        assert result[0]["children"][0]["title"] == "Acme Corp"

    def test_caches_result(self):
        client = self._make_client([])
        loader = HierarchyLoader(client, "db-tracker")

        loader.load()
        loader.load()

        client.query_database.assert_called_once()

    def test_filters_done_tasks(self):
        client = self._make_client([])
        loader = HierarchyLoader(client, "db-tracker")
        loader.load()

        call_kwargs = client.query_database.call_args
        assert call_kwargs.kwargs["filter"]["property"] == "Status"
