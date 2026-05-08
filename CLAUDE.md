# CLAUDE.md

## Project

Nzyme is an AI-driven sync engine that extracts action items from Notion meeting notes and writes them to a Team Task Tracker database. It uses OpenAI gpt-5-mini with function calling, guided by a natural-language playbook stored as a Notion page.

Built for Kibo Ventures (PE/VC fund, ~10-20 people). Meeting notes may be in English, Spanish, or mixed.

## Setup

```bash
# Shared venv lives one directory up (../venv/)
cd ../  # from project root
python -m venv venv
source venv/Scripts/activate  # Windows/Git Bash
pip install -e "nzyme-task-tracker/.[dev]"
```

Environment: copy `.env.example` to `.env` and fill in credentials. Never commit `.env`.

## LLM keys — two-key setup

Nzyme uses **two separate API keys** to balance cost and quality. When a run fails with an "API key not valid" error, check which endpoint was called to know which key to rotate.

| Stage | Model | Env var |
|-------|-------|---------|
| Transcript correction (heavy) | `gemini-3-flash-preview` (Gemini 3 Flash Preview — **not** `gemini-2.5-flash`) | `GEMINI_API_KEY` |
| Task extraction (heavy) | `gemini-3-flash-preview` (Gemini 3 Flash Preview — **not** `gemini-2.5-flash`) | `GEMINI_API_KEY` |
| Task classification (light) | `gpt-5-mini` | `OPENAI_API_KEY` |
| Literal-notes extraction (light) — `Auto-extract Tasks = false` path | `gpt-5-mini` | `OPENAI_API_KEY` |
| Semantic dedup embeddings (light) | `text-embedding-3-small` | `OPENAI_API_KEY` |

- Endpoint `generativelanguage.googleapis.com` → it's the Gemini key (`GEMINI_API_KEY`).
- Endpoint `api.openai.com` → it's the OpenAI key (`OPENAI_API_KEY`).

When suggesting any run command to Santiago, always prefix with this key/model split so he knows which key to check if the run fails.

## Commands

```bash
# Run tests (see docs/testing.md for details)
../venv/Scripts/python -m pytest tests/ -v

# Watch mode — loop continuously (Ctrl+C to stop)
python -m src.main --watch
python -m src.main --watch --dry-run --verbose

# One-shot: run both template injection + AI extraction (default)
python -m src.main
python -m src.main --dry-run --verbose

# One-shot: run only template injection
python -m src.main --inject-templates

# One-shot: run only AI extraction pipeline
python -m src.main --sync

# One-shot: run the Done-task archive sweep (mirrors the weekly Sunday Lambda job)
python -m src.main --archive
python -m src.main --archive --dry-run --verbose

# Manual run targeting one specific Meeting Notes DB (skips Org Chart discovery)
python -m src.main --sync --db-id <notion_db_id> --verbose

# Manual run with per-stage model overrides (provider auto-detected from prefix)
python -m src.main --sync \
    --correction-model gemini-3-flash-preview \
    --extraction-model gemini-3-flash-preview \
    --classification-model gpt-5-mini

# Pause / resume the Lambda extraction cron without redeploying
./scripts/pause-lambda.sh
./scripts/resume-lambda.sh

# Lint
../venv/Scripts/python -m ruff check src/ tests/

# Deploy Lambda — code-only changes (fast, ~10 seconds)
./scripts/quick-deploy.sh

# Deploy Lambda — full (infra/dependency changes, uses SAM + CloudFormation)
./scripts/deploy.sh
```

## Deploying to AWS Lambda

Two deploy scripts in `scripts/`:

| Script | When to use | What it does | Speed |
|--------|------------|--------------|-------|
| `quick-deploy.sh` | **Code-only changes** (default) | Copies `src/` into the SAM build dir, zips, uploads directly via `aws lambda update-function-code` | ~10 seconds |
| `deploy.sh` | Dependency or infrastructure changes (`requirements.txt`, `template.yaml`) | Full `sam build` + `sam deploy` with CloudFormation | ~2-3 minutes |

**Always use `quick-deploy.sh`** unless you changed dependencies or `template.yaml`. It requires a prior `sam build` (the `.aws-sam/build/` directory must exist with dependencies installed).

