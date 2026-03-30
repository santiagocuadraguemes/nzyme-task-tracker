"""Shared pytest fixtures for the Nzyme test suite."""
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def mock_client():
    """Return a MagicMock mimicking NotionClientWrapper."""
    client = MagicMock()
    client.query_database = MagicMock(
        return_value={"results": [], "has_more": False, "next_cursor": None}
    )
    client.get_block_children = MagicMock(return_value=[])
    client.get_page = MagicMock(return_value={})
    client.create_page = MagicMock(
        return_value={"id": "new-page-id", "object": "page"}
    )
    client.update_page = MagicMock(
        return_value={"id": "updated-page-id", "object": "page"}
    )
    client.retrieve_database = MagicMock(return_value={"properties": {}})
    return client
