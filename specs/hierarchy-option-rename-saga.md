# Feature: Notion select / multi-select option rename saga

## Background — why the PR2 + PR4 design needs a fix

PR2 (`macro_block_sync`) and PR4 (`detail_applier_sync`, `external_org_applier_sync`) all
assume that a Notion select / multi-select option can be **renamed in place**
via `data_sources.update` by sending the option's existing `id` together with a
new `name`. The wrapper docstring at `src/notion_client_wrapper.py:236-239` and
the planners' CASE A all bake that assumption in.

**The assumption is wrong.** Verified 2026-05-21 via
`scripts/diag_work_area_options.py` against Jacob's `Work area`:

* PATCH `data_sources.update` with `{id, name: NEW}` → returns 200, response
  echoes the new state. Re-fetch the data source → option still has the OLD
  name.
* Same behavior with `databases.update` (legacy endpoint), with and without
  the `color` field, with and without the `id` field. Rename is silently
  no-op'd in every variant.

What DOES work via PATCH:

* Adding new options (omit `id`; Notion assigns one in the response).
* Removing options (omit them from the array; Notion drops them).
* `pages.update` to change a page's property value — used by
  `tracker_applier_sync` already and works correctly.
* Renaming options in the Notion UI (non-public endpoints — not available
  to us).

Because of the silent no-op, the planner's CASE A "name changed" branch
produces a PATCH that Notion accepts and ignores. Drift accumulates: today
Jacob and Santiago each have 4 `Work area` options whose names diverge from
canonical because the planner has been trying to rename them every morning and
Notion has been ignoring it. `Supabase.work_area_option_mappings` was updated
optimistically with the new (desired) name, so the mapping table now claims
"canonical and Notion match" while they don't.

## Feature description

Replace every "id-preserving rename" code path in the three appliers with a
**5-step saga** that achieves a logical rename by creating a new option,
migrating tagged pages onto it, and dropping the old option. Option IDs change
on rename — this is the cost of working around Notion's API limitation. The
mapping table absorbs the churn: it always points at the current live option
id for each canonical row, regardless of how many sagas have rotated the id
over time.

Idempotent — a saga that fails partway resumes cleanly on the next tick.
Generic across `select` and `multi_select` — the only delta is the per-page
migration shape.

## User story

As Santiago,

1. I want a canonical-source rename (e.g. `Operations and AI enablement` →
   `Operations & AI enablement` in Tier 0) to actually land in every active
   member DB on the next 07:00 Madrid tick — without me touching 9 DBs by
   hand, and without `diag_work_area_options.py` being the only thing that
   tells me the prior approach silently failed.

2. I want each member's per-meeting tags to **follow the rename automatically**
   — pages currently tagged with the old name should end up tagged with the
   new name when the saga completes. No human-side action.

3. I want the saga to be safe to interrupt: if AWS Lambda times out mid-saga,
   or one page migration fails, the next morning's tick should finish the job
   rather than start over and create duplicates.

4. I want clear errors when a saga can't complete (a page migration fails, a
   query 5xx's) so I can investigate — not silent drift like today.

## Problem statement

* **Wrong contract baked into 3 appliers + 1 wrapper docstring.** The
  planner's "CASE A — current_name != desired_sanitized → rename" path
  produces a PATCH that does nothing. The deployed cron has been re-issuing
  these no-op PATCHes daily; the `renamed=N` counter in the logs has been
  lying. After this PR the planner emits a `_RenameIntent` instead of mutating
  in place; the I/O layer executes the saga.

