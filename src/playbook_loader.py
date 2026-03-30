"""Fetch and cache the Nzyme playbook from a Notion page."""
from __future__ import annotations

import logging

from src.notion_client_wrapper import NotionClientWrapper
from src.utils.blocks_to_text import blocks_to_text

logger = logging.getLogger(__name__)


class PlaybookLoader:
    """Loads the playbook Notion page and converts it to plain text.

    Caches the result for the lifetime of the instance (one sync cycle).
    """

    def __init__(self, client: NotionClientWrapper, page_id: str) -> None:
        self._client = client
        self._page_id = page_id
        self._cache: str | None = None

    def load(self) -> str:
        """Return the playbook as plain text. Cached after first call."""
        if self._cache is not None:
            return self._cache
        blocks = self._client.get_block_children(self._page_id)
        self._cache = blocks_to_text(blocks, self._client)
        logger.info("Loaded playbook (%d chars)", len(self._cache))
        return self._cache
