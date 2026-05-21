# Feature: Detail + External Org canonical-driven appliers (PR4)

## Feature Description

Extend the PR2 canonical-driven applier pattern to the remaining two shared member-DB select properties: `Detail` (multi-select, ~33 options today) and `External Org` (select, ~21 options today). After this PR, **every shared dropdown on every Meeting Notes DB** is propagated automatically from a single source of truth — adding, renaming, archiving, or removing an option in one place flows to all 9 member DBs the next morning.

Two new sources, mirrored from the architectures the user picked when this spec was drafted:

* **`Detail`** — new Notion Settings DB (`Detail Options`) acts as the editing surface, mirroring the Hierarchy DB pattern that already works for Work area. Each Detail row has a Name, a Color (Notion's standard select colors), a Parent Work area relation (→ Hierarchy DB Tier 0), and an Active flag.
* **`External Org`** — Supabase-only source. Reads `public."ReportingNz_deals"` filtered by `stage` (Portfolio + 3 dealflow stages) — no Notion editing surface for the option list itself, because the canonical record already lives in Supabase via Affinity sync. Each option is linked to a Hierarchy DB Tier 1 / Tier 2 row (PortCos under `Value Creation for Portfolio`, Dealflow companies under `Dealflow - Main Opportunities`).

The orchestrator (`src/hierarchy/__init__.py`) extends to 6 sub-syncs in order: 2 canonical mirrors → 3 appliers → tracker reconciler.

Cost: **zero LLM**. Pure Notion + Supabase REST.

## User Story

As Santiago,
1. I want **Detail** options to live in one place so renaming `Tech DD` once propagates to every team member's DB without me coordinating manually,
2. I want **External Org** options to reflect the live state of the deal pipeline (Portfolio + active dealflow) without anyone hand-editing 9 dropdowns when a deal moves stage,
3. I want each External Org option **linked** to its Hierarchy DB row (Tier 1 PortCo / Tier 2 Workstream) so the analytical join "what meetings touched Project Lavare" is one SQL query, not a 9-DB Notion crawl.

So that the same canonical-driven pattern PR2 introduced for `Work area` covers every shared dropdown — and adding a new member DB is **bootstrap-only**: the first sync run after the DB is created auto-discovers the real option_ids via the existing match-by-sanitized-name path, with zero manual seeding.

## Problem Statement

Today:

* **`Detail`** options drift independently on each member DB. There's no source of truth — renaming `Talent management` to `People & Talent` would require editing 9 DBs by hand. Two DBs already show drift (`Operations` vs `Operations and AI enablement` partial overlap; legacy options like `1:1` and `Commitee` linger). No way to add a new option centrally.
* **`External Org`** options are tagged on meetings but **manually maintained per-DB** despite the underlying entities (companies / deals) already living in `public."ReportingNz_deals"` (synced from Affinity). When a deal moves from `Under analysis (team assigned, moderate effort)` → `Discarded`, the option stays in every member DB as visual cruft. Six of 9 DBs have stale company options like `Cremalleras Rubí` and `Bip&Drive` that aren't on the current pipeline.
* **Hierarchy DB Tier 1 / Tier 2** rows already exist for many of the active deals (e.g. `Project Lavare`, `Civislend`, `Azenea`, `Kuma`) but there's no machine-readable link between those rows and the corresponding `ReportingNz_deals` rows. Joining "meetings → tasks → deals" today requires manual name-matching across two systems.
* **New member DBs** onboard today only if someone notices and seeds option_ids manually (see the 2026-05-21 incident where my hand seed used URL-encoded option_ids that didn't match the raw API ids → planner couldn't find them → bootstrap fallback failed for drift rows → 3 duplicate options created on Jacob + Santiago, recovered via `scripts/cleanup_stray_work_area_options.py`).

## Solution Statement

Three new sub-syncs + two new Notion artifacts + four new Supabase tables, mirroring PR2's canonical → applier shape:

1. **`detail_canonical_mirror_sync`** — Notion `Detail Options` Settings DB → `public.detail_rows` (parallel to `hierarchy_rows`). One-way mirror. Detects created/edited/deleted/reactivated. Writes per-tick change log to `public.detail_sync_runs`.
2. **`detail_applier_sync`** — `detail_rows` → every active member DB's `Detail` multi-select. Same pure-planner + I/O `sync()` shape as `macro_block_sync`. Mapping table `public.detail_option_mappings` pins `(detail_notion_page_id, member_db_id) → option_id`. Multi-select doesn't change the applier semantics — the property type just lets pages carry multiple option ids; id-preserving rename still works because we PATCH the data source schema, not the per-page values.
3. **`external_org_applier_sync`** — `public."ReportingNz_deals"` filtered by stage → every active member DB's `External Org` select. No canonical mirror needed (ReportingNz_deals IS the canonical). Mapping table `public.external_org_option_mappings` pins `(deal_id, member_db_id) → option_id`. Stage-driven color: Portfolio → orange; the three dealflow stages → blue. Stage-driven sort order: Portfolio first (alpha within), then DD phase (alpha), then Working on a deal (alpha), then Under analysis (alpha).
4. **`deal_hierarchy_links`** — separate Supabase table for the analytical join. One row per `(deal_id, hierarchy_page_id)` pairing. Populated lazily by `external_org_applier_sync` via match-by-sanitized-name against `hierarchy_rows` children of the two Tier 0 rows specified by the user (`Value Creation for Portfolio` for Portfolio-stage deals; `Dealflow - Main Opportunities` for the three dealflow stages). Unmatched deals get an option but no link row (logged as a detail warning; operator creates the matching Hierarchy row, next tick links automatically).

Bootstrap-by-sanitized-name path stays unchanged on the planner side — that's the mechanism that auto-discovers real option_ids for new member DBs on first contact. The PR2 incident proved it works on the 3 in-sync rows; the only failure mode is name drift, addressed in §"New member DB onboarding" below.

### What this PR explicitly does NOT do

* Auto-create Hierarchy DB Tier 1 / Tier 2 rows for new deals. Operator creates them in Notion; next tick links them via name match. (Reasoning: the Hierarchy DB is the human editing surface for the project taxonomy — automating row creation from a data feed would surprise the operator and risk creating spurious rows on noise data.)
* Sync option **colors** for Detail beyond the value stored in the Settings DB's `Color` column. Operator picks the color when creating the Detail row; applier writes that color to every member DB. Existing operator-set colors on member DBs are overwritten on first sync (one-time correction).
* Reverse-flow any data Supabase → Notion → Affinity. ReportingNz_deals → External Org options is one-way.
* Centralize Color for Work area itself (PR5 territory if we want that).
* Garbage-collect orphan `(archived) X` options on member DBs.

## Relevant Files

### Existing files referenced (no behaviour change, just extension)

* `src/hierarchy/__init__.py` — `_SUB_SYNCS` registry. Extend with three new entries in the order specified in §"Implementation Plan".
* `src/hierarchy/base.py` — `SyncReport`. No schema change required; existing counters cover the new sub-syncs' vocabulary.
* `src/hierarchy/canonical_mirror_sync.py` — re-export `_http`, `_supabase_creds`. New sub-syncs cross-import from here, matching the convention `macro_block_sync` + `tracker_applier_sync` established.
* `src/hierarchy/macro_block_sync.py` — `_sanitize_option_name`. New appliers cross-import from here (same sanitization rules apply to Detail + External Org option names on the Notion side).
* `src/meeting_db_registry.py` — `discover_meeting_dbs` is reused verbatim.
* `src/notion_client_wrapper.py` — `retrieve_data_source` + `update_data_source` already cover the new appliers' needs.
* `src/main.py` — the existing `--sub-sync NAME` CLI flag accepts new sub-sync names with zero code change (it filters from whatever `_SUB_SYNCS` exposes).
* `.env.example` — adds one new env var: `DETAIL_OPTIONS_DB_ID`.
* `template.yaml` — adds `DETAIL_OPTIONS_DB_ID` to the Lambda env. No new IAM permissions (Supabase + Notion creds already wired).
* `scripts/deploy.sh` — pass-through for `DETAIL_OPTIONS_DB_ID`.

### New files

* `src/hierarchy/detail_canonical_mirror_sync.py`
* `src/hierarchy/detail_applier_sync.py`
* `src/hierarchy/external_org_applier_sync.py`
* `tests/hierarchy/test_detail_canonical_mirror_sync.py`
* `tests/hierarchy/test_detail_applier_sync.py`
* `tests/hierarchy/test_external_org_applier_sync.py`
* `specs/hierarchy-detail-external-org-applier.md` — this spec.
* (Migrations applied via Supabase MCP — DDL text lives in §"Step by Step Tasks" / Step 1 for traceability.)

### New Notion artifacts (created manually by Santiago before deploy)

* **`Detail Options`** database, created inside the existing `Settings` page (or a new sibling). Schema:
  * `Name` — title
  * `Color` — select, options = the 10 Notion standard colors (`default`, `gray`, `brown`, `orange`, `yellow`, `green`, `blue`, `purple`, `pink`, `red`)
  * `Parent Work area` — relation → Hierarchy DB (single-direction is fine; the relation can be left two-way if the operator prefers a back-reference on Hierarchy DB)
  * `Active` — checkbox

  Initial population: one row per Detail option that currently exists on the consolidated member-DB set (~33 rows; the operator picks parent Work area + color matching today's per-DB color convention).

## Implementation Plan

### Phase 1: Foundation

1. **Create the Notion Settings DB** (manual; one-off). Spec above.
2. **Apply 4 Supabase migrations** via `mcp__claude_ai_Supabase__apply_migration`, project `yphbrpbwpakjduhmoimw`:

```sql
-- 1. detail_rows — canonical mirror of the Detail Options Settings DB
CREATE TABLE public.detail_rows (
    notion_page_id           text PRIMARY KEY,
    name                     text NOT NULL,
    color                    text NOT NULL DEFAULT 'default',
    parent_hierarchy_page_id text,  -- FK-in-spirit to hierarchy_rows.notion_page_id
    active                   boolean NOT NULL DEFAULT true,
    first_seen_at            timestamptz NOT NULL DEFAULT now(),
    last_seen_at             timestamptz NOT NULL DEFAULT now(),
    last_changed_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at               timestamptz
);
CREATE INDEX detail_rows_parent_idx
    ON public.detail_rows (parent_hierarchy_page_id) WHERE deleted_at IS NULL;
CREATE INDEX detail_rows_active_idx
    ON public.detail_rows (active) WHERE deleted_at IS NULL;
ALTER TABLE public.detail_rows ENABLE ROW LEVEL SECURITY;
COMMENT ON TABLE public.detail_rows IS
    'Canonical mirror of the Notion Detail Options Settings DB. '
    'Updated by src/hierarchy/detail_canonical_mirror_sync.py.';
```

```sql
-- 2. detail_sync_runs — audit per tick (parallel to hierarchy_sync_runs)
CREATE TABLE public.detail_sync_runs (
    id                bigserial PRIMARY KEY,
    ran_at            timestamptz NOT NULL DEFAULT now(),
    rows_created      integer NOT NULL DEFAULT 0,
    rows_edited       integer NOT NULL DEFAULT 0,
    rows_deleted      integer NOT NULL DEFAULT 0,
    rows_reactivated  integer NOT NULL DEFAULT 0,
    rows_unchanged    integer NOT NULL DEFAULT 0,
    errors            integer NOT NULL DEFAULT 0,
    changes           jsonb
);
CREATE INDEX detail_sync_runs_ran_at_idx
    ON public.detail_sync_runs (ran_at DESC);
ALTER TABLE public.detail_sync_runs ENABLE ROW LEVEL SECURITY;
```

```sql
-- 3. detail_option_mappings — pin (detail row, member DB) → Notion option_id
CREATE TABLE public.detail_option_mappings (
    detail_notion_page_id text NOT NULL,
    member_db_id          text NOT NULL,
    option_id             text NOT NULL,
    option_name           text NOT NULL,
    last_synced_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (detail_notion_page_id, member_db_id)
);
CREATE INDEX detail_option_mappings_member_idx
    ON public.detail_option_mappings (member_db_id);
ALTER TABLE public.detail_option_mappings ENABLE ROW LEVEL SECURITY;
COMMENT ON TABLE public.detail_option_mappings IS
    'Maps (Detail Settings row, member Meeting Notes DB) → the Notion select '
    'option id used for `Detail`. Maintained by src/hierarchy/detail_applier_sync.py.';
```

```sql
-- 4. external_org_option_mappings — pin (ReportingNz_deals row, member DB) → option_id
CREATE TABLE public.external_org_option_mappings (
    deal_id        uuid NOT NULL,  -- FK-in-spirit to ReportingNz_deals.id
    member_db_id   text NOT NULL,
    option_id      text NOT NULL,
    option_name    text NOT NULL,
    last_synced_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (deal_id, member_db_id)
);
CREATE INDEX external_org_option_mappings_member_idx
    ON public.external_org_option_mappings (member_db_id);
ALTER TABLE public.external_org_option_mappings ENABLE ROW LEVEL SECURITY;
COMMENT ON TABLE public.external_org_option_mappings IS
    'Maps (ReportingNz_deals row, member Meeting Notes DB) → the Notion select '
    'option id used for `External Org`. Maintained by '
    'src/hierarchy/external_org_applier_sync.py.';
```

```sql
-- 5. deal_hierarchy_links — analytical join between deals + Hierarchy rows
CREATE TABLE public.deal_hierarchy_links (
    deal_id           uuid PRIMARY KEY,  -- 1:1 (a deal has at most one hierarchy row)
    hierarchy_page_id text NOT NULL,     -- → hierarchy_rows.notion_page_id
    matched_by        text NOT NULL,     -- 'sanitized_name' | 'manual' (future)
    first_linked_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX deal_hierarchy_links_hierarchy_idx
    ON public.deal_hierarchy_links (hierarchy_page_id);
ALTER TABLE public.deal_hierarchy_links ENABLE ROW LEVEL SECURITY;
COMMENT ON TABLE public.deal_hierarchy_links IS
    'Analytical join: each ReportingNz_deals row → the Hierarchy DB row '
    '(Tier 1 PortCo under Value Creation, or Tier 2 Workstream under '
    'Dealflow - Main Opportunities) that represents the same deal. '
    'Populated lazily by external_org_applier_sync via match-by-sanitized-name.';
```

3. **`.env` + `template.yaml` + `scripts/deploy.sh`** — add `DETAIL_OPTIONS_DB_ID`. No new secrets; the existing `SUPABASE_KEY` / `NOTION_API_TOKEN` cover everything.
4. **`src/config.py`** — add `detail_options_db_id: str | None` to `SyncConfig`; read from env.

### Phase 2: Core implementation

#### 2a. `src/hierarchy/detail_canonical_mirror_sync.py`

Near-copy of `canonical_mirror_sync.py`. Differences:

* Mirrors a SMALLER row schema: `(notion_page_id, name, color, parent_hierarchy_page_id, active)` — no Tier (always flat), no Tracker Node, no Notes.
* `_MIRRORED_FIELDS = ("name", "color", "parent_hierarchy_page_id", "active")`.
* `_load_notion_snapshot` reads the `Color` select and the `Parent Work area` relation (first id).
* Writes to `public.detail_rows` + appends one row to `public.detail_sync_runs` per tick.
* Diff types: created / edited / deleted / reactivated / unchanged — identical semantics to PR1.

`SUB_SYNC_NAME = "detail_canonical_mirror_sync"`.

#### 2b. `src/hierarchy/detail_applier_sync.py`

Near-copy of `macro_block_sync.py`. Differences:

* Reads `detail_rows` instead of `hierarchy_rows` Tier 0.
* Writes `Detail` (type=`multi_select`) instead of `Work area` (type=`select`).
  * The PATCH payload shape is identical: `{Detail: {multi_select: {options: [...]}}}`.
  * `retrieve_data_source` returns options under `.properties["Detail"].multi_select.options` — same accessor pattern.
* When PATCHing, **include `color` on every option** — color is canonical-driven for Detail (read from `detail_rows.color`). This overrides whatever color the option had previously on each member DB; first run normalizes colors across all DBs.
* Mapping table: `detail_option_mappings`, PK `(detail_notion_page_id, member_db_id)`.
* `SUB_SYNC_NAME = "detail_applier_sync"`.

Reuses `_sanitize_option_name` from `macro_block_sync` (cross-import). Reuses `_http`, `_supabase_creds` from `canonical_mirror_sync`. Reuses `discover_meeting_dbs` from `meeting_db_registry`.

The planner is identical to PR2's `_plan_member_db_update` modulo: (a) `desired_color` is part of the comparison key for "did this option change?"; (b) the property type string in the PATCH is `multi_select`.

#### 2c. `src/hierarchy/external_org_applier_sync.py`

Same applier shape but with **no canonical mirror** — the source is read live from Supabase each tick:

```sql
SELECT id, name, stage
FROM public."ReportingNz_deals"
WHERE is_active = true
  AND stage IN (
    'Portfolio',
    'DD phase',
    'Working on a deal (significant effort)',
    'Under analysis (team assigned, moderate effort)'
  )
ORDER BY
  CASE stage
    WHEN 'Portfolio' THEN 0
    WHEN 'DD phase' THEN 1
    WHEN 'Working on a deal (significant effort)' THEN 2
    WHEN 'Under analysis (team assigned, moderate effort)' THEN 3
  END,
  name;
```

Today's row counts (verified 2026-05-21): Portfolio=4, DD phase=1, Working on a deal=3, Under analysis=5 → **13 options total** in External Org per member DB after first run.

**Color rule**: `Portfolio → orange`; the three dealflow stages → `blue`. Encoded as a `_STAGE_TO_COLOR` constant at the module top.

**Hierarchy linkage** (populates `deal_hierarchy_links`): for each deal,
- Portfolio stage → search `hierarchy_rows` for the Tier 1 child of `Value Creation for Portfolio` (`c3a645bf-edae-4176-9373-4b0f958f3c72`) whose `_sanitize_option_name(name) == _sanitize_option_name(deal.name)`. Match → upsert `(deal_id, hierarchy_page_id)`. No match → log warning, skip the link (option still gets created/synced).
- The three dealflow stages → search `hierarchy_rows` for the Tier 2 child of `Dealflow - Main Opportunities` (`009aebf3-8d24-4862-b67f-0978390db56b`) whose sanitized name matches. Same fallback semantics.

**Mapping + planner**: identical pattern to `macro_block_sync` (PR2). Bootstrap-adopt-by-sanitized-name still applies on first contact with a member DB. Stage transitions OUT of the filter (e.g., Portfolio → Discarded) → the deal disappears from the canonical read → existing mapping found → CASE B "mapping has option_id but row missing from canonical" → rename the option to `(archived) X`. (This is a new CASE in the planner; PR2 doesn't have it because hierarchy_rows tombstones rather than disappears.)

`SUB_SYNC_NAME = "external_org_applier_sync"`.

#### 2d. Update `src/hierarchy/__init__.py`

Final `_SUB_SYNCS` order:

```python
_SUB_SYNCS: list[SubSync] = [
    canonical_mirror_sync.sync,         # Hierarchy DB → hierarchy_rows
    detail_canonical_mirror_sync.sync,  # Detail Settings DB → detail_rows
    macro_block_sync.sync,              # Tier 0 → member-DB Work area
    detail_applier_sync.sync,           # detail_rows → member-DB Detail
    external_org_applier_sync.sync,     # ReportingNz_deals → member-DB External Org
    tracker_applier_sync.sync,          # hierarchy_rows → Team Task Tracker
]
```

Reasoning for ordering: every canonical mirror runs before the applier(s) reading it. Among appliers, order is independent (different Notion targets per member DB; different mapping tables). Tracker last because it's the largest write surface.

### Phase 3: New member DB onboarding (template alignment)

This phase prevents the kind of bootstrap failure we hit on 2026-05-21 (drift between member-DB option names and canonical, → CASE D creates).

**Mechanism**: a new sub-sync `template_options_sync` that runs DAILY before any applier. For each per-member Meeting Notes DB **template** (resolvable via `retrieve_data_source` → `default_page_template`), update its `Work area`, `Detail`, and `External Org` option sets to mirror canonical. Notion templates store their own options independent of the parent DB's options — this keeps the template "ready" for cloning.

Skipped from PR4 scope to keep the diff manageable. Filed as follow-up issue `template_options_sync` (PR5). Until then, onboarding a new member DB requires: (a) Notion-side: clone the template; (b) one-off: run a recovery script (variant of `scripts/cleanup_stray_work_area_options.py`, generalized to take `--member-db-id` and align names from canonical without seed values) before flipping `Active=true` on the Org Chart.

The remaining 7 inactive DBs (with current wrong mappings for `work_area_option_mappings` due to the 2026-05-21 incident): when each is activated, run the alignment script on it. Until activation, the bad mappings are inert.

### Phase 4: CLI + Lambda

* CLI: zero change. `--sub-sync detail_canonical_mirror_sync` etc. all work via the existing `--sub-sync` arg.
* Lambda: zero behaviour change. `_handle_hierarchy_sync` already iterates `_SUB_SYNCS`.

## Step by Step Tasks

IMPORTANT: Execute every step in order, top to bottom.

### 1. Apply Supabase migrations

Via `mcp__claude_ai_Supabase__apply_migration` (project `yphbrpbwpakjduhmoimw`), five separate migrations with the names + DDL in §"Phase 1, step 2". Verify with `list_tables` after.

### 2. Create the Notion `Detail Options` Settings DB

Manual one-off. Capture its database id and add to `.env`:

```
DETAIL_OPTIONS_DB_ID=<32-char hex from URL>
```

Add the same to `template.yaml` and `scripts/deploy.sh`.

### 3. Bootstrap-populate the Settings DB

One row per current Detail option (~33 rows). Operator (or a one-off `scripts/seed_detail_options.py`) creates them with the right Color + Parent Work area. Set `Active = true` on all.

The script could read the union of all member DBs' current Detail options, dedupe by name, and infer the parent from the most common color → Work area mapping (per the 2026-05-21 schema dump: blue → Sourcing, orange → ops/value, pink → Investor Relations, yellow → Talent, green → Operations & AI). Operator reviews + adjusts.

### 4. Implement `src/config.py` change

Add `detail_options_db_id: str | None = None` to `SyncConfig`; read `DETAIL_OPTIONS_DB_ID` from env in the factory.

### 5. Implement `src/hierarchy/detail_canonical_mirror_sync.py`

Mirror `canonical_mirror_sync.py` with the smaller schema. See §"Phase 2, 2a" for the deltas.

### 6. Implement `src/hierarchy/detail_applier_sync.py`

Mirror `macro_block_sync.py`. Differences listed in §"Phase 2, 2b". Pay special attention to:
* `multi_select` (not `select`) on the PATCH payload + the retrieve accessor.
* `color` is canonical-driven — include in the comparison so a Settings-DB color change triggers a member-DB rename even if the name didn't change.

### 7. Implement `src/hierarchy/external_org_applier_sync.py`

Same applier shape, source is the SQL query in §"Phase 2, 2c". Plus the hierarchy-link population step. Filter is hard-coded for now (`_ALLOWED_STAGES`); revisit when stage taxonomy changes.

### 8. Wire `_SUB_SYNCS` in `src/hierarchy/__init__.py`

Per the final order in §"Phase 2, 2d". Update the docstring near `_SUB_SYNCS` explaining the dependency chain (canonical mirrors before appliers reading them).

### 9. Unit tests

`tests/hierarchy/test_detail_canonical_mirror_sync.py`:
* created / edited / deleted / reactivated diff cases against fixture data.
* Color-only change → `edited` (not unchanged).
* Parent Work area changed → `edited`.

`tests/hierarchy/test_detail_applier_sync.py`:
* TestPlanMemberDbUpdate — mirror PR2's test cases plus a color-only divergence case.
* TestSync — multi-select PATCH shape; mapping back-fill from PATCH response.

`tests/hierarchy/test_external_org_applier_sync.py`:
* The stage filter: only the 4 allowed stages produce options.
* Sort order: Portfolio rows ordered first, alpha within stage.
* Color rule: orange for Portfolio, blue otherwise.
* Stage transition Portfolio → Discarded → existing mapping found → option renamed to `(archived) X`.
* Hierarchy link found / not found cases.

### 10. Run local validation

```powershell
../venv/Scripts/python -m pytest tests/hierarchy/ -v
../venv/Scripts/python -m pytest tests/ -v
../venv/Scripts/python -m ruff check src/hierarchy/ tests/hierarchy/
```

### 11. Manual end-to-end test (Santiago — per CLAUDE.md hard rule, agent does NOT run)

```powershell
# Dry-run preview
python -m src.main --sync-hierarchy --sub-sync detail_canonical_mirror_sync --sub-sync detail_applier_sync --sub-sync external_org_applier_sync --dry-run --verbose

# Live one-shot, composed with canonical mirrors first
python -m src.main --sync-hierarchy --sub-sync canonical_mirror_sync --sub-sync detail_canonical_mirror_sync --sub-sync macro_block_sync --sub-sync detail_applier_sync --sub-sync external_org_applier_sync --verbose
```

Endpoints: Notion + Supabase only. **No GEMINI_API_KEY / OPENAI_API_KEY needed.**

Sanity check after live run via Supabase MCP:

```sql
-- Detail mappings populated for every active member DB
SELECT member_db_id, count(*) FROM detail_option_mappings GROUP BY 1;
-- External Org: 13 options (today's count) on every active member DB
SELECT member_db_id, count(*) FROM external_org_option_mappings GROUP BY 1;
-- Hierarchy link coverage
SELECT
  count(*) FILTER (WHERE hierarchy_page_id IS NOT NULL) AS linked,
  count(*) FILTER (WHERE hierarchy_page_id IS NULL)    AS unlinked
FROM external_org_option_mappings m
LEFT JOIN deal_hierarchy_links l USING (deal_id);
-- Sanitized names (no commas anywhere on the Notion side)
SELECT count(*) FROM detail_option_mappings WHERE option_name LIKE '%,%';
SELECT count(*) FROM external_org_option_mappings WHERE option_name LIKE '%,%';
-- Expected: 0 each.
```

### 12. Deploy

`./scripts/deploy.sh` (full deploy — `template.yaml` changed for the new env var). Tomorrow's 05:00 UTC cron picks up the new behaviour.

### 13. Documentation updates

* `docs/architecture.md` — extend the Hierarchy DB sub-sync table with three new rows; describe External Org's Supabase-only source + dealflow_links analytical join.
* `docs/notion-schema.md` — document the new `Detail Options` Settings DB schema + the constraint that `Detail` + `Work area` + `External Org` option names + colors on member DBs are owned by their respective canonicals.
* `CLAUDE.md` — extend the `[DETAILS INSIDE]` hard rule with a sentence noting that all three shared dropdowns flow from canonicals; never edit them on member DBs directly.

### 14. Final validation

Re-run pytest + ruff after docs edits.

## Testing Strategy

### Unit tests

All in `tests/hierarchy/test_*.py`. Follow the PR2/PR3 test shape (TestSanitize / TestPlanMemberDbUpdate / TestSync, mocked NotionClientWrapper + patched `_http`).

### Integration tests

None automated (project posture). Manual is the dry-run command in Step 11.

### Edge cases (especially for `external_org_applier_sync`)

* Deal transitions from filtered stage → outside the filter (e.g., Portfolio → Discarded). Existing mapping must trigger `(archived) X` rename, NOT a hard delete.
* Two deals with the same `name` and the same `stage` → sanitized-name collision (same as PR2's collision detector). Both skipped, error per pair.
* New deal added to ReportingNz_deals → bootstrap-create path; Hierarchy link attempted, may fail (operator hasn't made the Hierarchy row yet) — option still gets created; link materialises on a future tick after the Hierarchy row is added.
* Member DB option created manually with a name matching a current deal → bootstrap-adopt by sanitized name (heals on first contact).
* ReportingNz_deals row deleted entirely (rare; Affinity row removed) → existing mapping → CASE B fall-through → archive.

### For `detail_applier_sync`:

* Color-only change in Settings DB → next tick PATCHes color, mapping `option_name` doesn't change but `last_synced_at` does.
* Parent Work area change → no behaviour change today (parent is canonical metadata only); becomes relevant if PR5 ever drives Detail colors from Work area's color (not done in PR4).
* Multi-select page values: PATCHing the option list with id-preserving renames does NOT touch per-page values; pages stay tagged on the same option_id with the new label.

## Acceptance Criteria

* Running `python -m src.main --sync-hierarchy --verbose` (all sub-syncs) emits 6 log lines in the order specified above; all `errors=0` on a fully-aligned workspace.
* Renaming a Detail Settings DB row → next tick → every active member DB's matching `Detail` multi-select option is renamed in place (id preserved; every page tag preserved).
* Renaming a Detail Settings DB row's color → next tick → color updates on every member DB.
* Setting `Active=false` on a Detail Settings DB row → next tick → matching member DB options renamed to `(archived) X`. Mapping kept (re-activation un-archives).
* Adding a new Detail Settings DB row → next tick → option created on every member DB with the specified color.
* A deal in `ReportingNz_deals` moves stage `Under analysis (...)` → `Portfolio` → next tick → matching External Org option moves to top of dropdown (alpha within Portfolio) and color flips orange.
* A deal moves stage `Working on a deal (...)` → `Discarded` → next tick → existing option archived as `(archived) X`. Tagged historical meetings still resolve.
* A deal whose name sanitizes the same as a Hierarchy Tier 1 child of "Value Creation for Portfolio" gets a `deal_hierarchy_links` row populated; unmatched deals don't (and warn in details).
* All unit tests pass; `ruff check src/hierarchy/ tests/hierarchy/` clean.
* `SELECT count(*) FROM detail_option_mappings WHERE option_name LIKE '%,%'` → 0.
* `SELECT count(*) FROM external_org_option_mappings WHERE option_name LIKE '%,%'` → 0.
* Hard rules preserved (no `[DETAILS INSIDE]` row deletion, no @mentions in Notion content, no silent failures).

## Documentation Update (MANDATORY)

* `docs/architecture.md` — sub-sync table extended; brief description of the canonical-driven dropdown pattern now generalised to 3 properties.
* `docs/notion-schema.md` — Detail Options Settings DB schema + the canonical-ownership rule.
* `CLAUDE.md` — one-sentence addition: all three shared dropdowns flow from canonicals; never edit on member DBs.

## Validation Commands

```bash
../venv/Scripts/python -m pytest tests/hierarchy/ -v
../venv/Scripts/python -m pytest tests/ -v
../venv/Scripts/python -m ruff check src/hierarchy/ tests/hierarchy/
```

Santiago-run (per CLAUDE.md "never run the pipeline yourself"):

```powershell
# Dry-run preview, all new sub-syncs only
python -m src.main --sync-hierarchy --sub-sync detail_canonical_mirror_sync --sub-sync detail_applier_sync --sub-sync external_org_applier_sync --dry-run --verbose

# Live full chain
python -m src.main --sync-hierarchy --verbose
```

Endpoints: Notion + Supabase only. **No GEMINI_API_KEY / OPENAI_API_KEY needed.**

## Notes

* **Why Supabase-only for External Org.** ReportingNz_deals already exists, is synced from Affinity, and is the only place "what companies are we touching" is canonically tracked. Creating a parallel Notion DB would mean two systems to keep in sync — and the operator (Santiago) explicitly wanted to avoid that. Stage-driven filtering + sort encodes the user-facing dropdown semantics inside the applier; no extra UI surface needed.
* **Why a Notion Settings DB for Detail.** Detail has no equivalent in Supabase (it's a categorical taxonomy, not a data feed). Operators want to edit it interactively. Mirroring the Hierarchy DB pattern keeps the mental model consistent.
* **Why color is canonical-owned for Detail.** Operator wanted colors to match the parent Work area for visual consistency across member DBs. Storing color centrally and propagating on every tick achieves this without per-DB drift.
* **Why a separate `deal_hierarchy_links` table** (instead of a column on `ReportingNz_deals` or `external_org_option_mappings`). ReportingNz_deals is owned by the Affinity sync — adding columns to it risks conflicts. Putting the link on `external_org_option_mappings` would duplicate the same `(deal_id → hierarchy_page_id)` value across 9 rows per deal (one per member DB). A dedicated 1:1 table is the right normalised place.
* **Why no auto-create of Hierarchy rows from new deals.** Avoiding surprise. The operator manually creates the Hierarchy row (Tier 1 PortCo or Tier 2 Workstream) when a deal becomes material; the link materialises automatically on the next tick. This keeps the Hierarchy DB an intentional human-curated artefact rather than a data dump.
* **Why we keep `_sanitize_option_name` cross-imported from `macro_block_sync`.** Single source of truth for the Notion-side comma stripping rule. If we ever need to extend the sanitization (e.g., normalize `and` ↔ `&` to heal drift), changing it in one place updates all 3 appliers.
* **Cost.** Zero LLM. Per tick: 1 retrieve_data_source + ≤1 update_data_source per member DB, per applier. With 9 active member DBs (today's max) × 3 appliers = at most 27 retrieves + 27 PATCHes per morning. Well under any rate limit.
* **What this PR explicitly does NOT do.** Auto-create Hierarchy rows; sync option colors for Work area itself; centralize template alignment (filed as PR5 `template_options_sync`); garbage-collect orphan `(archived) X` options; back-fill Hierarchy links for unmatched deals.
* **Recovery for the 7 inactive DBs whose `work_area_option_mappings` rows still carry URL-encoded option_ids from the 2026-05-21 incident.** Defer until activation. When activating a member, run a generalized variant of `scripts/cleanup_stray_work_area_options.py` (or just delete those member's bad mappings and let bootstrap rediscover real ids — works because their option NAMES haven't drifted from each other; only Jacob + Santiago needed the option-deletion step because they got duplicate options created).

## Open future questions

* Should there be a UI surface in Notion showing the live state of External Org with its Hierarchy linkage (e.g., a synced view inside Settings)? Could be a Notion linked-database view on the Hierarchy DB filtered to Tier 1/Tier 2 children of the two relevant Tier 0 rows, with a Properties column showing the deal stage. Out of scope for PR4.
* Should Detail's `Parent Work area` cascade — i.e., if Operator changes a Detail row's parent, should the color auto-update to match the new parent's convention? Not in PR4 (Color is an explicit column the operator owns). Could be a PR5 enhancement.
* Should `template_options_sync` (PR5) be a daily cron, or only fire when a new active row appears in the Org Chart? Daily is safer (handles operator edits to templates); on-demand is leaner. Punt to PR5.
* Stage filter for External Org is hard-coded today. If the Sales team changes the stage taxonomy, the code needs an update. Could be made dynamic via a Supabase config table, but that's a tiny lift compared to the design churn risk — keep hard-coded for now.
