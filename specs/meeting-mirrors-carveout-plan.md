# Feature: Carve Meeting Mirrors out of the monolith into its own Lambda

> Planning artifact. **Plan only — no code, no deploys, no pipeline runs in the
> planning session.** Companion to `specs/meeting-mirrors-carveout-handover.md`
> (the brief). Step 4 of the Lambda-split migration
> (`docs/architecture-lambda-split.md`), following the `nzyme-fundraising`
> pattern.

## Feature Description

Today the Meeting Mirrors feature (clone tagged meeting pages into topic-specific
Notion DBs, merge later contributors' notes) runs **inside** the monolith's
per-page extraction loop: `src/pipeline.py → run_sync_for_page → _run_topic_mirror
→ src.topic_mirror.mirror_to_topic_dbs`. It shares one deploy, one failure domain,
and one Notion rate-limit budget with task extraction, fundraising, and the sync.

This feature extracts Meeting Mirrors into a **standalone Lambda + repo**
(`nzyme-meeting-mirrors`), scheduled independently, that:

- **decides in Supabase** what/whether to mirror (candidate discovery, routing,
  confidentiality gate, cross-DB dedup discovery, readiness, idempotency) by
  reading the Neo mirror tables `meeting_transcripts`, `meeting_rule_rows`,
  `org_chart_rows` — **no Notion polling for discovery**;
- **acts in Notion** for the operations that are inherently Notion→Notion: the
  `template_id` page clone and the block-level contributor-notes merge;
- owns a **Supabase claim table** (`mirror_meeting_posts`) for idempotency,
  fail-closed;
- has a **dry-run / shadow** no-write mode for a changeset-gated cutover.

### CRITICAL framing — this is NOT a clean copy of fundraising

Fundraising was **zero-Notion** (read Supabase → POST to Affinity). Meeting
Mirrors is **fundamentally Notion→Notion** and two findings (verified against the
live Neo project `yphbrpbwpakjduhmoimw` during planning) make that unavoidable:

1. **`meeting_transcripts.raw` is always `NULL`** (`src/meeting_row.py:245`
   hard-codes `"raw": None`) and the tag columns are **flattened**:
   `macro_work_block`/`external_org` are single-select text, and **`detail` is a
   `", "`-joined string** of the source multi-select (`_select_or_multi_value`,
   `src/meeting_row.py:41`). So Supabase does **not** hold the source page in a
   shape that can rebuild the clone's properties. The clone needs the **live
   source Notion page** (`Meeting type` select, `Detail` multi_select, `External
   Org` select, `AI Summary` rich_text, `Governance: Edit & View Access` people,
   `Date`, `created_by`) — `template_id` duplication then copies the
   `meeting_notes` block server-side.
2. **The contributor merge copies the notes container block-for-block**,
   preserving block types and `color` (`notes_extractor.fetch_notes_blocks_for_clone`
   → `template_injector._block_to_create_format`). `meeting_transcripts.notes` is
   a flattened `blocks_to_text` rendering — **insufficient** for the merge. The
   block read stays a Notion call.

**Net:** the Supabase win here is real but thinner than fundraising — it removes
the per-page coupling and gives independent deploy/schedule/claim, but the
clone/merge mechanics (`writer.py`, `notes_extractor.py`) move across **almost
unchanged** and still talk to Notion. The carve-out replaces the *orchestration*
(discovery, routing, gating, idempotency), not the *mechanics*.

## User Story

As the operator of the Nzyme sync platform (Santiago),
I want Meeting Mirrors to run as its own scheduled Lambda that discovers work from
the Supabase mirror and claims each unit of work idempotently,
So that mirror failures, deploys, and Notion rate-limit usage are isolated from
task extraction and the other workers, and the monolith shrinks toward being just
Sync + Webhook + Housekeeping.

## Problem Statement

Meeting Mirrors is bundled into the monolith's 5-minute extraction tick:

- A mirror bug or a Notion 429 storm during the clone/merge can disrupt or slow
  task extraction (they share the per-page loop and the Notion client).
- It cannot be deployed, paused, or rolled back independently.
- It has **no idempotency claim** of its own — re-run safety relies entirely on
  querying the target Notion DB each tick (`find_existing_mirror`) and on the
  `Processed` flag in the per-member DBs. That's fragile and Notion-bound.
- It is the **only remaining heavy Notion-writing worker** still inside the
  monolith besides Housekeeping, blocking the migration's "workers read the copy"
  end state.

## Solution Statement

Stand up `nzyme-meeting-mirrors` as a separate SAM stack + repo, mirroring
`nzyme-fundraising`'s shape (`config.py` env-driven settings, `supabase_io.py`
stdlib PostgREST client, `candidates.py` discovery, `state.py` claim table,
`runner.py` per-tick loop, `lambda_handler.py`, `main.py` CLI, `template.yaml`,
`scripts/deploy.sh` + `quick-deploy.sh`).

- **Discovery / routing / gating / dedup** become Supabase reads in new modules
  (`candidates.py`, `routing.py`, `confidentiality.py`).
- **Clone/merge mechanics** are ported from `src/topic_mirror/writer.py` +
  `notes_extractor.py` + `outcome.py` essentially verbatim, fed by a
  Supabase-driven candidate instead of the pipeline's `page` dict. They keep
  reading the live source page + target DB from Notion.
