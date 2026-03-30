# AI-Driven Task Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the deterministic Python extraction pipeline with an OpenAI-powered task extractor that reads a Notion playbook and creates tasks via function calling.

**Architecture:** A scheduled sync loop polls for finished meetings (Date < now-2h, Processed=false), fetches each meeting's content + a Notion playbook page + the current Task Tracker hierarchy, sends it all to OpenAI GPT-4.1 with a `create_task` tool definition, then validates and writes the returned tasks to Notion via the existing API wrapper.

**Tech Stack:** Python 3.11+, OpenAI Python SDK, notion-client, Pydantic, pytest

**Spec:** `docs/superpowers/specs/2026-03-27-ai-driven-task-extraction-design.md`

---

### Task 1: Delete Old Modules and Update Dependencies

**Files:**
- Delete: `src/extraction/` (entire directory)
- Delete: `src/dedup/` (entire directory)
- Delete: `src/schema/` (entire directory)
- Delete: `src/sources/base.py`
- Delete: `src/sources/multi_source.py`
- Delete: `src/sources/registry.py`
- Delete: `src/tracker/writer.py`
- Delete: `tests/test_block_parser.py`, `tests/test_date_parser.py`, `tests/test_mention_parser.py`, `tests/test_dedup.py`, `tests/test_multi_source.py`, `tests/test_schema_mapper.py`, `tests/test_writer.py`
- Modify: `pyproject.toml`
- Modify: `.env.example`

- [ ] **Step 1: Delete old extraction, dedup, schema directories and unused source/tracker files**

```bash
rm -rf src/extraction src/dedup src/schema
rm -f src/sources/base.py src/sources/multi_source.py src/sources/registry.py
rm -f src/tracker/writer.py
```

- [ ] **Step 2: Delete obsolete test files**

```bash
rm -f tests/test_block_parser.py tests/test_date_parser.py tests/test_mention_parser.py
rm -f tests/test_dedup.py tests/test_multi_source.py tests/test_schema_mapper.py
rm -f tests/test_writer.py
```

- [ ] **Step 3: Update pyproject.toml**

Replace the full contents of `pyproject.toml` with:

```toml
[build-system]
requires = ["setuptools>=68.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "nzyme-task-tracker"
version = "0.2.0"
description = "AI-driven sync engine that extracts action items from Notion meeting notes into a Team Task Tracker."
requires-python = ">=3.11"
dependencies = [
    "notion-client",
    "python-dotenv",
    "pydantic",
    "openai",
]

[project.optional-dependencies]
dev = [
    "pytest",
    "ruff",
]

[project.scripts]
nzyme-task-tracker = "src.main:main"
```

- [ ] **Step 4: Update .env.example**

Replace the full contents of `.env.example` with:

```bash
# Required
NOTION_API_TOKEN=
OPENAI_API_KEY=
MEETING_NOTES_DB_ID=
TEAM_TRACKER_DB_ID=
PLAYBOOK_PAGE_ID=

# Optional
OPENAI_MODEL=gpt-4.1
BUFFER_HOURS=2
LOG_LEVEL=INFO
DRY_RUN=false
```

- [ ] **Step 5: Verify project still imports cleanly**

```bash
python -c "from src.utils.logger import setup_logging; from src.utils.rate_limiter import RateLimiter; from src.notion_client_wrapper import NotionClientWrapper; print('OK')"
```

Expected: `OK` — the kept infrastructure modules import without errors.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: remove deterministic extraction pipeline, update deps for AI-driven approach"
```

---

### Task 2: Rewrite Config Module

**Files:**
- Modify: `src/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_config.py`:

```python
"""Tests for the new AI-driven config loader."""

import os
import pytest
from unittest.mock import patch

from src.config import SyncConfig, load_config


class TestSyncConfig:
    def test_valid_config_all_required_fields(self):
        config = SyncConfig(
            notion_api_token="secret_abc",
            openai_api_key="sk-abc",
            meeting_notes_db_id="db-meetings",
            team_tracker_db_id="db-tracker",
            playbook_page_id="page-playbook",
        )
        assert config.notion_api_token == "secret_abc"
        assert config.openai_api_key == "sk-abc"
        assert config.openai_model == "gpt-4.1"
        assert config.buffer_hours == 2
        assert config.log_level == "INFO"
        assert config.dry_run is False

    def test_missing_required_field_raises(self):
        with pytest.raises(Exception):
            SyncConfig(
                notion_api_token="secret_abc",
                # missing openai_api_key
                meeting_notes_db_id="db-meetings",
                team_tracker_db_id="db-tracker",
                playbook_page_id="page-playbook",
            )

    def test_custom_optional_fields(self):
        config = SyncConfig(
            notion_api_token="secret_abc",
            openai_api_key="sk-abc",
            meeting_notes_db_id="db-meetings",
            team_tracker_db_id="db-tracker",
            playbook_page_id="page-playbook",
            openai_model="gpt-4o",
            buffer_hours=4,
            dry_run=True,
        )
        assert config.openai_model == "gpt-4o"
        assert config.buffer_hours == 4
        assert config.dry_run is True


