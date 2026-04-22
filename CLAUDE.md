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
| Fundraising next-step summary (light) | `gpt-5-mini` | `OPENAI_API_KEY` |
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

When a meeting is tagged `Meeting type = Fundraising`, the pipeline mirrors a short summary of the extracted next step to Affinity's **Nzyme - LP Funnel** list (id `168609`) after the primary tracker write. Off by default; enable with `FUNDRAISING_BRANCH_ENABLED=true`.

**What it does (current, note-only mode):**
1. Matches the meeting to exactly one LP via attendee emails → Affinity persons → organization → list entry. On 0 or >1 matches, skips.
2. LLM-summarises the classified tasks into a short next-step text.
3. Posts an HTML meeting note attached to the matched LP's **opportunity** (title + summary + Notion backlink).

Field updates (`Nzyme next step`, `Follow Up Date`, `OWNER`, `DETAILS`) are **deferred** — `write_next_step_to_lp` and `_resolve_owner` remain in the module and can be re-wired in `src/fundraising/__init__.py` once the note-only flow is validated.

**Email source (manual):** GCal isn't yet available to Lambda, so LPs are matched from a manual **`LP Emails`** rich-text property on the meeting page (comma/semicolon-separated). These emails are merged into the attendee list in `pipeline.py` before the fundraising branch runs; internal Kibo emails and emails not matching any LP are silently ignored by the matcher. This also unblocks Lambda-mode fundraising runs.

**Soft-fail**: Affinity errors are logged but never block the tracker write.

**Key files:**
- `src/affinity_client.py` — V1 REST wrapper (Basic auth, rate-limited)
- `src/fundraising/__init__.py` — orchestrator `write_to_affinity` (currently wired to `post_meeting_note_to_lp`)
- `src/fundraising/lp_matcher.py` — one-LP-per-meeting resolution (logs per-email match/ignore)
- `src/fundraising/next_step_summarizer.py` — enum-constrained LLM call (produces note summary)
- `src/fundraising/affinity_writer.py` — `post_meeting_note_to_lp` (note-only, attaches via `opportunity_ids`); `write_next_step_to_lp` kept for future field-write re-enable
- `src/fundraising/data/kibo_user_map.json` — static Notion/email ↔ Affinity user-id map; only needed once OWNER field writes are re-enabled

**Env vars:** `FUNDRAISING_BRANCH_ENABLED`, `AFFINITY_API_KEY`, `AFFINITY_LP_FUNNEL_LIST_ID` (default 168609), `KIBO_USER_MAP_PATH` (optional override).

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
