# Fundraising → Affinity branch (opt-in)

When a meeting's `Macro Work Block` matches an active **"Fire Affinity LP Funnel (no transcript)"** or **"Fire Affinity LP Funnel (with transcript)"** rule in the Meeting Rules DB (currently `Macro Work Block = Investor Relations & Fundraising`), the pipeline mirrors a meeting note to Affinity's **Nzyme - LP Funnel** list (id `168609`). Off by default; enable with `FUNDRAISING_BRANCH_ENABLED=true`.

The two Action variants (split 2026-06-03) behave identically except that **(with transcript)** appends the meeting's raw transcript to the note — see "Full transcript section" below. The pre-split tag `Fire Affinity LP Funnel` is normalized to the no-transcript variant at load, so an un-renamed rule keeps firing. If a page somehow matches both variants, with-transcript wins.

## Fires on EVERY matching meeting (like template injection)

The branch runs for every meeting whose `Macro Work Block` matches the rule — **independent of the extraction outcome**. It does not matter whether the page produced tasks, has any notes, or whether the owner opted into AI extraction (`Auto-extract Tasks = false`): a fundraising meeting happening is itself worth logging against the LP. When neither user notes nor the AI Summary are populated, the note degrades to title + Notion backlink.

Mechanically, the branch is its own step (`_mirror_meeting_to_affinity` in `pipeline.py`) that runs **before** the extraction `if/elif/else`, so the literal-notes "no action items" and "auto-extract but no transcript" early returns cannot skip it. The only remaining downstream gate is whether an attendee maps to an LP opportunity (`Skipped: no LP match` otherwise). Previously the branch sat after the task write and was silently skipped whenever an extraction path returned early — fixed 2026-05-27.

### Spans ALL members, not just active ones (2026-05-27)

The extraction sweep discovers Meeting Notes DBs via the Org Chart. Normally it polls only `Active = true` members, but fundraising meetings are held by partners who aren't on the task tracker, so the sweep loads the registry with `include_inactive=True` (`load_registry`/`discover_meeting_dbs`). **Inactive** members are polled *only* for the fundraising branch: after `_mirror_meeting_to_affinity` runs, `process_meeting` checks `owner.active` and — when false — marks the page processed and returns, skipping task extraction, the tracker write, and the topic-mirror. Active members (currently Vicente, Santiago) keep the full pipeline. `MeetingDB.active` carries the flag; only the extraction sweep (`run_sync` and the Lambda `_handle_extraction`) passes `include_inactive=True` — Supabase sync, hierarchy/detail appliers, and the topic-mirror keep the active-only default. Members with no Meeting Notes DB URL in the Org Chart have nothing to poll.

## What it does (current, note-only mode)

