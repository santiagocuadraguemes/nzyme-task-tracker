# Fundraising → Affinity branch — MIGRATED to a standalone Lambda (2026-06-08)

> **This feature no longer lives in nzyme-task-tracker.** The in-monolith
> fundraising → Affinity branch (`_mirror_meeting_to_affinity`, `src/fundraising/`,
> `src/affinity_client.py`, and the `FUNDRAISING_BRANCH_ENABLED` / `AFFINITY_*`
> config) was removed on 2026-06-08 after the logic was migrated to its own
> Lambda. Do not re-add it here.

## Where it lives now

The standalone repo **`nzyme-fundraising`** (SAM stack `nzyme-fundraising`,
company AWS account, eu-west-1) owns this end to end. It reads ready fundraising
meetings from the Supabase mirror and posts LP notes to Affinity's
**Nzyme - LP Funnel** list (id `168609`). Zero Notion, zero LLM. See that repo's
`README.md` and `docs/how-it-works.md` for current behavior, readiness rules, LP
matching, and CloudWatch visibility.

## What nzyme-task-tracker still provides (the contract)

The standalone Lambda depends on two things this repo maintains — keep them intact:

1. **`public.meeting_transcripts`** (the Supabase mirror, written by the
   `SupabaseSync` cron via `src/meeting_row.py` + `src/supabase_sync.py`). The
   fundraising Lambda reads its candidates from here — `macro_work_block`,
   `meeting_start`, `last_edited_time`, `attendee_emails` (GCal-resolved),
   `notes`, `notion_summary`, `transcript`. **`include_inactive=True` on the
   Supabase sync must stay** — fundraising meetings are held by partners whose
   member DBs are otherwise inactive, and the mirror is how their meetings reach
   the consumer.
2. **`public.affinity_meeting_posts`** (the claim-before-post table) — shared
   idempotency. One row per meeting page; statuses `claimed` / `posted` /
   `skipped_*` / `failed`.

## Historical note

The branch originally ran inline in the extraction pipeline (fired on every
meeting tagged `Macro Work Block = Investor Relations & Fundraising`, note-only
mode, with optional full-transcript variant). The readiness signal in the new
Lambda is page-quiet (`last_edited_time`) + `meeting_start`, NOT `meeting_end`
(the mirror never populates `meeting_end`). The Meeting Rules DB Affinity actions
(`Fire Affinity LP Funnel …`) are still recognized by `route_registry` /
`config_mirror_sync` for the meeting-rules mirror, but the new Lambda matches
rules via env config, not those rows.
