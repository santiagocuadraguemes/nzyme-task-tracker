# Architecture

> **Scope (post lambda-split, 2026-06).** This repo is the **monolith** and now runs
> only two jobs: real-time **meeting-template injection** (webhook) and the
> **Notion → Supabase mirror** (Sync). The AI extraction pipeline, the hierarchy /
> Detail / External-Org appliers, the weekly Done-task archive, meeting mirrors, and
> fundraising were all carved out into separate Lambdas. This doc describes what
> remains here; see [architecture-lambda-split.md](architecture-lambda-split.md) for
> the full split and where each job now lives.
>
> | Concern | Now lives in |
> |---|---|
> | Task extraction (transcript + literal-notes, classifier, semantic dedup, tracker writer, hierarchy/deal context) | `nzyme-task-extraction` |
> | Hierarchy / Detail / External-Org appliers **+** weekly Done-task archive sweep | `nzyme-housekeeping` |
> | Meeting Mirrors | `nzyme-meeting-mirrors` |
> | Fundraising → Affinity | `nzyme-fundraising` |
> | **Template injection + Notion → Supabase mirror** | **this repo** |
>
> The carved-out workers are pull-model consumers: they read the Supabase copy this
> repo's Sync maintains, not Notion directly.

## What this repo does

`src/main.py` (locally) and `src/webhook/lambda_handler.py` (in Lambda) drive two
independent jobs:

1. **Template injection** — inject the meeting-note template (`## Action Items` +
   `## Notes` headings) into new meeting pages across every per-member Meeting Notes
   DB. Real-time via the Notion-automation webhook on page creation, plus a safety-net
   pass on each watch-mode tick.
2. **Notion → Supabase mirror (Sync)** — replicate meetings + member config + meeting
   rules into Supabase (the Neo project) as the single read surface the consumer
   Lambdas pull from.

## Entry point (`src/main.py`)

- CLI args: `--watch`, `--inject-templates`, `--supabase-sync`, `--db-id`, `--dry-run`, `--verbose`
- Loads `SyncConfig` from `.env` via Pydantic; configures Logfire when `LOGFIRE_TOKEN` is set
- Creates a `NotionClientWrapper` and runs in one of two modes:

**Watch mode** (`--watch`): continuous loop. Template injection runs every
`WATCH_INTERVAL`s (default 10s); the Supabase sync runs every `SYNC_INTERVAL`s
(default 5 min). Ctrl+C to stop.

**One-shot mode** (default): runs `run_inject_templates()` and/or the Supabase
mirror once and exits. With no flag, it injects templates.

## Per-member Meeting Notes DBs

Each team member has their own Meeting Notes database. The same meeting commonly
appears in multiple DBs (each attendee's personal notes capture different
commitments). Both jobs iterate every active member DB on every cycle.

**Registry source of truth:** the Nzyme Org Chart DB. Every active row carries a
`Meeting Notes DB` URL property pointing at that member's database.
`src/meeting_db_registry.py` reads these rows once per cycle and returns one
`MeetingDB(db_id, owner_name, owner_email, auto_extract_tasks)` per active member
with a URL set. Joiners and leavers are managed entirely in Notion (no redeploy):

- **Add a member:** create their DB, set `Active=true` on their Org Chart row, paste the DB URL into `Meeting Notes DB`.
- **Remove a member:** flip `Active=false` (or clear the URL).

`MEETING_NOTES_DB_ID` (env / `--db-id`) is an **override** — when set, registry
discovery is bypassed and only that DB is processed (useful for tests / single-DB
dev runs). The `auto_extract_tasks` flag is still read and mirrored to Supabase
(`org_chart_rows.auto_extract_tasks`) for the extraction consumer, but this repo no
longer routes on it.

**Webhook setup stays per-DB:** each member's Notion automation points at the same
API Gateway URL. No workspace-level webhook exists in Notion.

