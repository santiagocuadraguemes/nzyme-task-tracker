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

## Architecture

### Unified pipeline flow (transcript-first)

The pipeline processes every meeting transcript-first. If a meeting has a `meeting_notes` block (Notion AI recording), it uses the 3-LLM-call transcript path. Otherwise, it falls back to the notes-based extraction.

```
pipeline.run_sync() / run_sync_for_page():
  0. template_injector → inject "Your own notes" section (if enabled)
  then for each unprocessed meeting:
    1. Load shared context (hierarchy, categories, users, deals, terminology, org chart, classifier prompt)
    2. Check for meeting_notes block on page
    IF transcript exists:
      a. Resolve attendees (GCal → Notion → governance fallback)
      b. Correct transcript (LLM call 1: TranscriptCorrector)
      c. Extract tasks (LLM call 2: TaskExtractor)
      d. Classify tasks (LLM call 3: TaskClassifier)
    ELSE (notes fallback):
      a. Fetch page content as plain text
      b. AI extract + classify (1 LLM call: AIExtractor)
    3. Semantic dedup → filter duplicates
    4. Assignee fallback → default to meeting creator
    5. team_writer → create task pages in Team Task Tracker
    6. Mark meeting page as Processed
```

### Webhook / Lambda mode (serverless)

```
Notion automation (page created) → API Gateway → Lambda: webhook_handler
  → inject template, set "Template Injected" = true (if enabled)

CloudWatch cron (1 min) → Lambda: extraction_handler
  → query: Processed=false AND Date<=now AND last_edited_time < now-3min
  → for each ready page: run_sync_for_page (transcript-first, notes fallback)
```

Key design: prompts, hierarchy, category options, deal context, terminology, and org chart are all read dynamically from Notion at runtime. Schema changes in Notion require no code changes.

**GCal in Lambda:** Google Calendar attendee lookup requires OAuth credentials and is skipped in Lambda. Lambda falls back to Notion meeting_notes attendees or governance property. CLI mode uses GCal when available.

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

Threshold is configurable via `SEMANTIC_DEDUP_THRESHOLD` (default 0.85). When the embeddings API is unavailable (e.g., non-OpenAI provider), semantic dedup degrades gracefully to layers 1-2 only.

## Key Files

| File | Responsibility |
|------|---------------|
| `src/pipeline.py` | Orchestrates the unified sync cycle (transcript-first, notes fallback) |
| `src/ai_extractor.py` | OpenAI prompt + function calling for notes-based extraction (fallback path) |
| `src/hierarchy_loader.py` | Queries tracker DB, builds parent-child tree (depth 4, smart pruning) |
| `src/deal_context.py` | Loads deal workplan context from Deal Workplans DB |
| `src/template_injector.py` | Injects "Your own notes" section into new meeting pages |
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

Name resolution uses the **Google People API** (`listDirectoryPeople`) to fetch the full Workspace directory, building an `{email → full_name}` lookup. This is reliable across the whole org — no email-prefix guessing.

**Setup:** Google Cloud project with Calendar API + People API enabled + Desktop OAuth credentials (`credentials.json` → `token.json`). OAuth scopes: `calendar.readonly` + `directory.readonly`.

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
| `src/transcript_pipeline/gcal_attendees.py` | Google Calendar OAuth + attendee lookup + People API directory resolution |

### Notion databases (env vars in `.env`)

- **Terminology DB** (`TERMINOLOGY_DB_ID`): Term, Phonetic Variants, Category, Context, Active
- **Org Chart DB** (`ORG_CHART_DB_ID`): Name, Role, Department, Seniority, Typical Topics, Active

### Logfire

All LLM calls (correction, extraction, classification) are tracked via logfire. Token usage is automatic via `logfire.instrument_openai()`.

## Team Task Tracker: Parent Items vs Extracted Tasks

The tracker contains two types of items — never delete or archive parent items.

- **Parent/hierarchy items** (e.g. "Investing", "Nzyme Operations", "Value Creation") have **no "Meeting - Relation"** set. They form the organizational structure used by `hierarchy_loader.py` to give the AI context for categorizing tasks. Deleting them breaks the hierarchy.
- **Extracted tasks** are created by the AI pipeline and **always have "Meeting - Relation"** set (linking back to the source meeting page).

When cleaning up test data, only archive items that have a "Meeting - Relation" value.

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
