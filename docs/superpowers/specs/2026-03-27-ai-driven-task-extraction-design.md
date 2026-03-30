# Nzyme Task Tracker: AI-Driven Task Extraction — Design Spec

**Date:** 2026-03-27
**Status:** Draft

## Context

Nzyme is a sync engine for Kibo Ventures (PE/VC fund, 10-20 people) that extracts action items from Notion Meeting Notes and writes them to a Team Task Tracker database. The current Python engine uses deterministic logic: hardcoded block parsing, bilingual date extraction, mention resolution, entity matching, and category mapping.

**Problem:** Every Notion schema change (new category, renamed property, hierarchy restructuring) requires code changes. The extraction logic is brittle and can't handle nuanced judgment calls (e.g., "is this a task or just a discussion point?").

**Solution:** Replace the deterministic extraction pipeline with an AI-driven approach. An OpenAI model (GPT-4.1) receives the meeting content, a natural-language "playbook" of rules (stored as a Notion page), and the current tracker hierarchy, then returns structured task data via function calling. Python code validates and writes the tasks to Notion.

## Architecture Overview

```
Every 15 min (cron locally, AWS Lambda later):

1. POLL ─── Query Meeting Notes DB
│        filter: Date < (now - 2h) AND Processed = false
│        → List of unprocessed meeting pages
│
2. GATHER ── For each meeting page:
│   ├── Fetch page content (all blocks → plain text)
│   ├── Fetch playbook page from Notion
│   └── Fetch Team Task Tracker hierarchy snapshot
│
3. EXTRACT ── Call OpenAI API (GPT-4.1) with function calling
│   ├── System prompt + playbook rules
│   ├── User message: meeting text + hierarchy + attendees
│   ├── Tool: create_task(...)
│   └── Response: list of tool_calls
│
4. WRITE ─── For each tool_call:
│   ├── Validate against schema
│   └── Create page in Team Task Tracker via Notion API
│
5. MARK ──── Set Processed = true on meeting page
```

## Trigger Mechanism

**Approach:** Delayed polling with 2-hour buffer.

- A scheduler (cron locally, CloudWatch Events on AWS) invokes the engine every 15 minutes.
- The engine queries the Meeting Notes DB for pages where `Date < (now - 2 hours)` AND `Processed = false`.
- The 2-hour buffer ensures meetings have ended and AI-generated notes have been fully written.
- Each invocation runs exactly one sync cycle (stateless, idempotent).

**Source mode:** Single shared Meeting Notes DB. The multi-source/registry mode is removed.

## Components

### `src/main.py` — Entry Point

- Parses CLI args (`--dry-run`, `--verbose`)
- Loads config
- Calls `pipeline.run_sync()`
- Exits with appropriate code (0 = success, 1 = error)
- Designed for both local `python -m src.main` and Lambda handler invocation

### `src/config.py` — Configuration (adapted)

Pydantic-validated config from environment variables:

| Variable | Required | Description |
|---|---|---|
| `NOTION_API_TOKEN` | Yes | Notion integration token |
| `OPENAI_API_KEY` | Yes | OpenAI API key |
| `OPENAI_MODEL` | No | Model name (default: `gpt-4.1`) |
| `MEETING_NOTES_DB_ID` | Yes | Meeting Notes database ID |
| `TEAM_TRACKER_DB_ID` | Yes | Team Task Tracker database ID |
| `PLAYBOOK_PAGE_ID` | Yes | Notion page ID for the playbook |
| `BUFFER_HOURS` | No | Hours to wait after meeting date (default: `2`) |
| `LOG_LEVEL` | No | Logging level (default: `INFO`) |
| `DRY_RUN` | No | Log tasks but don't write (default: `false`) |

Removed: `SYNC_MODE`, `SINGLE_SOURCE_DB_ID`, `REGISTRY_DB_ID`, `TASK_TRACKER_DB_ID` (was Macro tracker), `SYNC_INTERVAL_MINUTES`.

### `src/notion_client_wrapper.py` — Notion API Wrapper (kept)

Existing wrapper, unchanged. Provides:
- Rate limiting (3 req/s token-bucket)
- Automatic retry with exponential backoff on 429/5xx
- Transparent pagination

### `src/pipeline.py` — Orchestrator

The main sync loop. For each unprocessed meeting:

1. Calls `playbook_loader.load()` to get playbook text
2. Calls `hierarchy_loader.load()` to get tracker hierarchy snapshot
3. Calls `single_source.get_page_content()` to get meeting text + metadata
4. Calls `ai_extractor.extract()` with all the above
5. For each returned task, calls `team_writer.create_task()`
6. Calls `single_source.mark_page_processed()`

Error handling: if extraction or writing fails for a meeting, log the error and continue to the next meeting. Don't mark failed meetings as processed (they'll be retried next cycle).

### `src/ai_extractor.py` — OpenAI Integration

