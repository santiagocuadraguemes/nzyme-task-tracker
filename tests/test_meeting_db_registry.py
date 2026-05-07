"""Tests for src.meeting_db_registry — Org Chart-driven DB discovery."""
from unittest.mock import MagicMock

from src.config import SyncConfig
from src.meeting_db_registry import (
    MeetingDB,
    _extract_db_id,
    discover_meeting_dbs,
    find_owner_for_page,
    load_registry,
)


def _make_config(**overrides) -> SyncConfig:
    defaults = {
        "notion_api_token": "secret_abc",
        "openai_api_key": "sk-abc",
        "team_tracker_db_id": "db-tracker",
        "system_prompt_page_id": "page-system-prompt",
        "user_prompt_page_id": "page-user-prompt",
    }
    defaults.update(overrides)
    return SyncConfig(**defaults)


def _make_org_row(
    *,
    name: str,
    email: str | None,
    mn_db_url: str | None,
    auto_extract_tasks: bool | None = None,
) -> dict:
    props = {
        "Name": {"type": "title", "title": [{"plain_text": name}]},
        "Email": {"type": "email", "email": email},
        "Meeting Notes DB": {"type": "url", "url": mn_db_url},
        "Active": {"type": "checkbox", "checkbox": True},
    }
    if auto_extract_tasks is not None:
        props["Auto-extract Tasks"] = {
            "type": "checkbox", "checkbox": auto_extract_tasks,
        }
    return {"properties": props}


class TestExtractDbId:
    def test_parses_bare_notion_url(self):
        url = "https://www.notion.so/34583e67e2e78081b515f5e33926f153"
        assert _extract_db_id(url) == "34583e67-e2e7-8081-b515-f5e33926f153"

    def test_parses_workspace_prefixed_url(self):
        url = "https://www.notion.so/kiboventures/34583e67e2e78081b515f5e33926f153?v=abc"
        assert _extract_db_id(url) == "34583e67-e2e7-8081-b515-f5e33926f153"

    def test_parses_url_with_slug(self):
        url = "https://www.notion.so/kiboventures/Reyes-Meeting-Notes-b07976472620499fa4b89be7b03c07d0"
        assert _extract_db_id(url) == "b0797647-2620-499f-a4b8-9be7b03c07d0"

    def test_returns_none_for_non_notion_url(self):
        assert _extract_db_id("https://example.com/page-1234") is None

    def test_returns_none_for_empty(self):
        assert _extract_db_id("") is None
        assert _extract_db_id(None) is None


