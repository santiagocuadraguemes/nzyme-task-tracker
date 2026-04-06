# Architecture

## Pipeline Overview

```
main.py → pipeline.run_sync():
  0. template_injector → inject "Your own notes" section into new meeting pages
  then for each unprocessed meeting:
    1. playbook_loader  → fetch playbook rules from Notion page
    2. hierarchy_loader → snapshot Team Task Tracker parent-child tree
    3. single_source    → fetch meeting content as plain text (filtered by INCLUDE_AI_NOTES)
    4. ai_extractor     → call OpenAI with playbook + hierarchy + content
    5. team_writer      → create task pages in Team Task Tracker
    6. single_source    → mark meeting page as Processed
```

## Entry Point (`src/main.py`)

- Parses CLI args: `--watch`, `--inject-templates`, `--sync`, `--dry-run`, `--verbose`
- Loads `SyncConfig` from `.env` via Pydantic
- Configures Logfire for OpenAI observability (uses `LOGFIRE_TOKEN` when provided)
- Creates `NotionClientWrapper` and runs in one of two modes:

**Watch mode** (`--watch`): Continuous loop. Template injection runs every `WATCH_INTERVAL` seconds (default 10s). Sync extraction runs every `SYNC_INTERVAL` seconds (default 5 min). Ctrl+C to stop.

**One-shot mode** (default): Runs `run_inject_templates()` and/or `run_sync()` once and exits. If neither `--inject-templates` nor `--sync` is passed, both run.

## Pipeline Orchestrator (`src/pipeline.py`)

Two independent entry points:

### `run_inject_templates()` (`--inject-templates`)

Fetches the meeting note template from Notion (`MEETING_TEMPLATE_PAGE_ID`), then injects it into pages that don't have it yet. Queries `Template Injected=false` (created in last 12 hours) to catch pages ASAP. Sets `Template Injected=true` after successful injection.

### `run_sync()` (`--sync`)

Instantiates all components with a shared `NotionClientWrapper`, then:

1. **Load playbook** (required) — abort cycle on failure
2. **Load hierarchy** (optional) — degrade gracefully on failure (tasks go to top level)
3. **Load categories** dynamically from DB schema — fall back to `["Other"]` on failure
4. **Poll unprocessed meetings** — `Date < (now - buffer_hours)` AND `Processed = false`
5. **Build dedup fingerprints** — loads already-processed meetings for cross-cycle dedup
6. **For each meeting page:**
   - Check fingerprint `(normalized_title|date)` against dedup set; skip duplicates
   - Fetch page content (blocks to text, filtered by `include_ai_notes` config); skip if empty
   - Call AI extractor with full context
   - Write extracted tasks via team writer
   - Mark page as Processed
7. **Log summary** — total tasks processed

Helper functions:
- `_meeting_fingerprint()` — strips Notion's `(1)`, `(2)` suffixes, lowercases, combines with date
- `_load_categories()` — reads Category select options from DB schema
- `_build_seen_fingerprints()` — collects fingerprints from processed meetings
- `_inject_templates()` — injects "Your own notes" section into new meeting pages via `template_injector`

## Component Details

### TemplateInjector (`src/template_injector.py`)

- **Input:** NotionClientWrapper + template page ID + target page ID
- **Output:** Boolean (True if template was injected)
- Fetches template blocks dynamically from a Notion template page (`MEETING_TEMPLATE_PAGE_ID`), converts from "read" to "create" format, filters out AI blocks
- Detects if template is already injected by checking for the first heading match (idempotent)
- On empty/new pages the blocks land at the top; if AI content already exists they go at the bottom (the pipeline filters by block type regardless of position)
- Edit the template in Notion to change what gets injected — no code changes needed

### PlaybookLoader (`src/playbook_loader.py`)

- **Input:** Playbook Notion page ID
- **Output:** Plain text (markdown) of the playbook content
- Fetches all blocks from the page, converts via `blocks_to_text`
- **Caches** result for the lifetime of the instance (one sync cycle)

### HierarchyLoader (`src/hierarchy_loader.py`)

- **Input:** Team Task Tracker database ID
- **Output:** List of root nodes with nested children: `[{id, title, category, children: [...]}]`
- Queries all non-Done tasks from the tracker
- Builds parent-child tree from "Parent item" self-relation
- Prunes to 2 levels (categories + entities, no leaf tasks)
- Removes nodes with empty titles
- **Caches** result for the lifetime of the instance

### SingleSource (`src/sources/single_source.py`)

