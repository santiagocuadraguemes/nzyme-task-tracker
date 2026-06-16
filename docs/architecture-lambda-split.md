# Architecture: splitting the monolith into focused Lambdas

> **Status:** nearly complete. Five of the six programs are carved out and live;
> only the **Webhook** (and the Sync that co-tenants with it) remains in the
> monolith, by design. Updated 2026-06-16.

## Why

Today everything runs inside **one** Lambda (plus its API Gateway webhook). Every
job talks directly to Notion, and the meeting-processing jobs (task extraction,
meeting mirrors, fundraising) all run inside a single per-page loop. That couples
unrelated features into one deploy, one failure domain, and one rate-limit budget
against Notion.

The migration makes **Notion the editing front-end only**, mirrors everything into
**Supabase (the Neo project) as the single read surface**, and splits the work into
focused programs that each do one thing. The "do one useful thing" workers read the
Supabase copy instead of querying Notion, so they stop competing for Notion's API
and stop stepping on each other.

## The sorting principle

Two questions decide where each job lives:

1. **Real-time or scheduled?** Template injection must fire the moment a page is
   created — it can't wait for a poll. Everything else runs on a timer.
2. **Reads the Supabase copy, or writes back into Notion's structure?** The workers
   read the copy. The "cleanup" jobs write Notion dropdowns / tracker rows.

## What the monolith does today (full inventory)

