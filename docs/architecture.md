# Architecture

## Pipeline Overview

The pipeline routes each meeting through one of three extraction paths. The first decision is the per-member `Auto-extract Tasks` flag on the Org Chart (default `true`). For members who opt out (`false`), the **literal-notes path** runs instead of the transcript pipeline: a single light LLM call (gpt-5-mini) on the page's notes content, using the Notion-hosted prompt at `LITERAL_NOTES_EXTRACTION_PROMPT_PAGE_ID` that instructs the model to keep titles verbatim. For members who opt in (default), the pipeline is transcript-first and falls back to a notes-based AI extractor when no transcript exists. It iterates over a **registry of per-member Meeting Notes databases** discovered from the Org Chart (one DB per active team member).

```
main.py → pipeline.run_sync():
  0. template_injector → inject `## Action Items` + `## Notes` headings across every DB (if enabled)
  Discover registry from Org Chart (active rows with a "Meeting Notes DB" URL,
                                    each carrying owner.auto_extract_tasks)
  Load shared context once (hierarchy, categories, users, deals, terminology, org chart,
                            classifier prompt, literal-notes extraction prompt)
  for each member DB:
    Build (db_id, title, date) fingerprint set from already-processed meetings in this DB
    for each unprocessed meeting in this DB:
      1. Skip if fingerprint matches a processed meeting in THIS DB
      2. Decide path on owner.auto_extract_tasks (CLI override wins when set):
         IF auto_extract_tasks is False:
           a. Resolve attendees (GCal → Notion → governance fallback) — used as LLM context
           b. literal_notes_extractor.extract → 1 LIGHT LLM call (gpt-5-mini) with the
              Notion-hosted prompt; returns tasks shaped for the classifier:
              {title (verbatim), assignee, internal_assignees, external_assignees, supporting_quote}.
              If the model returns 0 tasks → log WARNING and mark Processed (no further fallback).
           c. TaskClassifier → category, parent, deal_page_id, assignee_id
              (resolves internal_assignees against the Team Members list).
         ELIF transcript_block_id present:
           a. Resolve attendees (GCal → Notion → governance fallback)
           b. Correct transcript (LLM call 1)
           c. Extract tasks (LLM call 2)
           d. Classify tasks (LLM call 3)
         ELIF meeting_notes block present (transcription paused/disabled):
           a. Notes-only AIExtractor on the meeting_notes notes container
         ELSE:
           a. Notes-only AIExtractor on the page's plain-text content
      3. Semantic dedup → filter duplicates (workspace-wide, cross-DB)
      4. Assignee fallback → default to meeting creator on every path
      5. team_writer → create task pages in Team Task Tracker
      6. Mark meeting page as Processed