class TestLoadConfig:
    def test_load_config_from_env(self):
        env = {
            "NOTION_API_TOKEN": "secret_test",
            "OPENAI_API_KEY": "sk-test",
            "MEETING_NOTES_DB_ID": "db-meet",
            "TEAM_TRACKER_DB_ID": "db-track",
            "PLAYBOOK_PAGE_ID": "page-pb",
            "BUFFER_HOURS": "3",
            "DRY_RUN": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            config = load_config()
        assert config.notion_api_token == "secret_test"
        assert config.openai_api_key == "sk-test"
        assert config.buffer_hours == 3
        assert config.dry_run is True

    def test_load_config_missing_required_raises(self):
        env = {"NOTION_API_TOKEN": "secret_test"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(KeyError):
                load_config()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_config.py -v
```

Expected: FAIL — `SyncConfig` still has the old fields.

- [ ] **Step 3: Rewrite src/config.py**

Replace the full contents of `src/config.py` with:

```python
"""Configuration for the Nzyme AI-driven Task Tracker.

Loads settings from environment variables (with .env file support).
Validates required fields using Pydantic.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field


class SyncConfig(BaseModel):
    """Validated runtime configuration."""

    notion_api_token: str = Field(..., description="Notion integration token.")
    openai_api_key: str = Field(..., description="OpenAI API key.")
    openai_model: str = Field("gpt-4.1", description="OpenAI model name.")
    meeting_notes_db_id: str = Field(..., description="Meeting Notes database ID.")
    team_tracker_db_id: str = Field(..., description="Team Task Tracker database ID.")
    playbook_page_id: str = Field(..., description="Notion page ID for the playbook.")
    buffer_hours: int = Field(2, description="Hours to wait after meeting date before processing.")
    log_level: str = Field("INFO", description="Logging level.")
    dry_run: bool = Field(False, description="Log tasks but don't write to Notion.")


def load_config() -> SyncConfig:
    """Build a SyncConfig from environment variables."""
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    return SyncConfig(
        notion_api_token=os.environ["NOTION_API_TOKEN"],
        openai_api_key=os.environ["OPENAI_API_KEY"],
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1"),
        meeting_notes_db_id=os.environ["MEETING_NOTES_DB_ID"],
        team_tracker_db_id=os.environ["TEAM_TRACKER_DB_ID"],
        playbook_page_id=os.environ["PLAYBOOK_PAGE_ID"],
        buffer_hours=int(os.getenv("BUFFER_HOURS", "2")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        dry_run=os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes"),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_config.py -v
```

Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/config.py tests/test_config.py
git commit -m "feat: rewrite config for AI-driven architecture (OpenAI + playbook)"
```

---

### Task 3: Playbook Loader

**Files:**
- Create: `src/playbook_loader.py`
- Create: `tests/test_playbook_loader.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_playbook_loader.py`:

```python
"""Tests for the playbook loader — converts Notion blocks to plain text."""

from unittest.mock import MagicMock
from src.playbook_loader import PlaybookLoader


def _make_text_block(text: str, block_type: str = "paragraph") -> dict:
    return {
        "type": block_type,
        block_type: {
            "rich_text": [{"plain_text": text}],
        },
        "has_children": False,
    }


def _make_heading_block(text: str, level: int = 1) -> dict:
    heading_type = f"heading_{level}"
    return {
        "type": heading_type,
        heading_type: {
            "rich_text": [{"plain_text": text}],
        },
        "has_children": False,
    }


def _make_bulleted_list_block(text: str) -> dict:
    return {
        "type": "bulleted_list_item",
        "bulleted_list_item": {
            "rich_text": [{"plain_text": text}],
        },
        "has_children": False,
    }


class TestBlocksToText:
    def test_paragraph(self):
        loader = PlaybookLoader(MagicMock(), "page-id")
        blocks = [_make_text_block("Hello world")]
        result = loader.blocks_to_text(blocks)
        assert result == "Hello world"

    def test_heading_and_paragraph(self):
        loader = PlaybookLoader(MagicMock(), "page-id")
        blocks = [
            _make_heading_block("Title", level=1),
            _make_text_block("Body text"),
        ]
        result = loader.blocks_to_text(blocks)
        assert result == "# Title\nBody text"

    def test_heading_levels(self):
        loader = PlaybookLoader(MagicMock(), "page-id")
        blocks = [
            _make_heading_block("H1", level=1),
            _make_heading_block("H2", level=2),
            _make_heading_block("H3", level=3),
        ]
        result = loader.blocks_to_text(blocks)
        assert result == "# H1\n## H2\n### H3"

    def test_bulleted_list(self):
        loader = PlaybookLoader(MagicMock(), "page-id")
        blocks = [
            _make_bulleted_list_block("First item"),
            _make_bulleted_list_block("Second item"),
        ]
        result = loader.blocks_to_text(blocks)
        assert result == "- First item\n- Second item"

    def test_empty_blocks(self):
        loader = PlaybookLoader(MagicMock(), "page-id")
        assert loader.blocks_to_text([]) == ""

    def test_mixed_content(self):
        loader = PlaybookLoader(MagicMock(), "page-id")
        blocks = [
            _make_heading_block("Rules", level=2),
            _make_bulleted_list_block("Rule one"),
            _make_bulleted_list_block("Rule two"),
            _make_text_block("Additional notes"),
        ]
        result = loader.blocks_to_text(blocks)
        assert result == "## Rules\n- Rule one\n- Rule two\nAdditional notes"


class TestLoad:
    def test_load_fetches_blocks_and_converts(self):
        client = MagicMock()
        client.get_block_children.return_value = [
            _make_heading_block("Playbook", level=1),
            _make_text_block("Extract tasks from meetings."),
        ]
        loader = PlaybookLoader(client, "page-123")
        result = loader.load()
        assert result == "# Playbook\nExtract tasks from meetings."
        client.get_block_children.assert_called_once_with("page-123")

    def test_load_caches_result(self):
        client = MagicMock()
        client.get_block_children.return_value = [_make_text_block("Cached")]
        loader = PlaybookLoader(client, "page-123")
        result1 = loader.load()
        result2 = loader.load()
        assert result1 == result2 == "Cached"
        assert client.get_block_children.call_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_playbook_loader.py -v
```

Expected: FAIL — `src.playbook_loader` does not exist.

- [ ] **Step 3: Implement src/playbook_loader.py**

```python
"""Fetches the playbook Notion page and converts blocks to plain text.

The playbook is a Notion page containing natural-language rules that the
AI model follows when extracting tasks. This module fetches the page's
blocks and converts them to a readable text format for the prompt.
"""
from __future__ import annotations

import logging

from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)

# Block types that contain rich_text
_RICH_TEXT_BLOCK_TYPES = {
    "paragraph",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
    "toggle",
    "quote",
    "callout",
}

_HEADING_TYPES = {"heading_1": "#", "heading_2": "##", "heading_3": "###"}
_LIST_TYPES = {"bulleted_list_item": "- ", "numbered_list_item": "1. "}


class PlaybookLoader:
    """Fetches and converts a Notion playbook page to plain text.

    Parameters
    ----------
    client:
        Authenticated NotionClientWrapper.
    page_id:
        Notion page ID of the playbook.
    """

    def __init__(self, client: NotionClientWrapper, page_id: str) -> None:
        self._client = client
        self._page_id = page_id
        self._cached: str | None = None

    def blocks_to_text(self, blocks: list[dict]) -> str:
        """Convert a list of Notion blocks to plain text."""
        lines: list[str] = []
        for block in blocks:
            block_type = block.get("type", "")
            text = self._extract_rich_text(block.get(block_type, {}))

            if block_type in _HEADING_TYPES:
                prefix = _HEADING_TYPES[block_type]
                lines.append(f"{prefix} {text}")
            elif block_type in _LIST_TYPES:
                prefix = _LIST_TYPES[block_type]
                lines.append(f"{prefix}{text}")
            elif text:
                lines.append(text)

        return "\n".join(lines)

    @staticmethod
    def _extract_rich_text(block_content: dict) -> str:
        """Extract plain text from a block's rich_text array."""
        rich_text = block_content.get("rich_text", [])
        return "".join(part.get("plain_text", "") for part in rich_text)

    def load(self) -> str:
        """Fetch the playbook page and return its content as text.

        Result is cached for the lifetime of this loader instance
        (one sync cycle).
        """
        if self._cached is not None:
            return self._cached

        logger.info("Fetching playbook page %s", self._page_id)
        blocks = self._client.get_block_children(self._page_id)
        self._cached = self.blocks_to_text(blocks)
        logger.debug("Playbook loaded (%d chars)", len(self._cached))
        return self._cached
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_playbook_loader.py -v
```

Expected: All 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/playbook_loader.py tests/test_playbook_loader.py
git commit -m "feat: add playbook loader — fetches Notion page and converts to text"
```

---

### Task 4: Hierarchy Loader

**Files:**
- Create: `src/hierarchy_loader.py`
- Create: `tests/test_hierarchy_loader.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_hierarchy_loader.py`:

```python
"""Tests for the hierarchy loader — builds tracker tree from Notion pages."""

import json
from unittest.mock import MagicMock
from src.hierarchy_loader import HierarchyLoader


def _make_page(page_id: str, title: str, category: str | None = None, parent_id: str | None = None) -> dict:
    """Build a minimal Notion page dict for testing."""
    properties: dict = {
        "Task": {
            "type": "title",
            "title": [{"plain_text": title}],
        },
        "Status": {
            "type": "status",
            "status": {"name": "Not Started"},
        },
    }
    if category:
        properties["Category"] = {"type": "select", "select": {"name": category}}
    if parent_id:
        properties["Parent item"] = {"type": "relation", "relation": [{"id": parent_id}]}
    else:
        properties["Parent item"] = {"type": "relation", "relation": []}
    return {"id": page_id, "properties": properties}


class TestBuildHierarchy:
    def test_single_root_no_children(self):
        loader = HierarchyLoader(MagicMock(), "db-id")
        pages = [_make_page("p1", "Dealflow", category="Dealflow")]
        tree = loader.build_tree(pages)
        assert len(tree) == 1
        assert tree[0]["id"] == "p1"
        assert tree[0]["title"] == "Dealflow"
        assert tree[0]["children"] == []

    def test_root_with_children(self):
        loader = HierarchyLoader(MagicMock(), "db-id")
        pages = [
            _make_page("cat1", "Dealflow", category="Dealflow"),
            _make_page("ent1", "Deal X", parent_id="cat1"),
            _make_page("ent2", "Deal Y", parent_id="cat1"),
        ]
        tree = loader.build_tree(pages)
        assert len(tree) == 1
        root = tree[0]
        assert root["title"] == "Dealflow"
        assert len(root["children"]) == 2
        child_titles = {c["title"] for c in root["children"]}
        assert child_titles == {"Deal X", "Deal Y"}

    def test_multiple_roots(self):
        loader = HierarchyLoader(MagicMock(), "db-id")
        pages = [
            _make_page("cat1", "Dealflow", category="Dealflow"),
            _make_page("cat2", "Internal", category="Internal"),
            _make_page("ent1", "Deal X", parent_id="cat1"),
        ]
        tree = loader.build_tree(pages)
        assert len(tree) == 2

    def test_nested_children(self):
        loader = HierarchyLoader(MagicMock(), "db-id")
        pages = [
            _make_page("cat1", "Dealflow", category="Dealflow"),
            _make_page("ent1", "Deal X", parent_id="cat1"),
            _make_page("task1", "Send term sheet", parent_id="ent1"),
        ]
        tree = loader.build_tree(pages)
        root = tree[0]
        entity = root["children"][0]
        assert entity["title"] == "Deal X"
        assert len(entity["children"]) == 1
        assert entity["children"][0]["title"] == "Send term sheet"

    def test_empty_pages(self):
        loader = HierarchyLoader(MagicMock(), "db-id")
        assert loader.build_tree([]) == []

    def test_to_json_is_valid(self):
        loader = HierarchyLoader(MagicMock(), "db-id")
        pages = [
            _make_page("cat1", "Dealflow", category="Dealflow"),
            _make_page("ent1", "Deal X", parent_id="cat1"),
        ]
        tree = loader.build_tree(pages)
        json_str = json.dumps(tree)
        parsed = json.loads(json_str)
        assert parsed[0]["id"] == "cat1"


class TestLoad:
    def test_load_queries_db_and_builds_tree(self):
        client = MagicMock()
        pages = [
            _make_page("cat1", "Dealflow", category="Dealflow"),
            _make_page("ent1", "Deal X", parent_id="cat1"),
        ]
        client.query_database.return_value = {"results": pages}
        loader = HierarchyLoader(client, "db-123")
        tree = loader.load()
        assert len(tree) == 1
        assert tree[0]["children"][0]["title"] == "Deal X"
        client.query_database.assert_called_once()

    def test_load_caches_result(self):
        client = MagicMock()
        client.query_database.return_value = {"results": [_make_page("p1", "Root")]}
        loader = HierarchyLoader(client, "db-123")
        result1 = loader.load()
        result2 = loader.load()
        assert result1 == result2
        assert client.query_database.call_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_hierarchy_loader.py -v
```

Expected: FAIL — `src.hierarchy_loader` does not exist.

- [ ] **Step 3: Implement src/hierarchy_loader.py**

```python
"""Builds a hierarchy snapshot of the Team Task Tracker.

Queries all non-Done pages from the tracker database and organizes them
into a parent-child tree using the Parent item / Sub-item relations.
The resulting JSON is passed to the AI model as context.
"""
from __future__ import annotations

import logging
from typing import Any

from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)


class HierarchyLoader:
    """Queries the Team Task Tracker and builds a hierarchy tree.

    Parameters
    ----------
    client:
        Authenticated NotionClientWrapper.
    database_id:
        UUID of the Team Task Tracker database.
    """

    def __init__(self, client: NotionClientWrapper, database_id: str) -> None:
        self._client = client
        self._db_id = database_id
        self._cached: list[dict[str, Any]] | None = None

    def build_tree(self, pages: list[dict]) -> list[dict[str, Any]]:
        """Build a parent-child tree from a flat list of Notion pages.

        Returns a list of root nodes, each with a recursive ``children`` list.
        """
        nodes: dict[str, dict[str, Any]] = {}
        child_of: dict[str, str] = {}

        for page in pages:
            page_id = page.get("id", "")
            title = self._get_title(page)
            category = self._get_category(page)
            parent_rel = (
                page.get("properties", {})
                .get("Parent item", {})
                .get("relation", [])
            )
            node: dict[str, Any] = {
                "id": page_id,
                "title": title,
                "children": [],
            }
            if category:
                node["category"] = category
            nodes[page_id] = node

            if parent_rel:
                child_of[page_id] = parent_rel[0].get("id", "")

        # Attach children to parents
        for child_id, parent_id in child_of.items():
            if parent_id in nodes:
                nodes[parent_id]["children"].append(nodes[child_id])

        # Roots are nodes that are not children of anything
        root_ids = set(nodes.keys()) - set(child_of.keys())
        return [nodes[rid] for rid in root_ids if rid in nodes]

    @staticmethod
    def _get_title(page: dict) -> str:
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title":
                parts = prop.get("title", [])
                return "".join(p.get("plain_text", "") for p in parts)
        return ""

    @staticmethod
    def _get_category(page: dict) -> str | None:
        cat_prop = page.get("properties", {}).get("Category", {})
        select = cat_prop.get("select")
        if select:
            return select.get("name")
        return None

    def load(self) -> list[dict[str, Any]]:
        """Fetch the tracker hierarchy and return the tree.

        Filters out Done tasks to reduce noise. Result is cached for
        the lifetime of this loader instance (one sync cycle).
        """
        if self._cached is not None:
            return self._cached

        logger.info("Fetching Team Task Tracker hierarchy from %s", self._db_id)
        response = self._client.query_database(
            database_id=self._db_id,
            filter={
                "property": "Status",
                "status": {"does_not_equal": "Done"},
            },
        )
        pages = response.get("results", [])
        self._cached = self.build_tree(pages)
        logger.info(
            "Hierarchy loaded: %d root nodes, %d total pages",
            len(self._cached), len(pages),
        )
        return self._cached
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_hierarchy_loader.py -v
```

Expected: All 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hierarchy_loader.py tests/test_hierarchy_loader.py
git commit -m "feat: add hierarchy loader — builds tracker tree for AI context"
```

---

### Task 5: Adapt Meeting Source

**Files:**
- Modify: `src/sources/single_source.py`
- Create: `tests/test_single_source.py` (rewrite — old one deleted)

- [ ] **Step 1: Write the failing test**

Create `tests/test_single_source.py`:

```python
"""Tests for the adapted single meeting notes source."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, call
from src.sources.single_source import SingleSource


def _make_text_block(text: str) -> dict:
    return {
        "type": "paragraph",
        "paragraph": {"rich_text": [{"plain_text": text}]},
        "has_children": False,
    }


def _make_heading_block(text: str, level: int = 2) -> dict:
    heading_type = f"heading_{level}"
    return {
        "type": heading_type,
        heading_type: {"rich_text": [{"plain_text": text}]},
        "has_children": False,
    }


def _make_todo_block(text: str, checked: bool = False) -> dict:
    return {
        "type": "to_do",
        "to_do": {
            "rich_text": [{"plain_text": text}],
            "checked": checked,
        },
        "has_children": False,
    }


class TestGetPages:
    def test_filters_by_date_buffer_and_processed(self):
        client = MagicMock()
        client.query_database.return_value = {"results": []}
        source = SingleSource(client, "db-123", buffer_hours=2)
        now = datetime(2026, 3, 27, 14, 0, 0, tzinfo=timezone.utc)
        source.get_pages(now=now)

        call_args = client.query_database.call_args
        db_filter = call_args.kwargs.get("filter") or call_args[1].get("filter")
        # Should have an "and" filter with both conditions
        assert "and" in db_filter
        conditions = db_filter["and"]
        assert len(conditions) == 2

    def test_returns_results(self):
        client = MagicMock()
        pages = [{"id": "page-1"}, {"id": "page-2"}]
        client.query_database.return_value = {"results": pages}
        source = SingleSource(client, "db-123", buffer_hours=2)
        result = source.get_pages()
        assert len(result) == 2


class TestGetPageContent:
    def test_converts_blocks_to_text(self):
        client = MagicMock()
        client.get_block_children.return_value = [
            _make_heading_block("Discussion"),
            _make_text_block("We talked about the deal."),
            _make_todo_block("@Santiago send the term sheet by Friday"),
        ]
        source = SingleSource(client, "db-123")
        text = source.get_page_content("page-1")
        assert "Discussion" in text
        assert "We talked about the deal." in text
        assert "send the term sheet" in text

    def test_handles_nested_blocks(self):
        """Blocks with has_children=True should trigger recursive fetch."""
        parent_block = {
            "id": "block-parent",
            "type": "toggle",
            "toggle": {"rich_text": [{"plain_text": "Toggle header"}]},
            "has_children": True,
        }
        child_block = _make_text_block("Nested content")

        client = MagicMock()
        client.get_block_children.side_effect = [
            [parent_block],       # First call: page children
            [child_block],        # Second call: toggle children
        ]
        source = SingleSource(client, "db-123")
        text = source.get_page_content("page-1")
        assert "Toggle header" in text
        assert "Nested content" in text


class TestGetPageMetadata:
    def test_extracts_title_date_type_attendees(self):
        client = MagicMock()
        page = {
            "id": "page-1",
            "properties": {
                "Meeting": {
                    "type": "title",
                    "title": [{"plain_text": "Q1 Deal Review"}],
                },
                "Date": {
                    "type": "date",
                    "date": {"start": "2026-03-27"},
                },
                "Meeting type": {
                    "type": "select",
                    "select": {"name": "Deal review"},
                },
                "Attendees": {
                    "type": "people",
                    "people": [
                        {"id": "user-1", "name": "Santiago"},
                        {"id": "user-2", "name": "Ana"},
                    ],
                },
            },
        }
        source = SingleSource(client, "db-123")
        meta = source.get_page_metadata(page)
        assert meta["title"] == "Q1 Deal Review"
        assert meta["date"] == "2026-03-27"
        assert meta["meeting_type"] == "Deal review"
        assert len(meta["attendees"]) == 2
        assert meta["attendees"][0] == {"id": "user-1", "name": "Santiago"}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_single_source.py -v
```

Expected: FAIL — `SingleSource` constructor doesn't accept `buffer_hours`, missing `get_page_content`, missing `get_page_metadata`.

- [ ] **Step 3: Rewrite src/sources/single_source.py**

Replace the full contents:

```python
"""Single shared meeting-notes database source.

Queries one Notion database for unprocessed meetings, fetches their
content as plain text, and marks them processed after extraction.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)

# Block types with rich_text content
_HEADING_TYPES = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### "}
_LIST_TYPES = {"bulleted_list_item": "- ", "numbered_list_item": "1. "}
_RICH_TEXT_TYPES = {"paragraph", "to_do", "toggle", "quote", "callout"}


class SingleSource:
    """Fetches meeting-note pages from a single shared Notion database.

    Parameters
    ----------
    client:
        Authenticated NotionClientWrapper.
    database_id:
        UUID of the shared meeting-notes database.
    buffer_hours:
        Hours to wait after meeting date before considering it ready.
    """

    def __init__(
        self,
        client: NotionClientWrapper,
        database_id: str,
        buffer_hours: int = 2,
    ) -> None:
        self._client = client
        self._database_id = database_id
        self._buffer_hours = buffer_hours

    def get_pages(self, now: datetime | None = None) -> list[dict]:
        """Return unprocessed meeting pages whose date + buffer has passed."""
        if now is None:
            now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=self._buffer_hours)

        db_filter = {
            "and": [
                {"property": "Processed", "checkbox": {"equals": False}},
                {"property": "Date", "date": {"before": cutoff.isoformat()}},
            ]
        }
        response = self._client.query_database(
            database_id=self._database_id,
            filter=db_filter,
            sorts=[{"property": "Date", "direction": "descending"}],
        )
        pages = response.get("results", [])
        logger.info("Found %d unprocessed meetings (cutoff: %s)", len(pages), cutoff.isoformat())
        return pages

    def get_page_content(self, page_id: str) -> str:
        """Fetch all blocks from a page and convert to plain text."""
        blocks = self._client.get_block_children(page_id)
        return self._blocks_to_text(blocks)

    def _blocks_to_text(self, blocks: list[dict]) -> str:
        """Recursively convert Notion blocks to plain text."""
        lines: list[str] = []
        for block in blocks:
            block_type = block.get("type", "")
            block_content = block.get(block_type, {})
            text = self._extract_rich_text(block_content)

            if block_type in _HEADING_TYPES:
                lines.append(f"{_HEADING_TYPES[block_type]}{text}")
            elif block_type in _LIST_TYPES:
                lines.append(f"{_LIST_TYPES[block_type]}{text}")
            elif block_type == "to_do":
                checked = block_content.get("checked", False)
                marker = "[x]" if checked else "[ ]"
                lines.append(f"- {marker} {text}")
            elif text:
                lines.append(text)

            # Recurse into nested blocks
            if block.get("has_children"):
                child_blocks = self._client.get_block_children(block["id"])
                child_text = self._blocks_to_text(child_blocks)
                if child_text:
                    lines.append(child_text)

        return "\n".join(lines)

    @staticmethod
    def _extract_rich_text(block_content: dict) -> str:
        rich_text = block_content.get("rich_text", [])
        return "".join(part.get("plain_text", "") for part in rich_text)

    def get_page_metadata(self, page: dict) -> dict[str, Any]:
        """Extract metadata from a meeting page: title, date, type, attendees."""
        props = page.get("properties", {})

        # Title
        title = ""
        title_prop = props.get("Meeting", {})
        if title_prop.get("type") == "title":
            title = "".join(
                p.get("plain_text", "") for p in title_prop.get("title", [])
            )

        # Date
        date = ""
        date_prop = props.get("Date", {})
        date_val = date_prop.get("date")
        if date_val:
            date = date_val.get("start", "")

        # Meeting type
        meeting_type = ""
        type_prop = props.get("Meeting type", {})
        select_val = type_prop.get("select")
        if select_val:
            meeting_type = select_val.get("name", "")

        # Attendees
        attendees: list[dict[str, str]] = []
        att_prop = props.get("Attendees", {})
        for person in att_prop.get("people", []):
            attendees.append({
                "id": person.get("id", ""),
                "name": person.get("name", ""),
            })

        return {
            "title": title,
            "date": date,
            "meeting_type": meeting_type,
            "attendees": attendees,
        }

    def mark_page_processed(self, page_id: str) -> None:
        """Set Processed = true on a meeting-note page."""
        self._client.update_page(
            page_id=page_id,
            properties={"Processed": {"checkbox": True}},
        )
        logger.debug("Marked page %s as processed", page_id)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_single_source.py -v
