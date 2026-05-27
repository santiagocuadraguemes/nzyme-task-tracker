# CLAUDE.md

## Project

Nzyme is an AI-driven sync engine that extracts action items from Notion meeting notes and writes them to a Team Task Tracker database. Built for Kibo Ventures (PE/VC fund, ~10-20 people). Meeting notes may be in English, Spanish, or mixed.

For pipeline detail, see [docs/architecture.md](docs/architecture.md).

## Setup

```bash
# Shared venv lives one directory up (../venv/)
cd ../  # from project root
python -m venv venv
source venv/Scripts/activate  # Windows/Git Bash
pip install -e "nzyme-task-tracker/.[dev]"
```

Environment: copy `.env.example` to `.env` and fill in credentials. Never commit `.env` or `.secrets/`.

## LLM keys — two-key setup

Nzyme uses **two separate API keys** to balance cost and quality. When a run fails with an "API key not valid" error, check which endpoint was called to know which key to rotate.

| Stage | Model | Env var |
|-------|-------|---------|
| Task extraction (heavy) — single merged correction + extraction call | `gemini-3-flash-preview` (Gemini 3 Flash Preview — **not** `gemini-2.5-flash`) | `GEMINI_API_KEY` |
| Task classification (light) | `gpt-5-mini` | `OPENAI_API_KEY` |
| Literal-notes extraction (light) — `Auto-extract Tasks = false` path | `gpt-5-mini` | `OPENAI_API_KEY` |
| Semantic dedup embeddings (light) | `text-embedding-3-small` | `OPENAI_API_KEY` |

- Endpoint `generativelanguage.googleapis.com` → Gemini key (`GEMINI_API_KEY`).
- Endpoint `api.openai.com` → OpenAI key (`OPENAI_API_KEY`).

**When suggesting any run command, always prefix with this key/model split so Santiago knows which key to check if the run fails.**

The transcript path runs a single merged Gemini call (correction + extraction inline; ~60-70% cheaper than a 2-call flow). See [docs/transcript-pipeline.md](docs/transcript-pipeline.md) for internals, schema candidates, and the cost-optimisation harness.

## Common commands

```bash
# Tests
../venv/Scripts/python -m pytest tests/ -v

# Watch mode — loop continuously (Ctrl+C to stop)
python -m src.main --watch
python -m src.main --watch --dry-run --verbose

# One-shot: run both template injection + AI extraction (default)
python -m src.main

# One-shot: run only template injection / only sync / only weekly archive sweep
python -m src.main --inject-templates
python -m src.main --sync
python -m src.main --archive

# Lint
../venv/Scripts/python -m ruff check src/ tests/

# Deploy Lambda — code-only (fast) / full (deps or template.yaml changed)
./scripts/quick-deploy.sh
./scripts/deploy.sh

# Pause / resume the Lambda extraction cron without redeploying
./scripts/pause-lambda.sh
./scripts/resume-lambda.sh
```

For per-stage model overrides, `--db-id`, the manual debug workflow, and deploy mechanics, see [docs/deployment.md](docs/deployment.md).

## Hard rules