```

CLI override (debugging only): `python -m src.main --sync --auto-extract-tasks` or `--no-auto-extract-tasks` forces every page in the run onto that path regardless of the per-row Org Chart flag. The override sets `SyncConfig.auto_extract_tasks_override` and is consulted by `_should_auto_extract(config, owner)` before the registry value.

## Per-member Meeting Notes DBs

Each team member has their own Meeting Notes database. The same meeting commonly appears in multiple DBs (each attendee's personal notes capture different commitments from their own perspective). The pipeline polls every DB on every cycle.

**Registry source of truth:** the Nzyme Org Chart DB. Every active row carries a `Meeting Notes DB` URL property pointing at that member's database. `src/meeting_db_registry.py` reads these rows once per cycle and returns one `MeetingDB(db_id, owner_name, owner_email)` per active member with a URL set. Joiners and leavers are managed entirely in Notion (no redeploy):

- **Add a member:** create their DB, set `Active=true` on their Org Chart row, paste the DB URL into `Meeting Notes DB`.
- **Remove a member:** flip `Active=false` (or clear the URL).

`MEETING_NOTES_DB_ID` env var is an **override** — when set, registry discovery is bypassed and only that DB is polled. Useful for tests and single-DB dev runs. In production, leave it unset and let the Org Chart drive.

**Cross-DB fingerprint:** `_meeting_fingerprint(db_id, title, date)` prefixes the dedup key with the DB ID. Two team members' notes about the same meeting (identical title + date in their own DBs) therefore produce different fingerprints and are both processed. Duplicate task titles across DBs are caught later by the workspace-wide semantic dedup layer. Within a single DB, Notion's `(1)` / `(2)` suffix on duplicate pages still collapses correctly.

**Webhook setup stays per-DB:** each member's Notion automation must point at the same API Gateway URL. No workspace-level webhook exists in Notion.

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

1. **Discover registry** (`load_registry()`) — reads active Org Chart rows with a `Meeting Notes DB` URL set, returns a list of `MeetingDB` entries. Aborts the cycle on failure.
2. **Load shared context** (`_load_sync_context()`) — prompts, hierarchy, categories, users, deals, semantic dedup, terminology, org chart, classifier prompt. Loaded **once** per cycle (not per DB) since none of it varies by member DB. Abort on prompt load failure (required). Other context degrades gracefully.
3. **For each member DB in the registry:**
   a. **Poll unprocessed meetings** — `created_time < (now - buffer_hours)` AND `Processed = false`
   b. **Build per-DB fingerprints** — loads already-processed meetings in THIS DB
   c. **For each meeting page:**
      - Check fingerprint `(db_id, normalized_title, date)` against this DB's dedup set; skip duplicates
      - Check for `meeting_notes` block → transcript path or notes fallback
      - **Transcript path:** resolve attendees → correct → extract → classify (3 LLM calls)
      - **Notes fallback:** fetch content → AI extract + classify (1 LLM call)
      - Semantic dedup → assignee fallback → write tasks → mark Processed
4. **Log summary** — total tasks processed across all DBs

(The Done-task archive sweep no longer runs every cycle — it has been moved to a dedicated weekly Sunday cron. See "Done-task archive sweep" below.)

Helper functions:
- `_should_auto_extract(config, owner)` — combines the CLI override with the per-member registry flag; returns the boolean used to gate the routing decision
- `_process_via_literal_notes()` — light LLM extraction with the Notion-hosted prompt at `LITERAL_NOTES_EXTRACTION_PROMPT_PAGE_ID`, then the standard classifier. Title preservation is enforced by the prompt, not by code
- `_process_via_transcript()` — transcript extraction path (correct → extract → classify)
- `_process_via_notes()` — notes extraction path (AIExtractor, AI-driven fallback)
- `_resolve_attendees()` — GCal → Notion → governance attendee chain
- `_meeting_fingerprint(db_id, title, date)` — strips Notion's `(1)`, `(2)` suffixes, lowercases, combines with db_id + date
- `_load_categories()` — reads Category select options from DB schema
- `_build_seen_fingerprints(source, db_id)` — collects fingerprints from processed meetings in one DB

## Component Details

### MeetingDBRegistry (`src/meeting_db_registry.py`)

- **Input:** `SyncConfig` + `NotionClientWrapper`
- **Output:** `list[MeetingDB(db_id, owner_name, owner_email, auto_extract_tasks)]`
- `discover_meeting_dbs(client, org_chart_db_id)` queries the Org Chart for `Active=true` rows, parses the `Meeting Notes DB` URL property, reads the `Auto-extract Tasks` checkbox (default `True` when missing), and returns one entry per parseable URL (skipping rows without a URL, with an unparseable URL, or with a URL already claimed by an earlier row)
- `load_registry(config, client)` returns the override (single-DB list from `MEETING_NOTES_DB_ID`) when set, else discovers from the Org Chart; raises if neither is configured
- `find_owner_for_page(registry, page_database_id)` returns the registry entry matching a page's parent DB
- Notion DB IDs are normalized (dashes stripped, lowercased) before comparison, so registry entries with hyphenated UUIDs match payload IDs without hyphens

### Fundraising branch — multi-DB behavior

If two Kibo members independently capture the same LP meeting in their respective DBs, both pages fire the Affinity post and the LP opportunity ends up with two notes. That's intentional: each member's notes capture distinct insights and are independently valuable on the LP timeline. An earlier creator-owns-DB guard tried to enforce "exactly one post per meeting" by skipping when `page.creator != db.owner`, but its premise was false (a meeting recorded by member A in member B's DB has no parallel page in A's DB), so it silently dropped legitimate posts. Removing it favors a small chance of duplicates over the certainty of missed posts.

### TemplateInjector (`src/template_injector.py`)

- **Input:** NotionClientWrapper + template page ID + target page ID
- **Output:** Boolean (True if template was injected)
- Fetches template blocks dynamically from a normal Notion page (`MEETING_TEMPLATE_PAGE_ID`, e.g. the "Generic Template" page under the Templates folder), converts from "read" to "create" format, filters out AI blocks
- **Injects INSIDE the page's `meeting_notes` block** — locates the AI Meeting block on the target page, reads `meeting_notes.children.notes_block_id`, and appends template blocks at the start of that human-notes container (not at the page root)
- Retries the meeting_notes lookup a few times (~3 × 1s) to absorb the race between page creation and Notion attaching the block; if still missing, returns False and the next cron tick retries
- Idempotency: scans the children of `notes_block_id` for the template's first heading; skips if already present
- Edit the template page in Notion to change what gets injected — no code changes needed

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
- Prunes to 4 levels (categories → sub-categories → entities → deals). At max depth, only keeps nodes that have children (organizational nodes), filtering out leaf tasks
- Removes nodes with empty titles
- **Caches** result for the lifetime of the instance

### DealContextLoader (`src/deal_context.py`)

- **Input:** Deal Workplans database ID (optional, set via `DEAL_WORKPLANS_DB_ID`)
- **Output:** List of `DealInfo` with name, page IDs, and workstreams
- Queries Deal Workplans DB for all deals
- For each deal, fetches the page's child blocks to discover inline databases (Workplan, Action Items) by title pattern
- Loads active workstreams (Status != Done) from each deal's Workplan DB
- Extracts workstream title, status, type, and adviser
- Resolves each deal's Team Task Tracker page ID from the `🖇️ Team Task Tracker` relation
- Gracefully handles missing inline DBs and per-deal failures
- Deal context is formatted and injected into the system prompt as `{{DEAL_CONTEXT}}`
- Meeting titles are scanned for deal name matches; detected deals are appended as hints to the user prompt

### SingleSource (`src/sources/single_source.py`)

| Method | Purpose |
|--------|---------|
| `get_unprocessed_pages(buffer_hours)` | Filter: `Processed=false AND created_time < (now - buffer)` |
| `get_ready_pages(idle_minutes)` | Filter: `Processed=false AND last_edited_time < (now - idle)` |
| `get_processed_pages()` | All processed meetings (for dedup fingerprinting) |
| `get_page_content(page_id, include_ai_notes)` | Fetch blocks, optionally filter out AI-generated blocks, convert to text via `blocks_to_text` |
| `get_page_metadata(page)` | Extract title, date, meeting_type, attendees from properties |
| `mark_processing(page_id)` | Set `Processing=true` (concurrency lock) |
| `clear_processing(page_id)` | Set `Processing=false` (release lock on failure) |
| `mark_template_injected(page_id)` | Set `Template Injected=true` checkbox |
| `mark_page_processed(page_id)` | Set `Processed=true` + clear `Processing` lock |

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
- **`create_task(task)`** — Maps dict to Notion properties, creates page. Skips if title already exists.
- **`link_tasks_to_meeting(meeting_page_id, task_ids)`** — After a batch is written, patches the source meeting page's `Task - Relation` to include the new task IDs (merging with any existing list). This is the only meeting↔task linkage now: the reverse `Meeting - Relation` on the tracker was removed when DBs went per-member, since one relation can't span N source DBs.
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
- **Data source resolution:** Notion API 2025-09-03+ replaced `databases.query` with `data_sources.query`. The wrapper resolves database IDs to data source IDs and caches the mapping.
- **API version:** `2026-03-11` — supports `meeting_notes` blocks for transcript extraction.

### Utilities

- **`blocks_to_text`** (`src/utils/blocks_to_text.py`) — Converts Notion blocks to markdown. Supports headings, lists, to-dos, dividers, callouts, quotes, toggles. Recursively fetches nested children.
- **`RateLimiter`** (`src/utils/rate_limiter.py`) — Token-bucket, configurable req/s (default 3.0)
- **`logger`** (`src/utils/logger.py`) — One-time `setup_logging()`, format: `YYYY-MM-DDTHH:MM:SS | LEVEL | module | message`

## Data Flow

### Transcript path (default)
```
Meeting Notes DB page
  → find_meeting_notes_block() → meeting_notes block
  → fetch transcript text + attendees + human notes
  → _resolve_attendees() (GCal → Notion → governance)
  → TranscriptCorrector.correct() → corrected transcript (LLM call 1)
  → TaskExtractor.extract() → raw tasks (LLM call 2)
  → TaskClassifier.classify() → classified tasks (LLM call 3)
  → SemanticDedup + assignee fallback
  → TeamTaskTrackerWriter.write_batch() → Notion pages