```

Expected: All 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sources/single_source.py tests/test_single_source.py
git commit -m "feat: adapt meeting source — add date buffer, content fetching, metadata extraction"
```

---

### Task 6: AI Extractor

**Files:**
- Create: `src/ai_extractor.py`
- Create: `tests/test_ai_extractor.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_ai_extractor.py`:

```python
"""Tests for the AI extractor — builds OpenAI prompt and parses tool_calls."""

import json
from unittest.mock import MagicMock, patch
from src.ai_extractor import AIExtractor, TOOL_DEFINITION


class TestBuildMessages:
    def test_system_prompt_includes_playbook_and_hierarchy(self):
        extractor = AIExtractor(api_key="sk-test", model="gpt-4.1")
        messages = extractor.build_messages(
            playbook_text="## Rules\n- Extract tasks",
            hierarchy_json='[{"id":"p1","title":"Dealflow","children":[]}]',
            attendees=[{"id": "u1", "name": "Santiago"}],
            meeting_title="Q1 Review",
            meeting_date="2026-03-27",
            meeting_type="Deal review",
            meeting_content="We discussed the deal. @Santiago to send term sheet by Friday.",
        )
        system_msg = messages[0]
        assert system_msg["role"] == "system"
        assert "## Rules" in system_msg["content"]
        assert "Extract tasks" in system_msg["content"]
        assert "Dealflow" in system_msg["content"]
        assert "Santiago" in system_msg["content"]

    def test_user_message_includes_meeting_content(self):
        extractor = AIExtractor(api_key="sk-test", model="gpt-4.1")
        messages = extractor.build_messages(
            playbook_text="rules",
            hierarchy_json="[]",
            attendees=[],
            meeting_title="Standup",
            meeting_date="2026-03-27",
            meeting_type="Standup",
            meeting_content="@Ana to update the dashboard.",
        )
        user_msg = messages[1]
        assert user_msg["role"] == "user"
        assert "update the dashboard" in user_msg["content"]
        assert "Standup" in user_msg["content"]


class TestParseToolCalls:
    def test_parses_single_tool_call(self):
        extractor = AIExtractor(api_key="sk-test", model="gpt-4.1")
        mock_tool_call = MagicMock()
        mock_tool_call.function.name = "create_task"
        mock_tool_call.function.arguments = json.dumps({
            "title": "Send term sheet",
            "assignee_id": "user-1",
            "priority": "High",
            "category": "Dealflow",
            "due_date": "2026-04-01",
            "parent_task_id": "page-123",
        })
        tasks = extractor.parse_tool_calls([mock_tool_call])
        assert len(tasks) == 1
        assert tasks[0]["title"] == "Send term sheet"
        assert tasks[0]["assignee_id"] == "user-1"
        assert tasks[0]["priority"] == "High"
        assert tasks[0]["parent_task_id"] == "page-123"

    def test_parses_multiple_tool_calls(self):
        extractor = AIExtractor(api_key="sk-test", model="gpt-4.1")
        calls = []
        for i, title in enumerate(["Task A", "Task B"]):
            tc = MagicMock()
            tc.function.name = "create_task"
            tc.function.arguments = json.dumps({
                "title": title,
                "assignee_id": f"user-{i}",
                "priority": "Medium",
                "category": "Internal",
            })
            calls.append(tc)
        tasks = extractor.parse_tool_calls(calls)
        assert len(tasks) == 2
        assert tasks[0]["title"] == "Task A"
        assert tasks[1]["title"] == "Task B"

    def test_skips_non_create_task_calls(self):
        extractor = AIExtractor(api_key="sk-test", model="gpt-4.1")
        tc = MagicMock()
        tc.function.name = "unknown_tool"
        tc.function.arguments = "{}"
        tasks = extractor.parse_tool_calls([tc])
        assert tasks == []

    def test_handles_empty_tool_calls(self):
        extractor = AIExtractor(api_key="sk-test", model="gpt-4.1")
        assert extractor.parse_tool_calls([]) == []
        assert extractor.parse_tool_calls(None) == []

    def test_handles_optional_fields(self):
        extractor = AIExtractor(api_key="sk-test", model="gpt-4.1")
        tc = MagicMock()
        tc.function.name = "create_task"
        tc.function.arguments = json.dumps({
            "title": "Simple task",
            "assignee_id": "user-1",
            "priority": "Low",
            "category": "Other",
        })
        tasks = extractor.parse_tool_calls([tc])
        assert tasks[0].get("due_date") is None
        assert tasks[0].get("parent_task_id") is None
        assert tasks[0].get("status", "Not Started") == "Not Started"


class TestToolDefinition:
    def test_tool_definition_has_required_fields(self):
        params = TOOL_DEFINITION["function"]["parameters"]
        assert "title" in params["properties"]
        assert "assignee_id" in params["properties"]
        assert "priority" in params["properties"]
        assert "category" in params["properties"]
        required = params["required"]
        assert "title" in required
        assert "assignee_id" in required


class TestExtract:
    @patch("src.ai_extractor.OpenAI")
    def test_extract_calls_openai_and_returns_tasks(self, MockOpenAI):
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client

        # Mock response
        mock_tool_call = MagicMock()
        mock_tool_call.function.name = "create_task"
        mock_tool_call.function.arguments = json.dumps({
            "title": "Follow up on deal",
            "assignee_id": "user-1",
            "priority": "High",
            "category": "Dealflow",
        })
        mock_message = MagicMock()
        mock_message.tool_calls = [mock_tool_call]
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_response

        extractor = AIExtractor(api_key="sk-test", model="gpt-4.1")
        tasks = extractor.extract(
            playbook_text="Extract tasks",
            hierarchy_json="[]",
            attendees=[],
            meeting_title="Review",
            meeting_date="2026-03-27",
            meeting_type="Deal review",
            meeting_content="@Santiago follow up on deal.",
        )
        assert len(tasks) == 1
        assert tasks[0]["title"] == "Follow up on deal"
        mock_client.chat.completions.create.assert_called_once()

    @patch("src.ai_extractor.OpenAI")
    def test_extract_returns_empty_when_no_tool_calls(self, MockOpenAI):
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client

        mock_message = MagicMock()
        mock_message.tool_calls = None
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_response

        extractor = AIExtractor(api_key="sk-test", model="gpt-4.1")
        tasks = extractor.extract(
            playbook_text="rules",
            hierarchy_json="[]",
            attendees=[],
            meeting_title="Standup",
            meeting_date="2026-03-27",
            meeting_type="Standup",
            meeting_content="No action items today.",
        )
        assert tasks == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_ai_extractor.py -v
```