1. Matches the meeting to **all** LPs via attendee emails → Affinity persons → opportunity → list entry. Multi-LP meetings (e.g. cross-LP intros) get the same note posted to every match.
2. Resolves the meeting's **people** (owner/host + attendees) to Affinity person ids via `resolve_attendee_person_ids` (searches every attendee email, internal Kibo included, and keeps the ids of those that exist in Affinity).
3. Posts an HTML meeting note attached to each matched LP's **opportunity** *and* to those person ids — so it shows on the people's timelines, not just the LP org. The note has a plain **title** — run through `strip_title_datetime()` to drop the raw ISO timestamp Notion appends to auto-created meeting names (`… 2026-05-29T14:00:00.000+02:00`), which looked ugly in Affinity — an **Owner** line (the `db_owner`, i.e. the Kibo member whose Meeting Notes DB the meeting lives in / who hosted it; rendered right under the title, omitted when the owner can't be resolved — `db_owner == "?"`), and two labeled sections:
   - **Manual notes** — the user's `## Notes` content with the meeting template scaffolding (headings, empty bullets, `[placeholder]`) stripped by `_strip_template_scaffolding`. When the user never touched the template, this reads **"No manual notes"**.
   - **Summary** — the Notion-generated AI summary, read from the **meeting_notes block** (`summary_block_id`, via `_fetch_block_summary`) — i.e. what the user sees in the block — falling back to the legacy `AI Summary` page property only when the block has none. Omitted entirely when both are empty. (Most meetings carry the summary in the block, not the property — reading only the property is why the summary was missing before 2026-05-27.)

   No LLM call is made here. A Notion backlink (`View full meeting notes in Notion`) is appended last.

### Full transcript section (with-transcript rules only)

When the matched rule's Action is **"Fire Affinity LP Funnel (with transcript)"**, a third labeled section — **Full transcript** — is rendered between Summary and the backlink. It's the **raw Notion transcript** (`transcript_block_id` → `blocks_to_text`), posted **full length, deliberately uncapped** — not the Gemini-corrected text, which may not exist because the branch runs before extraction (and for inactive members / `Auto-extract = false` pages extraction never runs). Raw keeps the branch zero-LLM-cost and universally available. When the page has no transcript (transcription paused, recording not yet processed), the section is omitted and the note posts as the no-transcript variant — logged at `INFO`.

**Failing handler:** transcript notes can be very large, so each opportunity post gets a fallback: if Affinity rejects the full note (`AffinityError`, most plausibly payload size — note the client has already burned its transient-error retries by then), the writer retries once with the transcript section swapped for an omission notice pointing at the Notion backlink. A successful fallback counts as **Posted** — a batch retry would only duplicate the note — but logs at `WARNING` and surfaces in the outcome line as `transcript_omitted_for=[<opportunity ids>]`. If the fallback also fails, the opportunity lands in `failed` with both error messages and the page goes to `Failed: API error` as usual.

Field updates (`Nzyme next step`, `Follow Up Date`, `OWNER`, `DETAILS`) are **abandoned**. The deferred-re-enable plan has been removed in full: the LLM `next_step_summarizer`, `write_next_step_to_lp`, the `_resolve_owner` Affinity-user resolver, the `KiboUserMap` Notion↔Affinity static map, and the `kibo_user_map.json` data file are all gone. If field writes ever come back, build the new path fresh against the current Affinity V1 client.

## Email source

GCal attendees (via the service-account secret on Lambda) are the only source. For **LP matching**, internal Kibo emails, **known partner addresses** (`PARTNER_LP_EMAILS` in `lp_matcher.py`), and emails not matching any LP are silently ignored. Partners are LPs in our own funnel but attend fundraising meetings as hosts/co-investors, so matching them would log the meeting against their own LP entry — `PARTNER_LP_EMAILS` is the Org Chart partner roster (Seniority = Partner / Co-founding Partner) plus the non-Kibo addresses some attend under (e.g. `@oliverwyman.com`); hardcoded for now, refresh from the Org Chart if the roster changes. For **person attachment** (step 2 above), every attendee email is searched — internal Kibo people and partners included — so the owner/host gets tagged on the note too. (A manual `LP Emails` meeting-page property used to be merged in as a fallback — removed 2026-05-27; no one used it.)

## Multi-DB behavior

If two Kibo members independently capture the same LP meeting in their respective DBs, both pages fire and Affinity gets two notes on the same opportunity. That's intentional — each member's notes capture distinct insights and are independently valuable on the LP timeline.

## Visibility (CloudWatch + Supabase — no Notion property)

Every fundraising-branch run emits a single structured log line at the end:

```
fundraising outcome: page=<16-char-prefix> db_owner=<member> status=<enum> detail=<text>
```

Possible status values: `Posted`, `Skipped: no external attendees`, `Skipped: no LP match`, `Failed: API error`. Failures log at `ERROR`, others at `INFO`. To list every fundraising attempt over a window, query CloudWatch Logs Insights with `filter @message like /fundraising outcome:/`.

Since 2026-06-04 the same outcomes are also queryable in Supabase (Neo project) — every claimed page leaves a row in `public.affinity_meeting_posts` (see Idempotency below):

```sql
select page_id, owner_name, status, detail, attempts, completed_at
from affinity_meeting_posts where status = 'failed';
```

## Retries

`AffinityClient` retries transient failures (429, 5xx) up to 5 times with exponential backoff within the same Lambda invocation (~30 s of resilience). Beyond that, the claim row lands at `status='failed'` and the page is left at `Processed=true`; manual recovery is still to clear `Processed` on the page — the next cron tick re-runs the pipeline, the branch **re-claims the failed row** (`attempts` increments) and retries the post. There is no automatic retry cron sweep (future work). Crucially, the rest of the re-run is now duplicate-safe: an already-`posted` page is never posted again.

## Idempotency (claim-before-post via Supabase, 2026-06-04)

Implemented in `src/fundraising/state.py` against `public.affinity_meeting_posts` in the Neo Supabase project — one row per meeting page, keyed by the canonical page UUID (joins `meeting_transcripts.page_id`; deliberately **no FK** — the mirror lags ~5 min and the claim must precede the mirror row).

After the rule match and before `write_to_affinity`, the pipeline must **win an atomic claim**:

1. **Insert-claim** — `POST ... on_conflict=page_id` with `Prefer: resolution=ignore-duplicates,return=representation`. A non-empty response means this invocation created the row and owns the post.
2. **Lost insert → decide by the existing row's status**: `posted` / `skipped_*` → terminal, never re-posted; `failed` → re-claimed for retry; `claimed` older than 45 min (`STALE_CLAIM_MINUTES` — a crashed run) → re-claimed; `claimed` and fresh → another invocation owns it, skip.
3. Re-claims are **conditional PATCHes** (`status=eq.failed` / `status=eq.claimed&claimed_at=lt.<cutoff>`) — the WHERE filter is the server-side concurrency guard, so a row transitions exactly once under a race.

After `write_to_affinity` returns, the terminal status, detail, and opportunity ids are PATCHed back onto the row (`record_outcome`).

**Fail-closed:** any Supabase error during the claim → log `ERROR`, skip the Affinity post entirely this run (the next pipeline retry of the page tries again). The branch never posts without a confirmed claim.

**Residual duplicate window (accepted):** if the Affinity post succeeds but the outcome write-back fails, the row stays `claimed`; a re-run after 45 min re-claims and may post a duplicate note. Same applies to a `failed` retry after a partial multi-LP success — the retry re-posts to **all** matched opportunities (the previously-posted ids are recorded on the failed row's `opportunity_ids` for audit).

Reads `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`/`SUPABASE_KEY` from env (same as every hierarchy sub-sync); the table is RLS-enabled with no policies = service-role only. Dry-run never touches the table (the whole branch is skipped). Created via migration `create_affinity_meeting_posts`.

## Key files

- `src/pipeline.py` — `_mirror_meeting_to_affinity` evaluates the rule (deriving `include_transcript` from the Action variant), resolves attendees (hoisted to run once for all paths), and calls the orchestrator. Invoked before the extraction branches so it fires on every matching meeting.
- `src/topic_mirror/route_registry.py` — `load_routes` / `match_routes`; a rule's `Match Property` must be one of `Macro Work Block` / `Detail` / `External Org` (the page property name), **not** the DB's display label. The LP-funnel rule must use `Macro Work Block`. `AFFINITY_LP_ACTIONS` groups both Affinity Action variants; the legacy tag is normalized here.
- `src/affinity_client.py` — V1 REST wrapper (Basic auth, rate-limited, 5 retries)
- `src/fundraising/__init__.py` — orchestrator `write_to_affinity(..., include_transcript)`; returns a `FundraisingOutcome`. Never raises. `_strip_template_scaffolding` decides manual-notes-vs-"No manual notes"; `_fetch_transcript_text` pulls the raw transcript when asked.
- `src/fundraising/outcome.py` — `FundraisingStatus` enum + `FundraisingOutcome` dataclass (carries `opportunity_ids` for the claim-row audit)
- `src/fundraising/state.py` — Supabase claim-before-post (`claim_post` / `record_outcome` against `public.affinity_meeting_posts`); re-uses the `_http` helper from `canonical_mirror_sync`
- `src/fundraising/lp_matcher.py` — `resolve_lp_list_entries` returns *all* matched list_entry_ids (multi-LP meetings post to every match); `resolve_attendee_person_ids` maps attendee emails → Affinity person ids for note attachment
- `src/fundraising/affinity_writer.py` — `post_meeting_note_to_lps(..., manual_notes, ai_summary, person_ids, meeting_owner, transcript)` builds the HTML note (title + optional Owner line + labeled sections) and attaches it to each opportunity + the person ids; returns `(posted, failed, degraded)` — `degraded` flags opportunities that got the transcript-omitted fallback note

## Env vars

`FUNDRAISING_BRANCH_ENABLED`, `AFFINITY_API_KEY`, `AFFINITY_LP_FUNNEL_LIST_ID` (default 168609), `MEETING_RULES_DB_ID` (the rule registry — required for the branch to fire; shared with the Topic Mirror feature).