```

### Notes fallback path
```
Meeting Notes DB page (no meeting_notes block)
  → SingleSource.get_page_content() → plain text
  → AIExtractor.extract() → tasks with category/parent (1 LLM call)
  → SemanticDedup + assignee fallback
  → TeamTaskTrackerWriter.write_batch() → Notion pages
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

## Done-task archive sweep

A dedicated weekly Lambda job sweeps Done tasks out of the live Team Task Tracker and into a separate **Team Task Tracker — Archive** DB. Filter: `Status = Done` AND `last_edited_time` older than 3 days (the grace window so the team sees completed work in the next Monday standup).

- **Schedule:** Sunday 06:00 UTC, declared as the `WeeklyArchive` event on `NzymeFunction` in `template.yaml`. The schedule sends `{"job":"weekly_archive"}` as the event input; the unified Lambda handler routes that to `_handle_weekly_archive`.
- **Behavior:** for each match, copy properties to the archive DB (write-shape conversion done by `_copy_property_for_write`) → soft-delete the original via `archive_page`. Re-runs are idempotent: an archive copy carries a `Source Page ID` rich-text marker, and `_load_archived_source_ids` builds the skip-set on each run.
- **Hierarchy relations are dropped on copy** (`Parent item`, `Sub-item`) — once parents are also archived, references would dangle. The cross-DB `Deal Relation` is preserved. Meeting backlinks aren't preserved: the reverse linkage now lives on the Meeting Notes side as `Task - Relation`, and the archived task page doesn't reciprocate that.
- **Read-only types skipped on copy:** `formula`, `rollup`, `created_time`, `last_edited_time`, `created_by`, `last_edited_by`, `unique_id`. Notion auto-populates the relevant ones on the new page.
- **Configuration:** `TASK_ARCHIVE_DB_ID` env var (SAM parameter `TaskArchiveDbId`). When unset, the weekly job logs a warning and exits as a no-op — useful for environments where the archive DB doesn't exist yet.
- **Manual trigger:** `python -m src.main --archive` runs the same sweep locally (respects `--dry-run`).
- **Per-task error handling:** one failed archive (copy or source-archive) doesn't block the rest of the batch.