Builds and executes the OpenAI API call.

**System prompt structure:**
```
You are a task extraction assistant for a PE/VC fund team.
Your job is to extract action items from meeting notes and create tasks.

## Rules (Playbook)
{playbook_text}

## Team Task Tracker Schema
- Task (title): string
- Status: "Not Started" | "In Progress" | "Done"
- Assignee: Notion user ID
- Due Date: ISO date string
- Priority: "High" | "Medium" | "Low"
- Category: "Dealflow" | "Origination" | "Portfolio" | "Internal" | "Other"
- Parent item: page ID of parent task (optional)

## Existing Hierarchy
{hierarchy_json}

## Attendees in this meeting
{attendees_list_with_user_ids}
```

**User message:**
```
Extract action items from this meeting:

Meeting: {title}
Date: {date}
Type: {meeting_type}

{meeting_content_as_plain_text}
```

**Tool definition:**
```json
{
  "type": "function",
  "function": {
    "name": "create_task",
    "description": "Create a task in the Team Task Tracker",
    "parameters": {
      "type": "object",
      "properties": {
        "title": { "type": "string", "description": "Task title" },
        "assignee_id": { "type": "string", "description": "Notion user ID of assignee" },
        "due_date": { "type": "string", "description": "ISO date (YYYY-MM-DD) or null" },
        "priority": { "enum": ["High", "Medium", "Low"] },
        "category": { "enum": ["Dealflow", "Origination", "Portfolio", "Internal", "Other"] },
        "parent_task_id": { "type": "string", "description": "Page ID of parent task, or null for top-level" },
        "status": { "enum": ["Not Started", "In Progress", "Done"], "default": "Not Started" }
      },
      "required": ["title", "assignee_id", "priority", "category"]
    }
  }
}
```

**Parsing:** Extract `tool_calls` from the response. Each `create_task` call becomes a validated task dict passed to the writer.

**Edge cases:**
- If the model returns no tool_calls → meeting had no action items (still mark processed)
- If the model returns an invalid `assignee_id` or `parent_task_id` → log warning, skip that field (create task without assignee / at top level)
- Token limit: meeting content + playbook + hierarchy should fit comfortably in GPT-4.1's context (1M tokens). Even large meetings + full tracker hierarchy will be well under 10K tokens.

### `src/playbook_loader.py` — Playbook Fetcher

- Fetches the playbook Notion page by ID (`PLAYBOOK_PAGE_ID`)
- Recursively reads all blocks, converts to plain text
- Preserves headings and bullet structure for readability in prompt
- Caches within a single sync cycle (fetched once per run, not per meeting)

### `src/hierarchy_loader.py` — Tracker Hierarchy Snapshot

Queries the Team Task Tracker to build a hierarchy snapshot:

1. Query all pages (or at least those with Status != "Done" to reduce noise)
2. Build parent-child tree using the `Parent item` / `Sub-item` relations
3. Output structure:
```json
[
  {
    "id": "page-id-1",
    "title": "Dealflow",
    "category": "Dealflow",
    "children": [
      { "id": "page-id-2", "title": "Deal X", "children": [...] },
      { "id": "page-id-3", "title": "Deal Y", "children": [] }
    ]
  },
  ...
]
```
4. Cached per sync cycle (one query per run)

### `src/sources/single_source.py` — Meeting Notes Source (adapted)

Adapted from existing. Changes:
- Filter: `Date < (now - BUFFER_HOURS)` AND `Processed = false` (adds buffer)
- New method: `get_page_content(page_id)` → recursively fetch all blocks, convert to plain text
- Kept: `mark_page_processed(page_id)` → sets `Processed = true`

### `src/tracker/team_writer.py` — Task Writer (adapted)

Adapted from existing. Changes:
- Accepts validated task dicts from `ai_extractor` (not `ActionItem` objects)
- Creates pages with properties: Task (title), Status, Assignee, Due Date, Priority, Category
- Sets `Parent item` relation when `parent_task_id` is provided
- Dry-run mode: logs what would be written without calling Notion API
- Returns created page IDs for logging

## Playbook Content (First Draft)

Stored as a Notion page in "PRUEBAS SANTI" (ID: `2a283e67-e2e7-806c-9768-f51c5146e60b`).

