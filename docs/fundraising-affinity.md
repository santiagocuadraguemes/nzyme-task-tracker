# Fundraising → Affinity branch (opt-in)

When a meeting is tagged `Meeting type = Fundraising`, the pipeline mirrors a meeting note to Affinity's **Nzyme - LP Funnel** list (id `168609`) after the primary tracker write. Off by default; enable with `FUNDRAISING_BRANCH_ENABLED=true`.

## What it does (current, note-only mode)

1. Matches the meeting to **all** LPs via attendee emails → Affinity persons → organization → list entry. Multi-LP meetings (e.g. cross-LP intros) get the same note posted to every match.
2. Composes the note body from the **user's `## Notes`** content (inside the meeting_notes block) + Notion's auto-populated **`AI Summary`** page property — no LLM call here.
3. Posts an HTML meeting note attached to each matched LP's **opportunity** (title + composed body + Notion backlink). When neither user notes nor the AI Summary property is populated, the note degrades to title + backlink only.

Field updates (`Nzyme next step`, `Follow Up Date`, `OWNER`, `DETAILS`) are **abandoned**. The previous deferred-re-enable plan (and the LLM `next_step_summarizer` that fed it) has been removed; `write_next_step_to_lp` and `_resolve_owner` remain in the module as standalone helpers but nothing in production wires them up.

## Email source

GCal attendees (via the service-account secret on Lambda) are the primary source. The manual **`LP Emails`** rich-text property on the meeting page is merged into the attendee list in `pipeline.py` as a belt-and-braces fallback — useful for meetings GCal doesn't surface or where LP emails were not on the calendar invite. Internal Kibo emails and emails not matching any LP are silently ignored by the matcher.

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

- `src/affinity_client.py` — V1 REST wrapper (Basic auth, rate-limited, 5 retries)
- `src/fundraising/__init__.py` — orchestrator `write_to_affinity`; returns a `FundraisingOutcome`. Never raises.
- `src/fundraising/outcome.py` — `FundraisingStatus` enum + `FundraisingOutcome` dataclass
- `src/fundraising/lp_matcher.py` — `resolve_lp_list_entries` returns *all* matched list_entry_ids (multi-LP meetings post to every match)
- `src/fundraising/affinity_writer.py` — `post_meeting_note_to_lps` loops over opportunity ids and returns `(posted, failed)`
- `src/fundraising/data/kibo_user_map.json` — static Notion/email ↔ Affinity user-id map; only needed once OWNER field writes are re-enabled

## Env vars

`FUNDRAISING_BRANCH_ENABLED`, `AFFINITY_API_KEY`, `AFFINITY_LP_FUNNEL_LIST_ID` (default 168609), `KIBO_USER_MAP_PATH` (optional override).
