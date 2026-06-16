# CLAUDE.md

## Project

Nzyme is an AI-driven system that extracts action items from Notion meeting notes (English/Spanish/mixed) and writes them to a Team Task Tracker, built for Kibo Ventures (PE/VC fund, ~10-20 people).

**This repo runs the two jobs left after the lambda-split migration:** real-time **meeting-template injection** (webhook) and the **Notion → Supabase mirror** (Sync). They run as **two Lambda functions in one stack** (`nzyme-task-tracker`, company account `607081650195`), sharing this code package: `nzyme-webhook` (`webhook_handler`, API Gateway-triggered) and `nzyme-task-tracker` (`cron_handler`, schedule-triggered Sync — keeps that name for the heartbeat alarm). Both sit behind the **same** API Gateway, so the webhook URL (api-id `9g8txmxkef`) is fixed — **don't repoint the ~10 Notion automations**. Task **extraction** was carved out to the standalone `nzyme-task-extraction` project (cut over 2026-06-15); **meeting mirrors**, **fundraising**, and **housekeeping** are likewise their own Lambdas. See [docs/architecture-lambda-split.md](docs/architecture-lambda-split.md) for the full split and [docs/architecture.md](docs/architecture.md) for pipeline detail.

## Setup

```bash
# Shared venv lives one directory up (../venv/)
cd ../  # from project root
python -m venv venv
source venv/Scripts/activate  # Windows/Git Bash
pip install -e "nzyme-task-tracker/.[dev]"
```

Environment: copy `.env.example` to `.env` and fill in credentials. Never commit `.env` or `.secrets/`.

## LLM keys

This repo (template injection + Supabase sync) makes **no LLM calls** — it needs only the Notion token plus Supabase credentials. The two-key cost/quality split — Gemini 3 Flash Preview (`GEMINI_API_KEY`) for heavy extraction, `gpt-5-mini` / `text-embedding-3-small` (`OPENAI_API_KEY`) for classification, literal-notes, and dedup embeddings — moved with task extraction to **`nzyme-task-extraction`**; see that project's docs for the key/model split and the cost-optimisation harness.

## Common commands

```bash
# Tests
../venv/Scripts/python -m pytest tests/ -v

# Watch mode — loop continuously: inject templates every tick, Supabase sync on interval (Ctrl+C to stop)
python -m src.main --watch
python -m src.main --watch --dry-run --verbose

# One-shot: inject templates (default) / run only the Notion → Supabase mirror
python -m src.main
python -m src.main --inject-templates
python -m src.main --supabase-sync

# Lint
../venv/Scripts/python -m ruff check src/ tests/

# Deploy Lambda — code-only (fast) / full (deps or template.yaml changed)
./scripts/quick-deploy.sh
./scripts/deploy.sh
```

For `--db-id`, dry-run options, and deploy mechanics, see [docs/deployment.md](docs/deployment.md). The extraction cron (and its `pause-lambda.sh` / `resume-lambda.sh`) moved to `nzyme-task-extraction`.

## Hard rules