Expected: FAIL — `src.ai_extractor` does not exist.

- [ ] **Step 3: Implement src/ai_extractor.py**

```python
"""AI-powered task extraction using OpenAI function calling.

Sends meeting content, playbook rules, hierarchy context, and attendee
info to OpenAI. The model returns create_task() tool calls which are
parsed into validated task dicts.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from openai import OpenAI

logger = logging.getLogger(__name__)

TOOL_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "create_task",
        "description": "Create a task in the Team Task Tracker.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Task title — concise and actionable.",
                },
                "assignee_id": {
                    "type": "string",
                    "description": "Notion user ID of the assignee.",
                },
                "due_date": {
                    "type": "string",
                    "description": "ISO date (YYYY-MM-DD) or null if none.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["High", "Medium", "Low"],
                },
                "category": {
                    "type": "string",
                    "enum": ["Dealflow", "Origination", "Portfolio", "Internal", "Other"],
                },
                "parent_task_id": {
                    "type": "string",
                    "description": "Page ID of the parent task from the hierarchy, or null for top-level.",
                },
                "status": {
                    "type": "string",
                    "enum": ["Not Started", "In Progress", "Done"],
                    "default": "Not Started",
                },
            },
            "required": ["title", "assignee_id", "priority", "category"],
        },
    },
}


class AIExtractor:
    """Extracts tasks from meeting content using OpenAI function calling.

    Parameters
    ----------
    api_key:
        OpenAI API key.
    model:
        Model name (e.g. "gpt-4.1").
    """

    def __init__(self, api_key: str, model: str = "gpt-4.1") -> None:
        self._client = OpenAI(api_key=api_key)
        self._model = model

    def build_messages(
        self,
        playbook_text: str,
        hierarchy_json: str,
        attendees: list[dict[str, str]],
        meeting_title: str,
        meeting_date: str,
        meeting_type: str,
        meeting_content: str,
    ) -> list[dict[str, str]]:
        """Build the system and user messages for the OpenAI call."""
        attendees_text = "\n".join(
            f"- {a['name']} (user ID: {a['id']})" for a in attendees
        ) or "No attendee information available."

        system_prompt = (
            "You are a task extraction assistant for a PE/VC fund team.\n"
            "Your job is to extract action items from meeting notes and create tasks "
            "by calling the create_task function for each one.\n"
            "If there are no action items, do not call any functions.\n\n"
            f"## Rules (Playbook)\n{playbook_text}\n\n"
            "## Team Task Tracker Schema\n"
            "- Task (title): string — concise, actionable description\n"
            '- Status: "Not Started" | "In Progress" | "Done"\n'
            "- Assignee: Notion user ID from the attendees list\n"
            "- Due Date: ISO date (YYYY-MM-DD) or omit if none\n"
            '- Priority: "High" | "Medium" | "Low"\n'
            '- Category: "Dealflow" | "Origination" | "Portfolio" | "Internal" | "Other"\n'
            "- Parent item: page ID from the hierarchy below (optional)\n\n"
            f"## Existing Task Hierarchy\n{hierarchy_json}\n\n"
            f"## Attendees in this meeting\n{attendees_text}"
        )

        user_message = (
            f"Extract action items from this meeting:\n\n"
            f"Meeting: {meeting_title}\n"
            f"Date: {meeting_date}\n"
            f"Type: {meeting_type}\n\n"
            f"{meeting_content}"
        )

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

    def parse_tool_calls(self, tool_calls: list | None) -> list[dict[str, Any]]:
        """Parse create_task tool calls into validated task dicts."""
        if not tool_calls:
            return []

        tasks: list[dict[str, Any]] = []
        for tc in tool_calls:
            if tc.function.name != "create_task":
                logger.warning("Unexpected tool call: %s", tc.function.name)
                continue
            try:
                args = json.loads(tc.function.arguments)
                task = {
                    "title": args["title"],
                    "assignee_id": args["assignee_id"],
                    "priority": args["priority"],
                    "category": args["category"],
                    "due_date": args.get("due_date"),
                    "parent_task_id": args.get("parent_task_id"),
                    "status": args.get("status", "Not Started"),
                }
                tasks.append(task)
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Failed to parse tool call: %s", e)
                continue

        return tasks

    def extract(
        self,
        playbook_text: str,
        hierarchy_json: str,
        attendees: list[dict[str, str]],
        meeting_title: str,
        meeting_date: str,
        meeting_type: str,
        meeting_content: str,
    ) -> list[dict[str, Any]]:
        """Run the full extraction: build prompt, call OpenAI, parse results."""
        messages = self.build_messages(
            playbook_text=playbook_text,
            hierarchy_json=hierarchy_json,
            attendees=attendees,
            meeting_title=meeting_title,
            meeting_date=meeting_date,
            meeting_type=meeting_type,
            meeting_content=meeting_content,
        )

        logger.info("Calling OpenAI (%s) for meeting: %s", self._model, meeting_title)
        response = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=[TOOL_DEFINITION],
            tool_choice="auto",
        )

        tool_calls = response.choices[0].message.tool_calls
        tasks = self.parse_tool_calls(tool_calls)
        logger.info("Extracted %d tasks from meeting: %s", len(tasks), meeting_title)
        return tasks
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_ai_extractor.py -v
```

