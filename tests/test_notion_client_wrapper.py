"""Regression tests for src.notion_client_wrapper.NotionClientWrapper.

Kept narrow to API-version-sensitive behavior — Notion has historically
renamed body fields between versions (most recently 2026-03-11 swapped
``archived`` for ``in_trash`` on ``pages.update``).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.notion_client_wrapper import NotionClientWrapper
from src.utils.rate_limiter import RateLimiter


def _wrapper() -> tuple[NotionClientWrapper, MagicMock]:
    """Wrapper backed by a MagicMock SDK client + a 1000 req/s rate limiter
    so tests don't sleep."""
    raw = MagicMock()
    wrapper = NotionClientWrapper(
        raw, rate_limiter=RateLimiter(max_requests_per_second=1000),
    )
    return wrapper, raw


class TestArchivePage:
    """archive_page must send ``in_trash=True`` on Notion API 2026-03-11.

    Sending the legacy ``archived=True`` returns 400 with
    ``"body.archived should be not present, instead was true"``.
    """

    def test_sends_in_trash_true_not_archived(self):
        wrapper, raw = _wrapper()
        raw.pages.update.return_value = {"id": "p-1", "in_trash": True}

        wrapper.archive_page("p-1")

        raw.pages.update.assert_called_once()
        kwargs = raw.pages.update.call_args.kwargs
        assert kwargs == {"page_id": "p-1", "in_trash": True}
        # Guard against accidental regression to the legacy field name.
        assert "archived" not in kwargs