- A **minimal `NotionClientWrapper`** + the handful of helper functions the
  mechanics need are copied into the new repo (the heavy Notion surface is the
  cost of this carve-out vs. fundraising's zero-Notion).
- A new Supabase **claim table `mirror_meeting_posts`**, keyed by
  `(page_id, target_db_id)`, provides idempotency and an audit trail.
- **Readiness** is page-quiet on `last_edited_time` (NEVER `meeting_end` — it is
  `NULL` for 0/628 rows, verified).
- Cutover: deploy in **`SHADOW`** (decide + log, zero Notion writes), watch a
  clean tick on real data, flip to live, then disable the in-monolith branch with
  `TOPIC_MIRROR_ENABLED=false` + redeploy, via the **changeset-review** gate.

### Decision: standalone repo + copied minimal Notion client (recommended)

The handover lists "new repo vs module" as an open question. **Recommendation:
new standalone repo** for consistency with the migration's per-worker
deploy/failure isolation (the whole point of the split, and the proven fundraising
shape). The cost — unique to Mirrors — is that it needs a **non-trivial slice of
the monolith's Notion code** (`NotionClientWrapper` + 4 helper modules), so unlike
fundraising's single copied `supabase_io.py`, this repo carries a **duplicated
Notion client**. That duplication is the accepted trade-off; it is called out in
Notes as a future consolidation candidate (a shared `nzyme-notion` package). If
Santiago prefers to avoid the duplication, the fallback is "same monolith repo,
separate SAM stack + handler" — noted but not the recommended path.

## Relevant Files

### Monolith — read/port FROM (do not change behaviour except the retirement flag)

- `src/topic_mirror/__init__.py` — `mirror_to_topic_dbs` orchestrator. **Replaced**
  by the new repo's `runner.py` + `routing.py` + `confidentiality.py`. The
  routing/gate/loop logic is the spec for the new orchestration.
- `src/topic_mirror/writer.py` — `clone_or_merge` and all clone/merge mechanics
  (`_build_clone_properties`, `_filter_to_target_schema`,
  `_ensure_select_options_on_target`, `_clone_into_target`, `find_existing_mirror`,
  `_internal_attendee_ids`, contributor labelling, Owner/Internal-attendees union).
  **Ported nearly verbatim** — it is Notion-side and correct.
- `src/topic_mirror/notes_extractor.py` — `fetch_notes_blocks_for_clone`. **Ported
  verbatim** (needs `_block_to_create_format` + `find_meeting_notes_block`).
- `src/topic_mirror/confidentiality.py` — pure `read_confidential` /
  `mirror_allowed` resolver. **Ported**, but `read_confidential` is re-pointed at
  the Supabase `confidential` column instead of live page properties (pure
  `mirror_allowed` is reused unchanged).
- `src/topic_mirror/outcome.py` — `MirrorStatus` / `MirrorAction` / `MirrorOutcome`.
  **Ported verbatim.**
- `src/topic_mirror/route_registry.py` — `load_routes`/`match_routes` + the
  `Mirror to DB` / Affinity action constants. **DO NOT TOUCH in the monolith** —
  it still backs `config_mirror_sync` and the Affinity actions. The new repo gets
  its own `routing.py` reading `meeting_rule_rows` from Supabase (it does not
  import this module).
- `src/pipeline.py:1322` `_run_topic_mirror` + call sites at `:1551`, `:1599`,
  `:1620` — the current invocation. After cutover these become **dormant** behind
  `config.topic_mirror_enabled`; the retirement step flips the flag (see Step 14).
- `src/meeting_row.py` — the **writer side of the mirror contract**. Confirms
  `raw=None`, `detail` flattening, which columns exist. Read-only reference; the
  new repo depends on this contract (and Step 13 keeps `include_inactive=True` on
  the sync intact).
- `docs/meeting-mirrors.md` — feature behaviour spec (dedup key, contributor
  merge, confidentiality truth table, async-clone caveat, mirror DB schema
  convention). Source of truth for parity.
- `docs/architecture-lambda-split.md` — migration target + rollout order. Update
  Mirrors' status on completion.

### Monolith — Notion code the new repo must COPY (minimal slice)

- `src/notion_client_wrapper.py` — copy the **subset** used by the mechanics:
  `get_page`, `get_block_children`, `query_database`, `append_block_children`
  (incl. `position`), `update_page`, `retrieve_data_source`,
  `update_data_source`, `list_users`, `_call_with_retry` + `_client` (the
  `notion-client` `Client`), and `pages.create`. (Confirm the exact method set
  against `writer.py`/`notes_extractor.py` imports during implementation.)
- `src/transcript_pipeline/fetch_transcript.py` — `find_meeting_notes_block`,
  `extract_attendee_ids`, `strip_title_datetime` (imported by `writer.py`).
- `src/template_injector.py` — `_block_to_create_format` (imported by
  `notes_extractor.py`). Copy this function + its transitive helpers only.
- `src/config.py` (`SyncConfig`) — reference for which Notion/GCal settings the
  copied client needs; the new repo's `config.py` declares only what Mirrors uses
  (Notion token, Supabase creds, rule/readiness knobs). No LLM keys, no GCal.

### Reference — the pattern to copy (do not modify)

- `C:\Users\Santiago Cuadra\vscode_projects\nzyme-fundraising\` — entire repo.
  Especially `src/candidates.py`, `src/state.py`, `src/supabase_io.py`,
  `src/config.py`, `src/runner.py`, `src/lambda_handler.py`, `src/main.py`,
  `template.yaml`, `scripts/deploy.sh` (the `add_param` inner-quoting fix),
  `scripts/quick-deploy.sh`, `tests/`.

### New Files (in the new repo `nzyme-meeting-mirrors/`)

- `src/__init__.py`
- `src/supabase_io.py` — copied verbatim from fundraising (stdlib PostgREST,
  service-role key, `_http` / `_supabase_creds`).
- `src/config.py` — `Settings` (pydantic, frozen, `from_env`). Notion token +
  Supabase creds + readiness/rule knobs + `DRY_RUN`/`SHADOW`/`PARALLEL`.
- `src/notion_client.py` — copied minimal `NotionClientWrapper`.
- `src/notion_helpers.py` — copied `find_meeting_notes_block`,
  `extract_attendee_ids`, `strip_title_datetime`, `_block_to_create_format`,
  `blocks_to_text` (whatever the mechanics transitively need).
- `src/routing.py` — `Route` dataclass + `load_routes_from_supabase` (reads
  `meeting_rule_rows` where `action='Mirror to DB'`, `active`, `deleted_at IS
  NULL`) + `match_routes` (matches a candidate's flattened tags, **splitting
  `detail` on `", "`**).
- `src/confidentiality.py` — ported `mirror_allowed` + a Supabase-sourced
  `read_confidential`.
- `src/candidates.py` — `MirrorCandidate` + `select_candidates` (mirror rows
  matching any active Mirror-to-DB rule value across the 3 tag columns, readiness
  filter, minus terminally-claimed `(page_id, target_db_id)` pairs).
- `src/state.py` — claim table client for `mirror_meeting_posts`
  (`claim_mirror`, `record_outcome`), modeled on fundraising `state.py`.
- `src/writer.py` — ported `clone_or_merge` + mechanics.
- `src/outcome.py` — ported enums + dataclass, extended with per-route claim
  bookkeeping if needed.
- `src/runner.py` — `run_once` per-tick loop (select → per-route claim → fetch
  live page → confidentiality gate → clone/merge → record).
- `src/lambda_handler.py`, `src/main.py` — entry points (copied shape).
- `template.yaml`, `scripts/deploy.sh`, `scripts/quick-deploy.sh`,
  `pyproject.toml`/`requirements.txt`, `README.md`, `docs/how-it-works.md`.
- `tests/` — `conftest.py` + `test_*` mirroring the module layout.
- Supabase migration SQL for `public.mirror_meeting_posts` (applied to the Neo
  project via the changeset/MCP `apply_migration`, reviewed first).

## Implementation Plan

### Phase 1: Foundation

Scaffold the repo from the fundraising template; create the Supabase claim table;
copy the minimal Notion client + helpers; stand up `config.py`/`supabase_io.py`.
Goal: the repo imports, `Settings.from_env()` validates, and a no-op tick can read
Supabase. No Notion writes yet.

### Phase 2: Core Implementation

Build the Supabase decision layer (`routing.py`, `candidates.py`,
`confidentiality.py`, `state.py`) and port the Notion act layer (`writer.py`,
`notes_extractor` into `writer.py`/helpers, `outcome.py`). Wire `runner.run_once`:
select candidates → per (candidate, route) claim → fetch live source page →
confidentiality gate → `clone_or_merge` → `record_outcome`. Add `SHADOW`/`DRY_RUN`.
Full unit-test coverage with mocked Supabase + Notion.

### Phase 3: Integration

Deploy in `SHADOW`, observe a clean tick against real data in CloudWatch, flip to
live (changeset-gated), confirm clones/merges match the legacy branch, then retire
the in-monolith branch (`TOPIC_MIRROR_ENABLED=false` + redeploy) and clean up.
Keep docs in sync.

## The Supabase ↔ Notion boundary (precise enumeration)

**DECIDE — Supabase reads only, no Notion:**

1. **Candidate discovery** — `meeting_transcripts` rows where any of
   `macro_work_block` / `external_org` (exact) or a value in the split `detail`
   set matches an active Mirror-to-DB rule value; `meeting_start >=
   MIRROR_SINCE` and within `MIRROR_CANDIDATE_LOOKBACK_DAYS`; page-quiet
   (`last_edited_time < now - MIRROR_PAGE_QUIET_MIN`).
2. **Routing** — `meeting_rule_rows` (`action='Mirror to DB'`, `active`,
   `deleted_at IS NULL`) → the set of `target_db_id`s each candidate maps to.
3. **Confidentiality gate** — `meeting_transcripts.confidential` +
   `org_chart_rows.default_mirror_visibility` (joined via
   `meeting_transcripts.db_id = org_chart_rows.meeting_notes_db_id`). Pure
   `mirror_allowed`.
4. **Owner display name** for the `<Name>'s notes` heading —
   `meeting_transcripts.owner_name` (the DB owner).
5. **Idempotency filter** — drop `(page_id, target_db_id)` pairs already terminal
   in `mirror_meeting_posts`.

**ACT — inherent Notion reads + writes (unavoidable):**

6. **Fetch the live source page** (`client.get_page(page_id)`) — for clone
   properties (`Meeting type`, `Detail` multi_select, `External Org`, `AI
   Summary` rich_text, `Governance: Edit & View Access` people, `Date`) and the
   **Owner UUID** (`created_by.id` — authoritative on the live page; the mirror's
   `created_by_id` is `NULL` for ~480/628 rows so is not relied on).
7. **`find_existing_mirror`** — query the target Notion DB by `Date` + normalized
   title (the cross-DB dedup probe; also how late contributors find the primary).
8. **Clone path** — `retrieve_data_source` (target schema) →
   `_filter_to_target_schema` → `_ensure_select_options_on_target`
   (`update_data_source`) → `pages.create` with `template_id` →
   `_label_first_contributor_notes` (poll + `append_block_children`
   `position:start`).
9. **Merge path** — `fetch_notes_blocks_for_clone` (read THIS contributor's notes
   container blocks from THEIR source page) → poll for the mirror's
   `notes_block_id` → append `<Name>'s notes` H3 + blocks → union `Owner` +
   `Internal attendees`.
10. **`_internal_attendee_ids`** — read source page blocks + `list_users`.

**Open question RESOLVED:** the merge needs the source notes **block** (Notion),
not `meeting_transcripts.notes` text — block-level fidelity (types + colour) is
required for parity. `notes` text is insufficient.

## Claim / dedup state model

**Table `public.mirror_meeting_posts`, composite PK `(page_id, target_db_id)`.**
One row per (source contributor page, target topic DB).

Rationale for the key:
- A single source page can match **multiple** Mirror-to-DB rules → multiple target
  DBs, so work is per `(page_id, target_db_id)`.
- "Primary clone vs contributor merge" is decided per **meeting identity**
  (`normalize(title)+date[:10]`) **within a target DB** — but that decision is
  made at act-time by `find_existing_mirror` against Notion (the existing mirror
  page is the source of truth for "already cloned"). The claim table does **not**
  need to store the dedup key to be correct; it stores it for audit/observability.
- Idempotency: a terminal status for `(page_id, target_db_id)` means this
  contributor is done for this DB → skip. A **late contributor** is a *different*
  `page_id`, claims its own pair, and `find_existing_mirror` routes it to MERGE
  (not re-clone) automatically. **No time-boxed "merge window" is needed** — the
  merge stays open as long as the primary mirror page exists in Notion.

```sql
-- Supabase migration (Neo project yphbrpbwpakjduhmoimw). Review via changeset /
-- apply_migration; do not blind-apply.
create table if not exists public.mirror_meeting_posts (
    page_id        uuid        not null,   -- source contributor page (joins meeting_transcripts.page_id)
    target_db_id   uuid        not null,   -- topic DB this row mirrored into
    db_id          uuid,                   -- source member Meeting Notes DB (audit)
    owner_name     text,                   -- DB owner display name (audit)
    dedup_key      text,                   -- normalize(title)+date[:10] (audit/observability)
    status         text        not null,   -- claimed | cloned | merged | noop | skipped_confidential | failed
    action         text,                   -- MirrorAction value once known (cloned/merged/noop)
    mirror_page_id uuid,                   -- resulting/target mirror page in the topic DB
    detail         text,                   -- free-text outcome detail (capped)
    attempts       integer     not null default 1,
    claimed_at     timestamptz not null default now(),
    completed_at   timestamptz,
    created_at     timestamptz not null default now(),
    claimed_by     text,                   -- optional invocation/run tag
    primary key (page_id, target_db_id)
);

create index if not exists mirror_meeting_posts_status_claimed_at_idx
    on public.mirror_meeting_posts (status, claimed_at);

alter table public.mirror_meeting_posts enable row level security;
-- No policies: service-role key (RLS bypass) only, same as affinity_meeting_posts.
```

**Claim semantics (ported from fundraising `state.py`, keyed on the pair):**
- Insert-claim with `Prefer: resolution=ignore-duplicates,return=representation`
  on `on_conflict=page_id,target_db_id`; non-empty response = we own this unit.
- `cloned`/`merged`/`noop`/`skipped_confidential` are **terminal**.
- `failed` rows and stale `claimed` rows (older than `STALE_CLAIM_MINUTES`,
  e.g. 45) are re-claimed via a conditional PATCH (server-side WHERE = the
  concurrency guard).
- **Fail closed:** any Supabase error → log ERROR → no claim → no Notion write.
- `record_outcome` writes the terminal status + `action` + `mirror_page_id`.

**Re-run safety belt-and-braces:** even if a claim were lost,
`find_existing_mirror` + the "contributor already in `Owner`" check in
`clone_or_merge` already make a redundant run a NOOP (the legacy design). The claim
table prevents the *double-clone-within-a-tick* race and gives audit, but does not
replace the Notion-grounded idempotency — both layers hold.

## Readiness rule

- **Page-quiet only:** `last_edited_time < now - MIRROR_PAGE_QUIET_MIN`
  (default 30 min, same as fundraising). This guarantees the source page's
  `meeting_notes` block (transcript / AI summary / notes) has settled before we
  clone it. **`meeting_end` is never used** — verified `NULL` for 0/628 rows.
- **Historical floor** `MIRROR_SINCE` (ISO date) so the first deploy can't
  backfill the entire mirror history into the topic DBs.
- **Rolling window** `MIRROR_CANDIDATE_LOOKBACK_DAYS` (default 14) bounds the
  candidate set per tick; terminal pairs age out.
- **Merge latency interaction:** a 2nd contributor arriving days later is fine —
  when their page goes quiet and is within the lookback window, they're selected,
  claim their pair, and merge into the existing primary mirror. The lookback
  window must comfortably exceed the realistic gap between contributors editing
  the same meeting (14 days is generous; document the trade-off, configurable).

## Routing fidelity note (the `detail` flattening trap)

`meeting_rule_rows.match_property` can be `Macro Work Block`, `Detail`, or
`External Org`. The mirror stores `macro_work_block`/`external_org` as plain text
(exact-match) but `detail` as a **`", "`-joined** multi-select string (3/628 rows
currently carry >1 value). `match_routes` MUST split `detail` on `", "` into a set
before membership-testing the rule's `match_value`, otherwise a meeting tagged
`Detail = ["AI & Tech", "Legal DD"]` won't match a `Detail = "AI & Tech"` rule.
Unit-test this explicitly. (The server-side candidate query can pre-filter with
`detail=ilike.*<value>*` per rule value to narrow the set, then the Python
`match_routes` does the exact split-and-match — `ilike` alone would false-match
substrings, so the Python step is authoritative.)

## Step by Step Tasks

IMPORTANT: Execute every step in order, top to bottom. (Implementation steps —
for the later `/implement` run, NOT this planning session.)

### Step 1 — Scaffold the repo
- Create `nzyme-meeting-mirrors/` beside `nzyme-fundraising`. Copy the skeleton:
  `pyproject.toml`/`requirements.txt`, `.gitignore`, `.env.example`,
  `scripts/deploy.sh` + `quick-deploy.sh` (rename function/stack to
  `nzyme-meeting-mirrors`, keep the `add_param` inner-quoting fix verbatim),
  `tests/conftest.py` shell. `git init`, first commit.
- Copy `src/supabase_io.py` **verbatim** from fundraising.

### Step 2 — Create the Supabase claim table
- Write the `mirror_meeting_posts` migration SQL (above). Apply to the Neo project
  via reviewed `apply_migration` (NOT blind). Verify with `list_migrations` +
  a `select` on the empty table.

### Step 3 — Copy the minimal Notion client + helpers
- Create `src/notion_client.py` with the subset of `NotionClientWrapper` the
  mechanics use (enumerate by grepping imports in the ported `writer.py` /
  `notes_extractor.py`). Create `src/notion_helpers.py` with
  `find_meeting_notes_block`, `extract_attendee_ids`, `strip_title_datetime`,
  `_block_to_create_format`, `blocks_to_text`.
- Add a smoke test that the client constructs from a fake token and that helpers
  import. Mock the `notion-client` `Client`.

### Step 4 — `config.py`
- `Settings` (pydantic frozen) with `from_env`: `NOTION_TOKEN`, Supabase creds
  (validated via `_supabase_creds()`), `MIRROR_RULE_*`/readiness knobs
  (`MIRROR_SINCE`, `MIRROR_PAGE_QUIET_MIN`, `MIRROR_CANDIDATE_LOOKBACK_DAYS`),
  `DRY_RUN`, `SHADOW`, `MIRROR_PARALLEL`, `LOG_LEVEL`. Fail loud on missing creds.
  Note: API version `2026-03-11` must be pinned in the copied Notion client.
- `tests/test_config.py`.

### Step 5 — `routing.py`
- `Route` dataclass (`match_property`, `match_value`, `target_db_id`, `label`).
- `load_routes_from_supabase`: GET `meeting_rule_rows?action=eq.Mirror to DB&
  active=eq.true&deleted_at=is.null&select=label,match_property,match_value,
  target_db_id`. Convert `target_db_id` uuid → the dashed form Notion accepts.
- `match_routes(routes, candidate)`: macro/external exact; **detail split on
  `", "`**.
- `tests/test_routing.py` incl. the multi-value `detail` case and an inactive /
  tombstoned rule.

### Step 6 — `confidentiality.py`
- Port `mirror_allowed` verbatim. Add `read_confidential(candidate)` reading the
  Supabase `confidential` column (currently `NULL` for all 628 rows → blank →
  owner default). Resolve owner default from the joined
  `org_chart_rows.default_mirror_visibility` (NULL → `Shared`).
- `tests/test_confidentiality.py` — the full truth table from
  `docs/meeting-mirrors.md`.

### Step 7 — `candidates.py`
- `MirrorCandidate` dataclass (page_id, db_id, owner_name, title, meeting_start,
  last_edited_time, macro_work_block, detail, external_org, confidential,
  default_mirror_visibility-or-resolved).
- `select_candidates(settings, routes, now)`:
  - Query `meeting_transcripts` server-side: `meeting_start >= since` AND `>=
    now-lookback`, `last_edited_time < now-quiet`, ordered `meeting_start.asc`,
    bounded `limit`. (Tag pre-filter: OR across `macro_work_block in (...)`,
    `external_org in (...)`, `detail ilike` per detail rule value — or fetch the
    window and filter in Python for simplicity; choose the simpler correct one
    and document.)
  - Resolve `default_mirror_visibility` per candidate (join or a second batched
    query keyed on `db_id`).
  - For each candidate, compute matched routes via `match_routes`; expand into
    `(candidate, route)` units.
  - Drop units whose `(page_id, target_db_id)` is terminal in
    `mirror_meeting_posts` (batched status query, fundraising-style); count
    `legacy_claimed`/`already_terminal`.
- `tests/test_candidates.py` — readiness boundaries, `meeting_end` ignored,
  multi-route expansion, terminal-filtering, lookback aging.

### Step 8 — `outcome.py`
- Port `MirrorStatus` / `MirrorAction` / `MirrorOutcome` verbatim.

### Step 9 — `writer.py`
- Port `clone_or_merge` + all private mechanics + `find_existing_mirror` +
  `notes_extractor.fetch_notes_blocks_for_clone` (fold into `writer.py` or a
  sibling module). Re-point imports at the copied `notion_client` /
  `notion_helpers`. **No behaviour change** — this is the parity surface.
- `tests/test_writer.py` — port the monolith's topic_mirror writer tests; mock
  Notion. Cover clone (props filtered, options PATCHed, first-contributor label),
  merge (append + Owner/Internal-attendees union), noop (owner already present),
  async-clone poll exhaustion.

### Step 10 — `state.py`
- Port fundraising `state.py`, re-keyed on `(page_id, target_db_id)`:
  `claim_mirror(page_id, target_db_id, db_id, owner_name, dedup_key)` and
  `record_outcome(page_id, target_db_id, status, action, mirror_page_id, detail)`.
  Keep `STALE_CLAIM_MINUTES`, fail-closed, stale/`failed` re-claim.
- `tests/test_state.py` — claim win/lose, terminal skip, stale re-claim, failed
  re-claim, fail-closed on Supabase error.

### Step 11 — `runner.py`
- `run_once(settings, now=None)`:
  1. `load_routes_from_supabase`; `select_candidates` → `(candidate, route)` units
     (+ counts).
  2. Per unit: confidentiality gate (Supabase) → if blocked, in live mode claim +
     `record_outcome(skipped_confidential)`, log, continue.
  3. `SHADOW`: log the would-be action (clone vs merge unknown pre-Notion — log
     "would act"), touch nothing, continue.
  4. Live: `claim_mirror`; if lost → tally `claim_lost`, continue.
  5. `client.get_page(page_id)` (live). `DRY_RUN`: run match/decision but skip the
     write and the claim (mirror the fundraising dry-run shape).
  6. `clone_or_merge(...)` → `record_outcome` with the resulting action +
     `mirror_page_id`.
  7. One structured `meeting-mirror outcome: page=… target=… owner=… status=…
     detail=…` line per unit (parity with the legacy `topic mirror outcome:`
     format; add `[NEW-LAMBDA-WIN]` during the parallel period).
- Per-unit `try/except` so one bad page can't kill the tick; top-level
  discovery errors propagate (cron retries).
- `tests/test_runner.py` — shadow/dry-run/live paths, claim-lost, gate-blocked,
  per-unit failure isolation.

### Step 12 — Entry points + infra
- `src/lambda_handler.py`, `src/main.py` (CLI `--once --dry-run --shadow
  --verbose`), `template.yaml` (Function `nzyme-meeting-mirrors`, handler,
  `MemorySize`/`Timeout` sized for Notion I/O + the async-clone polls — Timeout
  ≥ 300s given `_NOTES_BLOCK_POLL_*` waits and multi-route pages; JSON logging;
  `Schedule rate(15 minutes)`, `Enabled: true`; all params with the env mapping
  + the fundraising-style `DRY_RUN`/`SHADOW`/`PARALLEL` switches). `README.md` +
  `docs/how-it-works.md`.

### Step 13 — Pre-cutover verification (no monolith change yet)
- Confirm the monolith Sync keeps `include_inactive=True` and that
  `config_mirror_sync` + `route_registry` Affinity actions are untouched.
- Build + deploy the new stack via the **changeset-review** gate
  (`--no-execute-changeset`, review, `aws cloudformation execute-change-set`),
  with `SHADOW=true`. Give Santiago the commands; he runs them.
- Watch CloudWatch for one clean SHADOW tick on real data: candidates discovered,
  routes matched, gate decisions, zero Notion writes, zero claims. Diff the
  would-be actions against what the monolith actually mirrored.

### Step 14 — Cutover + monolith retirement
- Flip the new Lambda to live (`SHADOW=false`, keep `MIRROR_PARALLEL=true`
  briefly) via changeset gate. Watch a live tick: clones/merges land in the topic
  DBs identically to the legacy branch; claim rows go terminal; `[NEW-LAMBDA-WIN]`
  lines show whenever the new Lambda beat the (still-running) monolith branch.
- Once clean, **disable the in-monolith branch**: set `TOPIC_MIRROR_ENABLED=false`
  in the monolith's deploy config and redeploy (changeset gate). The monolith's
  `_run_topic_mirror` early-returns; `mirror_to_topic_dbs` returns `DISABLED`.
- Set `MIRROR_PARALLEL=false` on the new Lambda (drops the `[NEW-LAMBDA-WIN]`
  tagging / legacy-claimed INFO noise).
- Cleanup (follow-up commit, mirroring how fundraising's in-monolith branch was
  removed in `086fcc0`): delete `src/topic_mirror/__init__.py` orchestrator usage
  + `_run_topic_mirror` + its call sites from `src/pipeline.py`; remove dead
  `TOPIC_MIRROR_ENABLED`/`MEETING_RULES_DB_ID` config **only if** nothing else
  uses them (confirm `route_registry`/`config_mirror_sync` independence first).
  Keep `route_registry.py` (still used by `config_mirror_sync`).

### Step 15 — Docs + validation
- Update `docs/meeting-mirrors.md` (monolith) with a "migrated to
  `nzyme-meeting-mirrors`" banner (mirroring `docs/fundraising-affinity.md`).
  Update `docs/architecture-lambda-split.md` Mirrors status → done; mark rollout
  step 4 complete.
- Run the Validation Commands (below) in both repos.

## Testing Strategy

### Unit Tests
- **routing**: exact macro/external match; **`detail` multi-value split**;
  inactive/tombstoned rules skipped; unparseable target ignored.
- **confidentiality**: full truth table (`Confidential`/`Shareable`/blank ×
  `Private`/`Shared`/unset); Supabase `confidential` NULL → owner default; missing
  org row → `Shared`.
- **candidates**: page-quiet boundary (just-under vs just-over `quiet_min`);
  `meeting_end` never consulted; `since` floor; lookback aging; multi-route
  expansion; terminal-pair filtering; legacy-claimed counting.
- **state**: claim insert-win; lost insert → terminal skip; `failed` re-claim;
  stale `claimed` re-claim; fail-closed on Supabase error; composite-key isolation
  (same page_id, two target DBs claim independently).
- **writer** (ported): clone property filtering + select-option PATCH +
  first-contributor label; merge append + Owner/Internal-attendees union; noop
  when owner already present; async-clone poll exhaustion → no Owner update (retry
  later).
- **runner**: shadow (no writes/claims), dry-run (match but no write/claim), live
  happy path, claim-lost, gate-blocked → `skipped_confidential`, per-unit
  exception isolation, top-level discovery error propagates.
- **config**: `from_env` validation; missing creds raise; bool/csv parsing.

All Notion + Supabase clients mocked (fundraising `conftest.py` + the monolith's
`mock_client` fixture as templates). 100% mocked — no live calls in unit tests.

### Integration Tests
- **SHADOW tick against the real Neo mirror** (read-only): `python -m src.main
  --once --shadow` locally → confirm candidate counts, route matches, and gate
  decisions are sane and match expectations from the live data (5 active
  Mirror-to-DB rules, the tagged meetings). Santiago runs it.
- **DRY_RUN tick**: same, plus the live `get_page` read path exercised, still no
  writes/claims.
- **One live mirror end-to-end** on a deliberately-tagged test meeting: clone into
  a scratch topic DB, then a 2nd contributor page → merge. Verify against the
  monolith's behaviour. Santiago runs it.

### Edge Cases
- **Late tagging (named, must-test improvement over the monolith).** A meeting
  tagged (e.g. `External Org = White Vega`) hours/days *after* it was first
  processed. The monolith MISSES this (its per-page loop short-circuits on the
  `Processed` checkbox at `pipeline.py:1466-1470`, so the mirror step never re-runs).
  The new design MUST catch it: the tag edit bumps `last_edited_time`, the Sync
  updates the Supabase row, and discovery is Supabase-driven (not `Processed`-gated),
  so the meeting becomes a candidate once page-quiet — provided it is within
  `MIRROR_CANDIDATE_LOOKBACK_DAYS` and past `MIRROR_SINCE`. Test: a row tagged after
  a prior terminal claim *for a different target DB* is mirrored into the new DB;
  a row tagged within the window with no prior claim is mirrored; a row tagged
  *outside* the lookback window is NOT (document this limit; window is configurable).
- **Additive re-tagging.** An already-mirrored meeting gains a second tag pointing
  to a *different* target DB → mirrored into the new DB too (claims are per
  `(page_id, target_db_id)`); the original DB's terminal claim is untouched. Tag
  *removal* never deletes a mirror (v1 additive-only scope, parity with the monolith).
- Source page deleted/archived between discovery and `get_page` (404) → soft-fail
  the unit, record `failed`, continue (port the monolith's archived-race handling).
- A page matching **multiple** Mirror-to-DB routes → independent claims per target
  DB; one route failing leaves the others' claims intact (PARTIAL_FAILURE parity).
- **Multi-contributor merge (named, must-preserve behaviour).** A 2nd/3rd person
  taking notes on the same meeting: their notes are appended into the existing
  mirror under a `<Name>'s notes` heading, **block-for-block (types + colour
  preserved, not flattened text)**, with Owner + Internal attendees accumulating.
  Ported from `writer.py`/`notes_extractor.py` essentially verbatim; tested for
  exact parity with the monolith.
- Late 2nd contributor (hours/days later, within lookback) → MERGE not re-clone,
  discovered independently when THEIR page goes quiet (more reliable than the
  monolith, which required their page to re-enter the extraction loop).
- 2nd contributor's page not yet quiet → deferred (not selected) until quiet.
- `detail` multi-value meeting matching some-but-not-all detail rules.
- Async clone still populating when a same-tick 2nd contributor is processed →
  poll exhaustion → contributor not added to Owner, retried next tick (claim stays
  non-terminal? — design: on poll-exhaustion record `noop`/keep `claimed` so a
  later tick retries; match the monolith's "Owner not updated → retry" intent).
- Confidential column populated in future (currently 0/628) → gate must honour it.
- Supabase unreachable mid-tick → fail closed, no Notion writes.

## Acceptance Criteria

- New repo `nzyme-meeting-mirrors` deploys as its own SAM stack
  (`nzyme-meeting-mirrors`, company account, eu-west-1, `rate(15 minutes)`).
- `mirror_meeting_posts` exists in the Neo project with composite PK
  `(page_id, target_db_id)`, RLS-enabled, no policies.
- A SHADOW tick reads candidates from Supabase, matches routes, applies the gate,
  and writes nothing to Notion or the claim table.
- A live tick clones first contributors and merges later contributors **identically
  to the legacy monolith branch** (same dedup key, same `<Name>'s notes` labelling,
  same Owner/Internal-attendees accumulation, same property filtering), idempotent
  across re-runs.
- Readiness keys off `last_edited_time` page-quiet only; `meeting_end` is never
  referenced.
- **Late-tagged meetings are caught** (within the lookback window) — a meeting
  tagged after it was first processed is mirrored, unlike the monolith. The
  lookback window is configurable and its limit is documented.
- **Multi-contributor merge is preserved at full fidelity** — later note-takers'
  notes merge block-for-block (types + colour) into the existing mirror, never
  re-cloning, identical to the monolith.
- `detail` multi-value routing works (split on `", "`).
- The monolith branch is disabled via `TOPIC_MIRROR_ENABLED=false` and later
  removed, with `route_registry.py` + `config_mirror_sync` + the Affinity actions
  + `include_inactive=True` on the sync all intact.
- All unit tests pass in both repos; `ruff` clean.
- Docs updated (`docs/meeting-mirrors.md`, `docs/architecture-lambda-split.md`,
  new repo `README.md` + `docs/how-it-works.md`).

## Documentation Update (MANDATORY)

### README.md (new repo)
- [x] Feature description (Notion→Notion mirror worker; decide-in-Supabase /
      act-in-Notion), "How it works" (select → claim → fetch live page → clone/merge
      → record), the readiness rule, the `detail`-split routing note.
- [ ] Installation: shared `../venv`, `requirements.txt`, `notion-client` dep.
- [ ] Configuration: every env var (`NOTION_TOKEN`, `SUPABASE_*`, `MIRROR_*`,
      `DRY_RUN`/`SHADOW`/`MIRROR_PARALLEL`, `LOG_LEVEL`).
- [ ] Usage: `python -m src.main --once [--dry-run|--shadow]`; deploy scripts.

### API Documentation
- [ ] N/A (no HTTP API). Document the `mirror_meeting_posts` schema + claim state
      machine in `docs/how-it-works.md` instead.

### Technical Docs
- [ ] `docs/architecture-lambda-split.md` — flip Mirrors to done; mark rollout
      step 4 complete; update the picture/inventory.
- [ ] `docs/meeting-mirrors.md` (monolith) — "migrated to nzyme-meeting-mirrors"
      banner (like `docs/fundraising-affinity.md`); keep the behaviour spec as the
      parity reference.
- [ ] New repo `docs/how-it-works.md` — boundary table, claim model, readiness,
      cutover/retirement.

## Validation Commands

Execute to validate with zero regressions. **Santiago runs all pipeline/deploy
commands** — provide them, do not execute. PowerShell on Windows; venv at
`../venv/`.

New repo (`nzyme-meeting-mirrors`):
- `../venv/Scripts/python -m pytest tests/ -v` — all unit tests pass.
- `../venv/Scripts/python -m ruff check src/ tests/` — lint clean.
- `python -m src.main --once --shadow --verbose` — SHADOW tick: candidates +
  routes + gate decisions logged, **zero** Notion/claim writes.
  *(Notion endpoint = `api.notion.com` via `NOTION_TOKEN`; Supabase via
  `SUPABASE_URL` + service-role `SUPABASE_KEY`. No LLM keys — this worker is
  zero-LLM.)*
- `python -m src.main --once --dry-run --verbose` — DRY_RUN: live `get_page`
  exercised, no writes/claims.
- Deploy (changeset-gated): `sam build` then `sam deploy ... --no-execute-changeset`,
  review, `aws cloudformation execute-change-set ...` (company profile).

Monolith (`nzyme-task-tracker`), after the retirement step:
- `../venv/Scripts/python -m pytest tests/ -v` — green with the branch disabled /
  removed.
- `../venv/Scripts/python -m ruff check src/ tests/`.
- Confirm `config_mirror_sync` + `route_registry` tests still pass (Affinity
  actions intact).

## Notes

- **Notion-client duplication is the accepted cost** of this carve-out (fundraising
  needed none). A future consolidation could extract a shared `nzyme-notion`
  package consumed by the monolith + this repo + a future Extraction repo; out of
  scope here. Track it as tech debt.
- **The Supabase win is narrower than fundraising's**: discovery/routing/gating/
  idempotency move to Supabase, but every clone/merge still does live Notion
  reads + writes. The value is decoupling + independent deploy/claim, not
  eliminating Notion I/O.
- **Two-key model note (CLAUDE.md):** this worker uses **no LLM keys**. The only
  external creds are `NOTION_TOKEN` + the Supabase service-role key. Surface that
  in run commands so a failing run points at the right credential.
- **GCal:** not needed — attendee resolution for the mirror happens in the Sync
  (`meeting_row.extract_row(resolve_attendees=True)`); Mirrors reads `Internal
  attendees` from the live page's `meeting_notes` block exactly as the monolith
  does, no GCal calls.
- **`raw` column:** if a future change made the Sync populate `meeting_transcripts.raw`
  with the full page payload, the clone-property build could move to Supabase and
  the only remaining Notion call would be the `template_id` clone itself + the
  merge block reads. Not proposed now (the Sync writes `raw=None`), but noted as
  the path to a thinner Notion surface.
- **No @mentions** in any Notion content the merge appends (the ported
  `_block_to_create_format` already strips mention tags — verify the cleanup
  survives the copy).
- **No silent failures:** keep the `MirrorOutcome` soft-fail-per-unit-with-a-logged
  -line shape (the sanctioned exception); let discovery/config errors crash the
  tick visibly.
- **Confidential is 0/628 today** — the gate is currently driven entirely by the
  24 org rows with `default_mirror_visibility` set; the meeting-level override is
  wired but dormant. Test both anyway.
