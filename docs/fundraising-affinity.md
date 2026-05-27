# Fundraising → Affinity branch (opt-in)

When a meeting's `Macro Work Block` matches an active **"Fire Affinity LP Funnel"** rule in the Meeting Rules DB (currently `Macro Work Block = Investor Relations & Fundraising`), the pipeline mirrors a meeting note to Affinity's **Nzyme - LP Funnel** list (id `168609`). Off by default; enable with `FUNDRAISING_BRANCH_ENABLED=true`.

## Fires on EVERY matching meeting (like template injection)

The branch runs for every meeting whose `Macro Work Block` matches the rule — **independent of the extraction outcome**. It does not matter whether the page produced tasks, has any notes, or whether the owner opted into AI extraction (`Auto-extract Tasks = false`): a fundraising meeting happening is itself worth logging against the LP. When neither user notes nor the AI Summary are populated, the note degrades to title + Notion backlink.

Mechanically, the branch is its own step (`_mirror_meeting_to_affinity` in `pipeline.py`) that runs **before** the extraction `if/elif/else`, so the literal-notes "no action items" and "auto-extract but no transcript" early returns cannot skip it. The only remaining downstream gate is whether an attendee maps to an LP opportunity (`Skipped: no LP match` otherwise). Previously the branch sat after the task write and was silently skipped whenever an extraction path returned early — fixed 2026-05-27.

## What it does (current, note-only mode)

1. Matches the meeting to **all** LPs via attendee emails → Affinity persons → opportunity → list entry. Multi-LP meetings (e.g. cross-LP intros) get the same note posted to every match.
2. Resolves the meeting's **people** (owner/host + attendees) to Affinity person ids via `resolve_attendee_person_ids` (searches every attendee email, internal Kibo included, and keeps the ids of those that exist in Affinity).
3. Posts an HTML meeting note attached to each matched LP's **opportunity** *and* to those person ids — so it shows on the people's timelines, not just the LP org. The note has a plain **title (no date** — Notion titles already embed it) and two labeled sections:
   - **Manual notes** — the user's `## Notes` content with the meeting template scaffolding (headings, empty bullets, `[placeholder]`) stripped by `_strip_template_scaffolding`. When the user never touched the template, this reads **"No manual notes"**.
   - **Summary** — the Notion-generated AI summary, read from the **meeting_notes block** (`summary_block_id`, via `_fetch_block_summary`) — i.e. what the user sees in the block — falling back to the legacy `AI Summary` page property only when the block has none. Omitted entirely when both are empty. (Most meetings carry the summary in the block, not the property — reading only the property is why the summary was missing before 2026-05-27.)

   No LLM call is made here. A Notion backlink (`View full meeting notes in Notion`) is appended last.

Field updates (`Nzyme next step`, `Follow Up Date`, `OWNER`, `DETAILS`) are **abandoned**. The deferred-re-enable plan has been removed in full: the LLM `next_step_summarizer`, `write_next_step_to_lp`, the `_resolve_owner` Affinity-user resolver, the `KiboUserMap` Notion↔Affinity static map, and the `kibo_user_map.json` data file are all gone. If field writes ever come back, build the new path fresh against the current Affinity V1 client.

## Email source

GCal attendees (via the service-account secret on Lambda) are the only source. For **LP matching**, internal Kibo emails, **known partner addresses** (`PARTNER_LP_EMAILS` in `lp_matcher.py`), and emails not matching any LP are silently ignored. Partners are LPs in our own funnel but attend fundraising meetings as hosts/co-investors, so matching them would log the meeting against their own LP entry — `PARTNER_LP_EMAILS` is the Org Chart partner roster (Seniority = Partner / Co-founding Partner) plus the non-Kibo addresses some attend under (e.g. `@oliverwyman.com`); hardcoded for now, refresh from the Org Chart if the roster changes. For **person attachment** (step 2 above), every attendee email is searched — internal Kibo people and partners included — so the owner/host gets tagged on the note too. (A manual `LP Emails` meeting-page property used to be merged in as a fallback — removed 2026-05-27; no one used it.)

## Multi-DB behavior

If two Kibo members independently capture the same LP meeting in their respective DBs, both pages fire and Affinity gets two notes on the same opportunity. That's intentional — each member's notes capture distinct insights and are independently valuable on the LP timeline.

## Visibility (CloudWatch only — no Notion property)

Every fundraising-branch run emits a single structured log line at the end:

```
fundraising outcome: page=<16-char-prefix> db_owner=<member> status=<enum> detail=<text>
```

Possible status values: `Posted`, `Skipped: no external attendees`, `Skipped: no LP match`, `Failed: API error`. Failures log at `ERROR`, others at `INFO`. To list every fundraising attempt over a window, query CloudWatch Logs Insights with `filter @message like /fundraising outcome:/`.

## Retries

`AffinityClient` retries transient failures (429, 5xx) up to 5 times with exponential backoff within the same Lambda invocation (~30 s of resilience). Beyond that, the failure is logged loudly and the page is left at `Processed=true`; manual recovery is to clear `Processed` on the page so the next cron tick re-runs the full pipeline.

## Idempotency

Not implemented. Manual re-trigger of an already-posted page would create a duplicate note. Acceptable per design call.

## Key files

- `src/pipeline.py` — `_mirror_meeting_to_affinity` evaluates the rule, resolves attendees (hoisted to run once for all paths), and calls the orchestrator. Invoked before the extraction branches so it fires on every matching meeting.
- `src/topic_mirror/route_registry.py` — `load_routes` / `match_routes`; a rule's `Match Property` must be one of `Macro Work Block` / `Detail` / `External Org` (the page property name), **not** the DB's display label. The LP-funnel rule must use `Macro Work Block`.
- `src/affinity_client.py` — V1 REST wrapper (Basic auth, rate-limited, 5 retries)
- `src/fundraising/__init__.py` — orchestrator `write_to_affinity`; returns a `FundraisingOutcome`. Never raises. `_strip_template_scaffolding` decides manual-notes-vs-"No manual notes".
- `src/fundraising/outcome.py` — `FundraisingStatus` enum + `FundraisingOutcome` dataclass
- `src/fundraising/lp_matcher.py` — `resolve_lp_list_entries` returns *all* matched list_entry_ids (multi-LP meetings post to every match); `resolve_attendee_person_ids` maps attendee emails → Affinity person ids for note attachment
- `src/fundraising/affinity_writer.py` — `post_meeting_note_to_lps(..., manual_notes, ai_summary, person_ids)` builds the two-section HTML note and attaches it to each opportunity + the person ids; returns `(posted, failed)`

## Env vars

`FUNDRAISING_BRANCH_ENABLED`, `AFFINITY_API_KEY`, `AFFINITY_LP_FUNNEL_LIST_ID` (default 168609), `MEETING_RULES_DB_ID` (the rule registry — required for the branch to fire; shared with the Topic Mirror feature).
