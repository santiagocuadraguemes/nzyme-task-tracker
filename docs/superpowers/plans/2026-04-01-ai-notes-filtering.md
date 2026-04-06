# AI Notes Filtering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a config flag (`INCLUDE_AI_NOTES`) that controls whether Notion AI meeting notes blocks are included in the content sent to the AI extractor — defaulting to off (human notes only).

**Architecture:** Filter top-level blocks in `SingleSource.get_page_content()` using a whitelist of known human-written block types. When `include_ai_notes=False`, any block whose type is not in the whitelist (e.g., AI meeting notes blocks) is excluded along with its children. The flag is passed from `SyncConfig` through the pipeline.

**Tech Stack:** Python 3.11+, Pydantic, pytest

---

### File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `src/config.py` | Modify | Add `include_ai_notes` field |
| `.env.example` | Modify | Add `INCLUDE_AI_NOTES` env var |
| `src/sources/single_source.py` | Modify | Add block filtering logic |
| `src/pipeline.py` | Modify | Pass flag to `get_page_content()` |
| `tests/test_single_source.py` | Modify | Add filtering tests |
| `tests/test_pipeline.py` | Modify | Update `_make_config` for new field |

---

### Task 1: Config — add `include_ai_notes` field

**Files:**
- Modify: `src/config.py:11-26` (SyncConfig class)
- Modify: `src/config.py:29-45` (load_config function)
- Modify: `.env.example`

- [ ] **Step 1: Add field to SyncConfig**

```python
include_ai_notes: bool = Field(False, description="Include AI-generated meeting notes in extraction")
```

Add after `dry_run` field in `SyncConfig`.

- [ ] **Step 2: Wire in load_config**

```python
include_ai_notes=os.getenv("INCLUDE_AI_NOTES", "false").lower() in ("true", "1", "yes"),
```

Add after `dry_run` in `load_config()`.

- [ ] **Step 3: Add to .env.example**

```
INCLUDE_AI_NOTES=false  # Include AI-generated meeting notes (true/false)
```

Add in the Optional section.

- [ ] **Step 4: Commit**

```bash
git add src/config.py .env.example
git commit -m "feat: add INCLUDE_AI_NOTES config flag"
```

---

### Task 2: Filtering — add block type whitelist to SingleSource

**Files:**
- Modify: `src/sources/single_source.py:1-49`
- Test: `tests/test_single_source.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_single_source.py`:

```python
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
            "has_children": True,
            "ai_block": {"rich_text": [{"plain_text": "AI summary"}]},
        },
    ]
    source = SingleSource(client, "db-meetings")

    content = source.get_page_content("page-123", include_ai_notes=True)

    assert "Call Natalia" in content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `../venv/Scripts/python -m pytest tests/test_single_source.py -v -k "ai_block"`
Expected: FAIL — `get_page_content()` doesn't accept `include_ai_notes` parameter

- [ ] **Step 3: Implement filtering in SingleSource**

Add constant at module level:

```python
# Block types that represent human-written content.
# AI meeting notes (ai_block, etc.) are excluded from this set.
HUMAN_CONTENT_BLOCK_TYPES = frozenset({
    "heading_1", "heading_2", "heading_3",
    "paragraph", "bulleted_list_item", "numbered_list_item",
    "to_do", "toggle", "callout", "quote", "divider",
    "code", "table", "table_row", "column_list", "column",
    "image", "video", "file", "pdf", "bookmark", "embed",
    "audio", "equation", "breadcrumb", "link_preview",
    "synced_block", "template", "link_to_page",
})
```

Update `get_page_content`:

```python
def get_page_content(self, page_id: str, include_ai_notes: bool = True) -> str:
    """Fetch all blocks from a page and convert to plain text.

    When *include_ai_notes* is False, blocks whose type is not in the
    human-content whitelist are dropped (along with their children).
    """
    blocks = self._client.get_block_children(page_id)
    if not include_ai_notes:
        blocks = [b for b in blocks if b.get("type") in HUMAN_CONTENT_BLOCK_TYPES]
    return blocks_to_text(blocks, self._client)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `../venv/Scripts/python -m pytest tests/test_single_source.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/sources/single_source.py tests/test_single_source.py
git commit -m "feat: filter AI meeting notes blocks in SingleSource"
```

---

### Task 3: Pipeline — pass config flag through

**Files:**
- Modify: `src/pipeline.py:165`
- Modify: `tests/test_pipeline.py`

- [ ] **Step 1: Update pipeline to pass include_ai_notes**

In `pipeline.py`, change line 165 from:

```python
content = source.get_page_content(page_id)
```

to:

```python
content = source.get_page_content(page_id, include_ai_notes=config.include_ai_notes)
```

- [ ] **Step 2: Run full test suite**

Run: `../venv/Scripts/python -m pytest tests/ -v`
Expected: ALL PASS (pipeline tests use mocks, so the new kwarg is ignored by MagicMock)

- [ ] **Step 3: Commit**

```bash
git add src/pipeline.py
git commit -m "feat: wire INCLUDE_AI_NOTES flag through pipeline"
```