Expected: All 10 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ai_extractor.py tests/test_ai_extractor.py
git commit -m "feat: add AI extractor — OpenAI function calling for task extraction"
```

---

### Task 7: Adapt Team Writer

**Files:**
- Modify: `src/tracker/team_writer.py`
- Create: `tests/test_team_writer.py` (rewrite — old one deleted)

- [ ] **Step 1: Write the failing test**

Create `tests/test_team_writer.py`:

```python
"""Tests for the adapted team writer — accepts task dicts from AI extractor."""

from unittest.mock import MagicMock
from src.tracker.team_writer import TeamWriter


class TestBuildPageProperties:
    def test_full_task_with_all_fields(self):
        writer = TeamWriter(MagicMock(), "db-123")
        task = {
            "title": "Send term sheet",
            "assignee_id": "user-1",
            "priority": "High",
            "category": "Dealflow",
            "due_date": "2026-04-01",
            "parent_task_id": "parent-page-1",
            "status": "Not Started",
        }
        props = writer.build_page_properties(task)
        assert props["Task"]["title"][0]["text"]["content"] == "Send term sheet"
        assert props["Status"]["status"]["name"] == "Not Started"
        assert props["Priority"]["select"]["name"] == "High"
        assert props["Category"]["select"]["name"] == "Dealflow"
        assert props["Assignee (edit access)"]["people"][0]["id"] == "user-1"
        assert props["Due Date"]["date"]["start"] == "2026-04-01"
        assert props["Parent item"]["relation"][0]["id"] == "parent-page-1"

    def test_task_without_optional_fields(self):
        writer = TeamWriter(MagicMock(), "db-123")
        task = {
            "title": "Quick task",
            "assignee_id": "user-1",
            "priority": "Low",
            "category": "Internal",
            "due_date": None,
            "parent_task_id": None,
            "status": "Not Started",
        }
        props = writer.build_page_properties(task)
        assert "Due Date" not in props
        assert "Parent item" not in props

    def test_title_truncated_to_2000_chars(self):
        writer = TeamWriter(MagicMock(), "db-123")
        task = {
            "title": "A" * 3000,
            "assignee_id": "user-1",
            "priority": "Medium",
            "category": "Other",
        }
        props = writer.build_page_properties(task)
        assert len(props["Task"]["title"][0]["text"]["content"]) == 2000