* **`Supabase.work_area_option_mappings` is desynced.** The pre-saga code path
  wrote the DESIRED canonical name to `option_name` after every (silently
  no-op'd) PATCH — so Supabase claims the rename happened while Notion shows
  the old name. After the saga runs once, every active member's `option_id`
  *and* `option_name` will reflect the real, current Notion state.

* **No page-migration step.** Even if PATCH-rename worked, the planner has no
  mechanism for moving pages off the old option onto the new one. The saga
  introduces that step.

## Solution statement

**Five-step saga**, executed in the I/O layer of each applier whenever the
planner reports a name change (`_RenameIntent`):

1. **PATCH 1** — `data_sources.update` with the full options array containing
   the OLD option preserved + a new option appended (no `id`, just `name` +
   `color`). Notion assigns the new option an id, returns it in the response
   body. We extract `new_option_id` from the response (or re-fetch if the
   shape is malformed).

2. **Query tagged pages** in the member DB:
   * `select` →
     `{property, select: {equals: old_name}}`
   * `multi_select` →
     `{property, multi_select: {contains: old_name}}`

   Uses `query_database` (already paginates). Each result page carries the
   current value of the property — used directly in step 3 (no extra reads).

3. **Migrate each page** via `pages.update`:
   * `select` → set the property to `{select: {id: new_option_id}}`.
   * `multi_select` → take the page's current array, remove the entry whose
     name matches the old name (defensively also matches by id), add an
     entry `{id: new_option_id}`, write back the FULL array. Preserves every
     other tag on the page.

4. **PATCH 2** — `data_sources.update` with the options array MINUS the old
   option. Notion removes it. Safe because step 3 ensured no page references
   the old option any more.

5. **Mapping back-fill** — caller upserts `(canonical_id, member_db_id) →
   (new_option_id, desired_name)` into the per-property mapping table
   (`work_area_option_mappings` / `detail_option_mappings` /
   `external_org_option_mappings`). After this, the next tick sees the
   mapping as in-sync.

### Idempotency / mid-saga resume

The saga is restartable from any failure point because every step's effects
are detectable on the next tick:

| Last completed step | Next tick observable state | Resume path |
|---|---|---|
| 0 (nothing yet) | Old option only; mapping points at old id | Run full saga |
| 1 (PATCH 1 done; pages not migrated) | Both old + new options present; mapping still on old id; pages still tagged with old name | Skip PATCH 1 (resolve `new_option_id` by name lookup), resume from step 2 |
| 2-3 (PATCH 1 + some/all migrations done; PATCH 2 not) | Both options present; some pages migrated, some not; mapping on old id | Skip PATCH 1, re-run step 2 (only finds pages still on old name), step 4, step 5 |
| 4 (PATCH 2 done; mapping back-fill failed) | New option only; mapping still on old id (stale) | Next tick: mapping CASE B (stale option_id) → bootstrap-adopt by sanitized name (the existing healing path) → repairs mapping |

The resume hinges on **detecting "new option already exists" by name lookup**
in the current Notion options array. If we find an option whose name matches
`desired_sanitized` and whose id is NOT the old option's id, we use its id as
`new_option_id` and skip PATCH 1.

Notion's own UI enforces option-name uniqueness within a property; the API
generally honours the same constraint when adding via PATCH. The saga checks
defensively — if multiple options share the desired name, we pick the one
whose id is not the old id and log a detail line for operator visibility.

### Why this works for archive / unarchive too

Archive (`Sourcing` → `(archived) Sourcing`) and unarchive
(`(archived) Sourcing` → `Sourcing`) are both name changes. They go through
the same saga. The page-migration step ensures historical meetings tagged on
the live option follow into the archived option, so "what meetings touched
Sourcing" still resolves after the saga.

### What the saga does NOT cover

* **Color-only changes.** `Detail` and `External Org` carry canonical-driven
  color. If only the color changes (name unchanged), the existing single PATCH
  path is kept. The diag only proved that name renames are silently no-op'd;
  color-only PATCHes have not been independently verified to be broken. If
  they turn out to be broken too, the fix is a tiny extension here — emit a
  rename intent on `(name_changed or color_changed)` rather than just
  `name_changed`. Out of scope for this PR.
* **Creates / drops** — these already work via plain PATCH and stay unchanged.
* **Reordering** — `external_org_applier_sync` reorders options (Portfolio
  first, alpha within stage). The reorder happens in the FINAL PATCH after all
  sagas. The sagas themselves do not preserve ordering.

## Relevant files

### Modified

* `src/notion_client_wrapper.py` — fix the wrong docstring on
  `update_data_source` (lines 236-239). No method-signature change needed
  (`query_database` and `update_page` already exist and cover the saga's
  page-migration needs).
* `src/hierarchy/macro_block_sync.py` — planner CASE A + CASE C emit
  `_RenameIntent`s. I/O layer runs sagas then a final PATCH.
* `src/hierarchy/detail_applier_sync.py` — same pattern, multi-select page
  migration variant.
* `src/hierarchy/external_org_applier_sync.py` — same pattern, plus the
  stage-out archive (Pass 2) and the un-archive (CASE A re-entry) also go via
  the saga. Legacy cleanup + reorder behavior (PR4) is preserved by keeping
  the final PATCH path.

### New

* `src/hierarchy/_rename_saga.py` — shared, generic saga executor. Pure I/O
  module: takes a `NotionClientWrapper`, the property metadata, and one rename
  intent. Returns `(new_option_id, post_saga_state, detail_lines)`. Raises on
  hard failure; caller per-applier loop catches and surfaces as
  `report.errors += 1`.
* `specs/hierarchy-option-rename-saga.md` — this file.

### Tests (new cases per applier)

* `tests/hierarchy/test_macro_block_sync.py` — saga happy path; mid-saga
  resume; PATCH 2 / migration failure surfaces error.
* `tests/hierarchy/test_detail_applier_sync.py` — saga happy path
  (multi-select); page array swap preserves other tags; resume.
* `tests/hierarchy/test_external_org_applier_sync.py` — stage-out triggers
  saga to archive (not the silent in-place rename); re-entry triggers saga to
  un-archive; saga happy path for a deal-name rename.
* `tests/hierarchy/test_rename_saga.py` — unit tests for the shared helper:
  select + multi_select happy paths, resume detection, PATCH-1 failure raises,
  page migration failure raises, multi-select page swap preserves other
  multi-select tags on the same page.

## Implementation plan

### Phase 1 — wrapper docstring fix

`src/notion_client_wrapper.py:236-239`: replace the misleading "Pass option
ids when renaming" docstring with one that documents the real behaviour (PATCH
can add or remove options but cannot rename existing ones) and points callers
who need a logical rename at `src/hierarchy/_rename_saga.py`.

