# Meeting Mirrors branch (opt-in)

> **✅ Carved out → standalone `nzyme-meeting-mirrors` Lambda** (Lambda-split
> migration step 4, completed 2026-06-08). The in-monolith Meeting Mirrors
> branch has been **disabled in production** (`TOPIC_MIRROR_ENABLED=false` on the
> live function) and its **code removed from this repo** — the orchestrator,
> `writer.py`, `notes_extractor.py`, `confidentiality.py`, and `outcome.py` are
> gone, along with `_run_topic_mirror` in `pipeline.py` and the
> `topic_mirror_enabled` config field. **`src/topic_mirror/route_registry.py` is
> retained** — it still backs `config_mirror_sync` (Meeting Rules → Supabase) and
> the Affinity LP-funnel action constants. The live feature now runs as a
> "decide-in-Supabase / act-in-Notion" worker (reads `meeting_transcripts` /
> `meeting_rule_rows` / `org_chart_rows`, owns the `mirror_meeting_posts` claim
> table, clones/merges via Notion). **This document is retained as the
> behavioural / parity reference** for that worker — see
> `nzyme-meeting-mirrors/docs/how-it-works.md` and
> `specs/meeting-mirrors-carveout-plan.md`. (Note: these monolith edits are not
> live until the monolith is redeployed; the running function already has the
> branch disabled via the flag.)

After the primary task-tracker write, each meeting page is checked against a routing table. Pages tagged with values like `Detail = "AI & Tech"` or `External Org = "White Vega"` get cloned into topic-specific Notion DBs. Off by default; enable with `TOPIC_MIRROR_ENABLED=true` and `TOPIC_MIRROR_ROUTES_DB_ID=<routes DB>`.

## Mechanism

Notion `POST /v1/pages` with `template: {type: "template_id", template_id: <source_page_id>}` clones the source — including the AI-managed `meeting_notes` block (transcript, AI Summary, attendees, notes). Verified empirically in `scripts/replicate_meeting.py`.

**Schema-aware property filtering (writer.py).** Before each clone the writer fetches the target DB's schema, drops any property in the build dict that the target doesn't declare or whose type doesn't match, and PATCHes the target schema to add any missing `select` / `multi_select` option values (preserving the source's color). This is load-bearing under API `2026-03-11`: the `template_id` path now hard-errors on unknown property names (e.g. `"External Org is not a property that exists"`) instead of silently dropping them. Pipeline-control columns (`Processed`, `Processing`, `Template Injected`, `Task - Relation`) are absent on mirror targets and get filtered out by the same mechanism.

## Routing config (Notion-driven, no redeploy)

`Meeting Rules` DB (was: Topic Mirror Routes) under Nzyme Home with columns `Match Property` (select: Macro Work Block / Detail / External Org), `Match Value` (text), `Action` (select: Mirror to DB / Fire Affinity LP Funnel (no transcript) / Fire Affinity LP Funnel (with transcript) — the mirror consumes `Mirror to DB` rows only), `Target DB` (url), `Active` (checkbox). One row per rule. The pipeline reloads the table once per cron tick. See `docs/notion-schema.md` for the full property list.

## Confidentiality gate (opt-out)

A meeting can be kept out of the shared topic DBs even when it matches a rule.
The gate runs in `mirror_to_topic_dbs` **after** `match_routes` and **before**
the clone/merge loop — so only meetings that *would* have mirrored are gated.

Two inputs decide it:

- **`Confidential`** — a `select` on each per-member Meeting Notes DB:
  `Confidential` (force-private) / `Shareable` (force-share) / blank.
- **`Default Mirror Visibility`** — a `select` on each Org Chart row:
  `Private` / `Shared`. Read into `MeetingDB.default_mirror_visibility` by
  `discover_meeting_dbs` and threaded down via `_run_topic_mirror`. Used only
  when the meeting's `Confidential` value is blank. The default is keyed to the
  **DB owner** (the member whose Meeting Notes DB the page lives in), not the
  page's Notion `created_by` creator.

An explicit meeting value always wins; blank falls back to the owner default,
which itself defaults to `Shared`:

| Owner default ↓ / Meeting → | blank | `Shareable` | `Confidential` |
|---|---|---|---|
| **Shared** (or unset) | mirror ✅ | mirror ✅ | **skip** 🔒 |
| **Private** | **skip** 🔒 | mirror ✅ | **skip** 🔒 |

The resolver is the pure `mirror_allowed(confidential, owner_default)` in
`src/topic_mirror/confidentiality.py`. A held-back meeting returns
`MirrorStatus.SKIPPED_CONFIDENTIAL` and emits a CloudWatch line at INFO
(`topic mirror outcome: ... status=Skipped: confidential detail=<reason>; held
back from [<routes>]`) — distinct from the silent `NO_MATCH`.

**Graceful degradation / provisioning.** Both columns are *manual* (not
canonical-synced like `Macro Work Block`/`Detail`). A member DB without the
`Confidential` column reads the property as absent → blank → owner default; an
Org Chart row without `Default Mirror Visibility` defaults to `Shared` (mirror
as before). So the gate is fully back-compat and the columns can be added in
Notion at any time. Add `Default Mirror Visibility` (`Private`/`Shared`) to the
Org Chart and `Confidential` (`Confidential`/`Shareable`) to each member
Meeting Notes DB to make the feature usable.

## Cross-DB dedup + contributor merge