class TestWriteTask:
    def test_creates_page_with_correct_properties(self):
        client = MagicMock()
        client.create_page.return_value = {"id": "new-page-1"}
        writer = TeamWriter(client, "db-tracker")
        task = {
            "title": "Follow up",
            "assignee_id": "user-1",
            "priority": "Medium",
            "category": "Dealflow",
            "status": "Not Started",
        }
        result = writer.write_task(task)
        assert result == {"id": "new-page-1"}
        client.create_page.assert_called_once_with("db-tracker", writer.build_page_properties(task))

    def test_dry_run_does_not_call_api(self):
        client = MagicMock()
        writer = TeamWriter(client, "db-tracker", dry_run=True)
        task = {
            "title": "Should not write",
            "assignee_id": "user-1",
            "priority": "Low",
            "category": "Internal",
        }
        result = writer.write_task(task)
        assert result is None
        client.create_page.assert_not_called()


class TestWriteBatch:
    def test_writes_multiple_tasks(self):
        client = MagicMock()
        client.create_page.return_value = {"id": "new-page"}
        writer = TeamWriter(client, "db-tracker")
        tasks = [
            {"title": "Task A", "assignee_id": "u1", "priority": "High", "category": "Dealflow"},
            {"title": "Task B", "assignee_id": "u2", "priority": "Low", "category": "Internal"},
        ]
        results = writer.write_batch(tasks)
        assert len(results) == 2
        assert client.create_page.call_count == 2

    def test_continues_on_individual_failure(self):
        client = MagicMock()
        client.create_page.side_effect = [Exception("API error"), {"id": "page-2"}]
        writer = TeamWriter(client, "db-tracker")
        tasks = [
            {"title": "Fails", "assignee_id": "u1", "priority": "High", "category": "Dealflow"},
            {"title": "Succeeds", "assignee_id": "u2", "priority": "Low", "category": "Internal"},
        ]
        results = writer.write_batch(tasks)
        assert len(results) == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_team_writer.py -v
```

Expected: FAIL — old `TeamTaskTrackerWriter` class doesn't match new `TeamWriter` interface.

- [ ] **Step 3: Rewrite src/tracker/team_writer.py**

Replace the full contents:

```python
"""Team Task Tracker writer — creates task pages from AI-extracted dicts.

Accepts validated task dicts (from AIExtractor) and creates pages in the
Team Task Tracker Notion database with the correct properties and
parent-child relations.
"""
from __future__ import annotations

import logging
from typing import Any

from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)


