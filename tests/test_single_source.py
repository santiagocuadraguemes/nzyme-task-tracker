from unittest.mock import MagicMock

from src.sources.single_source import SingleSource


def _make_meeting_page(
    page_id: str, title: str, date: str, meeting_type: str,
    attendees: list[dict], created_by: dict | None = None,
) -> dict:
    page = {
        "id": page_id,
        "properties": {
            "Meeting": {
                "type": "title",
                "title": [{"plain_text": title}],
            },
            "Date": {
                "type": "date",
                "date": {"start": date} if date else None,
            },
            "Meeting type": {
                "type": "select",
                "select": {"name": meeting_type} if meeting_type else None,
            },
            "Attendees": {
                "type": "people",
                "people": attendees,
            },
            "Processed": {"type": "checkbox", "checkbox": False},
        },
    }
    if created_by:
        page["created_by"] = created_by
    return page


class TestSingleSource:
    def test_get_unprocessed_pages_applies_buffer_filter(self):
        client = MagicMock()
        client.query_database.return_value = {"results": []}
        source = SingleSource(client, "db-meetings")

        source.get_unprocessed_pages(buffer_hours=2)

        call_kwargs = client.query_database.call_args.kwargs
        conditions = call_kwargs["filter"]["and"]
        assert any(c.get("property") == "Processed" for c in conditions)
        assert any(c.get("timestamp") == "created_time" for c in conditions)

    def test_get_page_content_converts_blocks_to_text(self):
        client = MagicMock()
        client.get_block_children.return_value = [
            {
                "id": "b1",
                "type": "paragraph",
                "has_children": False,
                "paragraph": {"rich_text": [{"plain_text": "Meeting notes here"}]},
            },
        ]
        source = SingleSource(client, "db-meetings")

        content = source.get_page_content("page-123")

        assert content == "Meeting notes here"
        client.get_block_children.assert_called_once_with("page-123")

    def test_get_page_metadata(self):
        page = _make_meeting_page(
            "page-1", "Q1 Review", "2026-03-15", "Team sync",
            [{"id": "user-1", "name": "Santiago"}],
            created_by={"id": "user-1", "name": "Santiago"},
        )
        source = SingleSource(MagicMock(), "db-meetings")

        meta = source.get_page_metadata(page)

        assert meta["title"] == "Q1 Review"
        assert meta["date"] == "2026-03-15"
        assert meta["meeting_type"] == "Team sync"
        assert meta["attendees"] == [{"id": "user-1", "name": "Santiago"}]
        assert meta["created_by"] == {"id": "user-1", "name": "Santiago"}

    def test_get_page_metadata_missing_created_by(self):
        page = _make_meeting_page(
            "page-1", "Q1 Review", "2026-03-15", "Team sync", [],
        )
        source = SingleSource(MagicMock(), "db-meetings")

        meta = source.get_page_metadata(page)

        assert meta["created_by"] == {"id": "", "name": ""}

    def test_get_page_content_excludes_ai_blocks_by_default(self):
        client = MagicMock()
        client.get_block_children.return_value = [
            {
                "id": "b1",
                "type": "to_do",
                "has_children": False,
                "to_do": {"rich_text": [{"plain_text": "Call Natalia"}], "checked": False},
            },
            {
                "id": "b2",
                "type": "paragraph",
                "has_children": False,
                "paragraph": {"rich_text": []},
            },
            {
                "id": "ai-block",
                "type": "ai_block",
                "has_children": True,
                "ai_block": {"rich_text": [{"plain_text": "AI summary"}]},
            },
        ]
        source = SingleSource(client, "db-meetings")

        content = source.get_page_content("page-123", include_ai_notes=False)

        assert "Call Natalia" in content
        assert "AI summary" not in content

    def test_get_page_content_includes_ai_blocks_when_enabled(self):
        client = MagicMock()
        client.get_block_children.return_value = [
            {
                "id": "b1",
                "type": "to_do",
                "has_children": False,
                "to_do": {"rich_text": [{"plain_text": "Call Natalia"}], "checked": False},
            },
            {
                "id": "ai-block",
                "type": "ai_block",
                "has_children": False,
                "ai_block": {"rich_text": [{"plain_text": "AI summary"}]},
            },
        ]
        source = SingleSource(client, "db-meetings")

        content = source.get_page_content("page-123", include_ai_notes=True)

        assert "Call Natalia" in content

    def test_mark_page_processed(self):
        client = MagicMock()
        source = SingleSource(client, "db-meetings")

        source.mark_page_processed("page-1")

        client.update_page.assert_called_once_with(
            page_id="page-1",
            properties={"Processed": {"checkbox": True}, "Processing": {"checkbox": False}},
        )
