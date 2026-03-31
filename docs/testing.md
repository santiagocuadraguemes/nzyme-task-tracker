# Testing Guide

## Unit Tests (pytest)

### Running Tests

```bash
# All tests
../venv/Scripts/python -m pytest tests/ -v

# Single test file
../venv/Scripts/python -m pytest tests/test_ai_extractor.py -v

# Single test method
../venv/Scripts/python -m pytest tests/test_pipeline.py::TestRunSync::test_full_cycle -v
```

### Structure

Tests live in `tests/` mirroring `src/` structure. All Notion and OpenAI calls are mocked — no real API calls.

- `conftest.py` — Shared `mock_client` fixture (MagicMock of `NotionClientWrapper` with pre-configured return values for `query_database`, `get_block_children`, `get_page`, `create_page`, `update_page`, `retrieve_database`)
- Test naming: `test_<module>.py` tests `src/<module>.py`

### Key Patterns

- **Patching at import location:** `@patch("src.pipeline.SingleSource")`, not `@patch("src.sources.single_source.SingleSource")`
- **Builder helpers:** `_make_page()`, `_make_config()`, `_mock_tool_call()` — create minimal test objects
- **Error simulation:** `MagicMock(side_effect=Exception(...))` for API errors
- **Assertion style:** `assert_called_once_with()`, `call_args.kwargs["filter"]`, `call_count`

### Test File Coverage

| File | Tests |
|------|-------|
| `test_config.py` | SyncConfig validation, `load_config` from env vars |
| `test_blocks_to_text.py` | Block type conversion, nested children, empty blocks |
| `test_playbook_loader.py` | Load, cache behavior, empty page |
| `test_hierarchy_loader.py` | Flat pages, parent-child tree, pruning, caching, Done filter |
| `test_single_source.py` | Buffer filter, content conversion, metadata extraction, mark processed |
| `test_ai_extractor.py` | Task extraction, no tool calls, invalid JSON, dynamic category enum |
| `test_team_writer.py` | Full/minimal task creation, dry run, batch, dedup, error continuation |
| `test_pipeline.py` | Full cycle, no pages, per-page failure handling |

---

## Integration Testing (Notion MCP)

Integration tests verify the full pipeline against real Notion databases. These are run manually using the Notion MCP tools, not via pytest.

### Prerequisites

- Notion MCP tools available (`mcp__claude_ai_Notion__*`)
- Access to the workspace databases (see `docs/notion-schema.md` for IDs and schemas)
- `.env` configured with valid `NOTION_API_TOKEN` and `OPENAI_API_KEY`

### Workflow

#### Step 1: Clean Up Previous Test Artifacts

Before each test run, remove leftover test data from prior runs. **Always clean up BEFORE creating new test data**, not after — this ensures a clean state even if a previous run was interrupted.

How to identify test artifacts:
- Meeting pages with titles containing `[TEST]`
- Task pages in Team Task Tracker with titles containing `[TEST]`

Cleanup procedure:
1. Query **Meeting Notes DB** (`b07976472620499fa4b89be7b03c07d0`) for pages with title containing `[TEST]`
   - Use `notion-query-data-sources` with a title filter
   - For each found page: archive it using `notion-update-page` (set `archived: true`)
2. Query **Team Task Tracker** (`32f83e67e2e7803f9662f43125603afa`) for pages with title containing `[TEST]`
   - Same approach: query, then archive each

#### Step 2: Create Realistic Test Meeting Pages

Create 1-2 meeting pages in the Meeting Notes DB using `notion-create-pages`.

**Page properties:**
- **Meeting** (title): `[TEST] Meeting Name - test-run-YYYYMMDD-HHMMSS`
- **Date**: yesterday or older (must be > 2 hours ago for the buffer filter to pick it up)
- **Meeting type**: use one of the real types (e.g., "Portfolio review", "Team sync", "Deal review")
- **Processed**: `false`
- **Attendees**: use real Notion user IDs from the workspace

**Page content** (add as blocks after page creation):
Create realistic meeting notes including:
- Heading blocks for agenda items
- Paragraph blocks with discussion notes
- To-do blocks with action items mentioning attendees (e.g., `@Santiago to review term sheet by Friday`)
- Bullet list items with supporting notes
- Mix of English and Spanish content (bilingual team)

Example test meeting content:
```
# Q1 Portfolio Review

## Acme Corp Update
- Revenue grew 15% QoQ, runway extends to Q3 2027
- Need to review updated financials before board meeting
- @Santiago review term sheet and send comments by April 5
- @Maria schedule follow-up call with Acme CFO

## Internal
- Update investor reporting template for Q1
- @Santiago update Q1 report draft antes del viernes
- Discuss hiring plan for ops team next week
```

#### Step 3: Run the Pipeline

```bash
# Dry run — tasks logged but not written to Notion
python -m src.main --dry-run --verbose

# Full run — tasks created in Notion
python -m src.main --verbose
```

For iterative testing, start with `--dry-run` to verify extraction quality before doing a full run.

#### Step 4: Verify Results

After a full (non-dry-run) execution:

1. **Check Meeting Notes DB:**
   - The test meeting page should now have `Processed = true`
   - Use `notion-fetch` with the page ID to verify

2. **Check Team Task Tracker:**
   - Query for recently created task pages with `[TEST]` in the title
   - Verify each task has correct: title, assignee, category, parent item, priority, due date

3. **Expected behaviors to verify:**
   - Tasks assigned to correct attendees (Notion user IDs match)
   - Due dates correctly parsed (including relative dates and Spanish like "antes del viernes")
   - Categories match the meeting context
   - Parent items link to correct hierarchy nodes (entities/categories)
   - No duplicate tasks created (title-based dedup in `TeamTaskTrackerWriter`)

#### Step 5: Clean Up After Test

Archive all test artifacts using the same procedure as Step 1. Leave the workspace clean.

### Test Scenarios

#### Scenario A: Basic Task Extraction
Create a meeting with 2-3 clear action items with @mentions and due dates.
**Expected:** All tasks created with correct assignees and dates.

#### Scenario B: Empty Meeting
Create a meeting page with discussion notes but no action items.
**Expected:** Page marked as Processed, no tasks created.

#### Scenario C: Bilingual Content
Create a meeting with mixed English/Spanish action items.
**Expected:** Tasks extracted regardless of language; due dates parsed from both languages.

#### Scenario D: Hierarchy Placement
Create a meeting discussing a specific entity (e.g., "Acme Corp") that already exists in the Team Task Tracker hierarchy.
**Expected:** Tasks placed under the correct entity via `parent_task_id`.

#### Scenario E: Deduplication
Run the pipeline on a meeting, then uncheck `Processed` and run again.
**Expected:** Second run should not create duplicate tasks (title-based dedup in `TeamTaskTrackerWriter`). The meeting-level fingerprint dedup will also catch it if the processed meetings are queried.

### Naming Convention

**Always prefix test data with `[TEST]`** so it can be identified and cleaned up reliably:
- Meeting titles: `[TEST] Meeting Name - test-run-YYYYMMDD-HHMMSS`
- This prevents accidental deletion of real data during cleanup queries
- The timestamp suffix ensures uniqueness across test runs
