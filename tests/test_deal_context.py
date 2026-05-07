from unittest.mock import MagicMock

from src.deal_context import DealContextLoader


def _make_deal_page(page_id: str, name: str, tracker_rel_id: str | None = None) -> dict:
    """Helper to build a minimal Deal Workplans page."""
    props: dict = {
        "Name": {"type": "title", "title": [{"plain_text": name}]},
    }
    if tracker_rel_id:
        props["\U0001f587\ufe0f Team Task Tracker"] = {
            "type": "relation",
            "relation": [{"id": tracker_rel_id}],
        }
    else:
        props["\U0001f587\ufe0f Team Task Tracker"] = {
            "type": "relation",
            "relation": [],
        }
    return {"id": page_id, "properties": props}


def _make_db_block(block_id: str, title: str) -> dict:
    return {
        "id": block_id,
        "type": "child_database",
        "child_database": {"title": title},
    }


def _make_workstream_page(
    page_id: str, title: str, status: str = "In progress",
    ws_type: list[str] | None = None, adviser: list[str] | None = None,
) -> dict:
    props: dict = {
        "Workstream": {"type": "title", "title": [{"plain_text": title}]},
        "Status": {"type": "status", "status": {"name": status}},
        "Type": {
            "type": "multi_select",
            "multi_select": [{"name": t} for t in (ws_type or [])],
        },
        "Adviser": {
            "type": "multi_select",
            "multi_select": [{"name": a} for a in (adviser or [])],
        },
    }
    return {"id": page_id, "properties": props}


class TestDealContextLoader:
    def _make_client(
        self,
        deal_pages: list[dict],
        block_children: list[dict] | None = None,
        workstream_pages: list[dict] | None = None,
    ) -> MagicMock:
        client = MagicMock()
        # First call: deal workplans query, second call: workstream query
        query_results = [{"results": deal_pages}]
        if workstream_pages is not None:
            query_results.append({"results": workstream_pages})
        client.query_database.side_effect = query_results
        client.get_block_children.return_value = block_children or []
        return client

    def test_loads_deal_with_workstreams(self):
        deal_pages = [_make_deal_page("deal-1", "Citadel", tracker_rel_id="tracker-1")]
        blocks = [
            _make_db_block("wp-db-1", "Citadel Workplan"),
            _make_db_block("ai-db-1", "Citadel Action Items"),
            {"id": "text-1", "type": "paragraph"},  # non-DB block
        ]
        workstreams = [
            _make_workstream_page("ws-1", "FDD", "In progress", ["DD"], ["A&M"]),
            _make_workstream_page("ws-2", "Legal DD", "Not started", ["DD"], ["DLA"]),
        ]
        client = self._make_client(deal_pages, blocks, workstreams)
        loader = DealContextLoader(client, "deal-wp-db")

        deals = loader.load_deals()

        assert len(deals) == 1
        deal = deals[0]
        assert deal.name == "Citadel"
        assert deal.deal_page_id == "deal-1"
        assert deal.tracker_page_id == "tracker-1"
        assert deal.workplan_db_id == "wp-db-1"
        assert deal.action_items_db_id == "ai-db-1"
        assert len(deal.workstreams) == 2
        assert deal.workstreams[0].title == "FDD"
        assert deal.workstreams[0].adviser == ["A&M"]

    def test_deal_without_tracker_relation(self):
        deal_pages = [_make_deal_page("deal-2", "NewDeal")]
        client = self._make_client(deal_pages)
        loader = DealContextLoader(client, "deal-wp-db")

        deals = loader.load_deals()

        assert len(deals) == 1
        assert deals[0].tracker_page_id is None

    def test_deal_without_inline_dbs(self):
        deal_pages = [_make_deal_page("deal-3", "SimpleDeal", "tracker-3")]
        # No child_database blocks
        blocks = [{"id": "p1", "type": "paragraph"}]
        client = self._make_client(deal_pages, blocks)
        loader = DealContextLoader(client, "deal-wp-db")

        deals = loader.load_deals()

        assert len(deals) == 1
        assert deals[0].workplan_db_id is None
        assert deals[0].action_items_db_id is None
        assert deals[0].workstreams == []

    def test_empty_title_skipped(self):
        deal_pages = [{"id": "no-title", "properties": {
            "Name": {"type": "title", "title": []},
        }}]
        client = self._make_client(deal_pages)
        loader = DealContextLoader(client, "deal-wp-db")

        deals = loader.load_deals()

        assert len(deals) == 0

    def test_tracker_page_id_dropped_when_not_in_valid_parent_ids(self):
        """A deal whose Team Task Tracker relation points outside the hierarchy
        (e.g., at an extracted task) has tracker_page_id nulled so the
        classifier prompt never receives that id as a 'valid' parent_task_id."""
        deal_pages = [
            _make_deal_page("deal-good", "GoodDeal", tracker_rel_id="org-node-1"),
            _make_deal_page("deal-bad", "BadDeal", tracker_rel_id="extracted-task-99"),
        ]
        client = self._make_client(deal_pages)
        loader = DealContextLoader(client, "deal-wp-db")

        deals = loader.load_deals(valid_parent_ids={"org-node-1"})

        by_name = {d.name: d for d in deals}
        assert by_name["GoodDeal"].tracker_page_id == "org-node-1"
        assert by_name["BadDeal"].tracker_page_id is None

    def test_individual_deal_failure_doesnt_abort(self):
        deal_pages = [
            _make_deal_page("deal-ok", "GoodDeal", "tracker-ok"),
            _make_deal_page("deal-bad", "BadDeal", "tracker-bad"),
        ]
        client = MagicMock()
        client.query_database.return_value = {"results": deal_pages}
        # First call (GoodDeal blocks) succeeds, second (BadDeal blocks) fails
        client.get_block_children.side_effect = [
            [],
            Exception("Notion API error"),
        ]
        loader = DealContextLoader(client, "deal-wp-db")

        deals = loader.load_deals()

        assert len(deals) == 1
        assert deals[0].name == "GoodDeal"