## Template injection (`src/pipeline.py` + `src/template_injector.py`)

`src/pipeline.py` exposes two template-injection entry points (extraction was
removed when it moved to `nzyme-task-extraction`):

- **`run_inject_templates(config, client)`** (`--inject-templates`, watch tick) —
  loads the registry, then for each member DB queries pages with `Template
  Injected=false` created in the last 12 hours and injects the template, setting
  `Template Injected=true` on success. Per-DB failures are isolated.
- **`run_inject_templates_for_page(config, client, page_id)`** (webhook) — injects
  into a single page; derives the parent DB from the page itself, so it works for any
  member DB. Tolerates the page being deleted/archived between automation firing and
  the webhook arriving (404 / `in_trash` → skip, no error).

### TemplateInjector (`src/template_injector.py`)

- **Input:** NotionClientWrapper + template page ID + target page ID
- Fetches template blocks dynamically from a normal Notion page (`MEETING_TEMPLATE_PAGE_ID`, e.g. the "Generic Template" page), converts from "read" to "create" format, filters out AI blocks
- **Injects INSIDE the page's `meeting_notes` block** — locates the AI Meeting block, reads `meeting_notes.children.notes_block_id`, and appends template blocks at the start of that human-notes container (not at the page root)
- Retries the meeting_notes lookup a few times (~3 × 1s) to absorb the race between page creation and Notion attaching the block; if still missing, returns False and the next tick retries
- Idempotency: scans the children of `notes_block_id` for the template's first heading; skips if already present
- Edit the template page in Notion to change what gets injected — no code changes needed

## Notion → Supabase mirror (the consumer read surface)

The 5-min `SupabaseSync` cron (`src/supabase_sync.py`, plus a weekly 14-day safety
sweep on Sundays) maintains Supabase (Neo project) as the **single read surface for
consumer Lambdas** — one Notion poller (this sync) feeding independent pull-model
consumers (`nzyme-fundraising`, `nzyme-meeting-mirrors`, `nzyme-task-extraction`),
each with its own claim table and readiness rule. Three mirror tables:

| Table | Source | Writer | Notes |
|---|---|---|---|
| `public.meeting_transcripts` | every page in every member Meeting Notes DB (**including inactive members**) | `extract_row` (`src/meeting_row.py`) via `sync_incremental` | Full member-DB replica: title/date, `macro_work_block`, `detail`, `external_org`, `confidential`, `created_by_id/_name`, notes, in-block AI summary, raw transcript, GCal-resolved `attendee_emails` ("never downgrade" — NULL never overwrites stored emails), `task_page_ids`. Deliberately NOT mirrored: `Processed`/`Processing`/`Template Injected` (pipeline state — consumers use Supabase claim tables instead, e.g. `affinity_meeting_posts`). Rows upsert on every Notion edit and **converge over ticks** — a row existing says nothing about the meeting being over; consumers gate on `meeting_end` + `last_edited_time`. |
| `public.org_chart_rows` | every Org Chart row (incl. inactive, incl. members with no Meeting Notes DB) | `sync_org_chart` (`src/config_mirror_sync.py`) | Member config: email, `meeting_notes_db_id`, `active`, `auto_extract_tasks`, `default_mirror_visibility` (raw; NULL = consumer applies the "Shared" default), `seniority`. |
| `public.meeting_rule_rows` | every parseable Meeting Rules row (**incl. inactive** — `active` is a column so consumers can tell "off" from "deleted") | `sync_meeting_rules` (`src/config_mirror_sync.py`) | Same validation as `route_registry.load_routes` minus the Active filter; the legacy pre-split Affinity action tag is normalized at mirror time. |

All three use `notion_page_id`/`page_id` as stable identity; the config mirrors
tombstone vanished rows via `deleted_at` (revived automatically if the row
reappears). Config-mirror failures are isolated — they never block the meeting sync.