- Dedup key = `normalize(Meeting title) + Date.start[:10]`, matched workspace-wide on the target DB.
- **First contributor** processed → full `template_id` clone. Their entire notes container (Action Items + Notes + written notes) rides along verbatim in the cloned `meeting_notes` block. Stamp `Primary Source URL`, write `Owner` (people) with the Notion user UUID of the meeting page's creator, and write `Internal attendees` (people). Then label their notes: prepend a `<Name>'s notes` blue-background H3 at the **top** of the mirror's `notes_block_id` (`append_block_children(..., position={"type": "start"})`) so it sits above their whole section.
- **Subsequent contributor** with the same title+date → no re-clone. Copy their **entire** notes container verbatim from THEIR source page (every child of their `notes_block_id`, preserving block types + `color` via `_block_to_create_format` — nothing sliced), append a `<Name>'s notes` blue-bg H3 + that full copy at the **end** of the mirror's `meeting_notes.notes_block_id`, add their Notion user UUID to `Owner`, and union their member attendees into `Internal attendees`. (Option B.)
- Owner dedup key is the Notion user UUID (not name string), so two people with the same display name don't collapse.
- **Contributor labels** (every contributor, including the first): a `heading_3` with `color: "blue_background"` reading `<Org Chart name>'s notes`. The first contributor's label is prepended at the top of the notes container; each subsequent contributor's label + full notes copy are appended below. `position: start` is used rather than inserting after the `## Notes` heading because members rename that heading (e.g. "Notes [TEST]"), so a text-based anchor isn't reliable. Best-effort: if the async clone hasn't populated the notes container within the poll budget, the first contributor's notes are left unlabeled (still present) — a `Processed=false` re-trigger does not retry the label (Owner is already set), so the poll is generous.
- **`Internal attendees`** (people) = the meeting's attendees that resolve to a real Notion workspace member (`type == "person"`), read from the source page's `meeting_notes.calendar_event.attendees`. External participants aren't Notion users and never appear there, so the membership filter separates the team from any stray guest/bot id. Set at clone time and unioned on every subsequent contributor (kept current even when the notes-merge no-ops). Gracefully skipped on target DBs that don't declare the column.
- **Title cleanup:** Notion auto-names meetings `<GCal title> <ISO datetime>` (e.g. `… modelo 2026-05-29T14:00:00.000+02:00`). The mirror's `Meeting` title is run through `strip_title_datetime()` so it reads cleanly without the raw timestamp. `_normalize_title` also strips the datetime before matching, so dedup is robust whether the existing mirror was stored with or without the suffix.
- Tag removal does not delete mirrors (v1 scope — additive only).

## Owner resolution

The writer takes the Notion user UUID from `metadata['created_by']['id']` (the source page's creator). For meetings auto-created by the Notion AI Meeting integration, this is the meeting host; for manual notes it's the author. If created_by is missing, Owner is left unset on the mirror — the rest of the row still populates correctly.

## Mirror DB schema convention

Narrow on purpose. Each topic DB must declare exactly the columns it wants populated; everything else gets silently dropped by Notion at clone time. Standard column set: `Meeting` (title), `Date`, `Meeting type`, `Detail`, `External Org`, `AI Summary`, `Files & media`, `Owner` (people, multi-person — every contributor accumulates here), `Internal attendees` (people — the meeting's Notion-member attendees; accumulates across contributors), `Governance: Edit & View Access` (people — copied as-is from source), `Primary Source URL`. `Internal attendees` is optional per DB — the writer drops it when absent, so older mirror DBs keep working. `Tasks` was intentionally dropped from the convention on 2026-05-18 — the action items already live in the Team Task Tracker via `Task - Relation` on the source page, no need to mirror their rich-text dump.

## Async clone caveat

`pages.create` returns a blank shell; Notion populates the `meeting_notes` block over ~5–10 s. The writer polls up to ~12 s for the mirror's `notes_block_id` before appending a subsequent contributor's notes. If two members on the same meeting are processed inside the same cron tick AND the poll exhausts, the second contributor's notes are skipped for that tick — they are NOT added to `Contributors`, so a manual `Processed=false` re-trigger on their source page will retry. Rare in practice.

## Race fallback

The writer is fire-and-forget on `Contributors`. If the clone succeeded but the contributor add fails halfway, the next cron processing the same page hits the contributor-already-in list and becomes a noop. Idempotent.

## Visibility (CloudWatch only)

Every successful mirror or failure emits a single grep-friendly log line:
```
topic mirror outcome: page=<short> owner=<member> status=<enum> detail=<text>
```
`status` values: `Posted` (≥1 clone/merge succeeded), `Partial: some routes failed`, `Failed: all routes failed`. `Skipped: no matching route` and `Skipped: feature disabled` are intentionally NOT logged to avoid flooding CloudWatch for untagged pages. Query: `filter @message like /topic mirror outcome:/`.

## Key files

- `src/topic_mirror/__init__.py` — orchestrator `mirror_to_topic_dbs(...)`; returns `MirrorOutcome`. Never raises.
- `src/topic_mirror/outcome.py` — `MirrorStatus` / `MirrorAction` enums + `MirrorOutcome` dataclass.
- `src/topic_mirror/route_registry.py` — loads Routes DB and matches a page's tags against active routes.
- `src/topic_mirror/confidentiality.py` — pure `read_confidential` / `mirror_allowed` resolver for the confidentiality gate (meeting `Confidential` select + owner `Default Mirror Visibility`).
- `src/topic_mirror/notes_extractor.py` — `fetch_notes_blocks_for_clone` copies a contributor's ENTIRE notes container (all children of their `notes_block_id`) to create-format blocks, preserving structure + color.
- `src/topic_mirror/writer.py` — `clone_or_merge` per route: find-or-clone by title+date, then merge or noop. Owns contributor labeling (`_build_contributor_heading`, `_label_first_contributor_notes` — prepends at `position: start`) and `Internal attendees` (`_internal_attendee_ids`, `_update_internal_attendees`).

## Env vars

`TOPIC_MIRROR_ENABLED`, `TOPIC_MIRROR_ROUTES_DB_ID`.