| # | Job | Trigger today | Reads | Writes |
|---|-----|---------------|-------|--------|
| 1 | Copy Notion → Supabase (meetings + team list + rules) | every 5 min (+ Sunday full re-sync) | Notion | Supabase |
| 2 | Extract tasks from meetings | every 5 min | Notion | Team Task Tracker (Notion) |
| 3 | Meeting Mirrors (clone tagged meetings into topic DBs) | every 5 min (bundled into #2) | Notion | topic DBs (Notion) |
| 4 | Fundraising → Affinity | every 5 min (bundled into #2) | Notion | Affinity |
| 5 | Hierarchy + Detail + External Org cleanup (appliers) | daily 05:00 UTC | Hierarchy/Detail DBs + Affinity deals | Notion dropdowns + Tracker |
| 6 | Template injection (+ set Date field) | real-time, on page creation (webhook) | Notion | Notion |
| 7 | Weekly "Done" task archive sweep | Sunday 06:00 UTC | Team Task Tracker | Archive DB |

> **Carve-out status (2026-06-16):** #4 Fundraising, #3 Meeting Mirrors, #5
> Housekeeping, and #2 Extraction all now run as their own Lambdas
> (`nzyme-fundraising`, `nzyme-meeting-mirrors`, `nzyme-housekeeping`,
> `nzyme-task-extraction`); their in-monolith code has been removed from this
> repo. #1 Sync's config mirror is committed and live in Supabase. Only #6
> Webhook + #1 Sync (its co-tenant) — and the no-op #7 Archive — still run inside
> the monolith. Extraction cut over live 2026-06-15; this repo's working tree
> removes the dead extraction stack (the `_resolve_attendees` helper Sync still
> needs was first lifted into `src/attendees.py`).

## Target architecture — 6 programs

### Group A — plumbing (2)

**1. Sync** — Notion → Supabase, every 5 min + the Sunday full re-sync.
Reads Notion, writes Supabase. Mirrors three tables: `meeting_transcripts`,
`org_chart_rows`, `meeting_rule_rows`. Carries the heartbeat alarm
(`nzyme-supabase-sync-stalled`) because a stalled sync starves every downstream
worker.
*Status: ✅ done (2026-06-11). The team-list + rules mirror
(`config_mirror_sync.py`) is committed (ce139b0) and populating Supabase —
`meeting_rule_rows` and `org_chart_rows` (incl. `default_mirror_visibility`)
are live, which is what unblocked the Mirrors carve-out. Branch merged to
master and the monolith fully redeployed 2026-06-11.*

**2. Housekeeping** — daily. The Hierarchy + Detail + External Org appliers
(`src/hierarchy/`), **plus** the weekly Done-task archive sweep folded in (both are
scheduled jobs that tidy Notion). Reads the canonical lists / Affinity, writes Notion
dropdowns + Tracker nodes.
*Status: ✅ **fully carved out (2026-06-11)** → standalone repo `nzyme-housekeeping`
(SAM stack in org account `047719630984`; also subtree'd into the
`nzyme-fund/nzyme-meeting-notes` monorepo). The `src/hierarchy/` package, the archive
functions in `pipeline.py`, the two Schedule events, the cron handler branches, and the
`--sync-hierarchy`/`--archive` CLI flags were all **removed** from this monolith. The
one shared helper that had to stay behind — `canonical_mirror_sync._http`, still used by
the active `config_mirror_sync` — was lifted into `src/supabase_rest.py`. Smoke-tested
live (`hierarchy_sync` → `errors=0`). `TASK_ARCHIVE_DB_ID` is unset in prod, so the
weekly archive remains a no-op (carried over as-is — wiring the Archive DB is a separate
decision).*

### Group B — workers, each reads the Supabase copy and does one thing (3)

**3. Extraction** — reads meeting candidates from the copy, extracts tasks, writes
them to the Team Task Tracker. Own claim table for idempotency, own readiness rule.
Still reads a handful of static prompt pages from Notion at startup (classifier
prompt, terminology) — those rarely change and are not worth mirroring.
*Status: ✅ **carved out + cut over live (2026-06-15)** → `nzyme-task-extraction`
(folder in the `nzyme-fund/nzyme-meeting-notes` monorepo; SAM stack in org account
`047719630984`, `rate(5 min)`). Reads candidates from `meeting_transcripts` +
`org_chart_rows` (page-quiet gate, route on `auto_extract_tasks`); own claim table
`extraction_meeting_posts`. The monolith's `ScheduledExtraction` cron is **disabled**
and its extraction code is removed in this repo's working tree — the
`pipeline._resolve_attendees` helper that Sync still lazy-imports was first lifted into
`src/attendees.py`. Cutover was briefly blocked by a globally-revoked `OPENAI_API_KEY`
(which had silently broken the monolith's extraction too); resolved by rotating the key.*

**4. Meeting Mirrors** — reads the copy, clones tagged meetings into topic DBs.
Own claim table (`mirror_meeting_posts`).
*Status: ✅ carved out → standalone repo `nzyme-meeting-mirrors` (SAM stack
`nzyme-meeting-mirrors`, `rate(15 min)`). "Decide-in-Supabase / act-in-Notion":
discovery/routing/gating/idempotency read Supabase, the `template_id` clone +
block-level note merge stay Notion calls (it is NOT zero-Notion like
fundraising). Cut over 2026-06-08: deployed live (`rate(15 min)`), the in-monolith branch
disabled on the live function (`TOPIC_MIRROR_ENABLED=false`) and its code
removed from the repo (`route_registry.py` retained for `config_mirror_sync` +
the Affinity actions). The repo deletion takes effect on the next monolith
redeploy.*

**5. Fundraising** — reads the copy, writes to Affinity. Own claim table
(`affinity_meeting_posts`).
*Status: ✅ done — standalone repo `nzyme-fundraising`, currently in parallel-run /
cutover.*

### Group C — front door (1)

**6. Webhook** — the only real-time piece. On page creation: set the Date field and
inject the meeting template. Must be its own Lambda because it's triggered by an HTTP
call from Notion, not a timer.
*Status: still inside the monolith.*

## Picture

```
                 ┌─────────────────────────────┐
   Notion  ─────▶│ 1. SYNC  (every 5 min)      │─────▶  Supabase (the copy)
 (you type)      └─────────────────────────────┘              │
                                                               │ everything below
   Notion ──HTTP──▶ 6. WEBHOOK (instant: Date + template)      │ reads from here
                                                               ▼
                          ┌──────────────┬──────────────┬──────────────┐
                          │ 2.EXTRACTION │ 3.MIRRORS     │ 4.FUNDRAISING│
                          │ →Task Trkr ✅│ →topic DBs ✅ │ →Affinity ✅ │
                          └──────────────┴──────────────┴──────────────┘

   Hierarchy/Detail DBs ─▶ 5. HOUSEKEEPING (daily) ✅ ─▶ Notion dropdowns + Tracker
   Affinity deals       ─▶    (+ weekly Done-archiving)
```

## The shared pattern for every worker (Group B)

Fundraising is the reference implementation. Each worker:
- reads its candidates from `meeting_transcripts` (+ `org_chart_rows` /
  `meeting_rule_rows` for config and routing) — **no Notion polling**;
- owns a **claim table** in Supabase (one row per page) so retries and parallel runs
  never double-act; fail-closed when Supabase is unreachable;
- has an explicit **readiness rule** (e.g. meeting ended + page quiet for N minutes)
  rather than relying on a `Processed` flag in Notion;
- deploys, monitors, and fails independently of the others.

## Rollout order

1. **Finish Sync** — ✅ **Done (2026-06-11)** — `config_mirror_sync` merged to master
   and the monolith fully redeployed. The read surface is complete.
2. **Cut over Fundraising** — ✅ **Done (2026-06-08)** — no parallel period; legacy flag
   off, fundraising runs only in `nzyme-fundraising`.
3. **Carve out Meeting Mirrors** — ✅ **Done (2026-06-08)** — `nzyme-meeting-mirrors`
   (in the `nzyme-fund/nzyme-meeting-notes` monorepo), deployed live at
   `rate(15 min)`; in-monolith branch disabled and its code removed from this repo.
   First ticks cloned 10 + merged 1, zero failures.
4. **Carve out Housekeeping** — ✅ **Done (2026-06-11)** — standalone repo
   `nzyme-housekeeping` deployed to the org account; the appliers, archive sweep, cron
   schedules, handler branches, and CLI flags were removed from the monolith (shared
   `_http` moved to `src/supabase_rest.py`). Smoke-tested (`errors=0`).
5. **Carve out Extraction** — ✅ **Done (cut over 2026-06-15)** — `nzyme-task-extraction`
   (folder in the `nzyme-fund/nzyme-meeting-notes` monorepo, org account `047719630984`,
   `rate(5 min)`), built on the fundraising pattern. Worker is the live extractor; monolith
   `ScheduledExtraction` cron disabled and its extraction code removed from this repo.
6. **Webhook** — stays in the monolith, in the old account, indefinitely (see
   "Account placement" below). Splitting it is optional and last. **This is the only
   remaining piece in the monolith** (alongside Sync, its co-tenant).

## Account placement (decided 2026-06-11)

Two AWS accounts, both eu-west-1:

- **Old `company` account `607081650195`** (profile `company`) — keeps the **monolith**
  (`nzyme-task-tracker`) permanently. Its API Gateway host (`9g8txmxkef…`) is the URL
  all ~10 Notion meeting automations point to; keeping the monolith here means never
  repointing them. Whatever remains un-carved (Sync, Webhook, Archive until
  Housekeeping ships) lives here. *(This supersedes the 2026-06-09 idea of repointing
  webhooks to the staged org-account copy.)*
- **Org account `047719630984`** (SSO, profile `org`) — hosts every timer-driven
  carve-out. `nzyme-fundraising` + `nzyme-meeting-mirrors` were staged here dormant on
  2026-06-09; the cron flip (enable org rules → verify → disable company rules) is the
  pending cutover. All timer-driven carve-outs — Fundraising, Mirrors, Housekeeping, and
  now Extraction (`nzyme-task-extraction`, live 2026-06-15) — live here; future carve-outs
  deploy here directly, never to the old account.
- The staged `nzyme-task-tracker` copy in the org account (webhook host `zntuq4wrz6…`)
  is **redundant** under this decision — tear it down once the fundraising/mirrors flip
  is verified. Keep the org-account GCal secret (`nzyme/gcal-service-account`): the
  future Extraction worker needs it for attendee resolution.

## Honest caveats

- This is **6 Lambdas** for a ~10–20 person fund — more deploy/monitoring surface than
  one. Justified because the triggers (HTTP vs 5-min vs daily) and failure domains are
  genuinely different, and fundraising already proved the pattern. If we ever want fewer
  moving parts, Webhook and Housekeeping are the candidates to keep co-located.
- Extraction can't be a *pure* Supabase consumer — it needs a few Notion-hosted prompt
  pages at startup. That's fine; don't over-engineer by mirroring those too.