- **Never manually delete `Priority = [DETAILS INSIDE]` rows in the Team Task Tracker.** Those are architecture/hierarchy nodes, not tasks. Deleting them breaks the classifier's parent-assignment context. The marker is also the single source of truth that splits the classifier (sees architecture only) from the deduper (sees tasks only). The Supabase canonical (`public.hierarchy_rows` in the Neo project) is the source of truth for these rows' titles and parent links; `tracker_applier_sync` (daily 07:00 Madrid) reconciles them: inactive (`active=false`, `deleted_at IS NULL`) → soft-archive the title as `(archived) X` (row stays so the classifier still has parent context); **tombstoned** (you deleted the Hierarchy DB page in Notion → `deleted_at IS NOT NULL`) → the matching `[DETAILS INSIDE]` row is Notion-archived and the canonical mapping is cleared (children's `Parent item` to the now-archived row is also cleared). The applier is the only thing allowed to remove these rows. Edit names and parents in the Hierarchy DB, not in the Tracker.
- **Never edit member-DB option lists for `Macro Work Block` or `Detail` directly.** Both flow from canonicals: `Macro Work Block` from Hierarchy DB Tier 0 → `hierarchy_rows`; `Detail` from the `Detail Options` Settings DB → `detail_rows`. The daily `hierarchy_sync` cron PATCHes each member DB; manual edits are overwritten on the next run.
- **`External Org` is auto-synced from the deal pipeline — don't hand-edit it** (rebuilt 2026-06-02). Two daily sub-syncs drive it off `public."ReportingNz_deals"` (Affinity → Supabase). `deal_hierarchy_sync` writes one **Hierarchy DB** row per tracked deal, keyed by a `Deal ID` rich-text property on the Hierarchy DB: dealflow-stage deals (`DD phase` / `Working on a deal (significant effort)` / `Under analysis (team assigned, moderate effort)`) become `2. Workstream` children of **Dealflow - Main Opportunities**; `Portfolio` deals become `1. Project` children of **Value Creation for Portfolio**. Those rows cascade through `canonical_mirror_sync` → `tracker_applier_sync` into `[DETAILS INSIDE]` tracker nodes like any other hierarchy row. `external_org_applier_sync` then fans the same tracked deals out to the per-member `External Org` **select** (Portfolio→orange, dealflow→blue; ordered stage-priority then alpha; mapping table `external_org_option_mappings`). A deal leaving the tracked stages (or removed from Affinity) → its Hierarchy row goes `Active=false` (tracker node → `(archived) X`) and its member-DB option → `(archived) X`; re-entry un-archives in place. **Never hand-delete or edit a Hierarchy row carrying a `Deal ID`, and never edit member-DB `External Org` option lists** — edit deals in Affinity/Supabase. Hierarchy rows WITHOUT a `Deal ID` are hand-made and untouched by the sync. (The old single `🏢 External Orgs` Settings DB + `external_org_db_sync` were retired; `EXTERNAL_ORGS_DB_ID` is now dead config.)
- **Meeting → task is a one-way relation** owned by each per-member Meeting Notes DB via its `Task - Relation` property. The reverse `Meeting - Relation` on the tracker was removed when DBs went per-member — a single relation can't span N member DBs.
- **Meeting Mirrors confidentiality gate uses two manual columns.** A `Confidential` select (`Confidential`/`Shareable`) on each member Meeting Notes DB and a `Default Mirror Visibility` select (`Private`/`Shared`) on each Org Chart row decide whether a rule-matching meeting is actually mirrored (explicit meeting value wins; blank → owner default → `Shared`). Neither is canonical-synced — edit them in Notion directly; both degrade gracefully when absent. Logic in `src/topic_mirror/confidentiality.py`; see [docs/meeting-mirrors.md](docs/meeting-mirrors.md).
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
| [docs/fundraising-affinity.md](docs/fundraising-affinity.md) | Fundraising → Affinity migrated to the standalone `nzyme-fundraising` Lambda (2026-06-08); this repo only maintains the mirror + claim-table contract |
| [docs/meeting-mirrors.md](docs/meeting-mirrors.md) | Topic-mirror clones (template_id mechanism, cross-DB dedup, contributor merge, Owner resolution) |
| [docs/notion-schema.md](docs/notion-schema.md) | DB schemas, property mappings, IDs, hierarchy structure, MCP tool reference |
| [docs/onboarding-member-db.md](docs/onboarding-member-db.md) | Adding a new personal Meeting Notes DB: schema, Org Chart row, seeding option-ID→Supabase mappings, webhook; why option lists must start empty |
| [docs/testing.md](docs/testing.md) | Unit test patterns, integration testing workflow with Notion MCP |

**Keep docs in sync:** after any major change (new module, changed pipeline flow, new/renamed Notion properties, modified error handling, new test patterns), update the relevant doc file above. Review which docs are affected before considering the work complete.