| Method | Purpose |
|--------|---------|
| `get_unprocessed_pages(buffer_hours)` | Filter: `Processed=false AND created_time < (now - buffer)` |
| `get_ready_pages(idle_minutes)` | Filter: `Processed=false AND last_edited_time < (now - idle)` |
| `get_processed_pages()` | All processed meetings (for dedup fingerprinting) |
| `get_page_content(page_id, include_ai_notes)` | Fetch blocks, optionally filter out AI-generated blocks, convert to text via `blocks_to_text` |
| `get_page_metadata(page)` | Extract title, date, meeting_type, attendees from properties |
| `mark_template_injected(page_id)` | Set `Template Injected=true` checkbox |
| `mark_page_processed(page_id)` | Set `Processed=true` checkbox |

### AIExtractor (`src/ai_extractor.py`)

- **Input:** Meeting metadata + content + playbook + hierarchy + categories
- **Output:** List of task dicts: `[{title, assignee_id, due_date, priority, category, parent_task_id, status}]`
- Uses OpenAI chat completions with function calling (`tool_choice="auto"`)
- Parses `create_task` tool calls from response
- Handles: no tool calls (returns `[]`), invalid JSON (logs warning, skips that call)

#### Prompt Construction

**System prompt** (`SYSTEM_PROMPT_TEMPLATE`) includes:
- Playbook rules (natural language from Notion page) → `{playbook}`
- Team Task Tracker schema with property types
- Hierarchy as JSON with id/title/children → `{hierarchy}`
- Attendees list as `- Name (ID: xxx)` → `{attendees}`
- Category options as dynamic enum string → `{categories}`

**User prompt** (`USER_PROMPT_TEMPLATE`) includes:
- Meeting title, date, type → `{title}`, `{date}`, `{meeting_type}`
- Full meeting content as plain text → `{content}`

#### Tool Definition

`create_task` function with parameters:
- `title` (string, required) — clear, actionable task title
- `assignee_id` (string, required) — Notion user ID from attendees
- `priority` (enum, required) — "High" / "Medium" / "Low"
- `category` (enum, required) — dynamic from DB schema
- `due_date` (string|null) — ISO date YYYY-MM-DD
- `parent_task_id` (string|null) — page ID from hierarchy
- `status` (enum) — defaults to "Not Started"

### TeamTaskTrackerWriter (`src/tracker/team_writer.py`)

- **On init:** Queries all existing task titles for dedup (normalized: `.strip().lower()`)
- **`create_task(task)`** — Maps dict to Notion properties, creates page. Skips if title already exists. Sets "Meeting - Relation" to link back to the source meeting (bidirectional — auto-populates "Task - Relation" on the meeting page).
- **`write_batch(tasks)`** — Creates multiple tasks. Per-task error handling; failures don't abort batch.
- **Dry-run mode:** Logs what would be created, updates in-memory cache, but doesn't write to Notion.

### NotionClientWrapper (`src/notion_client_wrapper.py`)

Rate-limited facade over the Notion SDK, shared by all components in a sync cycle.

| Method | Purpose |
|--------|---------|
| `query_database(database_id, filter, sorts)` | Query with auto-pagination |
| `get_block_children(block_id)` | Fetch all child blocks with pagination |
| `get_page(page_id)` | Retrieve single page |
| `create_page(parent_database_id, properties)` | Create new page |
| `update_page(page_id, properties)` | Update page properties |
| `retrieve_database(database_id)` | Get database schema |

Internal behavior:
- **Rate limiting:** Token-bucket at 3 req/s (Notion API limit)
- **Retry:** Exponential backoff on 429/5xx errors, up to 3 retries
- **Pagination:** Transparently handles multi-page responses via `start_cursor`
- **Data source resolution:** Notion API 2025-09-03 replaced `databases.query` with `data_sources.query`. The wrapper resolves database IDs to data source IDs and caches the mapping.

### Utilities

- **`blocks_to_text`** (`src/utils/blocks_to_text.py`) — Converts Notion blocks to markdown. Supports headings, lists, to-dos, dividers, callouts, quotes, toggles. Recursively fetches nested children.
- **`RateLimiter`** (`src/utils/rate_limiter.py`) — Token-bucket, configurable req/s (default 3.0)
- **`logger`** (`src/utils/logger.py`) — One-time `setup_logging()`, format: `YYYY-MM-DDTHH:MM:SS | LEVEL | module | message`

## Data Flow