**Attendee resolution (`src/attendees.py`).** `extract_row` lazy-imports
`_resolve_attendees` to populate `attendee_emails`. The chain is GCal → Notion
meeting-block attendees → page governance-access fallback. The GCal lookup
impersonates the Notion page creator via a Domain-Wide-Delegation service account
(out-of-domain owners read through an in-domain proxy); names are resolved by
matching attendee emails against the Org Chart `Email` property. The supporting
modules `src/transcript_pipeline/fetch_transcript.py` (block/attendee/governance
extraction) and `src/transcript_pipeline/gcal_attendees.py` (Calendar API) are
retained here for this reason — the rest of `transcript_pipeline/` was carved out.
See [transcript-pipeline.md](transcript-pipeline.md) for the GCal proxy / auth
details.

**Heartbeat alarm:** `_handle_supabase_sync` logs `supabase sync heartbeat:` at INFO
on every tick (wording is load-bearing — a CloudWatch metric filter in
`template.yaml` counts it). The `nzyme-supabase-sync-stalled` alarm fires after 45
min without a heartbeat (`TreatMissingData: breaching` catches total silence, not
just errors); set `ALERT_EMAIL` in `.env` to get SNS email notifications. A stalled
sync starves every downstream consumer, so this alarm guards the whole fleet.

**Backfill / manual run:** `python scripts/sync_meeting_transcripts.py --full --days N`
re-syncs every page edited in the last N days through the same code path (and runs
the config mirrors at the end).

## NotionClientWrapper (`src/notion_client_wrapper.py`)

Rate-limited facade over the Notion SDK, shared by all components in a cycle.

| Method | Purpose |
|--------|---------|
| `query_database(database_id, filter, sorts)` | Query with auto-pagination |
| `get_block_children(block_id)` | Fetch all child blocks with pagination |
| `get_page(page_id)` | Retrieve single page |
| `create_page(parent_database_id, properties)` | Create new page |
| `update_page(page_id, properties)` | Update page properties |
| `retrieve_database(database_id)` | Get database schema |

Internal behavior:
- **Rate limiting:** token-bucket at 3 req/s (Notion API limit)
- **Retry:** exponential backoff on 429/5xx, up to 3 retries
- **Pagination:** transparently handles multi-page responses via `start_cursor`
- **Data source resolution:** Notion API 2025-09-03+ replaced `databases.query` with `data_sources.query`. The wrapper resolves database IDs to data source IDs and caches the mapping.
- **API version:** `2026-03-11` — supports `meeting_notes` blocks.

### Utilities

- **`blocks_to_text`** (`src/utils/blocks_to_text.py`) — converts Notion blocks to markdown (headings, lists, to-dos, dividers, callouts, quotes, toggles; recurses into nested children).
- **`RateLimiter`** (`src/utils/rate_limiter.py`) — token-bucket, configurable req/s (default 3.0).
- **`logger`** (`src/utils/logger.py`) — one-time `setup_logging()`, format `YYYY-MM-DDTHH:MM:SS | LEVEL | module | message`.

## Webhook / Lambda mode

The two remaining jobs run in **two separate Lambda functions** in one stack
(`nzyme-task-tracker`, company account `607081650195`), split 2026-06-16. Both share
the same code package (`src/webhook/lambda_handler.py`), each with its own entry point:

```
[Template injection — event-driven]  →  function nzyme-webhook  (webhook_handler)
Notion Automation (page created in any per-member DB) → API Gateway → Lambda (webhook)
  → derive parent DB from the page, set "Date" = page.created_time (with hour)
  → inject template → set "Template Injected" = true

[Notion → Supabase sync — scheduled]  →  function nzyme-task-tracker  (cron_handler)
CloudWatch Events (every 5 min, Input={"job":"supabase_sync"})      → _handle_supabase_sync       → incremental mirror
CloudWatch Events (Sunday,    Input={"job":"supabase_sync_full"})   → _handle_supabase_sync_full  → 14-day safety re-sync
```