### Phase 2 — shared saga helper

`src/hierarchy/_rename_saga.py`:

```python
@dataclass
class RenameIntent:
    """Logical rename of one option on one member DB."""
    old_option_id: str
    old_name: str
    desired_name: str
    desired_color: str | None        # None for select properties without canonical color
    # Free-form annotation used in detail lines (e.g. canonical row id) — opaque
    # to the saga.
    annotation: str = ""


def execute_rename_saga(
    *,
    client: NotionClientWrapper,
    member_db_id: str,
    property_name: str,
    property_type: str,              # "select" or "multi_select"
    intent: RenameIntent,
    current_state: list[dict[str, Any]],  # full options array on member DB at saga start
) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Run the 5-step saga. Returns (new_option_id, post_saga_state, detail_lines)."""
```

Behaviour:

1. **Detect resume**: scan `current_state` for `name == intent.desired_name`,
   id != `intent.old_option_id`. If found → that is `new_option_id`; skip
   PATCH 1. Else → execute PATCH 1 with `current_state` + appended new option
   (no id); read `new_option_id` from response (parse the patched options
   array and find the entry with `name == desired_name` and no pre-existing
   id-match). If both lookup paths fail, re-fetch the data source once.
2. **Query tagged pages**: build the property-type-specific filter, call
   `client.query_database(member_db_id, filter=...)`. Returns paginated full
   list.
3. **Migrate each page**:
   * select → `client.update_page(page_id, {property_name: {"select": {"id": new_option_id}}})`
   * multi_select → read each result page's current array from the query
     response, drop entries where `name == old_name OR id == old_option_id`,
     append `{"id": new_option_id}`, call
     `client.update_page(page_id, {property_name: {"multi_select": <new_array>}})`.
4. **PATCH 2**: `client.update_data_source(member_db_id, {property_name:
   {<property_type>: {"options": current_state_minus_old_plus_new}}})`. The
   "post-saga state" is computed as `current_state` minus old option, with
   new option (with `new_option_id`) appended.
5. Return `(new_option_id, post_saga_state, details)`. Caller assembles the
   mapping back-fill.

Hard failures (PATCH 1 / query / any migration / PATCH 2) raise.
Caller per-applier loop catches and records `report.errors += 1`. Partial
state on the member DB is fine — next tick resumes.

Tag-check failures (when the property is empty on every page) are NOT a
saga-level concern: the query returns empty results, migration loop runs zero
times, PATCH 2 proceeds normally.

### Phase 3 — per-applier refactor

For each of the 3 appliers:

1. Add `_RenameIntent` accumulator to `_PlannerResult` (or import shared
   `RenameIntent` from `_rename_saga`).
2. Planner: in CASE A (mapping hit) and CASE C (bootstrap-adopt) — whenever
   `current_name != desired_sanitized`, emit a `RenameIntent` and mutate
   `out[idx]` to carry the desired name (so the planner's final `new_options`
   still represents the desired state for the final PATCH). Color-only
   changes continue to be applied via mutation only (no rename intent).