```
Meeting Notes DB page
  → SingleSource.get_page_content(include_ai_notes) → plain text (human-only or full)
  → SingleSource.get_page_metadata() → {title, date, meeting_type, attendees}

Playbook Notion page
  → PlaybookLoader.load() → plain text (cached)

Team Task Tracker DB
  → HierarchyLoader.load() → [{id, title, category, children}] (cached)
  → _load_categories() → ["Category1", "Category2", ...]

All of the above
  → AIExtractor.extract() → [{title, assignee_id, due_date, priority, category, parent_task_id, status}]

Task dicts
  → TeamTaskTrackerWriter.write_batch() → Notion pages in Team Task Tracker
```

## Error Handling

| Failure | Behavior |
|---------|----------|
| Playbook fetch fails | Abort entire sync cycle (required for correct extraction) |
| Hierarchy fetch fails | Degrade gracefully — tasks go to top level (no parent) |
| Category fetch fails | Fall back to `["Other"]` |
| Single meeting fails | Log error, skip to next; failed meeting NOT marked processed (retry next cycle) |
| Single task write fails | Log error, continue with remaining tasks in batch |
| Notion API 429/5xx | Exponential backoff retry (up to 3 attempts) |
| Template injection fails | Log error, continue sync cycle — template injection is optional |
| Empty meeting content | Mark processed, skip extraction (no tasks to create) |
| Duplicate meeting | Skip extraction, mark processed (fingerprint-based dedup) |

## Auto-Archiving

At the end of each sync cycle, the pipeline archives tasks where `Status = Done` and `last_edited_time` is older than 3 days. This keeps the tracker clean while giving the team a grace period to review completed work in standups.

- Runs every cycle, even when there are no unprocessed meetings
- Per-task error handling: one failed archive doesn't block others
- Respects dry-run mode (logs what would be archived)
- Archived pages go to Notion's trash and can be restored if needed

## Webhook / Lambda Mode

An alternative to local polling, the webhook mode uses AWS Lambda for serverless execution:

```
[Template Injection — event-driven]
Notion Automation (page created) → API Gateway → Lambda: webhook_handler
  → inject template → set "Template Injected" = true

[AI Extraction — scheduled]
CloudWatch Events (every 1 min) → Lambda: extraction_handler
  → query: Processed=false AND Date<=now AND last_edited_time < now-3min
  → for each ready page: run AI extraction, write tasks, mark Processed=true
```

### Components

| File | Responsibility |
|------|---------------|
| `src/webhook/handler.py` | Parses Notion automation payload, validates DB, calls template injection |
| `src/webhook/lambda_handler.py` | Two Lambda entry points: `webhook_handler` (API Gateway) and `extraction_handler` (CloudWatch cron) |

### Single-Page Entry Points (`src/pipeline.py`)

- `run_inject_templates_for_page(config, client, page_id)` — Inject template into one page, set `Template Injected = true`
- `run_sync_for_page(config, client, page_id)` — Extract tasks from one page; guards on `Processed=false` and `Date<=now`
- `_load_sync_context(config, client)` — Shared helper that loads playbook, hierarchy, categories, users

### Why Not Webhook on Content Updates?

During meetings, pages are updated thousands of times. Instead of debouncing those events, a 1-minute cron queries Notion's `last_edited_time` to detect idle pages. Maximum latency: ~4 min after editing stops.

### AWS Resources

- API Gateway (HTTP API) — `POST /webhook/{token}`
- Lambda: `nzyme-webhook` (256 MB, 30s) — template injection
- Lambda: `nzyme-extraction` (512 MB, 120s) — AI extraction
- CloudWatch Events rule — `rate(1 minute)` triggers extraction

See `template.yaml` (SAM) for infrastructure definition and `scripts/deploy.sh` for deployment.

## Key Design Principles

1. **Dynamic schema** — Playbook, hierarchy, and category options are all fetched from Notion at runtime. Schema changes in Notion require no code changes.
1. **AI notes filtering** — `INCLUDE_AI_NOTES` (default `false`) controls whether Notion AI meeting notes blocks are sent to the extractor. When off, only human-written blocks (to-dos, paragraphs, headings, etc.) are included, using a whitelist of known content block types.
2. **Stateless cycles** — Each sync cycle is independent. No persistent state between runs.
3. **Graceful degradation** — Non-critical failures (hierarchy, categories, individual meetings/tasks) don't abort the cycle.
4. **Two-layer dedup** — Meeting-level (title+date fingerprint) and task-level (title match in tracker).
5. **Rate limiting** — All Notion API calls go through the wrapper with 3 req/s pacing and retry.
6. **Observability** — Logfire spans around each meeting, structured logging throughout.