Both functions sit behind the **same** `AWS::Serverless::HttpApi` resource; the
webhook split only moved the `POST /webhook/{token}` route's integration to
`nzyme-webhook`, so the api-id (`9g8txmxkef`) and URL are unchanged. The Sync
function keeps the name `nzyme-task-tracker` so the heartbeat metric filter on
`/aws/lambda/nzyme-task-tracker` keeps counting.

`cron_handler` returns HTTP 400 for an unrecognised job; `webhook_handler` returns
400 for a non-API-Gateway event (the old default `→ extraction` branch was removed
with the carve-out). The registry is reloaded per webhook, so Org Chart joiner/leaver
changes take effect within minutes without a redeploy.

### Components

| File | Responsibility |
|------|---------------|
| `src/webhook/handler.py` | Parses the Notion automation payload, validates the page's parent DB against the discovered registry, sets `Date = page.created_time`, calls template injection |
| `src/webhook/lambda_handler.py` | Two Lambda entry points: `webhook_handler` (API Gateway → template injection, backs `nzyme-webhook`) and `cron_handler` (`aws.events` → `supabase_sync` / `supabase_sync_full`, backs `nzyme-task-tracker`) |
| `src/meeting_db_registry.py` | Reads active Org Chart rows' `Meeting Notes DB` URL property, returns `[MeetingDB]` |
| `src/sources/single_source.py` | Per-DB query/mark helpers (`get_unprocessed_pages`, `mark_template_injected`, `mark_page_processed`, …) |

### AWS resources

- API Gateway (HTTP API, logical id `HttpApi`) — `POST /webhook/{token}` → `nzyme-webhook`
- Lambda `nzyme-webhook` (`WebhookFunction`, 256 MB / 30 s) — template injection
- Lambda `nzyme-task-tracker` (`NzymeFunction`, 512 MB / 300 s) — Supabase sync
- CloudWatch Events rules (on `nzyme-task-tracker`) — `SupabaseSync` (`rate(5 minutes)`) + `SupabaseWeeklySync` (Sunday)
- `nzyme-supabase-sync-stalled` alarm on the heartbeat metric filter (`/aws/lambda/nzyme-task-tracker`)

See `template.yaml` (SAM) for the infrastructure and `scripts/deploy.sh` /
`scripts/quick-deploy.sh` for deployment. Both functions stay in the old `company`
account permanently because the shared API Gateway host is the URL all ~10 Notion
automations point at (see [architecture-lambda-split.md](architecture-lambda-split.md)).

## Error handling

| Failure | Behavior |
|---------|----------|
| Registry load fails | Skip the injection cycle (logged); the mirror still runs |
| Template injection fails (one page / one DB) | Log error, continue with the rest — injection is best-effort |
| Page deleted/archived before webhook arrives | Skip silently (404 / `in_trash`), no error |
| Notion API 429/5xx | Exponential backoff retry (up to 3 attempts) |
| Config-mirror sub-sync fails | Isolated — never blocks the meeting mirror |
| Supabase sync stalls | `nzyme-supabase-sync-stalled` alarm fires after 45 min of silence |

## Key design principles

1. **Notion is the editing front-end; Supabase is the read surface.** This repo is the only Notion poller for meeting data; consumers read the copy.
2. **Dynamic discovery** — the member-DB registry and template content are read from Notion at runtime. Joiners/leavers and template changes need no redeploy.
3. **Stateless cycles** — each cycle is independent; no persistent in-process state between runs.
4. **Graceful degradation** — non-critical failures (one DB, one page, a config sub-sync) don't abort the cycle.
5. **Rate limiting** — all Notion calls go through the wrapper with 3 req/s pacing and retry.
6. **Observability** — Logfire spans + structured logging; a load-bearing heartbeat line backs the stalled-sync alarm.