3. I/O layer:
   1. Compute plan (unchanged).
   2. For each `RenameIntent` in `plan.renames`: call
      `execute_rename_saga(...)`. Track returned `new_option_id` and update
      the per-applier mapping accumulator. On success, swap `old_option_id →
      new_option_id` in the in-memory `plan.new_options` array (so the final
      PATCH carries the correct id).
   3. Issue the final `update_data_source` PATCH if `plan.new_options` still
      differs from the post-all-sagas state (creates, drops, reorder).
   4. Mapping back-fill: for each rename, write
      `(canonical_id, member_db_id) → (new_option_id, desired_name)`. For
      each create-from-scratch (pending_creates), use the existing
      back-fill-from-PATCH-response path.

External-org applier extras:

* Pass 2 (stage-out) currently mutates `out[idx]` to `(archived) X`. Change
  to: emit `RenameIntent(old, current_name, "(archived) " + current_name,
  current_color)` AND mutate. The saga handles the rename; the final PATCH
  handles the ordering.
* Re-entry un-archive (CASE A `(archived) Project Lavare` → `Project
  Lavare`): same change — emit rename intent.

### Phase 4 — tests

`tests/hierarchy/test_rename_saga.py` (new):

* `test_select_happy_path_executes_5_steps`
* `test_multi_select_happy_path_preserves_other_tags`
* `test_resume_skips_patch1_when_new_option_already_present`
* `test_resume_only_migrates_remaining_pages`
* `test_patch1_failure_raises`
* `test_page_migration_failure_raises_and_skips_patch2`
* `test_patch2_failure_raises`

Per applier (extending existing test files):

* macro_block_sync — `test_rename_via_saga_when_name_changes`,
  `test_resume_after_partial_saga_completes_cleanly`,
  `test_archive_goes_through_saga`.
* detail_applier_sync — `test_multi_select_rename_via_saga`,
  `test_multi_select_swap_preserves_other_tags_on_same_page`.
* external_org_applier_sync — `test_stage_out_archives_via_saga`,
  `test_re_entry_un_archives_via_saga`.

Existing tests that asserted the in-place rename PATCH shape (e.g.
`test_rename_via_mapping_preserves_id`, `test_name_change_via_mapping_preserves_id`,
`test_re_entry_un_archives_in_place`) are rewritten: same logical assertion
(canonical desired state lands on Notion + mapping reflects it), but via the
saga (PATCH 1 adds new id, page migration, PATCH 2 drops old id, mapping
upsert with new id).

## Step-by-step tasks

1. Update `src/notion_client_wrapper.py:236-239` docstring.
2. Add `src/hierarchy/_rename_saga.py` (shared helper + `RenameIntent`).
3. Refactor `src/hierarchy/macro_block_sync.py` (planner emits intents; I/O
   runs sagas + final PATCH + back-fill with new ids).
4. Refactor `src/hierarchy/detail_applier_sync.py` (same; multi-select
   migration variant).
5. Refactor `src/hierarchy/external_org_applier_sync.py` (same; Pass 2
   stage-out + CASE A un-archive also via saga; legacy cleanup + reorder
   preserved).
6. Add `tests/hierarchy/test_rename_saga.py`. Extend the three per-applier
   test files with saga cases. Rewrite the prior "id-preserving rename" tests
   to assert via saga.
7. Update `docs/architecture.md` — remove "id-preserving renames" wording
   from the 3 applier rows; describe the saga + that ids change on rename;
   note that page tags follow the migration.
8. Run validation: `../venv/Scripts/python -m pytest tests/hierarchy/ -v` then
   `../venv/Scripts/python -m pytest tests/ -v` then
   `../venv/Scripts/python -m ruff check src/ tests/`.

Santiago runs (per the never-run-the-pipeline-yourself hard rule):

```powershell
# Dry-run first
python -m src.main --sync-hierarchy --sub-sync macro_block_sync --dry-run --verbose

# Then live — the 4 drift renames on Jacob + Santi should land on this tick.
# Endpoints: Notion + Supabase only. No GEMINI_API_KEY / OPENAI_API_KEY needed.
python -m src.main --sync-hierarchy --verbose
```

Expected log line: `macro_block_sync: Jacob created=0 renamed=4 archived=0`
and same for Santiago; on the next tick `renamed=0` (idempotent).

