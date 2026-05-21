# Feature: Supabase-canonical-driven Hierarchy → Task Tracker applier (PR3)

## Feature Description

Add a new sub-sync `tracker_applier_sync` under `src/hierarchy/` that propagates every row in the Supabase `public.hierarchy_rows` canonical table (populated daily by `canonical_mirror_sync` from PR1) into the **Team Task Tracker** DB's architecture rows (`Priority = '[DETAILS INSIDE]'`). This is the **PR3 replacement** for the deleted `tracker_node_sync` — same downstream effect (Tracker titles + parent links follow the Hierarchy DB), but the source of truth is now Supabase, not direct Notion-to-Notion comparison.

Per canonical row, the applier ensures a Tracker `[DETAILS INSIDE]` row exists (creating one on first sight if `tracker_node_page_id` is unset), keeps its title aligned with `name` (or `(archived) name` when `active = false` or `deleted_at` is set), and points its `Parent item` relation at the parent canonical row's `tracker_node_page_id`. Soft-archive only — never delete (hard CLAUDE.md rule on `[DETAILS INSIDE]` rows).

It runs **after** `canonical_mirror_sync` in the daily 07:00 Madrid orchestrator (`_handle_hierarchy_sync` Lambda), so the applier always reads a canonical state that reflects today's Notion edits.

## User Story

