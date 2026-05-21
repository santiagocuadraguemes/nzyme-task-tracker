# Feature: Supabase-canonical-driven `macro_block_sync` rewrite + comma-in-option sanitization (PR2)

## Feature Description

Two things shipped together as one PR — they're tightly coupled because Notion's API forbids commas in select option names, and the PR2 rewrite would otherwise hit that 400 error every time someone has a Tier 0 with a comma in its name (which they do today: `Sourcing, Investing & Divesting (Dealflow)`).

**(A) Rewrite `src/hierarchy/macro_block_sync.py` as a Supabase-canonical-driven applier.** Same downstream behavior as today (each active member Meeting Notes DB's `Work area` select propagated from Tier 0), but the source of truth becomes `public.hierarchy_rows` in Neo Supabase instead of the Notion Hierarchy DB directly. A new mapping table `public.work_area_option_mappings` pins `(hierarchy_page_id, member_db_id) → option_id`, which lets the applier issue **real id-preserving renames** of select options — fixing today's "create new option, orphan the old one" limitation. Same applier pattern as PR3 (`tracker_applier_sync`).

**(B) Comma-in-option sanitization.** Sanitize names purely on the Notion-option side: strip commas, collapse whitespace, never run words together. The Hierarchy DB and Supabase canonical keep their commas verbatim — the sanitization is a one-way translation when writing to or matching against Notion select options.

```python
def _sanitize_option_name(name: str) -> str:
    """Strip commas (Notion rejects them in select options); collapse whitespace."""
    return " ".join(name.replace(",", " ").split())
```

Examples:
- `"Sourcing, Investing & Divesting (Dealflow)"` → `"Sourcing Investing & Divesting (Dealflow)"`
- `"A,B"` → `"A B"` (the inserted space matters — never run words together)
- `"  trailing  "` → `"trailing"` (whitespace collapse handles edges)

## User Story

As Santiago,
I want renames of Tier 0 rows in the Hierarchy DB to flow into each member's `Work area` select **without orphaning the old option** and **without 400-ing on commas**,
So that when I rename `Sourcing, Investing & Divesting (Dealflow)` to `WWW Sourcing, Investing & Divesting (Dealflow)`, the next morning's cron updates every member DB's option to `WWW Sourcing Investing & Divesting (Dealflow)` in place — same option id, every page tagged with it picks up the new label, and no manual cleanup of duplicates.

## Problem Statement

Today's `macro_block_sync`:
- Reads Tier 0 names directly from the Notion Hierarchy DB and compares against current option names on each member DB. No stable id mapping exists, so a rename in the Hierarchy DB is seen as "this name doesn't match any option → CREATE a new one" while the old option stays orphaned with whatever pages were tagged on it. (Exactly the limitation Santiago noticed earlier today.)
- Throws `Invalid select option, commas not allowed: …` (HTTP 400) whenever a Tier 0 name contains a comma — because Notion's API forbids commas in select option names (the comma is the multi-select separator). Surfaced in this morning's run as `errors=2` on the member DBs.

We need to (a) port `macro_block_sync` onto the Supabase canonical (introduced in PR1) using the applier pattern proven in PR3, and (b) handle the comma restriction transparently so a comma in the source-of-truth name doesn't break the Notion-side write.

## Solution Statement

A rewritten `src/hierarchy/macro_block_sync.py` that:

1. **Loads canonical state from Supabase** (`hierarchy_rows` filtered to `tier = '0. Macro Work Block'`, including tombstoned rows so soft-archive works).
2. **Loads the per-pair option mapping** from the new `work_area_option_mappings` table.
3. **For each active member DB** discovered via `discover_meeting_dbs`:
   - Loads the current `Work area` options.
   - For each Tier 0 canonical row, decides whether to keep, rename, create, archive, or adopt by sanitized-name match.
   - Issues one `update_data_source` PATCH per member DB if anything changed (Notion needs the full options array; ids are preserved when present).
   - Upserts the resulting mappings into Supabase.
4. **Sanitization** is bidirectional but one-sided: strip on write to Notion, strip on compare against Notion option names. The canonical / Hierarchy DB keep commas.

The applier shape is the same one PR3 ships: pure planner + I/O `sync()` + per-member try/except + dry-run branch. Cross-imports `_supabase_creds` + `_http` from `canonical_mirror_sync` to avoid duplicating HTTP/auth code (third sub-sync to do so, after `tracker_applier_sync` — pattern is now established).

## Relevant Files

Existing files referenced:

- `src/hierarchy/__init__.py` — `_SUB_SYNCS` registry. **Reorder** so `macro_block_sync` runs AFTER `canonical_mirror_sync` (it now reads canonical, not Notion directly). Final order: `canonical_mirror_sync` → `macro_block_sync` → `tracker_applier_sync`.
- `src/hierarchy/base.py` — `SyncReport` (no change; existing `created / renamed / archived / errors` cover this sub-sync's needs).
- `src/hierarchy/macro_block_sync.py` — the file being rewritten. The CURRENT outcome on Work area options is the behavior baseline; only the data source changes.
- `src/hierarchy/canonical_mirror_sync.py` — re-use `_supabase_creds` and `_http` via cross-import (same pattern PR3 uses).
- `src/hierarchy/tracker_applier_sync.py` — closest structural template (applier shape, Supabase-driven, the same cross-import).
- `tests/hierarchy/test_macro_block_sync.py` — the file being rewritten entirely. New tests follow the PR3 test shape (`TestSanitize`, `TestPlanMemberDbUpdate`, `TestSync`).
- `src/meeting_db_registry.py` (`discover_meeting_dbs`) — unchanged; continues to provide the `[MeetingDB]` list.
- `src/notion_client_wrapper.py` — `retrieve_data_source` + `update_data_source` (both already used by today's `macro_block_sync`; no new wrapper methods needed).
- `CLAUDE.md` hard rules — preserved (no behavior on `[DETAILS INSIDE]` rows; this PR is the Work-area side only).

### New Files

- `specs/hierarchy-macro-block-applier.md` — this spec.
- (Migration is applied via Supabase MCP — no committed `.sql` file. The DDL text lives inside Step 1 of this spec for traceability.)

## Implementation Plan

### Phase 1: Foundation

1. **Apply Supabase migration via MCP** (`mcp__claude_ai_Supabase__apply_migration`) creating `public.work_area_option_mappings` with the schema below. RLS enabled, no policies (matches the project's internal-table convention; service_role bypasses).
2. **No SAM template / IAM / env change.** Both `SUPABASE_URL` and `SUPABASE_KEY` are already on the deployed Lambda (verified during PR1).
3. **No `SyncReport` change.** Existing `created / renamed / archived / errors` fields cover this sub-sync's vocabulary. (`edited / deleted / reactivated / parent_fixed` stay at 0 for this sub-sync; that's expected — those are owned by the other sub-syncs.)

### Phase 2: Core Implementation

1. **`_sanitize_option_name(name) → str`** — private helper at module top. One-line implementation per the spec; unit-tested independently.
2. **Snapshot loaders:**
   - `_load_canonical_tier_0()` — Supabase GET on `hierarchy_rows` filtered to Tier 0. Includes tombstoned rows. Returns a `list[_CanonicalTier0Row]` (page_id, name, active, deleted_at).
   - `_load_mappings(member_db_ids)` — Supabase GET on `work_area_option_mappings` filtered to the current run's member DBs (PostgREST `member_db_id=in.(…)`). Returns `dict[(hierarchy_page_id, member_db_id), _Mapping]` where `_Mapping` carries `option_id` and `option_name`.
3. **Pure planner `_plan_member_db_update(canonical_rows, mappings, current_options, member_db_id)`** — for one member DB, returns:
   - `new_options: list[dict]` — full Notion options array to PATCH (preserves ids; mutates names; passes legacy options through verbatim).
   - `mapping_writes: list[_Mapping]` — rows to UPSERT into Supabase (bootstrap adopts + new creates).
   - `counters: {created, renamed, archived, errors}` — counts of actions on this member DB.
   - `details: list[str]` — warnings (sanitized-name collision, stale mapping detected, adopted-by-sanitized-name-match, etc.).
   - Pure — no I/O. Adoption of an existing option proceeds via match-by-sanitized-name; if a match is found AND the current option name contains a comma OR differs from the desired sanitized name, the planner queues a rename (heals the data while adopting).
4. **Collision detection** — before per-row planning, build a `desired_sanitized_name → [page_id…]` index. Any sanitized name with >1 page_id is flagged: both rows are skipped, one `errors += 1` per pair, descriptive detail.
5. **I/O `sync(client, config)`**:
   - Validate config (`org_chart_db_id` required) and Supabase env. Each failure → one error and return.
   - Load canonical Tier 0 from Supabase. Empty → benign warning (PR1 hasn't run yet), return with `errors=0`.
   - Discover member DBs.
   - Load mappings for those member DB ids.
   - For each member DB:
     - Try/except: retrieve_data_source → `current_options`.
     - Compute plan via `_plan_member_db_update`.
     - If counters all zero → log debug "in sync"; continue.
     - In dry-run: log per-action INFO lines (creates, renames, archives), update report counters, no API call.
     - Live: `client.update_data_source(member_db.db_id, {Work area: {select: {options: new_options}}})` → on failure, `errors += 1`, continue. On success, upsert mapping_writes to Supabase via PostgREST upsert (`on_conflict=hierarchy_page_id,member_db_id`). Mapping upsert failure → `errors += 1` and a detail noting the recovery path (next run's bootstrap-adopt heals it by sanitized-name match — no duplicate options created).

### Phase 3: Integration

1. **Reorder `_SUB_SYNCS`** in `src/hierarchy/__init__.py` so `macro_block_sync` runs after `canonical_mirror_sync`:

```python
_SUB_SYNCS: list[SubSync] = [
    canonical_mirror_sync.sync,   # was: macro_block_sync first
    macro_block_sync.sync,         # now reads from canonical_mirror_sync's output
    tracker_applier_sync.sync,
]
```

2. **No Lambda handler change.** `_handle_hierarchy_sync` already iterates `_SUB_SYNCS`.
3. **No template change.**
4. **CLI:** `python -m src.main --sync-hierarchy --sub-sync macro_block_sync [--dry-run] [--verbose]` already works through the `--sub-sync` flag added in PR1 — but note that in isolation it requires canonical to already be up-to-date in Supabase. To fully exercise the rewritten sub-sync after a Hierarchy DB edit, run `--sub-sync canonical_mirror_sync --sub-sync macro_block_sync` (composable, in order).

## Step by Step Tasks

IMPORTANT: Execute every step in order, top to bottom.

### 1. Apply Supabase migration via MCP

Use `mcp__claude_ai_Supabase__apply_migration` against project `yphbrpbwpakjduhmoimw` with name `create_work_area_option_mappings`:

```sql
-- Per (Hierarchy Tier 0 row × member DB) pairing of the Notion select option
-- id behind that member's `Work area` value for that work block. Lets
-- macro_block_sync rename options in place (id preserved) when the Hierarchy
-- DB name changes — without it, renames look like create + orphan.
--
-- Names stored here are the SANITIZED Notion option name (no commas); the
-- Hierarchy DB / hierarchy_rows keep their original names with commas.
CREATE TABLE public.work_area_option_mappings (
    hierarchy_page_id   text NOT NULL,
    member_db_id        text NOT NULL,
    option_id           text NOT NULL,
    option_name         text NOT NULL,
    last_synced_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (hierarchy_page_id, member_db_id)
);

COMMENT ON TABLE public.work_area_option_mappings IS
    'Maps (Hierarchy Tier 0 row, member Meeting Notes DB) → the Notion select '
    'option id used for `Work area`. Maintained by src/hierarchy/macro_block_sync.py. '
    'option_name carries the SANITIZED form (commas stripped) for diffing.';

COMMENT ON COLUMN public.work_area_option_mappings.option_id IS
    'Notion select option id (opaque, stable across renames). PATCHing the '
    'option name with this id preserves every page already tagged.';

CREATE INDEX work_area_option_mappings_member_idx
    ON public.work_area_option_mappings (member_db_id);

ALTER TABLE public.work_area_option_mappings ENABLE ROW LEVEL SECURITY;
```

Verify with `list_tables` after.

### 2. Replace `src/hierarchy/macro_block_sync.py`

Full rewrite. Top of file:
- Module docstring (new contract: source = Supabase canonical, not Notion Hierarchy DB; sanitization rules; mapping table; recovery path on mapping back-fill failure).
- Constants: `SUB_SYNC_NAME = "macro_block_sync"`, `_TIER_0_VALUE = "0. Macro Work Block"`, `_WORK_AREA_PROPERTY = "Work area"`, `_ARCHIVED_PREFIX = "(archived) "`, `_DETAIL_CAP = 50`.
- Cross-import: `from src.hierarchy.canonical_mirror_sync import _http, _supabase_creds`.

### 3. Implement `_sanitize_option_name`

```python
def _sanitize_option_name(name: str) -> str:
    """Strip commas (Notion forbids them in select options); collapse whitespace.

    Examples:
        "Sourcing, Investing & Divesting (Dealflow)"
          → "Sourcing Investing & Divesting (Dealflow)"
        "A,B" → "A B"   (never runs words together)
        "  x  " → "x"
    """
    return " ".join(name.replace(",", " ").split())
```

### 4. Implement dataclasses

```python
@dataclass
class _CanonicalTier0Row:
    notion_page_id: str
    name: str
    active: bool
    deleted_at: str | None

@dataclass
class _Mapping:
    hierarchy_page_id: str
    member_db_id: str
    option_id: str
    option_name: str

@dataclass
class _PlannerResult:
    new_options: list[dict[str, Any]] = field(default_factory=list)
    mapping_writes: list[_Mapping] = field(default_factory=list)
    created: int = 0
    renamed: int = 0
    archived: int = 0
    errors: int = 0
    details: list[str] = field(default_factory=list)
```

### 5. Implement `_load_canonical_tier_0()`

PostgREST GET:
```
/rest/v1/hierarchy_rows?select=notion_page_id,name,active,deleted_at&tier=eq.0.+Macro+Work+Block&limit=10000
```

Notice the URL-encoded space (`+`) in the tier value. Easier: use `urllib.parse.quote` to encode `"0. Macro Work Block"`. Returns `list[_CanonicalTier0Row]`. Sort by `name` for deterministic dry-run logs.

### 6. Implement `_load_mappings(member_db_ids)`

PostgREST GET with `member_db_id=in.(…)`:
```
/rest/v1/work_area_option_mappings?select=*&member_db_id=in.("mdb-a","mdb-b")
```

Returns `dict[(page_id, member_db_id), _Mapping]`.

### 7. Implement the collision detector

```python
def _detect_sanitized_collisions(canonical_rows) -> dict[str, list[str]]:
    by_sanitized: dict[str, list[str]] = {}
    for row in canonical_rows:
        desired = _sanitize_option_name(
            row.name if (row.active and not row.deleted_at) else f"(archived) {row.name}"
        )
        by_sanitized.setdefault(desired, []).append(row.notion_page_id)
    return {n: ids for n, ids in by_sanitized.items() if len(ids) > 1}
```

Called once at the top of `sync()`; collisions are surfaced as `errors += 1` per offending name, and those page_ids are excluded from per-member planning.

### 8. Implement the pure planner `_plan_member_db_update`

Inputs: `canonical_rows`, `mappings_for_this_member`, `current_options`, `member_db_id`, `skip_page_ids: set[str]` (the collision losers).

Output: `_PlannerResult`.

Algorithm:
1. Build indices over current options:
   - `by_id: {option_id: option}` (carry-through)
   - `by_sanitized_name: {sanitized(option.name): option}` (bootstrap match)
2. `out: list[dict] = [dict(opt) for opt in current_options]` (start with everything verbatim — preserves legacy options like `Standup` / `1:1`).
3. `out_by_idx: {option_id_or_name: index in out}` for fast in-place mutation.
4. For each canonical row (skipping collisions):
   - Compute `desired_sanitized = _sanitize_option_name(name or "(archived) name")`.
   - Look up mapping for `(page_id, member_db_id)`:
     - **Mapping found** + `mapping.option_id in by_id`:
       - Mutate `out[by_idx[mapping.option_id]] = {"id": mapping.option_id, "name": desired_sanitized}`.
       - If current option name != desired_sanitized → bump `renamed`; if going bare → `(archived)`, also `archived += 1`; if going `(archived)` → bare, decrement-archived semantics not needed (no negative counter, just don't increment `archived`).
       - Append `_Mapping(option_id, desired_sanitized, …)` to `mapping_writes` (update `option_name` even on no-op rename for freshness).
     - **Mapping found** + `mapping.option_id NOT in by_id` (user manually deleted): treat as no-mapping; add a detail noting the stale mapping; fall through to bootstrap-create. Do NOT carry the dropped mapping to `mapping_writes` (the row will be rewritten with a new option_id).
     - **No mapping**: try bootstrap-adopt via `by_sanitized_name[desired_sanitized]`:
       - If match → adopt that option's id; if current option name differs from desired_sanitized (has comma) → also rename (in-place via `out` mutation), bump `renamed`. Add to `mapping_writes` with the adopted id. Add detail: "adopted existing option by sanitized-name match".
       - If no match → append `{"name": desired_sanitized}` to `out`; bump `created`; queue `mapping_writes` placeholder with `option_id = ""` (will be back-filled from Notion's response after the PATCH — see step 9).
5. Track whether `out` actually changed vs `current_options` (compare element-wise by sanitized-name + id) so we can skip the PATCH when nothing differs.

### 9. Implement the I/O `sync(client, config)`

- Validate `org_chart_db_id` (required); record one error if missing.
- Validate Supabase creds (`_supabase_creds()`); record one error if missing.
- Load canonical Tier 0. **Empty → benign warning** (likely PR1 hasn't run yet); return with no writes, `errors=0`.
- Detect sanitized-name collisions; emit one error per collision; collect `skip_page_ids`.
- `discover_meeting_dbs(client, config.org_chart_db_id)`.
- `_load_mappings(member_db_ids)`.
- For each member DB:
  - `retrieve_data_source(member_db.db_id)` → on failure record error + continue.
  - Compute plan via `_plan_member_db_update`.
  - `_anything_changed` check; if no → log debug and continue.
  - **Dry-run branch**: log INFO lines per planned action, increment report counters from plan, continue (no API call).
  - **Live**: `client.update_data_source(member_db.db_id, {Work area: {select: {options: plan.new_options}}})`.
    - On failure: `errors += 1`, log, continue (skip mapping upsert).
    - On success:
      - For each created option (those without an `id` in `plan.new_options` before the PATCH), re-fetch the data source OR parse the PATCH response to recover the newly-assigned ids. (`update_data_source` returns the updated data source with option ids populated — confirm by reading the response.) Build the final `mapping_writes` list with real ids.
      - Upsert `mapping_writes` via `_http("POST", "/rest/v1/work_area_option_mappings?on_conflict=hierarchy_page_id,member_db_id", body=…, prefer="resolution=merge-duplicates,return=minimal")`. On failure: `errors += 1`, log, **document the recovery** in details ("mapping back-fill failed; next run will adopt by sanitized-name match without creating duplicates").
- Aggregate counters into `SyncReport`; cap `details` at `_DETAIL_CAP`.

### 10. Reorder `_SUB_SYNCS`

Edit `src/hierarchy/__init__.py`:
```python
_SUB_SYNCS: list[SubSync] = [
    canonical_mirror_sync.sync,
    macro_block_sync.sync,
    tracker_applier_sync.sync,
]
```

Update the module docstring comment that today says "macro_block_sync first (cheap, low blast radius)" — that's no longer the case.

### 11. Unit tests — `_sanitize_option_name`

Create `tests/hierarchy/test_macro_block_sync.py` (replaces the existing file). `TestSanitize` class:
- `"Sourcing, Investing & Divesting (Dealflow)"` → `"Sourcing Investing & Divesting (Dealflow)"`
- `"A,B"` → `"A B"` (no concatenation)
- `"A, B, C"` → `"A B C"`
- `",X"` → `"X"`
- `"X,"` → `"X"`
- `"  trailing  "` → `"trailing"`
- `"no comma"` → `"no comma"` (idempotent)
- `""` → `""`

### 12. Unit tests — pure planner

`TestPlanMemberDbUpdate` class:
- **noop**: mapping exists, option_id present, name matches → no PATCH planned, no rename count.
- **bootstrap-create**: no mapping, no matching option → CREATE; `created=1`; `mapping_writes` has placeholder id.
- **bootstrap-adopt clean**: no mapping, existing option matches desired sanitized name exactly → adopt; no rename; `mapping_writes` carries the adopted id.
- **bootstrap-adopt with comma cleanup**: no mapping, existing option is `"Sourcing, Investing & Divesting (Dealflow)"`, canonical is `"Sourcing, Investing & Divesting (Dealflow)"` → adopt + rename to `"Sourcing Investing & Divesting (Dealflow)"`; `renamed=1`; mapping carries adopted id + new name.
- **rename via mapping**: mapping exists, name diverged → renames in place (id preserved); `renamed=1`.
- **archive**: live + inactive → rename to `(archived) X`; `renamed=1`, `archived=1`.
- **reactivate**: previously `(archived) X` option, canonical now active → rename back to bare; `renamed=1`, `archived=0`.
- **mapping-stale** (option_id in mapping but not in current options): planner falls through to bootstrap; doesn't include the stale mapping in `mapping_writes`; details warn.
- **legacy options preserved**: `Standup`, `1:1` options not touched by Tier 0 — passed through verbatim.
- **sanitized-name collision** (driven by `_detect_sanitized_collisions`): both colliding canonical rows excluded via `skip_page_ids`; planner doesn't propose creates/renames for them.
- **tombstoned canonical** (`deleted_at` set) → produces `(archived) X` desired; mapping kept.

### 13. Unit tests — I/O `sync()`

`TestSync` class:
- Aborts when `org_chart_db_id` unset (errors=1; no canonical or Notion read).
- Aborts when Supabase env unset (errors=1).
- Canonical empty → benign warning, `errors=0`, no Notion writes (assert `discover_meeting_dbs` NOT called).
- Notion `retrieve_data_source` failure for one member → `errors=1`, other members still processed.
- One member's `update_data_source` failure → `errors=1`, mapping upsert NOT attempted for that member; other members proceed.
- Successful PATCH + successful mapping upsert → counters match plan; `_http` called exactly once for the mapping POST.
- Mapping upsert failure after successful Notion PATCH → `errors=1`, recovery detail in `report.details`.
- Sanitized-name collision detected → `errors=1`, both rows skipped on every member DB.
- Dry-run with creates + renames pending → 0 writes; counters reflect plan; `details` mentions `dry-run`.
- Two members where one is in-sync and one needs a rename → exactly one `update_data_source` call (on the out-of-sync one).
- Bootstrap-adopt-with-comma-cleanup against the actual comma test case (`Sourcing, Investing & Divesting (Dealflow)`): canonical has commas, current option has commas, no mapping → after run, planned `out` contains the sanitized comma-free name with the adopted id; mapping_writes has the adopted id.

### 14. Run validation locally

```powershell
../venv/Scripts/python -m pytest tests/hierarchy/ -v
../venv/Scripts/python -m pytest tests/ -v
../venv/Scripts/python -m ruff check src/hierarchy/ tests/hierarchy/
```

All three should pass cleanly (modulo the pre-existing `test_hierarchy_loader::test_depth_3_keeps_organizational_nodes` failure on `master` HEAD that's unrelated to all PR1/PR2/PR3 work).

### 15. Manual test (Santiago — per CLAUDE.md hard rule, agent does NOT run)

After PR1+PR3 are already running in Supabase:

```powershell
# Dry-run preview — Notion + Supabase reads, no writes.
python -m src.main --sync-hierarchy --sub-sync macro_block_sync --dry-run --verbose

# Live one-shot. Compose with canonical if you've just edited the Hierarchy DB
# (so canonical reflects today's truth before macro_block_sync reads it):
python -m src.main --sync-hierarchy --sub-sync canonical_mirror_sync --sub-sync macro_block_sync --verbose
```

Endpoints: Notion + Supabase only. **No GEMINI_API_KEY / OPENAI_API_KEY needed.**

Sanity check after live run via Supabase MCP:

```sql
-- Every active Tier 0 has a mapping on every active member DB.
SELECT
  (SELECT count(*) FROM hierarchy_rows WHERE tier='0. Macro Work Block' AND deleted_at IS NULL AND active) AS active_tier_0,
  count(DISTINCT hierarchy_page_id) AS mapped_hierarchies,
  count(DISTINCT member_db_id)      AS members
FROM work_area_option_mappings;

-- No option_name in the mapping carries a comma (sanitization happened).
SELECT count(*) FROM work_area_option_mappings WHERE option_name LIKE '%,%';
-- Expected: 0.
```

### 16. Deploy

`./scripts/quick-deploy.sh` — code-only deploy. No template change. Tomorrow's 05:00 UTC cron picks up the new behavior alongside PR1 + PR3.

### 17. Documentation updates

- `docs/architecture.md` — update the `macro_block_sync` row in the Hierarchy DB sub-sync table: source becomes Supabase canonical (was Notion Hierarchy DB); behavior includes real id-preserving renames + comma sanitization; remove the "known limitation: renames are seen as create+orphan" line (fixed); note the new `work_area_option_mappings` table.
- `docs/notion-schema.md` — under Hierarchy DB description, note that Tier 0 propagation now goes through the Supabase mapping table; member DB `Work area` option names are sanitized (commas stripped) while the source `name` in Hierarchy/Supabase keeps commas verbatim.
- `CLAUDE.md` — no change needed (the existing hard rules remain accurate).

### 18. Final validation

Re-run pytest + ruff after docs edits.

## Testing Strategy

### Unit Tests

All in `tests/hierarchy/test_macro_block_sync.py`. Three classes:
- `TestSanitize` — pure string transform (Step 11).
- `TestPlanMemberDbUpdate` — pure planner over fixture data (Step 12).
- `TestSync` — mocked `NotionClientWrapper` + patched `_http` (Step 13). `discover_meeting_dbs` patched as in the today's tests.

### Integration Tests

None automated (matches project posture). Manual integration is the dry-run command in Step 15, then the live command, then the Supabase sanity SQL.

### Edge Cases

- Comma sanitization variants in Step 11.
- Mapping-stale (option_id in mapping but not in Notion) → bootstrap path heals.
- Bootstrap-adopt collision (existing option matches by sanitized name but a comma-bearing option of the same sanitized form already exists somewhere else): treat as adopt for the first match, log if multiple.
- Sanitized-name collision (two Tier 0 rows sanitize to the same name) → both skipped, one error.
- Member DB removed from Org Chart between runs → not visited; existing mappings stay (harmless cruft; future GC could prune by checking against `discover_meeting_dbs` output, but out of scope).
- New member DB added → bootstrap path for that member only.
- Mapping back-fill PostgREST upsert failure → `errors += 1` with documented recovery (next run's bootstrap-adopt fixes via sanitized-name match — no duplicate options).
- Canonical empty (PR1 hasn't run) → benign warning, not error.
- Dry-run never PATCHes anything (asserted by raising on any non-GET in tests).
- Existing comma-bearing option on a member DB (created manually in Notion UI before this PR) → adopted by sanitized-name match AND renamed to its comma-free form on the same run (heals the data).

## Acceptance Criteria

- Running `python -m src.main --sync-hierarchy --sub-sync macro_block_sync --dry-run --verbose` against a fully-aligned workspace logs `created=0 renamed=0 archived=0 errors=0` per member DB and issues zero `update_data_source` calls.
- Renaming a Tier 0 row in the Hierarchy DB and running `--sub-sync canonical_mirror_sync --sub-sync macro_block_sync` live → `Work area` option on every member DB is renamed **in place** (same option_id; every page tagged keeps the new label). Zero create+orphan duplicates.
- Renaming `Sourcing, Investing & Divesting (Dealflow)` (comma in source) → no 400 from Notion; member DBs receive `Sourcing Investing & Divesting (Dealflow)` as the option name; canonical and Hierarchy DB keep the comma.
- Toggling `Active=false` on a Hierarchy Tier 0 → matching member DB option renamed to `(archived) <sanitized name>`. Mapping kept (so re-activation in Notion would un-archive cleanly via PR1's reactivated semantics).
- Removing a Tier 0 row from the Hierarchy DB → next morning's `canonical_mirror_sync` tombstones it; `macro_block_sync` then renames the matching member-DB option to `(archived) <sanitized name>`; mapping kept.
- A Tier 0 row whose mapped option_id was manually deleted from a member DB → next run creates a fresh option, updates the mapping; no error.
- Sanitized-name collision between two Tier 0 rows → both flagged, both skipped on every member DB, one error per collision.
- `SELECT count(*) FROM work_area_option_mappings WHERE option_name LIKE '%,%'` returns 0 after the first successful live run.
- All unit tests in `tests/hierarchy/` pass; `ruff check src/hierarchy/ tests/hierarchy/` clean.
- Hard rules preserved (no behavior on `[DETAILS INSIDE]` rows; no @mentions in Notion content; no silent failures).

## Documentation Update (MANDATORY)

After implementing this feature, update the following documentation:

### README.md
- [ ] N/A — no README in this project.

### API Documentation
- [ ] N/A — no public HTTP API change.

### Technical Docs
- [ ] `docs/architecture.md` — rewrite the `macro_block_sync` row in the Hierarchy DB sub-sync table: source is Supabase canonical; behavior includes id-preserving renames + comma sanitization; reference the new `work_area_option_mappings` table; remove the "known limitation: renames are seen as create+orphan" line (fixed).
- [ ] `docs/notion-schema.md` — under Hierarchy DB description, note that Tier 0 propagation now goes via the Supabase mapping table and that member-DB `Work area` option names are sanitized (commas stripped) while the canonical and Hierarchy DB names keep their commas verbatim.
- [ ] `CLAUDE.md` — no change required.

## Validation Commands

```bash
# Hierarchy tests (new file replaces the existing one)
../venv/Scripts/python -m pytest tests/hierarchy/ -v

# Full suite — no regressions
../venv/Scripts/python -m pytest tests/ -v

# Lint
../venv/Scripts/python -m ruff check src/hierarchy/ tests/hierarchy/
```

Santiago-run (per CLAUDE.md "never run the pipeline yourself" hard rule):

```powershell
# Dry-run preview — Notion + Supabase reads, no writes
python -m src.main --sync-hierarchy --sub-sync macro_block_sync --dry-run --verbose

# Live one-shot, composed with canonical to capture any fresh Hierarchy DB edits first
python -m src.main --sync-hierarchy --sub-sync canonical_mirror_sync --sub-sync macro_block_sync --verbose

# Supabase sanity (via the MCP)
SELECT count(*) FROM work_area_option_mappings WHERE option_name LIKE '%,%';
-- Expected: 0

SELECT member_db_id, count(*) AS mapped_rows
FROM work_area_option_mappings GROUP BY 1 ORDER BY 1;
-- Each active member DB should have a row per active Tier 0

# Deploy (code-only — no template change)
./scripts/quick-deploy.sh
```

After deploy, the daily 05:00 UTC cron will emit three lines per invocation in this order:

```
hierarchy_sync: name=canonical_mirror_sync ...
hierarchy_sync: name=macro_block_sync       ... (now reads canonical, NOT Notion Hierarchy DB)
hierarchy_sync: name=tracker_applier_sync   ...
```

## Notes

- **Why sanitize on the Notion side only.** The Hierarchy DB is the editing surface and humans expect commas in display names like "Sourcing, Investing & Divesting (Dealflow)". Stripping commas at the source-of-truth level would destroy the readable name and confuse anyone browsing the Hierarchy DB. Sanitizing only at the Notion-option write/compare boundary keeps the human-facing UX intact while satisfying Notion's API constraint.
- **Why insert a SPACE in place of the comma**, not delete it. `"A,B"` → `"A B"` keeps tokens visually distinct. `"AB"` would silently merge two distinct words on names that aren't comma+space separated. The whitespace-collapse second step handles the `"A, B"` → `"A B"` case (no double space).
- **Why bootstrap-adopt by sanitized-name match.** Many member DBs already have manually-created options like `"Sourcing, Investing & Divesting (Dealflow)"` (with the comma — added via Notion UI before this PR). On first run, the mapping table is empty; if we created a NEW comma-free option without checking, we'd duplicate every existing tag. Matching by sanitized name during bootstrap adopts the existing option and queues a rename to strip its comma — heals the data on first contact, no manual cleanup required.
- **Why no FK to `hierarchy_rows`.** `hierarchy_rows` never hard-deletes; tombstoning leaves the row in place. An FK adds zero safety and creates migration friction (e.g. if a future cleanup ever does delete rows). Skip.
- **Why module rename was rejected.** Considered renaming `macro_block_sync` → `macro_block_applier_sync` to match PR3's naming pattern. Rejected: CloudWatch log lines have been `macro_block_sync: …` since this morning's cron; renaming would create a discontinuity in observability with no functional benefit. Internals are rewritten; the public name stays.
- **Cost.** Zero LLM. Pure Notion + Supabase REST. Bootstrap: per member DB, 1 retrieve_data_source + 1 update_data_source + 1 mapping upsert. Steady state: per member DB, 1 retrieve_data_source + 0 PATCHes (when in-sync). Well under daily quota.
- **What this PR explicitly does NOT do.** Touch Tier 1 / Tier 2 (those aren't propagated to member-DB Work area today — only Tier 0 is). Retire the Notion Hierarchy DB `Tracker Node` relation (PR3 already wrote it as a human cache; future PR can retire). Garbage-collect orphan `(archived) X` options or mappings for departed members. Migrate existing tagged pages from old options to new ones (id-preserving rename means tags follow automatically — no migration needed).
- **Open future questions.**
  - Should `macro_block_sync` propagate Tier 1 (Project) or Tier 2 (Workstream) to a second select column on member DBs? Out of scope; depends on whether you want sub-categorization.
  - Should we lint Hierarchy DB names for "would sanitize to the same as another row"? Could be a defensive check in `canonical_mirror_sync` rather than only in this sub-sync. Easy follow-up.
  - Should member-DB option deletion be detected and the mapping cleared? Today the planner just creates a new option on a stale mapping. A periodic GC could prune mappings whose `option_id` no longer appears in any member DB.