## Acceptance criteria

* `update_data_source` docstring no longer claims id-preserving renames work.
* The saga executor in `src/hierarchy/_rename_saga.py` is generic over
  select / multi_select; the per-applier I/O layers call it without
  duplicating the 5-step logic.
* All 3 appliers route name changes (rename, archive, un-archive) through the
  saga. Color-only changes continue via the single PATCH path.
* Mid-saga resume works without creating duplicates: if PATCH 1 succeeded but
  PATCH 2 didn't, next tick finishes the job.
* For Jacob (`35083e67-e2e7-80b0-9d8b-c213e9b161f3`) and Santiago Cuadra
  (`34583e67-e2e7-8081-b515-f5e33926f153`), the first live run after this PR
  resolves the 4 drift `Work area` options (current Notion name → canonical
  sanitized) with `renamed=4` per member, no creates, no errors. Second tick
  → `renamed=0` (idempotent).
* New `tests/hierarchy/test_rename_saga.py` covers the cases above.
* Per-applier test files have saga cases; prior in-place-rename tests
  rewritten to the saga shape.
* `docs/architecture.md` Hierarchy DB sub-sync table updated.
* `pytest tests/hierarchy/` green; full suite green except the pre-existing
  unrelated `test_depth_3_keeps_organizational_nodes` failure on master.

## Edge cases + failure handling

* **PATCH 1 fails (5xx/network).** Raises → caller catches → `report.errors
  += 1` with a detail line. Mapping not updated. Next tick: state unchanged
  (PATCH 1 didn't take), full saga retried.
* **PATCH 1 succeeds; first page migration fails.** Raises → caller catches.
  State: old + new options both present in Notion; pages still on old name;
  mapping still on old id. Next tick: saga detects resume → migrates remaining
  pages → PATCH 2 → mapping back-fill.
* **PATCH 2 fails after pages migrated.** Raises → caller catches. State: old
  option still present but unused; new option in use. Next tick: saga's CASE A
  comparison sees `current_opt = new option` (because mapping was already
  updated? — no, mapping not updated; it's still on old id). Bootstrap-adopt
  by sanitized name picks up new option_id; old option is detected as
  "mapping has option_id no longer matching current state" and is dropped via
  a plain PATCH on a later tick (legacy-cleanup style).
* **Saga succeeds; mapping back-fill upsert fails.** State: Notion + pages
  correct; Supabase mapping still on old (now-deleted) id. Next tick: CASE B
  (stale option_id) → bootstrap-adopt by sanitized name → repairs mapping.
  Same recovery as the existing PR2 bootstrap path.
* **A page in the query result was edited between query and migration**
  (someone removed the External Org tag manually). `pages.update` still
  succeeds; the new value is whatever the migration wrote. Acceptable —
  Notion's last-writer-wins.
* **Multi-select page has both old and new option tagged** (operator
  manually tagged the new one before saga ran). Migration logic dedupes:
  the new array drops the old name AND drops any pre-existing entry for the
  new id, then appends the new id once. Net effect: no duplicates.
* **Two simultaneous renames touch the same option** (canonical changes
  twice between ticks). The planner picks the latest canonical name; saga
  produces one new option with the latest name. Intermediate name never lands
  on Notion.

## Why this isn't a separate sub-sync

The saga lives inside each applier's I/O layer rather than as its own
`*_sync.py` because:

* The trigger is detected during planning (CASE A name change). Splitting
  the trigger from the action would force a round-trip through Supabase to
  persist "pending rename intents" between sub-syncs.
* The saga's success criterion is the same as the applier's: the member DB
  ends the tick with the desired option list. Co-locating keeps the report
  / detail / error model unified.
* The shared helper module keeps the duplicated 5-step orchestration out of
  each applier — they each call one function.

## Open future questions

* If color-only changes turn out to also be silently no-op'd by Notion, the
  fix is one line in each planner: emit RenameIntent when `name_changed OR
  color_changed`. Not done now because: (a) the diag specifically tested
  name renames; (b) running the saga for a color-only change is expensive
  (option id churn + page migration with no actual tag change).
* `Auto-extract Tasks = false` flow uses the literal-notes path; this saga
  has no interaction with it.
* The Notion API may eventually add a real `data_sources.update_option`
  endpoint. If so, the saga becomes a one-line PATCH; the shared helper is
  the single replacement site.