## Webhook / Lambda Mode

An alternative to local polling, the webhook mode uses AWS Lambda for serverless execution:

```
[Template Injection — event-driven]
Notion Automation (page created in any per-member DB) → API Gateway → Lambda: webhook_handler
  → load registry, validate page's parent DB is in it
  → inject template → set "Template Injected" = true

[AI Extraction — scheduled]
CloudWatch Events (every 1 min) → Lambda: extraction_handler
  → load registry from Org Chart
  → for each member DB: query Processed=false AND last_edited_time < now-3min
  → for each ready page: run_sync_for_page

[Done-task archive — weekly]
CloudWatch Events (Sun 06:00 UTC, Input={"job":"weekly_archive"}) → Lambda: _handle_weekly_archive
  → query Team Task Tracker for Status=Done AND last_edited_time < now-3d
  → for each match: copy properties to TASK_ARCHIVE_DB_ID, archive original
```

The registry is reloaded once per cron tick — one extra Notion query per minute — so joiner/leaver changes in the Org Chart take effect within a minute without any redeploy.

### Components

| File | Responsibility |
|------|---------------|
| `src/webhook/handler.py` | Parses Notion automation payload, validates against discovered registry, sets `Date = page.created_time` (with hour), calls template injection |
| `src/webhook/lambda_handler.py` | Two Lambda entry points: `webhook_handler` (API Gateway) and `extraction_handler` (CloudWatch cron) |
| `src/meeting_db_registry.py` | Reads active Org Chart rows' `Meeting Notes DB` URL property, returns `[MeetingDB]` |

### Single-Page Entry Points (`src/pipeline.py`)

- `run_inject_templates_for_page(config, client, page_id)` — Inject template into one page, set `Template Injected = true`
- `run_sync_for_page(config, client, page_id, use_gcal, force)` — Extract tasks from one page (transcript-first, notes fallback). Guards on `Processed=false` unless `force=True`. `use_gcal=True` enables Google Calendar attendee lookup (CLI only).
- `_load_sync_context(config, client)` — Shared helper that loads prompts, hierarchy, categories, users, deals, terminology, org chart, classifier prompt

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
4. **Three-layer dedup** — Meeting-level (title+date fingerprint), semantic embeddings (cosine similarity), and task-level (exact title match in tracker).
5. **Rate limiting** — All Notion API calls go through the wrapper with 3 req/s pacing and retry.
6. **Observability** — Logfire spans around each meeting, structured logging throughout.