- **Never manually delete `Priority = [DETAILS INSIDE]` rows in the Team Task Tracker.** Those are architecture/hierarchy nodes, not tasks. Deleting them breaks the classifier's parent-assignment context. The marker is also the single source of truth that splits the classifier (sees architecture only) from the deduper (sees tasks only). The Supabase canonical (`public.hierarchy_rows` in the Neo project) is the source of truth for these rows' titles and parent links; `tracker_applier_sync` (daily 07:00 Madrid) reconciles them: inactive (`active=false`, `deleted_at IS NULL`) → soft-archive the title as `(archived) X` (row stays so the classifier still has parent context); **tombstoned** (you deleted the Hierarchy DB page in Notion → `deleted_at IS NOT NULL`) → the matching `[DETAILS INSIDE]` row is Notion-archived and the canonical mapping is cleared (children's `Parent item` to the now-archived row is also cleared). The applier is the only thing allowed to remove these rows. Edit names and parents in the Hierarchy DB, not in the Tracker.
- **Never edit member-DB option lists for `Macro Work Block` or `Detail` directly.** Both flow from canonicals: `Macro Work Block` from Hierarchy DB Tier 0 → `hierarchy_rows`; `Detail` from the `Detail Options` Settings DB → `detail_rows`. The daily `hierarchy_sync` cron PATCHes each member DB; manual edits are overwritten on the next run.
- **`External Org` is no longer fanned out to member DBs** (changed 2026-05-27). Instead `external_org_db_sync` mirrors `public."ReportingNz_deals"` into a single `🏢 External Orgs` Settings DB (`EXTERNAL_ORGS_DB_ID`) — one row per deal, keyed by a `Deal ID` text property; rows are created for the 4 tracked stages and never deleted (Stage is kept current). Edit deals in Affinity/Supabase, not in that DB. The legacy per-member `External Org` **select** column is now frozen/manual (still read by the extraction pipeline + topic-mirror, but not auto-synced). The old `external_org_option_mappings` and `deal_hierarchy_links` Supabase tables were dropped.
- **Meeting → task is a one-way relation** owned by each per-member Meeting Notes DB via its `Task - Relation` property. The reverse `Meeting - Relation` on the tracker was removed when DBs went per-member — a single relation can't span N member DBs.
- **No @mentions in Notion content** — `@`-mention tags trigger notifications.
- **No silent failures during dev/testing** — let errors crash visibly. Don't `except: pass`.
- **Never run the pipeline yourself** when Santiago asks how — give him the command, but he runs it.
- **Two parent DBs to be careful with**: live Team Task Tracker (`Status != Done`) and a sibling Team Task Tracker — Archive DB. The Sunday Lambda copies Done rows older than 5 days into the archive and soft-deletes the original. See [docs/architecture.md](docs/architecture.md) (Done-task archive sweep) for details.

## Conventions

- **Python 3.11+** with type hints (`from __future__ import annotations`)
- **Pydantic** for config validation
- **pytest** for testing; all unit tests use mocked Notion/OpenAI clients
- Tests live in `tests/` mirroring src structure (e.g., `test_pipeline.py` tests `src/pipeline.py`)
- `conftest.py` provides a `mock_client` fixture for `NotionClientWrapper`
- **API version `2026-03-11`** across the project (required for `meeting_notes` blocks)

## Documentation

When working on a specific area, read the relevant doc first:

| Doc | When to read |
|-----|--------------|
| [docs/architecture.md](docs/architecture.md) | Pipeline orchestrator, per-member DBs, components, error handling, webhook/Lambda mode, weekly Done-task archive sweep |
| [docs/deployment.md](docs/deployment.md) | Deploy scripts (quick vs full), SAM, pause/resume cron, manual CLI runs with per-stage model overrides + provider auto-detection |
| [docs/transcript-pipeline.md](docs/transcript-pipeline.md) | Merged vs legacy 2-call extraction, Gemini caching + `response_schema`, deterministic transcript cleanup, shadow-diff validation, output-token harness, GCal integration, transcript CLI |
| [docs/fundraising-affinity.md](docs/fundraising-affinity.md) | Fundraising → Affinity LP Funnel branch (note-only mode, LP matching, CloudWatch visibility) |
| [docs/meeting-mirrors.md](docs/meeting-mirrors.md) | Topic-mirror clones (template_id mechanism, cross-DB dedup, contributor merge, Owner resolution) |
| [docs/notion-schema.md](docs/notion-schema.md) | DB schemas, property mappings, IDs, hierarchy structure, MCP tool reference |
| [docs/testing.md](docs/testing.md) | Unit test patterns, integration testing workflow with Notion MCP |

**Keep docs in sync:** after any major change (new module, changed pipeline flow, new/renamed Notion properties, modified error handling, new test patterns), update the relevant doc file above. Review which docs are affected before considering the work complete.
