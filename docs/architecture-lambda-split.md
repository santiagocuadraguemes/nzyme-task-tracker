# Architecture: splitting the monolith into focused Lambdas

> **Status:** roadmap / in progress. This is the target we are migrating toward,
> not the current deployed shape. Updated 2026-06-08.

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

## Target architecture — 6 programs

### Group A — plumbing (2)

**1. Sync** — Notion → Supabase, every 5 min + the Sunday full re-sync.
Reads Notion, writes Supabase. Mirrors three tables: `meeting_transcripts`,
`org_chart_rows`, `meeting_rule_rows`. Carries the heartbeat alarm
(`nzyme-supabase-sync-stalled`) because a stalled sync starves every downstream
worker.
*Status: ~95% done. Final piece (team list + rules mirror, `config_mirror_sync.py`)
is uncommitted on branch `external-orgs-db-sync`. This is **step 1**.*

**2. Housekeeping** — daily. The Hierarchy + Detail + External Org appliers
(`src/hierarchy/`), **plus** the weekly Done-task archive sweep folded in (both are
scheduled jobs that tidy Notion). Reads the canonical lists / Affinity, writes Notion
dropdowns + Tracker nodes.
*Status: works today as a group, still inside the monolith.*

### Group B — workers, each reads the Supabase copy and does one thing (3)

**3. Extraction** — reads meeting candidates from the copy, extracts tasks, writes
them to the Team Task Tracker. Own claim table for idempotency, own readiness rule.
Still reads a handful of static prompt pages from Notion at startup (classifier
prompt, terminology) — those rarely change and are not worth mirroring.
*Status: still inside the monolith.*

**4. Meeting Mirrors** — reads the copy, clones tagged meetings into topic DBs.
Own claim table.
*Status: still inside the monolith.*

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
                          │ →Task Tracker│ →topic DBs    │ →Affinity ✅ │
                          └──────────────┴──────────────┴──────────────┘

   Hierarchy/Detail DBs ─▶ 5. HOUSEKEEPING (daily) ─▶ Notion dropdowns + Tracker
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

1. **Finish Sync** (step 1) — commit + deploy `config_mirror_sync`. Completes the read
   surface so the remaining workers have everything they need in Supabase.
2. **Cut over Fundraising** — flip the legacy flag off once the parallel window is clean.
3. **Carve out Extraction** — new standalone repo using the fundraising pattern.
4. **Carve out Meeting Mirrors** — same pattern; trickiest because it writes most to Notion.
5. **Split Housekeeping + Webhook** out of the monolith last (lowest risk, lowest churn).

## Honest caveats

- This is **6 Lambdas** for a ~10–20 person fund — more deploy/monitoring surface than
  one. Justified because the triggers (HTTP vs 5-min vs daily) and failure domains are
  genuinely different, and fundraising already proved the pattern. If we ever want fewer
  moving parts, Webhook and Housekeeping are the candidates to keep co-located.
- Extraction can't be a *pure* Supabase consumer — it needs a few Notion-hosted prompt
  pages at startup. That's fine; don't over-engineer by mirroring those too.