class TestDiscoverMeetingDbs:
    def test_returns_one_entry_per_active_row_with_url(self):
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                _make_org_row(
                    name="Reyes Rubio",
                    email="reyes@kiboventures.com",
                    mn_db_url="https://www.notion.so/b07976472620499fa4b89be7b03c07d0",
                ),
                _make_org_row(
                    name="Santiago Cuadra",
                    email="santiago@kiboventures.com",
                    mn_db_url="https://www.notion.so/34583e67e2e78081b515f5e33926f153",
                ),
            ],
        }

        result = discover_meeting_dbs(client, "org-chart-db")

        assert len(result) == 2
        assert result[0] == MeetingDB(
            db_id="b0797647-2620-499f-a4b8-9be7b03c07d0",
            owner_name="Reyes Rubio",
            owner_email="reyes@kiboventures.com",
        )
        assert result[1].owner_email == "santiago@kiboventures.com"

    def test_skips_rows_without_url(self):
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                _make_org_row(name="No DB Person", email="nope@x.com", mn_db_url=None),
                _make_org_row(
                    name="Has DB",
                    email="has@x.com",
                    mn_db_url="https://www.notion.so/34583e67e2e78081b515f5e33926f153",
                ),
            ],
        }

        result = discover_meeting_dbs(client, "org-chart-db")

        assert len(result) == 1
        assert result[0].owner_name == "Has DB"

    def test_skips_unparseable_urls(self):
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                _make_org_row(
                    name="Bad URL",
                    email="bad@x.com",
                    mn_db_url="https://example.com/not-a-notion-url",
                ),
            ],
        }

        result = discover_meeting_dbs(client, "org-chart-db")

        assert result == []

    def test_skips_duplicate_db_urls(self):
        client = MagicMock()
        url = "https://www.notion.so/34583e67e2e78081b515f5e33926f153"
        client.query_database.return_value = {
            "results": [
                _make_org_row(name="Owner A", email="a@x.com", mn_db_url=url),
                _make_org_row(name="Owner B", email="b@x.com", mn_db_url=url),
            ],
        }

        result = discover_meeting_dbs(client, "org-chart-db")

        assert len(result) == 1
        assert result[0].owner_name == "Owner A"

    def test_filters_for_active_rows(self):
        """Discovery passes Active=true filter to the query."""
        client = MagicMock()
        client.query_database.return_value = {"results": []}

        discover_meeting_dbs(client, "org-chart-db")

        call_kwargs = client.query_database.call_args.kwargs
        assert call_kwargs["filter"] == {
            "property": "Active", "checkbox": {"equals": True},
        }

    def test_auto_extract_tasks_defaults_false_when_column_missing(self):
        """Rows without the Auto-extract Tasks column get the False default."""
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                _make_org_row(
                    name="Old Row",
                    email="old@x.com",
                    mn_db_url="https://www.notion.so/34583e67e2e78081b515f5e33926f153",
                    # auto_extract_tasks omitted — column not present
                ),
            ],
        }

        result = discover_meeting_dbs(client, "org-chart-db")

        assert len(result) == 1
        assert result[0].auto_extract_tasks is False

    def test_auto_extract_tasks_false_propagates(self):
        """When the checkbox is explicitly False, it carries through to MeetingDB."""
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                _make_org_row(
                    name="Opt-Out",
                    email="optout@x.com",
                    mn_db_url="https://www.notion.so/34583e67e2e78081b515f5e33926f153",
                    auto_extract_tasks=False,
                ),
            ],
        }

        result = discover_meeting_dbs(client, "org-chart-db")

        assert len(result) == 1
        assert result[0].auto_extract_tasks is False

    def test_auto_extract_tasks_true_when_set_explicitly(self):
        """When the checkbox is explicitly True, it carries through."""
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                _make_org_row(
                    name="Opt-In",
                    email="optin@x.com",
                    mn_db_url="https://www.notion.so/34583e67e2e78081b515f5e33926f153",
                    auto_extract_tasks=True,
                ),
            ],
        }

        result = discover_meeting_dbs(client, "org-chart-db")

        assert len(result) == 1
        assert result[0].auto_extract_tasks is True


class TestLoadRegistry:
    def test_uses_override_when_meeting_notes_db_id_set(self):
        config = _make_config(meeting_notes_db_id="single-db-override")
        client = MagicMock()

        result = load_registry(config, client)

        assert len(result) == 1
        assert result[0].db_id == "single-db-override"
        # Should NOT have queried the Org Chart.
        client.query_database.assert_not_called()

    def test_falls_back_to_org_chart_when_no_override(self):
        config = _make_config(org_chart_db_id="org-chart-db")
        client = MagicMock()
        client.query_database.return_value = {
            "results": [
                _make_org_row(
                    name="Santiago",
                    email="santiago@x.com",
                    mn_db_url="https://www.notion.so/34583e67e2e78081b515f5e33926f153",
                ),
            ],
        }

        result = load_registry(config, client)

        assert len(result) == 1
        assert result[0].owner_name == "Santiago"

    def test_raises_when_no_source_configured(self):
        config = _make_config()  # neither override nor org_chart_db_id
        client = MagicMock()

        try:
            load_registry(config, client)
        except RuntimeError as e:
            assert "MEETING_NOTES_DB_ID" in str(e)
            assert "ORG_CHART_DB_ID" in str(e)
        else:
            raise AssertionError("Expected RuntimeError")


class TestFindOwnerForPage:
    def test_matches_by_normalized_db_id(self):
        registry = [
            MeetingDB(
                db_id="34583e67-e2e7-8081-b515-f5e33926f153",
                owner_name="Santiago", owner_email="santiago@x.com",
            ),
            MeetingDB(
                db_id="b0797647-2620-499f-a4b8-9be7b03c07d0",
                owner_name="Reyes", owner_email="reyes@x.com",
            ),
        ]

        # Notion sometimes returns DB IDs without dashes — registry has dashes.
        owner = find_owner_for_page(registry, "34583e67e2e78081b515f5e33926f153")
        assert owner is not None
        assert owner.owner_name == "Santiago"

    def test_returns_none_for_unknown_db(self):
        registry = [
            MeetingDB(db_id="abc", owner_name="A", owner_email=""),
        ]
        assert find_owner_for_page(registry, "xyz") is None

    def test_returns_none_for_empty_db_id(self):
        registry = [MeetingDB(db_id="abc", owner_name="A", owner_email="")]
        assert find_owner_for_page(registry, "") is None