**Important:** The script does `rm -rf` then `cp -r` to replace the src directory. A plain `cp -r src/ dest/src/` does NOT overwrite files on Windows/Git Bash — it silently keeps stale code. Always verify a deploy worked by checking that the `CodeSha256` in the output changed.

SAM CLI path (Windows): `C:/Users/Santiago Cuadra/AppData/Local/Packages/PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0/LocalCache/local-packages/Python313/Scripts/sam.exe`

For `sam build`, the venv Python must be on PATH:
```bash
PATH="C:/Users/Santiago Cuadra/vscode_projects/venv/Scripts:$PATH" sam build
```

### Pausing the extraction cron

`scripts/pause-lambda.sh` disables the EventBridge schedule rule (`*-NzymeFunctionScheduledExtraction-*`) so the 1-minute extraction cron stops firing. The webhook (template injection) keeps working. Use this when you want to run the pipeline manually from the CLI without competing with Lambda for the same meetings.

- `./scripts/pause-lambda.sh` — disables the rule (instant, no redeploy)
- `./scripts/resume-lambda.sh` — re-enables the rule

While paused, live AWS state drifts from `template.yaml` (which still says `Enabled: true`). The next full `./scripts/deploy.sh` resets the rule to enabled, so always resume before deploying — or know that deploy will resume it for you.

## Manual CLI runs (debug / experiment mode)

`python -m src.main --sync` accepts overrides for fine-grained control. Useful when you've paused the Lambda and want to step through a fixed set of meetings.

| Flag | Effect |
|------|--------|
| `--db-id <notion_db_id>` | Process exactly one Meeting Notes DB (skips Org Chart discovery). Equivalent to setting `MEETING_NOTES_DB_ID` in `.env`. |
| `--correction-model <model>` | Override the model for transcript correction. |
| `--extraction-model <model>` | Override the model for task extraction. |
| `--classification-model <model>` | Override the model for task classification. |
| `--verbose` | DEBUG-level logs for `src.*` (third-party loggers stay at WARNING via `setup_logging`). Dumps the corrected transcript and raw LLM response payloads. |

**Provider auto-detection**: model names starting with `gemini-` route through `GEMINI_API_KEY` + Gemini base URL; everything else routes through `OPENAI_API_KEY` + the OpenAI endpoint. So `--classification-model gemini-3-flash-preview` or `--correction-model gpt-5-mini` Just Works.

Default INFO output reads as a clear pipeline trace: per-meeting framing, then one log line per stage with model + elapsed + token counts. Embedding model (`text-embedding-3-small`) and fundraising-summary model are not exposed as flags — those are not interesting for the dev/test loop.

## Architecture

### Per-member Meeting Notes DBs (Org Chart-driven discovery)