As Santiago (operator of the Nzyme Settings DBs),
I want every edit I make in the Notion Hierarchy DB — rename, archive, reparent, add new, remove — to propagate to the Team Task Tracker's architecture rows automatically the next morning, **with the diff computed from Supabase canonical state** so renames are seen as renames (not as create+orphan),
So that the Tracker architecture stays in lock-step with the Hierarchy DB without my hand-aligning rows, and so the next architecture (PR2's `macro_block_sync` rewrite) can use the same canonical-driven pattern.

## Problem Statement

After PR1 we have:
- A canonical mirror of the Hierarchy DB in Supabase (`public.hierarchy_rows`) — populated daily with `created` / `edited` / `deleted` / `reactivated` semantics.
- Zero downstream consumers of that canonical state. The Tracker `[DETAILS INSIDE]` rows are currently un-synced (we deleted `tracker_node_sync` in PR1) — any new Hierarchy row stays orphaned on the Tracker side, and renames don't propagate.

We need a downstream applier that reads canonical and reconciles the Tracker, so:
1. The Tracker resumes following the Hierarchy DB without manual intervention.
2. Renames in the Hierarchy DB become real in-place title updates on Tracker rows (the `tracker_node_page_id` mapping survives renames because it's keyed by stable Notion `page_id`, not by name).
3. Row removals in Notion become soft-archives on the Tracker side (`(archived) X` rename), preserving the no-delete rule.
4. The applier pattern this PR establishes is the template for PR2's `macro_block_sync` rewrite.

## Solution Statement

A new sub-sync `src/hierarchy/tracker_applier_sync.py` shaped like `macro_block_sync.py` and `canonical_mirror_sync.py`: a pure planning function plus an I/O `sync(client, config) -> SyncReport`. Two snapshot loads (Supabase canonical + Notion Tracker `[DETAILS INSIDE]` rows), one pure diff, one two-pass create-then-reconcile execution.

Critically, this applier reads **all** rows from `public.hierarchy_rows` (including tombstoned ones — `deleted_at IS NOT NULL`). Tombstoned rows still produce a `desired` state of `(archived) X` on the Tracker side; that's how deletion in Notion turns into soft-archive in the Tracker without violating the no-delete rule.

The pairing column is `hierarchy_rows.tracker_node_page_id`. 49 of 50 rows have it set today (hand-aligned by Santiago; one tier-2 row may be new — verify against canonical). New canonical rows arrive with `tracker_node_page_id = NULL`; the applier creates the Tracker row and back-fills the column via PATCH to Supabase. As a courtesy, it also writes the new id to the Notion `Tracker Node` relation (human-readable cache); failure there is best-effort and logged.

Two-pass execution handles parents that were created in the same run:
- **Pass 1**: create missing Tracker rows (title only; no parent yet). For each, PATCH Supabase `tracker_node_page_id` with the new id, then best-effort write to Notion `Tracker Node`.
- **Re-plan**: walk the (now-fully-populated) canonical snapshot. New `to_update` entries appear for newly-created rows whose parent's `tracker_node_page_id` just became known.
- **Pass 2**: one PATCH per Tracker row (title + Parent item combined).

Registered as the third entry in `_SUB_SYNCS` — `macro_block_sync` first (legacy Notion-to-Notion), `canonical_mirror_sync` second (updates Supabase to today's truth), `tracker_applier_sync` third (reads the fresh canonical and applies).

## Relevant Files

Existing files referenced for pattern conformance:
- `src/hierarchy/__init__.py` — registry of sub-syncs. The new sub-sync gets appended after `canonical_mirror_sync.sync`. No structural change.
- `src/hierarchy/base.py` — `SyncReport` dataclass; **one field addition** needed: re-add `parent_fixed: int = 0` (counter for Parent item PATCHes — was in `SyncReport` during PR0/PR1 and removed when `tracker_node_sync` was deleted; this PR brings it back).
- `src/hierarchy/macro_block_sync.py` — reference for the per-row try/except, dry-run branch, structured details pattern.
- `src/hierarchy/canonical_mirror_sync.py` — reference for Supabase HTTP. Re-use `_supabase_creds` and the urllib helpers (`_http`) by either importing them or duplicating the small private helpers — see Step 1 below for the call.
- `tests/hierarchy/test_macro_block_sync.py` + `tests/hierarchy/test_canonical_mirror_sync.py` — reference test shape: pure-planner `TestPlan…` class + I/O `TestSync` class with mocks.
- `src/notion_client_wrapper.py` — exposes `create_page`, `update_page`, `query_database`. No new wrapper methods needed.
- `src/config.py` — uses `hierarchy_db_id` (only to write back the Notion `Tracker Node` best-effort) and `team_tracker_db_id`. Both already wired.
- `src/webhook/lambda_handler.py` — `_handle_hierarchy_sync` already iterates `_SUB_SYNCS`. No change.
- `template.yaml` — `HierarchySync` rule already exists. No change.

### New Files

- `src/hierarchy/tracker_applier_sync.py` — the new applier.
- `tests/hierarchy/test_tracker_applier_sync.py` — unit tests.

## Implementation Plan

### Phase 1: Foundation

Two small precursors:
1. **Re-add `parent_fixed: int = 0` to `SyncReport`** (`src/hierarchy/base.py`) and update the orchestrator's log line in `src/hierarchy/__init__.py` to include it. Other sub-syncs leave it at 0; the applier increments it for Parent item PATCHes so the `created / renamed / archived / parent_fixed / errors` story stays legible per tick.
2. **Decide the Supabase HTTP helper sharing question.** Two reasonable paths:
   - **(a) Import** `_supabase_creds` and `_http` from `src.hierarchy.canonical_mirror_sync`. Smaller code; slight private-symbol coupling.
   - **(b) Duplicate** the same ~30 lines into the new module so each sub-sync is self-contained.
   Recommend (a) — they're already module-private with leading underscore, and PR2 will want the same helpers; keeping them in one place beats triplicating. Document the cross-import in the new file's docstring.

No SAM template / cron / IAM / env change required. The Lambda already has `SUPABASE_URL` + `SUPABASE_KEY` (verified — see PR1 conversation).

### Phase 2: Core Implementation

1. **Snapshot loaders** (one Notion, one Supabase).
   - `_load_canonical_snapshot()` → `list[_CanonicalRow]`. Reads `hierarchy_rows` with `select=*` — includes tombstoned rows (no filter on `deleted_at`). Returns a list ordered by `tier, name` for determinism in dry-run logs.
   - `_load_tracker_snapshot(client, team_tracker_db_id)` → `dict[tracker_page_id, {"title": str, "parent_id": str | None}]`. Single paginated query filtered to `Priority = '[DETAILS INSIDE]'`. Skip pages with `archived = true` at the Notion-page level (log warning + exclude from snapshot so we never try to PATCH an archived page).

2. **Pure planner** `_plan_tracker_updates(canonical_rows, tracker_snapshot)` returning a `_PlannerResult` with:
   - `to_create: list[_CanonicalRow]` — rows whose `tracker_node_page_id` is NULL OR points at an id not in the Tracker snapshot (stale link). For each: planned title is `name` if live+active, else `(archived) name`.
   - `to_update: list[(tracker_id, payload_dict)]` — rows whose Tracker title and/or Parent item diverge from desired. Each entry's payload contains only the fields that differ.
   - Counters: `created`, `renamed`, `parent_fixed`, `archived` (count of title flips from bare → `(archived) X`).
   - `details: list[str]` for non-fatal warnings (stale tracker_id, duplicate fan-in, dangling parent, etc.).
   - Pure — no I/O, no logging beyond returning structured details.

3. **Desired-state derivation** (helper):
   - `_desired_title(row)` → `row.name` if `deleted_at is None and active`, else `f"(archived) {row.name}"`. Truncate to 2000 chars.
   - `_desired_parent_tracker_id(row, by_canonical_id)` → look up parent canonical row by `parent_notion_page_id`, return its `tracker_node_page_id` (may be `None` if parent is missing or hasn't been created yet — pass 2 catches this after pass 1).
   - **Parent edge-case decision (LOCK IN HERE):** when the parent canonical row is tombstoned, **keep the parent link** (parent's Tracker row still exists as `(archived) X`; the relation resolves and reading the Tracker still shows the historical structure). Clearing would lose context. Document this in the planner's docstring.

4. **I/O `sync(client, config)`**:
   - Validate config: `hierarchy_db_id` and `team_tracker_db_id` both set. (`hierarchy_db_id` is only needed for the best-effort Notion `Tracker Node` write-back; if missing, downgrade that write to a no-op + warning rather than erroring out.)
   - Validate Supabase env via `_supabase_creds()` — abort with one error if unset.
   - Load canonical snapshot + Tracker snapshot. Each failure → record one error and return.
   - **Empty canonical handling:** if the snapshot is empty, log a clear warning ("canonical empty — did `canonical_mirror_sync` run yet?") and return without writes. This is NOT counted as an error — it's a benign first-run-of-the-day-before-PR1-deployed condition.
   - Run pure planner.
   - **Pass 1** — for each `to_create`:
     - Dry-run: log per-row `DRY RUN would create name=… parent=…`; bump counters; assign a `dry-run-create-<page_id>` sentinel tracker_id; mutate in-memory canonical + snapshot so re-plan works.
     - Live:
       a. `client.create_page(team_tracker_db_id, {Task: desired_title, Priority: '[DETAILS INSIDE]'})` → capture new `tracker_id`.
       b. Supabase PATCH `hierarchy_rows?notion_page_id=eq.<page_id>` body `{"tracker_node_page_id": new_tracker_id, "last_changed_at": now}` — **must succeed** (this is the canonical mapping). On failure → `errors += 1`, leak the Tracker row, log loudly, continue to next row. Next run sees still-NULL canonical and creates another row (acceptable under no-delete rule; user reconciles `(archived) X` orphans by hand).
       c. Best-effort Notion `Tracker Node` write on the Hierarchy DB page: `update_page(hierarchy_page_id, {"Tracker Node": {"relation": [{"id": new_tracker_id}]}})`. Failure here → log warning + add to `details` but do **not** increment `errors` (Supabase is canonical; Notion column is cache).
       d. Mutate in-memory canonical: set `tracker_node_page_id` on the row. Add `tracker_id → {title, parent_id: None}` to `tracker_snapshot`. Re-plan in Step 5 will resolve parent.
   - **Re-plan**: run `_plan_tracker_updates` again on the mutated snapshots. `to_create` will be empty by construction; `to_update` now includes parent fixes for rows that just got their tracker_id.
   - **Pass 2** — for each `(tracker_id, payload)` in the reconcile plan:
     - Dry-run: log per-row `DRY RUN would patch tracker=… keys=[…]`; bump `renamed` / `parent_fixed` / `archived` as appropriate.
     - Skip sentinel tracker_ids (they don't exist in Notion); count counters only.
     - Live: single `client.update_page(tracker_id, properties=payload)`. On failure → `errors += 1`, continue.
   - Aggregate counters into `SyncReport` and return.

5. **Structured logging:**
   - `tracker_applier_sync: row=<page_id_8> action=create|rename|archive|parent_fix tracker=<tracker_id_8>` per applied row.
   - Final orchestrator log line picks up via `SyncReport.created / renamed / archived / parent_fixed / errors`.

### Phase 3: Integration

1. Append `tracker_applier_sync.sync` to `_SUB_SYNCS` in `src/hierarchy/__init__.py` **after** `canonical_mirror_sync.sync` (order matters — applier reads what mirror just wrote).
2. No Lambda handler change.
3. No template / cron / env change.
4. CLI: `python -m src.main --sync-hierarchy --sub-sync tracker_applier_sync [--dry-run] [--verbose]` already works through the `--sub-sync` filter added in PR1.

## Step by Step Tasks

IMPORTANT: Execute every step in order, top to bottom.

### 1. Re-add `parent_fixed` to SyncReport
- Edit `src/hierarchy/base.py`: add `parent_fixed: int = 0` between `reactivated` and `errors`.
- Edit `src/hierarchy/__init__.py`: extend the orchestrator's log line to include `parent_fixed=%d`.
- Run `pytest tests/hierarchy/` to confirm existing tests still pass (none assert on `parent_fixed`, so they should).

### 2. Create `src/hierarchy/tracker_applier_sync.py` skeleton
- Module docstring covering: contract, source/target, two-pass create-then-reconcile, parent-tombstone decision (keep link), known limitations (no orphan GC, pass-1 partial failure leaks).
- Constants: `SUB_SYNC_NAME = "tracker_applier_sync"`, `_ARCHIVED_PREFIX = "(archived) "`, `_DETAILS_INSIDE = "[DETAILS INSIDE]"`, `_TITLE_CAP = 2000`, `_DETAIL_CAP = 50`.
- Dataclasses: `_CanonicalRow` (notion_page_id, name, tier, active, parent_notion_page_id, tracker_node_page_id, deleted_at) and `_PlannerResult` (to_create, to_update, created, renamed, parent_fixed, archived, errors, details).
- Re-use `_supabase_creds` and `_http` via `from src.hierarchy.canonical_mirror_sync import _supabase_creds, _http`. Document the cross-import in the docstring.

### 3. Implement `_load_canonical_snapshot()`
- `_http("GET", "/rest/v1/hierarchy_rows?select=*&limit=10000")`.
- Build `[_CanonicalRow(...)]` from each row, including tombstoned ones (don't filter `deleted_at`).
- Return ordered by `(tier, name)` for deterministic dry-run logs.

### 4. Implement `_load_tracker_snapshot(client, team_tracker_db_id)`
- `client.query_database(database_id=team_tracker_db_id, filter={"property": "Priority", "select": {"equals": "[DETAILS INSIDE]"}})`.
- For each page: `tracker_id → {"title": str, "parent_id": str | None}`. Skip pages where `archived is True` (Notion-archived) with a warning.

### 5. Implement the pure planner `_plan_tracker_updates`
- Build `by_canonical_id = {row.notion_page_id: row for row in canonical_rows}`.
- Track `seen_tracker_ids: set[str]` for duplicate fan-in detection.
- For each canonical row:
  - Compute `desired_title = _desired_title(row)` (handles live+active, live+inactive, tombstoned).
  - Compute `desired_parent_tracker_id` by looking up parent canonical row's `tracker_node_page_id`. If parent row exists but has no `tracker_node_page_id` yet → `None` (re-plan handles it).
  - **CASE A** `tracker_node_page_id` is NULL OR not in `tracker_snapshot` → append to `to_create`; `created += 1`; if `(deleted_at is not None or not active)` also `archived += 1` (per spec — `archived` mirrors macro_block_sync semantics: count of titles flipped to `(archived) X`); add stale-id warning to details if applicable.
  - **CASE B** Tracker row exists → check `seen_tracker_ids` first (duplicate fan-in → log, skip); diff title and parent:
    - Title diff → add `Task` to patch; `renamed += 1`; if going bare → `(archived)`, also `archived += 1`.
    - Parent diff → add `Parent item` to patch; `parent_fixed += 1`.
    - Any patch → append `(tracker_id, payload)` to `to_update`.

### 6. Implement the I/O `sync(client, config)`
- Validate `team_tracker_db_id` (required). `hierarchy_db_id` optional — if missing, downgrade Notion `Tracker Node` writeback to no-op.
- Validate Supabase env via `_supabase_creds()`.
- Load both snapshots in try/except → one error and return on failure.
- If canonical empty → log warning, return (no error increment).
- Run planner.
- **Pass 1** loop with the per-row logic in Phase 2 Step 4.
- After pass 1, re-run `_plan_tracker_updates` against the mutated snapshots — `to_create` will be empty; `to_update` carries the parent fixes for new rows.
- **Pass 2** loop applying each `(tracker_id, payload)` via `update_page`.
- Forward planner-level details into `report.details` (capped at `_DETAIL_CAP`).

### 7. Wire into orchestrator
- Edit `src/hierarchy/__init__.py`: `from src.hierarchy import canonical_mirror_sync, macro_block_sync, tracker_applier_sync`.
- Append `tracker_applier_sync.sync` to `_SUB_SYNCS` AFTER `canonical_mirror_sync.sync`.

### 8. Unit tests — pure planner
Create `tests/hierarchy/test_tracker_applier_sync.py`. `TestPlanTrackerUpdates` class:
- Live + active + tracker matches → no-op (no `to_create`, no `to_update`, counters 0).
- Live + active + tracker title diverged → one `to_update` with `Task` only, `renamed=1`.
- Live + active + tracker parent diverged → one `to_update` with `Parent item` only, `parent_fixed=1`.
- Live + active + both diverged → one `to_update` with both fields, `renamed=1` + `parent_fixed=1`.
- Live + inactive + Tracker bare → one `to_update` flipping title to `(archived) X`, `renamed=1` + `archived=1`.
- Live + inactive + Tracker already `(archived) X` → no-op.
- Re-activated (was inactive, now active) → strip `(archived)` prefix, `renamed=1`, `archived=0`.
- Tombstoned (`deleted_at is not None`) + Tracker bare → flip to `(archived) X`, `renamed=1` + `archived=1`.
- Tombstoned + Tracker already `(archived) X` → no-op.
- `tracker_node_page_id` NULL → `to_create`, `created=1`.
- `tracker_node_page_id` stale (not in tracker snapshot) → `to_create`, `created=1`, stale-id warning in details.
- Tombstoned new row (NULL tracker_id + tombstoned) → `to_create` with `(archived) X` title, `created=1` + `archived=1`.
- Two canonical rows pointing to same `tracker_node_page_id` → one `to_update` only, duplicate warning in details.
- Root row (no parent) → no parent fix proposed.
- Live child + tombstoned parent → desired parent = parent's `tracker_node_page_id` (still kept; document the decision in a test assertion via comment).
- Live child + parent missing from canonical entirely → parent fix to `None` only if Tracker currently has a parent; otherwise no-op.
- Parent canonical exists but has no `tracker_node_page_id` (mid-run state) → parent target stays `None`, no exception.

### 9. Unit tests — I/O `sync()`
`TestSync` class:
- Aborts with one error when `team_tracker_db_id` is empty string.
- Aborts with one error when Supabase env unset (patch `os.environ` cleared).
- Notion Tracker query failure → one error, no Supabase calls beyond the credential check.
- Supabase canonical query failure → one error, no Notion writes.
- Canonical empty → warning logged, returns with `errors=0`, no writes.
- Bootstrap (all 5 rows need create) → 5 `create_page` + 5 Supabase PATCHes + 5 Notion `Tracker Node` PATCHes; `created=5`.
- Pure rename (canonical title differs from Tracker) → exactly one `update_page` on the Tracker id with `Task` only; `renamed=1`.
- Pure reparent → exactly one `update_page` with `Parent item` only; `parent_fixed=1`.
- Dry-run with creates + renames + parent fixes pending → zero API calls (assert via `client.create_page.assert_not_called()` and Supabase write helpers patched to raise on call); counters reflect plan; `details` mentions `dry-run`.
- Pass-1 `create_page` succeeds but Supabase `tracker_node_page_id` PATCH fails → `errors += 1`, Tracker leak logged with id in details, Notion `Tracker Node` write NOT attempted (no point with broken canonical).
- Notion `Tracker Node` writeback failure on success path → details warning, `errors` unchanged.
- One Tracker update fails in pass 2 → other Tracker updates still attempted; `errors=1`, others succeed.

### 10. Run validation locally (dry-run, then live)
- `../venv/Scripts/python -m pytest tests/hierarchy/ -v` — full hierarchy suite passes.
- `../venv/Scripts/python -m ruff check src/hierarchy/ tests/hierarchy/` — clean.
- Suggested for Santiago to run (per CLAUDE.md hard rule — don't run pipeline yourself):
  - **Dry-run preview (no writes, Notion + Supabase reads only):**
    ```powershell
    python -m src.main --sync-hierarchy --sub-sync tracker_applier_sync --dry-run --verbose
    ```
    Endpoints: Notion API + Supabase REST. **No `GEMINI_API_KEY` / `OPENAI_API_KEY` used.**
  - **Live one-shot:**
    ```powershell
    python -m src.main --sync-hierarchy --sub-sync tracker_applier_sync --verbose
    ```
    Same key/endpoint profile.
  - Verify Supabase post-run:
    ```sql
    SELECT count(*) AS total,
           count(*) FILTER (WHERE tracker_node_page_id IS NULL) AS unmapped
    FROM hierarchy_rows;
    ```
    The `unmapped` count should be 0 after a successful first live run.

### 11. Deploy
- `./scripts/quick-deploy.sh` — code-only deploy. No `template.yaml` change. Tomorrow's 05:00 UTC cron picks up the new sub-sync alongside `macro_block_sync` and `canonical_mirror_sync`.

### 12. Documentation updates
- `docs/architecture.md` — add the new sub-sync to the Hierarchy DB sync table; note the dependency order (`canonical_mirror_sync` must run first).
- `docs/notion-schema.md` — update the `Tracker Node` row in the Hierarchy DB schema section to reflect that it's now actively maintained by `tracker_applier_sync` (was: "currently populated by hand; will be consumed by Supabase-driven downstream sync in PR2").
- `CLAUDE.md` — extend the `[DETAILS INSIDE]` hard rule with one sentence about the Supabase-canonical-driven sync (replaces the version we reverted in PR1).

### 13. Final validation
- Re-run `pytest tests/hierarchy/` and `ruff check` after doc edits.

## Testing Strategy

### Unit Tests

All in `tests/hierarchy/test_tracker_applier_sync.py`. Same shape as `test_macro_block_sync.py` and `test_canonical_mirror_sync.py`:
- `TestPlanTrackerUpdates` — deterministic tests on `_plan_tracker_updates` covering every case in Step 8.
- `TestSync` — mocked `NotionClientWrapper` + patched `_http` from `canonical_mirror_sync`, asserting exact API call shapes and counter aggregation per Step 9.

### Integration Tests

No automated integration tests (matches the project posture per CLAUDE.md). Manual integration check via Notion MCP after the live run: query the Team Task Tracker for `[DETAILS INSIDE]` rows and confirm every Hierarchy row name is represented (or the `(archived)` variant for tombstoned).

### Edge Cases

- `tracker_node_page_id` set in canonical but Tracker page deleted manually → planner treats as `to_create`, recreates + re-links.
- `tracker_node_page_id` set but Tracker page is Notion-archived → loader excludes it from snapshot, planner treats as `to_create`, recreates.
- Two canonical rows pointing to same Tracker id → planner emits one `to_update`, logs duplicate.
- Live child + tombstoned parent → keep the parent link (decision locked in this PR).
- Tier-0 row (no parent) → no parent fix proposed even if Tracker has one (parent diff `None != something` → patch clears it).
- Pass-1 partial failure (create OK, Supabase PATCH fails) → orphan logged + counted as error; next run recreates.
- Empty canonical (PR1 mirror hasn't run or table missing) → benign warning, no writes, no error count.
- Title exceeds 2000 chars → truncate (defensive; Hierarchy names are short).
- Notion `Tracker Node` writeback fails on create → warning only; Supabase canonical is the real source.
- DST drift on cron — already accepted (existing description on `HierarchySync` rule).

## Acceptance Criteria

- Running `python -m src.main --sync-hierarchy --sub-sync tracker_applier_sync --dry-run --verbose` against a fully-aligned workspace reports `created=0 renamed=0 archived=0 parent_fixed=0 errors=0` and makes zero `create_page` / `update_page` API calls.
- Renaming a Hierarchy row's `Name` and running the live command renames exactly the matching Tracker row once (one `update_page` carrying only `Task`).
- Toggling `Active=false` on a Hierarchy row → matching Tracker row's title becomes `(archived) <Name>` on the next run; the Tracker page is NOT archived or deleted.
- Toggling back to `Active=true` → bare name restored on the Tracker.
- Removing a row from the Hierarchy DB (so `canonical_mirror_sync` tombstones it) → matching Tracker row title becomes `(archived) <Name>` on the next run.
- Adding a new Hierarchy row → after the next cron tick, a new `[DETAILS INSIDE]` Tracker row exists with the right title and parent, `hierarchy_rows.tracker_node_page_id` is populated, and Notion `Tracker Node` cache is written (best-effort).
- Reparenting in the Hierarchy DB → Tracker row's `Parent item` follows on the next run.
- Pass-1 partial failure surfaces with `errors >= 1` and the leaked Tracker id in `report.details`; sync continues to process subsequent rows.
- All unit tests in `tests/hierarchy/` pass; `ruff check src/hierarchy/ tests/hierarchy/` clean.
- Hard rule preserved: no `[DETAILS INSIDE]` row is ever `archive_page`d or deleted by this sub-sync; soft-archive only via title rename.
- After the first live run, the SQL query `SELECT count(*) FROM hierarchy_rows WHERE tracker_node_page_id IS NULL` returns 0.

## Documentation Update (MANDATORY)

After implementing this feature, update the following documentation:

### README.md
- [ ] N/A — no README in this project.

### API Documentation
- [ ] N/A — no public HTTP API change.

### Technical Docs
- [ ] `docs/architecture.md` — add `tracker_applier_sync` to the Hierarchy DB sub-sync table; document the dependency on `canonical_mirror_sync` running first (order matters in `_SUB_SYNCS`).
- [ ] `docs/notion-schema.md` — update the `Tracker Node` description in the Hierarchy DB section to reflect active maintenance by `tracker_applier_sync`; note that `hierarchy_rows.tracker_node_page_id` in Supabase is the authoritative mapping, the Notion `Tracker Node` relation is a human-readable cache.
- [ ] `CLAUDE.md` — extend the `[DETAILS INSIDE]` hard rule with: "The Supabase canonical (`public.hierarchy_rows`) is the source of truth for these rows' titles and parent links; `tracker_applier_sync` (daily 07:00 Madrid) reconciles them and soft-archives tombstoned/inactive rows as `(archived) X`. Edit names and parents in the Hierarchy DB, not in the Tracker."

## Validation Commands

Execute every command to validate the feature works correctly with zero regressions.

```bash
# Unit tests for the hierarchy package (including the new file)
../venv/Scripts/python -m pytest tests/hierarchy/ -v

# Full test suite — no regressions in the wider pipeline
../venv/Scripts/python -m pytest tests/ -v

# Lint
../venv/Scripts/python -m ruff check src/hierarchy/ tests/hierarchy/
```

Santiago-run (per CLAUDE.md hard rule — Claude does NOT execute these):

```powershell
# Dry-run preview — Notion + Supabase reads only (no Gemini/OpenAI keys involved)
python -m src.main --sync-hierarchy --sub-sync tracker_applier_sync --dry-run --verbose

# Live one-shot
python -m src.main --sync-hierarchy --sub-sync tracker_applier_sync --verbose

# Supabase sanity check (via the Supabase MCP or psql)
SELECT count(*) FROM hierarchy_rows WHERE tracker_node_page_id IS NULL;
# Expected: 0 after first successful live run.

# Deploy (code-only — no template.yaml change in this PR)
./scripts/quick-deploy.sh
```

After deploy, the 05:00 UTC cron emits **three** structured lines per invocation:

```
hierarchy_sync: name=macro_block_sync         created=… renamed=… archived=… edited=… deleted=… reactivated=… parent_fixed=… errors=…
hierarchy_sync: name=canonical_mirror_sync    created=… renamed=… archived=… edited=… deleted=… reactivated=… parent_fixed=… errors=…
hierarchy_sync: name=tracker_applier_sync     created=… renamed=… archived=… edited=… deleted=… reactivated=… parent_fixed=… errors=…
```

## Notes

- **Why read all canonical rows (including tombstoned), not just live ones.** Tombstoned canonical rows still drive a desired state on the Tracker side (the `(archived) X` title). If we filtered them out at the Supabase query level, the planner couldn't tell the difference between "row deleted from Notion → should be (archived) on Tracker" and "row never existed → ignore". Reading all rows + filtering in the planner keeps the diff logic local to one place.
- **Why two passes instead of topological sort.** Same reasoning as the deleted `tracker_node_sync` plan: a two-pass create-then-reconcile is dead-simple to reason about, naturally re-entrant (a second run does pass 2 only), and the planner stays pure. The cost is at most one extra Notion call per newly-created row on bootstrap; in steady state the second pass is a no-op.
- **Why `tracker_node_page_id` lives in BOTH Supabase and Notion.** Supabase is authoritative for the applier's logic. The Notion `Tracker Node` relation is a human-readable cache so curious humans browsing the Hierarchy DB can click through to the Tracker row. Discrepancy between the two never affects correctness (Supabase wins); the only effect is a stale Notion link until the next applier run heals it on a write.
- **Cost.** Zero LLM. Pure Notion + Supabase REST. Steady state: 1 Supabase GET (canonical) + 1 Notion query (Tracker `[DETAILS INSIDE]`) + 0 PATCHes when state matches. Bootstrap: ~150 API calls (50 creates + 50 Supabase back-fills + 50 Notion writebacks). All well under daily quota.
- **What this sub-sync explicitly does NOT do.** Touch `Status`, `Assignee`, `Due Date`, `Category`, or `Sub-item` on Tracker rows. Move existing real (non-architecture) tasks parented to a renamed node (parents are stored by id; renames don't break child→parent relations). Garbage-collect orphan `(archived) X` rows (future work — likely a manual or one-off cleanup script, not a recurring job). Migrate the Notion `Tracker Node` relation to be read-only (could be a future PR4 once we trust Supabase fully).
- **Relation to PR2.** PR2 rewrites `macro_block_sync` using the same applier shape (read canonical, diff target, apply). The Supabase HTTP helpers shared via this PR's import in `canonical_mirror_sync` keep PR2 dry. PR2 also adds a `work_area_option_mappings` table (since Notion select option ids aren't intrinsic to Hierarchy rows the way `tracker_node_page_id` is — Tier-0 rows can have many member-DB option mappings, one per member DB).
- **Open future question.** Should we drop the Notion `Tracker Node` relation entirely once PR3 is stable and humans trust Supabase? Probably yes — eliminates the dual-source-of-truth smell. But not in this PR. The relation also makes the spec's bootstrap test much easier to verify by hand (just look at Notion's relation column).
