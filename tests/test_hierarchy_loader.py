from unittest.mock import MagicMock

from src.hierarchy_loader import HierarchyLoader


def _make_page(
    page_id: str,
    title: str,
    category: str = "",
    parent_id: str | None = None,
) -> dict:
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

    def test_depth_4_keeps_organizational_nodes(self):
        """At max depth (4), only nodes with children are kept."""
        pages = [
            _make_page("cat", "Sourcing / Investing / Divesting", "Sourcing / Investing / Divesting"),
            _make_page("sub", "Investing", "", parent_id="cat"),
            _make_page("group", "Active Dealflow", "", parent_id="sub"),
            _make_page("deal", "Citadel", "", parent_id="group"),
            # workstream has children → kept at depth 4
            _make_page("ws", "DD workstream", "", parent_id="deal"),
            _make_page("task1", "Review report", "", parent_id="ws"),
            # leaf directly under deal → no children → pruned at depth 4
            _make_page("leaf", "Some leaf task", "", parent_id="deal"),
        ]
        client = self._make_client(pages)
        loader = HierarchyLoader(client, "db-tracker")

        result = loader.load()

        assert len(result) == 1
        cat = result[0]
        assert cat["title"] == "Sourcing / Investing / Divesting"
        sub = cat["children"][0]
        assert sub["title"] == "Investing"
        group = sub["children"][0]
        assert group["title"] == "Active Dealflow"
        deal = group["children"][0]
        assert deal["title"] == "Citadel"
        # workstream kept (has children), leaf task pruned
        assert len(deal["children"]) == 1
        assert deal["children"][0]["title"] == "DD workstream"
        # workstream's children are pruned (beyond max depth)
        assert deal["children"][0]["children"] == []

    def test_has_children_not_in_output(self):
        """The internal has_children flag should be stripped from output."""
        pages = [
            _make_page("cat", "Operations", "Operations"),
            _make_page("child", "Sub-op", "", parent_id="cat"),
        ]
        client = self._make_client(pages)
        loader = HierarchyLoader(client, "db-tracker")

        result = loader.load()

        assert "has_children" not in result[0]
        assert "has_children" not in result[0]["children"][0]

    def test_server_filter_status_and_priority(self):
        """Server-side filter excludes Done + non-architecture (Priority=[DETAILS INSIDE])."""
        client = self._make_client([])
        loader = HierarchyLoader(client, "db-tracker")
        loader.load()

        db_filter = client.query_database.call_args.kwargs["filter"]
        assert db_filter == {
            "and": [
                {"property": "Status", "status": {"does_not_equal": "Done"}},
                {"property": "Priority", "select": {"equals": "[DETAILS INSIDE]"}},
            ],
        }