Each active team member has their **own** Meeting Notes database. The same meeting can — and often will — appear in multiple DBs (each attendee's personal notes). The pipeline polls every per-member DB on every cycle.

The registry is built from the Nzyme **Org Chart** DB: every active row has a **`Meeting Notes DB`** URL property pointing at that member's database. Joiners and leavers are managed entirely in Notion — no code change or redeploy:

- **Add a member:** create their DB, set `Active=true` on their Org Chart row, paste the DB URL into `Meeting Notes DB`.
- **Remove a member:** flip `Active=false` (or clear the URL).
- **Pick the extraction style per member:** the `Auto-extract Tasks` checkbox on each Org Chart row decides which path runs for that member's meetings. `true` → full transcript pipeline. `false` (or unset — defaults false in the MVP deploy) → literal-notes path: a single LIGHT LLM call (gpt-5-mini) on the page's notes content, with a Notion-hosted prompt that instructs the model to keep titles verbatim and resolve assignees from inline @mentions / parenthesised initials / first-name match. Both paths run the same classifier afterwards, with the standard creator fallback if no assignee was resolved.

`MEETING_NOTES_DB_ID` is now optional — when set, it **overrides** discovery and polls only that one DB. Keep it unset in production. Useful for tests and single-DB dev runs.

`src/meeting_db_registry.py` owns discovery (`discover_meeting_dbs`, `load_registry`, `find_owner_for_page`). Webhook setup remains per-DB in Notion: each new member needs their own automation pointing at the API Gateway URL.

### Unified pipeline flow (transcript-first, multi-DB)

For every meeting, the pipeline first looks up the page's owner via the Org Chart-driven registry. The owner's `Auto-extract Tasks` flag is the top-level switch:

- `Auto-extract Tasks = true` → transcript-first path. With a `meeting_notes` block + transcript → 3-LLM transcript pipeline. Otherwise the legacy AI-extractor on the notes fallback.
- `Auto-extract Tasks = false` → **literal-notes path**: a single LIGHT LLM call (gpt-5-mini) on the page's `## Action Items` bullets. The prompt (Notion-hosted at `LITERAL_NOTES_EXTRACTION_PROMPT_PAGE_ID`) instructs the model to keep each bullet's title as the author typed it and to split assignee names into `internal_assignees` (Kibo) and `external_assignees` (outsiders). Then the **same classifier** as the transcript path adds category, parent, deal_page_id, and resolves `assignee_id`. If the model returns zero tasks, log a warning and mark the page Processed (no fallback to the transcript pipeline).

```
pipeline.run_sync():
  0. template_injector → inject `## Action Items` + `## Notes` headings across every DB (if enabled)
  Discover registry from Org Chart (one entry per active member with the URL set, carrying owner.auto_extract_tasks)
  Load shared context once (hierarchy, categories, users, deals, terminology, org chart, classifier prompt)
  for each member DB:
    Build per-DB fingerprint set from already-processed meetings
    for each unprocessed meeting:
      a. Skip if (db_id, normalized_title, date) already in this DB's fingerprint set
      b. Decide path:
         IF owner.auto_extract_tasks is False:
            → literal-notes path: 1 LIGHT LLM call on the page's notes content
              with the Notion-hosted prompt (titles verbatim, internal/external split),
              then the standard classifier. If the model returns 0 tasks → warn + mark processed.
         ELIF transcript exists:
            → 3-LLM transcript pipeline (correct → extract → classify)
         ELSE:
            → notes fallback (legacy AIExtractor on page content)
      c. Semantic dedup → filter duplicates (workspace-wide; catches cross-DB task duplicates)
      d. Assignee fallback → default to meeting creator on every path (literal, transcript, AI notes)
      e. team_writer → create task pages in Team Task Tracker
      f. Mark meeting page as Processed
```

CLI override (debugging only, ignores the per-member flag for the run):
- `--auto-extract-tasks` — force every page onto the transcript pipeline
- `--no-auto-extract-tasks` — force every page onto the literal-notes path

**Why the fingerprint includes `db_id`:** under the personal-notes model, two team members capturing the same meeting (same title + date in their own DBs) MUST be processed independently — each captures different commitments from their perspective. Workspace-wide semantic dedup on the Team Task Tracker side catches genuine task duplicates across DBs. Within a single DB, Notion's `(1)` / `(2)` suffix on duplicate pages still collapses correctly.

`run_sync_for_page()` (Lambda single-page entry point) derives the owning DB from the page's `parent.database_id`, so it works for any per-member DB without prior knowledge.

### Webhook / Lambda mode (serverless)

```
Notion automation (page created in any per-member DB) → API Gateway → Lambda: webhook_handler
  → load registry, validate page's parent DB is in the registry
  → set Date = page.created_time (with hour)
  → inject template INSIDE the page's meeting_notes block, set "Template Injected" = true (if enabled)

CloudWatch cron (1 min) → Lambda: extraction_handler
  → load registry from Org Chart
  → for each member DB: query Processed=false AND last_edited_time < now-3min
  → for each ready page: run_sync_for_page (transcript-first, notes fallback)
```

**Template page (not DB template):** `MEETING_TEMPLATE_PAGE_ID` points at a normal Notion page (the "Generic Template" under the Templates folder). The injector reads its blocks and appends them to `meeting_notes.children.notes_block_id` on the target page so they render inside the AI Meeting block's notes section. If the meeting_notes block hasn't been attached yet (race with page creation), the injector retries briefly and otherwise leaves it for the next cron tick.

The registry is reloaded once per cron tick (one extra Notion query per minute). No caching beyond the in-memory result for that tick — keeps joiner/leaver changes immediate.

Key design: prompts, hierarchy, category options, deal context, terminology, and org chart are all read dynamically from Notion at runtime. Schema changes in Notion require no code changes.

**GCal attendees (CLI + Lambda):** Google Calendar attendee lookup uses a **service account with Domain-Wide Delegation** (scope: `https://www.googleapis.com/auth/calendar`). The SA impersonates the meeting page's Notion creator per-meeting to search that user's primary calendar, falling back to `GCAL_DELEGATED_USER_DEFAULT` when the creator's email can't be resolved. Names are resolved by matching attendee emails against the **Email** property on the Notion Org Chart DB (the People API / directory scope is not authorized, so this is the single source of truth for email → name).

- **Local dev:** set `GOOGLE_SERVICE_ACCOUNT_FILE=.secrets/service-account.json` (gitignored) + `GCAL_DELEGATED_USER_DEFAULT` in `.env`.
- **Lambda:** create a Secrets Manager secret whose value is the raw `service-account.json` contents, then pass its ARN as the `GoogleServiceAccountSecretArn` SAM parameter at deploy time. The SAM template conditionally grants `secretsmanager:GetSecretValue` scoped to that ARN.
- **Org Chart prerequisite:** each active row must have the **Email** property populated for email-based matching to work. Rows without email fall back to name-substring matching (the pre-existing behavior).

**Notion API version:** The entire project uses API version `2026-03-11` (supports `meeting_notes` blocks).

### Deal-aware extraction (Investment Team)

When `DEAL_WORKPLANS_DB_ID` is set, the pipeline loads deal context from the Deal Workplans database:
- Discovers each deal's inline Workplan and Action Items databases
- Loads active workstreams (name, status, type, adviser) per deal
- Injects deal context into the AI prompt via `{{DEAL_CONTEXT}}`
- The AI can set `deal_page_id` on extracted tasks to populate the `Deal Relation` property
- Meeting titles are scanned for deal name matches, adding hints to the user prompt
- Hierarchy is exposed to depth 4 (categories → sub-categories → entities → deals)

This is fully optional — when `DEAL_WORKPLANS_DB_ID` is not set, the pipeline behaves exactly as before.

### Semantic dedup (embedding-based)

The pipeline uses a three-layer dedup strategy:
1. **Meeting-level**: fingerprint `(normalized_title|date)` prevents re-processing the same meeting
2. **Writer exact match**: `title.strip().lower()` catches identical task titles
3. **Semantic embeddings**: OpenAI `text-embedding-3-small` compares new task titles against all existing titles by cosine similarity. Catches semantically equivalent tasks even when worded differently or in different languages (e.g., "revisar informe FDD" ≈ "send FDD comments").

Threshold is configurable via `SEMANTIC_DEDUP_THRESHOLD` (default 0.80). When the embeddings API is unavailable (e.g., non-OpenAI provider), semantic dedup degrades gracefully to layers 1-2 only.

## Key Files

| File | Responsibility |
|------|---------------|
| `src/pipeline.py` | Orchestrates the unified sync cycle (transcript-first, notes fallback, multi-DB loop) |
| `src/meeting_db_registry.py` | Discovers per-member Meeting Notes DBs from the Org Chart's `Meeting Notes DB` URL property |
| `src/ai_extractor.py` | OpenAI prompt + function calling for notes-based extraction (fallback path) |
| `src/hierarchy_loader.py` | Queries tracker DB, builds parent-child tree (depth 4, smart pruning) |
| `src/deal_context.py` | Loads deal workplan context from Deal Workplans DB |
| `src/literal_notes_extractor.py` | LLM extractor for the `Auto-extract Tasks=False` path: gpt-5-mini call with the Notion-hosted `LITERAL_NOTES_EXTRACTION_PROMPT_PAGE_ID` prompt; output feeds into the same classifier the transcript path uses |
| `src/template_injector.py` | Injects `## Action Items` + `## Notes` headings into new meeting pages |
| `src/sources/single_source.py` | Polls Meeting Notes DB with buffer delay, fetches page content |
| `src/tracker/team_writer.py` | Maps task dicts to Notion properties, creates pages |
| `src/utils/blocks_to_text.py` | Converts Notion block arrays to plain text |
| `src/config.py` | Pydantic config from env vars |
| `src/notion_client_wrapper.py` | Rate-limited (3 req/s), auto-retry Notion API client (v2026-03-11) |
| `src/webhook/handler.py` | Processes Notion automation webhook payloads |
| `src/webhook/lambda_handler.py` | Unified AWS Lambda entry point (routes webhook + cron) |
| `template.yaml` | SAM template for AWS infrastructure |

## Transcript Pipeline

The transcript pipeline modules (`src/transcript_pipeline/`) handle transcript-based task extraction. These modules are integrated into the main pipeline orchestrator (`src/pipeline.py`) and are also available as a standalone CLI for diagnostics.

### Pipeline steps

1. **Fetch** raw transcript from Notion `meeting_notes` block
2. **Load context** from Terminology DB + Org Chart DB + Google Calendar attendees
3. **Correct** transcript via LLM (fix domain terms, speaker identification)
4. **Extract** action items via LLM (commitment-aware prompting)
5. **Classify** tasks via LLM (category, parent, assignee, deal mapping)
6. **Write** classified tasks to Team Task Tracker (via `TeamTaskTrackerWriter`)

### CLI (diagnostics + manual runs)

```bash
# Full pipeline: correct → extract → classify → write
python -m src.transcript_pipeline <page_id> --write
python -m src.transcript_pipeline <page_id> --write --dry-run

# Diagnostic: just correct the transcript
python -m src.transcript_pipeline <page_id> --correct

# Diagnostic: correct + extract (no write)
python -m src.transcript_pipeline <page_id> --extract

# Override model
python -m src.transcript_pipeline <page_id> --write --model gpt-5-mini

# Force OpenAI endpoint (when OPENAI_BASE_URL points to Gemini)
python -m src.transcript_pipeline <page_id> --write --openai

# Test GCal attendee lookup only
python -m src.transcript_pipeline <page_id> --gcal
```

**`--write` routes through `pipeline.run_sync_for_page()`** — the unified pipeline with dedup, classification, and all post-processing. Diagnostic flags (`--correct`, `--extract`) run standalone without the full pipeline.

### Google Calendar integration

GCal is the **authoritative attendee source** when a matching calendar event exists. The pipeline searches GCal by cleaned meeting title (ISO datetime suffix stripped via `strip_title_datetime()`), and when found, **replaces** Notion's attendee list entirely. Falls back to Notion attendees only when no GCal event is found.

**Auth:** Google Cloud **service account** with Domain-Wide Delegation, scope `https://www.googleapis.com/auth/calendar`. The SA impersonates the Notion page creator per-meeting (resolved via `client.users.retrieve`), falling back to `GCAL_DELEGATED_USER_DEFAULT`. Works identically in CLI and Lambda.

**Name resolution:** Calendar event attendees come back with emails only (no `displayName`), and the `directory.readonly` scope is not authorized. Names are resolved by matching attendee emails against the **Email** property on the Notion Org Chart DB. External attendees (non-Kibo emails) pass through with email-only — the LLM handles them as "external guests."

**Credentials:**
- **Local:** `GOOGLE_SERVICE_ACCOUNT_FILE=.secrets/service-account.json` (gitignored).
- **Lambda:** `GOOGLE_SERVICE_ACCOUNT_SECRET_ARN` pointing at a Secrets Manager secret holding the JSON. SAM template grants `secretsmanager:GetSecretValue` conditionally (only if the ARN parameter is set).

**Known issue:**
- **Date fallback**: The Notion "Date" property is empty for some meeting pages — currently falls back to `created_time` which may not match the actual meeting time.

### Task extraction

The `--extract` flag runs a second LLM call on the corrected transcript to extract action items. Uses commitment-aware prompting (hard/conditional/soft/group commitments), org chart context for role-based assignee resolution, and meeting metadata for relative date resolution. Outputs: title, assignee, priority, due date, confidence level, and supporting transcript quote.

### Key files

| File | Responsibility |
|------|---------------|
| `src/transcript_pipeline/__main__.py` | CLI entry point (diagnostics + `--write` routes through pipeline) |
| `src/transcript_pipeline/fetch_transcript.py` | Find meeting_notes block, extract transcript, resolve attendees, page metadata |
| `src/transcript_pipeline/context_loader.py` | Load terminology dictionary + org chart from Notion DBs |
| `src/transcript_pipeline/transcript_corrector.py` | LLM-based transcript correction (OpenAI) |
| `src/transcript_pipeline/task_extractor.py` | LLM-based action item extraction from corrected transcript |
| `src/transcript_pipeline/task_classifier.py` | LLM-based task classification (category, parent, assignee, deal) |
| `src/transcript_pipeline/gcal_attendees.py` | Google Calendar lookup via service account (DWD) — per-meeting impersonation, emails-only; names resolved downstream via Org Chart |

### Notion databases (env vars in `.env`)

- **Terminology DB** (`TERMINOLOGY_DB_ID`): Term, Phonetic Variants, Category, Context, Active
- **Org Chart DB** (`ORG_CHART_DB_ID`): Name, **Email** (required for GCal attendee matching), Role, Department, Seniority, Typical Topics, Active

### Logfire

All LLM calls (correction, extraction, classification) are tracked via logfire. Token usage is automatic via `logfire.instrument_openai()`.

## Fundraising → Affinity branch (opt-in)

When a meeting is tagged `Meeting type = Fundraising`, the pipeline mirrors a meeting note to Affinity's **Nzyme - LP Funnel** list (id `168609`) after the primary tracker write. Off by default; enable with `FUNDRAISING_BRANCH_ENABLED=true`.

**What it does (current, note-only mode):**
1. Matches the meeting to **all** LPs via attendee emails → Affinity persons → organization → list entry. Multi-LP meetings (e.g. cross-LP intros) get the same note posted to every match.
2. Composes the note body from the **user's `## Notes`** content (inside the meeting_notes block) + Notion's auto-populated **`AI Summary`** page property — no LLM call here.
3. Posts an HTML meeting note attached to each matched LP's **opportunity** (title + composed body + Notion backlink). When neither user notes nor the AI Summary property is populated, the note degrades to title + backlink only.

Field updates (`Nzyme next step`, `Follow Up Date`, `OWNER`, `DETAILS`) are **deferred** — `write_next_step_to_lp` and `_resolve_owner` remain in the module and can be re-wired in `src/fundraising/__init__.py` once the note-only flow is validated. `next_step_summarizer.py` likewise stays on disk (unimported) for the same reason.

**Email source:** GCal attendees (via the service-account secret on Lambda) are the primary source. The manual **`LP Emails`** rich-text property on the meeting page is merged into the attendee list in `pipeline.py` as a belt-and-braces fallback — useful for meetings GCal doesn't surface or where LP emails were not on the calendar invite. Internal Kibo emails and emails not matching any LP are silently ignored by the matcher.

**Multi-DB behavior:** if two Kibo members independently capture the same LP meeting in their respective DBs, both pages fire and Affinity gets two notes on the same opportunity. That's intentional — each member's notes capture distinct insights and are independently valuable on the LP timeline.

**Visibility (CloudWatch only — no Notion property):** every fundraising-branch run emits a single structured log line at the end:

```
fundraising outcome: page=<16-char-prefix> db_owner=<member> status=<enum> detail=<text>
```

Possible status values: `Posted`, `Skipped: no external attendees`, `Skipped: no LP match`, `Failed: API error`. Failures log at `ERROR`, others at `INFO`. To list every fundraising attempt over a window, query CloudWatch Logs Insights with `filter @message like /fundraising outcome:/`.

**Retries:** `AffinityClient` retries transient failures (429, 5xx) up to 5 times with exponential backoff within the same Lambda invocation (~30 s of resilience). Beyond that, the failure is logged loudly and the page is left at `Processed=true`; manual recovery is to clear `Processed` on the page so the next cron tick re-runs the full pipeline.

**Idempotency**: not implemented. Manual re-trigger of an already-posted page would create a duplicate note. Acceptable per design call.

**Key files:**
- `src/affinity_client.py` — V1 REST wrapper (Basic auth, rate-limited, 5 retries)
- `src/fundraising/__init__.py` — orchestrator `write_to_affinity`; returns a `FundraisingOutcome`. Never raises.
- `src/fundraising/outcome.py` — `FundraisingStatus` enum + `FundraisingOutcome` dataclass
- `src/fundraising/lp_matcher.py` — `resolve_lp_list_entries` returns *all* matched list_entry_ids (multi-LP meetings post to every match)
- `src/fundraising/next_step_summarizer.py` — enum-constrained LLM call (produces note summary)
- `src/fundraising/affinity_writer.py` — `post_meeting_note_to_lps` loops over opportunity ids and returns `(posted, failed)`
- `src/fundraising/data/kibo_user_map.json` — static Notion/email ↔ Affinity user-id map; only needed once OWNER field writes are re-enabled

**Env vars:** `FUNDRAISING_BRANCH_ENABLED`, `AFFINITY_API_KEY`, `AFFINITY_LP_FUNNEL_LIST_ID` (default 168609), `KIBO_USER_MAP_PATH` (optional override).

## Team Task Tracker: Parent Items vs Extracted Tasks

The tracker contains two types of items — never delete or archive parent items.

- **Parent/hierarchy items** (e.g. "Investing", "Nzyme Operations", "Value Creation") carry **`Priority = [DETAILS INSIDE]`**. They form the organizational structure used by `hierarchy_loader.py` to give the classifier context for categorizing tasks and assigning `parent_task_id`. Deleting them breaks the hierarchy.
- **Extracted tasks** are created by the AI pipeline with `Priority` set to `High`/`Medium`/`Low` (or unset).

The `[DETAILS INSIDE]` marker is the single source of truth for the hierarchy/task split. It scopes the classifier (sees architecture only) and the deduper (sees tasks only — `_load_existing_titles` excludes architecture rows so new task titles aren't compared against category labels).

To add a new architecture row in Notion: create the page and set `Priority = [DETAILS INSIDE]`. No code change needed.

**Meeting → Task linkage (one-way):** Each per-member Meeting Notes DB has a one-way `Task - Relation` property pointing into the Team Task Tracker. After tasks are written, the pipeline patches the source meeting page's `Task - Relation` to include the new task IDs (merging with anything already there). The reverse property `Meeting - Relation` on the tracker side was removed in the multi-DB migration — a single relation can't span N member DBs.

## Weekly Done-task archive sweep

Done tasks are swept out of the live Team Task Tracker once a week and copied into a separate **Team Task Tracker — Archive** DB (sibling page; same parent as the live tracker).

- **Schedule:** Sunday 06:00 UTC (`cron(0 6 ? * SUN *)`) — declared as the `WeeklyArchive` event on `NzymeFunction` in `template.yaml`. Triggers Lambda with input `{"job":"weekly_archive"}` so the unified handler routes to `_handle_weekly_archive`.
- **Filter:** `Status = Done` AND `last_edited_time` older than 5 days.
- **Behavior:** copy properties to the archive DB → soft-delete (`archive_page`) the original. Re-runs are idempotent via the `Source Page ID` rich-text marker on each archive copy.
- **Hierarchy relations dropped on copy:** `Parent item` and `Sub-item` are skipped; the archive is a flat record of completed work and would otherwise produce dangling relations as parents are also archived. The cross-DB `Deal Relation` is preserved. (Meeting → task linkage now lives on the Meeting Notes side as `Task - Relation`, so archived tasks don't carry meeting backlinks — reverse-link via Notion if needed.)
- **Env var:** `TASK_ARCHIVE_DB_ID`. Unset → the weekly job logs a warning and exits as a no-op.
- **Manual trigger (testing):** `python -m src.main --archive` runs the sweep once locally.
- **Code:** `_archive_done_tasks` + `_copy_property_for_write` + `_build_archive_payload` + `_load_archived_source_ids` in `src/pipeline.py`.

## Conventions

- **Python 3.11+** with type hints (`from __future__ import annotations`)
- **Pydantic** for config validation
- **pytest** for testing, all unit tests use mocked Notion/OpenAI clients
- Tests live in `tests/` mirroring src structure (e.g., `test_pipeline.py` tests `src/pipeline.py`)
- `conftest.py` provides a `mock_client` fixture for NotionClientWrapper
- No multi-source/registry mode — single Meeting Notes DB only

## Documentation

When working on specific areas, read the relevant doc:

- **Testing / QA**: Read `docs/testing.md` — unit test patterns, integration testing workflow with Notion MCP
- **Notion integration**: Read `docs/notion-schema.md` — DB schemas, property mappings, IDs, MCP tool reference
- **Architecture deep dive**: Read `docs/architecture.md` — pipeline details, component responsibilities, error handling, prompt construction

**Keep docs in sync:** After any major change (new module, changed pipeline flow, new/renamed Notion properties, modified error handling, new test patterns), update the relevant doc file above. Review which docs are affected before considering the work complete.