```markdown
# Nzyme Playbook — Task Extraction Rules

## What is an action item?
- Any to-do block with an @mention (assignee)
- Explicit commitments: "I'll do X", "Yo me encargo de X"
- Tasks with deadlines mentioned for specific people
- Follow-ups: "Let's circle back on X", "Hay que hacer seguimiento de X"

## What is NOT an action item?
- Discussion points or observations
- FYIs and status updates with no action required
- Decisions (record of what was decided, no task)
- Items already marked as done/completed in the notes
- Vague statements without a clear owner or deliverable

## Categories
- **Dealflow**: anything related to new deals, pipeline, inbound, outreach
- **Origination**: sourcing, networking, conferences, intros
- **Portfolio**: existing portfolio companies — board prep, follow-ons, support
- **Internal**: team ops, hiring, admin, legal, compliance, tooling
- **Other**: anything that doesn't fit above

## Priority Rules
- **High**: explicit deadline within 7 days, or flagged as urgent/critical
- **Medium**: has a deadline beyond 7 days, or clearly important but not urgent
- **Low**: no deadline, nice-to-have, low-impact tasks

## Hierarchy Placement
- If a deal name or company name is mentioned, place under that entity in the hierarchy
- If the entity doesn't exist in the hierarchy yet, create the task at the category level (don't create new parent entities)
- If no entity context, place at the category root

## Assignee Resolution
- @mentions in to-do blocks → direct assignee
- "I'll do X" / "Yo me encargo" → the speaker (match from attendees list)
- If multiple people are mentioned, assign to the first person and note others in title
- If no clear assignee, skip the task (don't create unassigned tasks)

## Due Date Extraction
- Explicit dates: "by April 1", "antes del 1 de abril" → 2026-04-01
- Relative: "by Friday", "para el viernes" → resolve to next occurrence
- "End of week" / "fin de semana" → Friday of current week
- "End of month" / "fin de mes" → last day of current month
- No date mentioned → leave Due Date empty

## Language
- Meeting notes may be in English, Spanish, or mixed. Handle both languages.
```

## Files to Delete

| File/Directory | Reason |
|---|---|
| `src/extraction/` (entire dir) | Replaced by AI extraction |
| `src/dedup/` (entire dir) | AI + Processed flag handle dedup |
| `src/schema/` (entire dir) | No multi-source schema mapping needed |
| `src/sources/multi_source.py` | Single source mode only |
| `src/sources/registry.py` | No registry needed |
| `src/sources/base.py` | ABC no longer needed with single source |
| `src/tracker/writer.py` | Macro Task Tracker removed |
| `src/extraction/fallback_extractor.py` | AI replaces this entirely |

## Files to Keep (and adapt)

| File | Changes |
|---|---|
| `src/config.py` | New env vars (OpenAI key, playbook ID), remove old ones |
| `src/notion_client_wrapper.py` | No changes |
| `src/utils/logger.py` | No changes |
| `src/utils/rate_limiter.py` | No changes |
| `src/sources/single_source.py` | Add buffer filter, add `get_page_content()` |
| `src/tracker/team_writer.py` | Accept task dicts instead of ActionItems |
| `pyproject.toml` | Add `openai` dep, remove unused deps |

## New Files

| File | Purpose |
|---|---|
| `src/pipeline.py` | Orchestrates the sync cycle |
| `src/ai_extractor.py` | OpenAI API call with function calling |
| `src/playbook_loader.py` | Fetches and converts playbook Notion page to text |
| `src/hierarchy_loader.py` | Queries tracker, builds hierarchy snapshot |

## Dependencies

**Add:**
- `openai` (OpenAI Python SDK)

**Remove:**
- `python-dateutil` (AI handles date parsing)

**Keep:**
- `notion-client`
- `python-dotenv`
- `pydantic`

## Error Handling

- **OpenAI API failure:** Log error, skip meeting, retry next cycle
- **Invalid tool_call fields:** Log warning, create task with valid fields only (skip invalid assignee/parent)
- **Notion API failure on write:** Log error, don't mark meeting processed (retry next cycle)
- **Empty meeting content:** Log info, mark processed (no tasks to extract)
- **Playbook fetch failure:** Abort entire cycle (playbook is required for correct extraction)
- **Hierarchy fetch failure:** Proceed without hierarchy (tasks created at top level); log warning

## Testing Strategy

- **Unit tests:** `ai_extractor` with mocked OpenAI responses, `playbook_loader` with mocked Notion pages, `hierarchy_loader` with mocked tracker queries
- **Integration test:** End-to-end with a real meeting page in "PRUEBAS SANTI" — run pipeline, verify tasks appear in Team Task Tracker
- **Dry-run mode:** `--dry-run` flag logs everything without writing to Notion

## Verification Plan

1. Set up `.env` with real credentials
2. Create a test meeting page in Meeting Notes DB with known content
3. Write the playbook to "PRUEBAS SANTI" page
4. Run `python -m src.main --dry-run` — verify extracted tasks in logs
5. Run `python -m src.main` — verify tasks appear in Team Task Tracker with correct properties, hierarchy, assignees
6. Verify meeting page is marked `Processed = true`
7. Re-run — verify no duplicate tasks are created (meeting already processed)

## Future Considerations (not in scope)

- AWS Lambda deployment + CloudWatch Events scheduler
- Monitoring/alerting on extraction failures
- Cost tracking for OpenAI API usage
- Feedback loop: team marks AI-created tasks as incorrect → improves playbook
