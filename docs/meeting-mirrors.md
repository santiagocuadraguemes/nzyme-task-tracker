# Meeting Mirrors branch (opt-in)

After the primary task-tracker write, each meeting page is checked against a routing table. Pages tagged with values like `Detail = "AI & Tech"` or `External Org = "White Vega"` get cloned into topic-specific Notion DBs. Off by default; enable with `TOPIC_MIRROR_ENABLED=true` and `TOPIC_MIRROR_ROUTES_DB_ID=<routes DB>`.

## Mechanism

Notion `POST /v1/pages` with `template: {type: "template_id", template_id: <source_page_id>}` clones the source — including the AI-managed `meeting_notes` block (transcript, AI Summary, attendees, notes). Verified empirically in `scripts/replicate_meeting.py`.

**Schema-aware property filtering (writer.py).** Before each clone the writer fetches the target DB's schema, drops any property in the build dict that the target doesn't declare or whose type doesn't match, and PATCHes the target schema to add any missing `select` / `multi_select` option values (preserving the source's color). This is load-bearing under API `2026-03-11`: the `template_id` path now hard-errors on unknown property names (e.g. `"External Org is not a property that exists"`) instead of silently dropping them. Pipeline-control columns (`Processed`, `Processing`, `Template Injected`, `Task - Relation`) are absent on mirror targets and get filtered out by the same mechanism.

## Routing config (Notion-driven, no redeploy)

`Topic Mirror Routes` DB under Nzyme Home with columns `Match Property` (select: Meeting type / Detail / External Org), `Match Value` (text), `Target DB` (url), `Active` (checkbox). One row per topic→DB mapping. The pipeline reloads the table once per cron tick. See `docs/notion-schema.md` for the full property list.

## Cross-DB dedup + contributor merge

- Dedup key = `normalize(Meeting title) + Date.start[:10]`, matched workspace-wide on the target DB.
- **First contributor** processed → full `template_id` clone. Their `## Notes` content rides along in the cloned `meeting_notes` block. Stamp `Primary Source URL`, write `Owner` (people) with the Notion user UUID of the meeting page's creator.
- **Subsequent contributor** with the same title+date → no re-clone. Pull just their `## Notes` content from THEIR source page, append a `### <Name>'s Notes` H3 heading + content **inside** the mirror's `meeting_notes.notes_block_id`, then add their Notion user UUID to the `Owner` people property. (Option B.)
- Owner dedup key is the Notion user UUID (not name string), so two people with the same display name don't collapse.
- The first contributor's notes intentionally stay unlabeled — Notion's `blocks.children.append` has no atomic prepend, so retroactive labeling would require destructive delete+rebuild on AI-cloned content. Documented trade-off; reconsider if it bites.
- Tag removal does not delete mirrors (v1 scope — additive only).

## Owner resolution

The writer takes the Notion user UUID from `metadata['created_by']['id']` (the source page's creator). For meetings auto-created by the Notion AI Meeting integration, this is the meeting host; for manual notes it's the author. If created_by is missing, Owner is left unset on the mirror — the rest of the row still populates correctly.

## Mirror DB schema convention

Narrow on purpose. Each topic DB must declare exactly the columns it wants populated; everything else gets silently dropped by Notion at clone time. Standard column set: `Meeting` (title), `Date`, `Meeting type`, `Detail`, `External Org`, `AI Summary`, `Files & media`, `Owner` (people, multi-person — every contributor accumulates here), `Governance: Edit & View Access` (people — copied as-is from source), `Primary Source URL`. `Tasks` was intentionally dropped from the convention on 2026-05-18 — the action items already live in the Team Task Tracker via `Task - Relation` on the source page, no need to mirror their rich-text dump.

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
- `src/topic_mirror/notes_extractor.py` — pulls a contributor's `## Notes` content from their source page and converts it to create-format blocks.
- `src/topic_mirror/writer.py` — `clone_or_merge` per route: find-or-clone by title+date, then merge or noop.

## Env vars

`TOPIC_MIRROR_ENABLED`, `TOPIC_MIRROR_ROUTES_DB_ID`.