class TeamWriter:
    """Writes task dicts to the Team Task Tracker Notion database.

    Parameters
    ----------
    client:
        Authenticated NotionClientWrapper.
    database_id:
        UUID of the Team Task Tracker database.
    dry_run:
        If True, log tasks but don't write to Notion.
    """

    def __init__(
        self,
        client: NotionClientWrapper,
        database_id: str,
        dry_run: bool = False,
    ) -> None:
        self._client = client
        self._db_id = database_id
        self._dry_run = dry_run

    def build_page_properties(self, task: dict[str, Any]) -> dict[str, Any]:
        """Convert a task dict into Notion page properties."""
        properties: dict[str, Any] = {
            "Task": {
                "title": [{"text": {"content": task["title"][:2000]}}],
            },
            "Status": {
                "status": {"name": task.get("status", "Not Started")},
            },
            "Priority": {
                "select": {"name": task["priority"]},
            },
            "Category": {
                "select": {"name": task["category"]},
            },
        }

        if task.get("assignee_id"):
            properties["Assignee (edit access)"] = {
                "people": [{"id": task["assignee_id"]}],
            }

        if task.get("due_date"):
            properties["Due Date"] = {
                "date": {"start": task["due_date"]},
            }

        if task.get("parent_task_id"):
            properties["Parent item"] = {
                "relation": [{"id": task["parent_task_id"]}],
            }

        return properties

    def write_task(self, task: dict[str, Any]) -> dict[str, Any] | None:
        """Create a single task page in the Team Task Tracker.

        Returns the created page dict, or None if dry_run is True.
        """
        if self._dry_run:
            logger.info(
                "DRY RUN — would create: '%s' [%s] assigned to %s",
                task["title"][:80], task["priority"], task.get("assignee_id", "?"),
            )
            return None

        properties = self.build_page_properties(task)
        page = self._client.create_page(self._db_id, properties)
        logger.info("Created task: '%s' (page: %s)", task["title"][:80], page.get("id"))
        return page

    def write_batch(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Write multiple tasks. Failures on individual tasks don't abort the batch."""
        created: list[dict[str, Any]] = []
        for task in tasks:
            try:
                result = self.write_task(task)
                if result is not None:
                    created.append(result)
            except Exception:
                logger.exception("Failed to write task: '%s'", task.get("title", "?")[:80])
        return created
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_team_writer.py -v
```

Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tracker/team_writer.py tests/test_team_writer.py
git commit -m "feat: rewrite team writer — accepts AI-extracted task dicts"
```

---

### Task 8: Pipeline Orchestrator

**Files:**
- Create: `src/pipeline.py`
- Create: `tests/test_pipeline.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_pipeline.py`:

```python
"""Tests for the pipeline orchestrator."""

import json
from unittest.mock import MagicMock, patch, call
from src.pipeline import Pipeline
from src.config import SyncConfig


def _make_config(**overrides) -> SyncConfig:
    defaults = {
        "notion_api_token": "secret_test",
        "openai_api_key": "sk-test",
        "meeting_notes_db_id": "db-meetings",
        "team_tracker_db_id": "db-tracker",
        "playbook_page_id": "page-playbook",
        "buffer_hours": 2,
        "dry_run": False,
    }
    defaults.update(overrides)
    return SyncConfig(**defaults)


def _make_meeting_page(page_id: str, title: str) -> dict:
    return {
        "id": page_id,
        "properties": {
            "Meeting": {"type": "title", "title": [{"plain_text": title}]},
            "Date": {"type": "date", "date": {"start": "2026-03-27"}},
            "Meeting type": {"type": "select", "select": {"name": "Deal review"}},
            "Attendees": {"type": "people", "people": [
                {"id": "user-1", "name": "Santiago"},
            ]},
        },
    }


class TestRunSync:
    @patch("src.pipeline.AIExtractor")
    @patch("src.pipeline.TeamWriter")
    @patch("src.pipeline.HierarchyLoader")
    @patch("src.pipeline.PlaybookLoader")
    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline.NotionClientWrapper")
    @patch("src.pipeline.NotionClient")
    def test_processes_one_meeting_end_to_end(
        self, MockNotionClient, MockWrapper, MockSource,
        MockPlaybook, MockHierarchy, MockWriter, MockAIExtractor,
    ):
        config = _make_config()

        # Setup mocks
        mock_source = MockSource.return_value
        mock_source.get_pages.return_value = [_make_meeting_page("p1", "Q1 Review")]
        mock_source.get_page_content.return_value = "Meeting notes text"
        mock_source.get_page_metadata.return_value = {
            "title": "Q1 Review",
            "date": "2026-03-27",
            "meeting_type": "Deal review",
            "attendees": [{"id": "user-1", "name": "Santiago"}],
        }

        MockPlaybook.return_value.load.return_value = "Playbook rules"
        MockHierarchy.return_value.load.return_value = [{"id": "cat1", "title": "Dealflow", "children": []}]

        mock_extractor = MockAIExtractor.return_value
        mock_extractor.extract.return_value = [
            {"title": "Send term sheet", "assignee_id": "user-1", "priority": "High", "category": "Dealflow"},
        ]

        mock_writer = MockWriter.return_value
        mock_writer.write_batch.return_value = [{"id": "new-page-1"}]

        pipeline = Pipeline(config)
        pipeline.run_sync()

        # Verify extraction was called with correct args
        mock_extractor.extract.assert_called_once()
        extract_kwargs = mock_extractor.extract.call_args.kwargs
        assert extract_kwargs["meeting_title"] == "Q1 Review"
        assert extract_kwargs["meeting_content"] == "Meeting notes text"
        assert "Playbook rules" in extract_kwargs["playbook_text"]

        # Verify tasks were written
        mock_writer.write_batch.assert_called_once()
        written_tasks = mock_writer.write_batch.call_args[0][0]
        assert len(written_tasks) == 1
        assert written_tasks[0]["title"] == "Send term sheet"

        # Verify meeting was marked processed
        mock_source.mark_page_processed.assert_called_once_with("p1")

    @patch("src.pipeline.AIExtractor")
    @patch("src.pipeline.TeamWriter")
    @patch("src.pipeline.HierarchyLoader")
    @patch("src.pipeline.PlaybookLoader")
    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline.NotionClientWrapper")
    @patch("src.pipeline.NotionClient")
    def test_skips_meeting_on_extraction_error(
        self, MockNotionClient, MockWrapper, MockSource,
        MockPlaybook, MockHierarchy, MockWriter, MockAIExtractor,
    ):
        config = _make_config()

        mock_source = MockSource.return_value
        mock_source.get_pages.return_value = [_make_meeting_page("p1", "Bad Meeting")]
        mock_source.get_page_content.return_value = "content"
        mock_source.get_page_metadata.return_value = {
            "title": "Bad Meeting", "date": "2026-03-27",
            "meeting_type": "Standup", "attendees": [],
        }

        MockPlaybook.return_value.load.return_value = "rules"
        MockHierarchy.return_value.load.return_value = []

        mock_extractor = MockAIExtractor.return_value
        mock_extractor.extract.side_effect = Exception("OpenAI API error")

        pipeline = Pipeline(config)
        pipeline.run_sync()  # Should not raise

        # Meeting should NOT be marked processed (will retry next cycle)
        mock_source.mark_page_processed.assert_not_called()

    @patch("src.pipeline.AIExtractor")
    @patch("src.pipeline.TeamWriter")
    @patch("src.pipeline.HierarchyLoader")
    @patch("src.pipeline.PlaybookLoader")
    @patch("src.pipeline.SingleSource")
    @patch("src.pipeline.NotionClientWrapper")
    @patch("src.pipeline.NotionClient")
    def test_marks_processed_when_no_tasks_extracted(
        self, MockNotionClient, MockWrapper, MockSource,
        MockPlaybook, MockHierarchy, MockWriter, MockAIExtractor,
    ):
        config = _make_config()

        mock_source = MockSource.return_value
        mock_source.get_pages.return_value = [_make_meeting_page("p1", "FYI Meeting")]
        mock_source.get_page_content.return_value = "Just updates, no tasks"
        mock_source.get_page_metadata.return_value = {
            "title": "FYI Meeting", "date": "2026-03-27",
            "meeting_type": "Team sync", "attendees": [],
        }

        MockPlaybook.return_value.load.return_value = "rules"
        MockHierarchy.return_value.load.return_value = []

        mock_extractor = MockAIExtractor.return_value
        mock_extractor.extract.return_value = []  # No tasks

        pipeline = Pipeline(config)
        pipeline.run_sync()

        # Still marked processed (was scanned, just had no tasks)
        mock_source.mark_page_processed.assert_called_once_with("p1")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_pipeline.py -v
```

Expected: FAIL — `src.pipeline` does not exist.

- [ ] **Step 3: Implement src/pipeline.py**

```python
"""Pipeline orchestrator — runs one sync cycle.

Polls for unprocessed meetings, extracts tasks via OpenAI, writes them
to the Team Task Tracker, and marks meetings as processed.
"""
from __future__ import annotations

import json
import logging

from notion_client import Client as NotionClient

from src.config import SyncConfig
from src.notion_client_wrapper import NotionClientWrapper
from src.sources.single_source import SingleSource
from src.playbook_loader import PlaybookLoader
from src.hierarchy_loader import HierarchyLoader
from src.ai_extractor import AIExtractor
from src.tracker.team_writer import TeamWriter

logger = logging.getLogger(__name__)


class Pipeline:
    """Orchestrates one full sync cycle.

    Parameters
    ----------
    config:
        Validated SyncConfig instance.
    """

    def __init__(self, config: SyncConfig) -> None:
        self._config = config
        notion = NotionClient(auth=config.notion_api_token)
        self._client = NotionClientWrapper(notion)
        self._source = SingleSource(
            self._client, config.meeting_notes_db_id, config.buffer_hours,
        )
        self._playbook = PlaybookLoader(self._client, config.playbook_page_id)
        self._hierarchy = HierarchyLoader(self._client, config.team_tracker_db_id)
        self._extractor = AIExtractor(config.openai_api_key, config.openai_model)
        self._writer = TeamWriter(
            self._client, config.team_tracker_db_id, config.dry_run,
        )

    def run_sync(self) -> None:
        """Execute one sync cycle: poll → extract → write → mark."""
        # Load shared context (cached per cycle)
        playbook_text = self._playbook.load()
        hierarchy = self._hierarchy.load()
        hierarchy_json = json.dumps(hierarchy, ensure_ascii=False)

        # Poll for unprocessed meetings
        pages = self._source.get_pages()
        if not pages:
            logger.info("No unprocessed meetings found")
            return

        logger.info("Processing %d meetings", len(pages))
        total_tasks = 0

        for page in pages:
            page_id = page.get("id", "")
            metadata = self._source.get_page_metadata(page)
            title = metadata["title"]

            try:
                # Gather content
                content = self._source.get_page_content(page_id)

                # Extract tasks via AI
                tasks = self._extractor.extract(
                    playbook_text=playbook_text,
                    hierarchy_json=hierarchy_json,
                    attendees=metadata["attendees"],
                    meeting_title=title,
                    meeting_date=metadata["date"],
                    meeting_type=metadata["meeting_type"],
                    meeting_content=content,
                )

                # Write tasks
                if tasks:
                    created = self._writer.write_batch(tasks)
                    total_tasks += len(created) if not self._config.dry_run else len(tasks)
                    logger.info(
                        "Meeting '%s': %d tasks extracted, %d written",
                        title, len(tasks), len(created) if not self._config.dry_run else 0,
                    )
                else:
                    logger.info("Meeting '%s': no action items found", title)

                # Mark processed (even if 0 tasks — the meeting was scanned)
                if not self._config.dry_run:
                    self._source.mark_page_processed(page_id)

            except Exception:
                logger.exception(
                    "Failed to process meeting '%s' (page: %s) — will retry next cycle",
                    title, page_id,
                )

        logger.info("Sync complete: %d total tasks from %d meetings", total_tasks, len(pages))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_pipeline.py -v
```

Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pipeline.py tests/test_pipeline.py
git commit -m "feat: add pipeline orchestrator — poll, extract, write, mark"
```

---

### Task 9: Rewrite Main Entry Point

**Files:**
- Modify: `src/main.py`

- [ ] **Step 1: Rewrite src/main.py**

Replace the full contents:

```python
"""Entry point for the Nzyme AI-driven Task Tracker.

Parses command-line arguments, loads configuration, and runs one sync cycle.

Usage:
    python -m src.main [--dry-run] [--verbose]
"""
from __future__ import annotations

import argparse
import sys

from src.config import load_config
from src.utils.logger import setup_logging, get_logger
from src.pipeline import Pipeline

logger = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Nzyme — AI-driven task extraction from Notion meeting notes",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Log extracted tasks but don't write to Notion",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()

    if args.dry_run is not None:
        config = config.model_copy(update={"dry_run": args.dry_run})
    if args.verbose:
        config = config.model_copy(update={"log_level": "DEBUG"})

    setup_logging(config.log_level)
    logger.info("Starting Nzyme sync (dry_run=%s)", config.dry_run)

    try:
        pipeline = Pipeline(config)
        pipeline.run_sync()
        logger.info("Sync complete")
    except Exception:
        logger.exception("Sync failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify the module is importable**

```bash
python -c "from src.main import parse_args; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/main.py
git commit -m "feat: rewrite entry point for AI-driven pipeline"
```

---

### Task 10: Run Full Test Suite and Verify

**Files:** None (verification only)

- [ ] **Step 1: Install updated dependencies**

```bash
pip install -e ".[dev]"
```

- [ ] **Step 2: Run entire test suite**

```bash
pytest tests/ -v
```

Expected: All tests pass (config: 4, playbook_loader: 8, hierarchy_loader: 8, single_source: 6, ai_extractor: 10, team_writer: 7, pipeline: 3 = **46 tests total**).

- [ ] **Step 3: Run linter**

```bash
ruff check src/ tests/
```

Expected: No errors. If there are warnings, fix them.

- [ ] **Step 4: Verify dry-run invocation (requires .env with real credentials)**

```bash
python -m src.main --dry-run --verbose
```

Expected: Logs show the pipeline running — fetches playbook, fetches hierarchy, polls for meetings. If no meetings match the date filter, it should log "No unprocessed meetings found" and exit cleanly.

- [ ] **Step 5: Final commit if any fixes were needed**

```bash
git add -A
git commit -m "chore: fix lint issues and verify full test suite"
```

---

### Task 11: Write Playbook to Notion (Manual)

**Files:** None (Notion content, not code)

- [ ] **Step 1: Open the playbook page in Notion**

Navigate to: https://www.notion.so/kiboventures/2a283e67e2e7806c9768f51c5146e60b

- [ ] **Step 2: Write the playbook content**

Copy the playbook from the design spec (section "Playbook Content (First Draft)") into the Notion page. Use Notion's native formatting:
- Heading 1 for the title
- Heading 2 for each section
- Bulleted lists for rules
- Bold for category/priority names

- [ ] **Step 3: Run a full end-to-end test**

Create a test meeting page in the Meeting Notes DB with:
- Title: "Test Meeting — Nzyme AI"
- Date: set to 3+ hours ago (so it passes the buffer)
- Attendees: add yourself
- Content: a few to-do blocks with @mentions and deadlines
- Processed: unchecked

Then run:

```bash
python -m src.main --verbose
```

Verify:
1. Tasks appear in Team Task Tracker with correct title, assignee, priority, category
2. Hierarchy placement is correct (if applicable)
3. Meeting page is marked Processed = true
4. Re-running produces no duplicates
