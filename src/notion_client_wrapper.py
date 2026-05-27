"""Thin wrapper around the official Notion SDK with rate limiting and retries.

Provides a simplified, rate-limited interface to the Notion API.  Every public
method automatically:

1. Acquires a slot from ``RateLimiter`` (default 3 req/s) before issuing
   the HTTP call.
2. Retries transient failures (429 / 5xx) with exponential back-off.

Key class:
    ``NotionClientWrapper`` — instantiated once per sync cycle and shared
    across all components that need Notion access.

Design notes:
    * The underlying ``notion_client.Client`` is injected at construction
      time so that unit tests can substitute a mock.
    * Return types are kept as raw ``dict`` to avoid coupling the rest of
      the codebase to Notion SDK internals.
"""
from __future__ import annotations

import random
import time
import logging
from typing import Any

from notion_client import APIResponseError, Client as NotionClient

from src.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


class NotionClientWrapper:
    """Rate-limited, retry-aware facade over the Notion SDK.

    Parameters
    ----------
    client:
        An authenticated ``notion_client.Client`` instance.
    rate_limiter:
        Optional ``RateLimiter``; a default one (3 req/s) is created if
        omitted.
    max_retries:
        Maximum number of retries for transient errors.
    """

    def __init__(
        self,
        client: NotionClient,
        rate_limiter: RateLimiter | None = None,
        max_retries: int = 4,
    ) -> None:
        self._client = client
        self._rate_limiter = rate_limiter or RateLimiter()
        self._max_retries = max_retries
        self._ds_id_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _call_with_retry(self, fn, **kwargs) -> Any:
        """Execute a Notion SDK call with rate limiting and retry logic.

        Sleeps grow as 2**attempt capped at 16s, plus 0-1s jitter so multiple
        per-DB queries in the same cron tick don't retry in lockstep.
        """
        for attempt in range(1, self._max_retries + 1):
            self._rate_limiter.acquire()
            try:
                return fn(**kwargs)
            except APIResponseError as e:
                if e.status in (429, 500, 502, 503, 504) and attempt < self._max_retries:
                    wait = min(2 ** attempt, 16) + random.uniform(0, 1)
                    logger.warning(
                        "Notion API error %s (attempt %d/%d), retrying in %.1fs",
                        e.status, attempt, self._max_retries, wait,
                    )
                    time.sleep(wait)
                else:
                    raise

    def _resolve_data_source_id(self, database_id: str) -> str:
        """Resolve a database ID to its primary data source ID.

        Notion API 2025-09-03+ replaced databases.query with
        data_sources.query. This method fetches the database schema
        to find the data source ID, then caches it.
        """
        if database_id in self._ds_id_cache:
            return self._ds_id_cache[database_id]

        db = self._call_with_retry(
            self._client.databases.retrieve, database_id=database_id
        )
        data_sources = db.get("data_sources", [])
        if not data_sources:
            raise ValueError(f"Database {database_id} has no data sources")

        ds_id = data_sources[0]["id"]
        self._ds_id_cache[database_id] = ds_id
        logger.debug("Resolved DB %s → data source %s", database_id, ds_id)
        return ds_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def query_database(
        self,
        database_id: str,
        filter: dict[str, Any] | None = None,
        sorts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Query a Notion database, returning the raw response dict.

        Automatically resolves the database ID to a data source ID,
        paginates, and applies rate limiting + retries.
        """
        ds_id = self._resolve_data_source_id(database_id)

        all_results: list[dict[str, Any]] = []
        has_more = True
        next_cursor: str | None = None

        while has_more:
            kwargs: dict[str, Any] = {"data_source_id": ds_id}
            if filter is not None:
                kwargs["filter"] = filter
            if sorts is not None:
                kwargs["sorts"] = sorts
            if next_cursor is not None:
                kwargs["start_cursor"] = next_cursor

            response = self._call_with_retry(
                self._client.data_sources.query, **kwargs
            )
            all_results.extend(response.get("results", []))
            has_more = response.get("has_more", False)
            next_cursor = response.get("next_cursor")

        return {"results": all_results, "has_more": False, "next_cursor": None}

    def get_block_children(self, block_id: str) -> list[dict[str, Any]]:
        """Return all child blocks of the given block/page.

        Handles pagination transparently.
        """
        all_blocks: list[dict[str, Any]] = []
        has_more = True
        next_cursor: str | None = None

        while has_more:
            kwargs: dict[str, Any] = {"block_id": block_id}
            if next_cursor is not None:
                kwargs["start_cursor"] = next_cursor

            response = self._call_with_retry(
                self._client.blocks.children.list, **kwargs
            )
            all_blocks.extend(response.get("results", []))
            has_more = response.get("has_more", False)
            next_cursor = response.get("next_cursor")

        return all_blocks

    def append_block_children(
        self,
        block_id: str,
        children: list[dict[str, Any]],
        position: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Add child blocks to a page or block.

        *position* controls where blocks are inserted:
        - ``None`` or ``{"type": "end"}`` — append at the end (default)
        - ``{"type": "start"}`` — prepend at the beginning
        """
        kwargs: dict[str, Any] = {"block_id": block_id, "children": children}
        if position is not None:
            kwargs["position"] = position
        return self._call_with_retry(
            self._client.blocks.children.append, **kwargs
        )

    def get_page(self, page_id: str) -> dict[str, Any]:
        """Retrieve a single Notion page by ID."""
        return self._call_with_retry(self._client.pages.retrieve, page_id=page_id)

    def create_page(
        self,
        parent_database_id: str,
        properties: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a new page inside a Notion database."""
        return self._call_with_retry(
            self._client.pages.create,
            parent={"database_id": parent_database_id},
            properties=properties,
        )

    def update_page(
        self,
        page_id: str,
        properties: dict[str, Any],
    ) -> dict[str, Any]:
        """Update properties on an existing Notion page."""
        return self._call_with_retry(
            self._client.pages.update,
            page_id=page_id,
            properties=properties,
        )

    def archive_page(self, page_id: str) -> dict[str, Any]:
        """Move a Notion page to trash (soft-delete; recoverable for 30 days).

        Notion API ``2026-03-11`` rejects the legacy ``archived=true`` body
        field with ``"body.archived should be not present, instead was true"``
        — the write parameter is now ``in_trash`` instead. (Responses still
        carry both ``archived`` and ``in_trash`` for backward compat.)
        """
        return self._call_with_retry(
            self._client.pages.update,
            page_id=page_id,
            in_trash=True,
        )

    def retrieve_database(self, database_id: str) -> dict[str, Any]:
        """Retrieve a Notion database by ID."""
        return self._call_with_retry(
            self._client.databases.retrieve, database_id=database_id
        )

    def retrieve_data_source(self, database_id: str) -> dict[str, Any]:
        """Retrieve the data source schema (properties, etc.) for a database."""
        ds_id = self._resolve_data_source_id(database_id)
        return self._call_with_retry(
            self._client.data_sources.retrieve, data_source_id=ds_id
        )

    def update_data_source(
        self,
        database_id: str,
        properties: dict[str, Any],
    ) -> dict[str, Any]:
        """PATCH a data source's schema (e.g. add or remove select options).

        Notion's PATCH supports two select-option operations cleanly:

          * **Add** — include an entry without an ``id`` (just ``name`` and
            optional ``color``). Notion assigns an id, returns it in the
            response.
          * **Remove** — omit the option from the array. Notion drops it.

        **Renaming an existing option in place is NOT supported.** Sending
        ``{id, name: NEW}`` returns 200 and the response echoes the new
        state, but a fresh ``data_sources.retrieve`` shows the OLD name —
        verified 2026-05-21 by ``scripts/diag_work_area_options.py`` across
        ``data_sources.update`` (with and without ``color``) and the legacy
        ``databases.update`` endpoint. Same for archive/un-archive
        (``X`` ↔ ``(archived) X``).

        To achieve a logical rename, use the saga in
        ``src/hierarchy/_rename_saga.py``: PATCH 1 add new option → migrate
        every tagged page via ``pages.update`` → PATCH 2 drop the old option.
        Option IDs change as a result; the per-property mapping tables
        (``work_area_option_mappings`` etc.) absorb the churn.
        """
        ds_id = self._resolve_data_source_id(database_id)
        return self._call_with_retry(
            self._client.data_sources.update,
            data_source_id=ds_id,
            properties=properties,
        )

    def list_users(self) -> list[dict[str, Any]]:
        """Return all users in the workspace, handling pagination."""
        all_users: list[dict[str, Any]] = []
        has_more = True
        next_cursor: str | None = None

        while has_more:
            kwargs: dict[str, Any] = {}
            if next_cursor is not None:
                kwargs["start_cursor"] = next_cursor

            response = self._call_with_retry(self._client.users.list, **kwargs)
            all_users.extend(response.get("results", []))
            has_more = response.get("has_more", False)
            next_cursor = response.get("next_cursor")

        return all_users

    def retrieve_user(self, user_id: str) -> dict[str, Any]:
        """Fetch a single user by ID. For person users, `person.email` carries the
        Workspace email (if the integration has user-info scope)."""
        return self._call_with_retry(self._client.users.retrieve, user_id=user_id)
