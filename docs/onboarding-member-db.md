# Onboarding a new personal Meeting Notes DB

Protocol for adding a new team member's Meeting Notes database so it works with
**every** feature (template injection, AI extraction, Supabase sync, Meeting
Mirrors, fundraising, and the daily hierarchy/tag fan-out).

Onboarding is **config-as-data** — no code change, no redeploy. The pipeline
re-reads the Org Chart every cron tick, so a correctly-set-up DB is picked up
within ~1 min (extraction), the next 5-min tick (Supabase sync), and the next
07:00 Madrid run (option fan-out).

> ⚠️ **The single most important rule:** create the `Macro Work Block` and
> `Detail` properties with **empty option lists**. Do **NOT** copy another
> member's options. See [Why empty option lists](#why-empty-option-lists-matter).

---

## Checklist

### 1. Create the DB schema

Easiest: duplicate an existing member DB (exact property names/types), **then
delete every option** from `Macro Work Block` and `Detail`.

> ⚠️ **Empty the option lists in the Notion UI, not via the MCP
> `update_data_source` DDL.** An `ALTER COLUMN … SET SELECT()` round-trips the
> whole schema and **silently drops every `rich_text` column** as a side
> effect. If you must use MCP and your DB has rich-text columns you want to
> keep, re-fetch afterward and `ADD COLUMN "X" RICH_TEXT` to restore them.

Required properties (names are case-sensitive and must match Notion exactly):

| Property | Type | Notes |
|---|---|---|
| `Meeting` | title | |
| `Date` | date | Webhook handler writes `Date = created_time` |
| `Meeting type` | select | include the `Fundraising` option → fires the Affinity LP branch |
| `Macro Work Block` | select | **create EMPTY** — options seeded by `macro_block_sync` |
| `Detail` | multi_select | **create EMPTY** — options seeded by `detail_applier_sync` |
| `External Org` | select | frozen/manual; populate by hand |
| `Confidential` | select | options `Confidential` / `Shareable`; Meeting Mirrors confidentiality gate. Optional — absent ⇒ blank ⇒ owner's `Default Mirror Visibility` default. See [docs/meeting-mirrors.md](meeting-mirrors.md) |
| `Files & media` | files | |
| `Governance: Edit & View Access` | people | |
| `Processed` | checkbox | pipeline state |
| `Processing` | checkbox | concurrency lock |
| `Template Injected` | checkbox | template-injection state |
| `Task - Relation` | relation → **shared** Team Task Tracker (`32f83e67e2e7803f9662f43125603afa`) | the only meeting↔task link |
| `Created` / `Created by` | created_time / created_by | `Created by` is optional for parity; the code reads the page-level `created_by`, not the column |

The tracker is **shared, not per-member** — point `Task - Relation` at the
existing one.

Older member DBs also carry legacy `AI Summary` (rich_text) and `Tasks`
(rich_text) columns. Neither is required: `Tasks` is unused by the code, and
`AI Summary` is only a null-safe fallback (the real summary lives in the
AI-managed `meeting_notes` block). Safe to omit on new DBs.

### 2. Register in the Org Chart

Add one row to the Nzyme Org Chart (`1a9aab32-5c56-40fa-b040-f9c7c040eace`):

- `Name` (title)
- `Email` — needed for GCal attendee → name resolution and transcript speaker ID
- `Active` = ✅ true
- `Meeting Notes DB` = the new DB's URL (a `?v=…&source=copy_link` suffix is fine — the parser takes the path ID)
- `Auto-extract Tasks` = `false` (literal-notes path) or `true` (full transcript pipeline)
- `Default Mirror Visibility` (select: `Private` / `Shared`) — optional; the member's Meeting Mirrors default for meetings left untagged by the `Confidential` column. Blank/unset ⇒ `Shared` (mirror as before). See [docs/meeting-mirrors.md](meeting-mirrors.md)
- `Role`, `Seniority`, `Department`, `Typical Topics` — used by the transcript pipeline

### 3. Seed the option lists + Supabase mappings (THE step people forget)

Run the hierarchy sync so the appliers create the `Macro Work Block` / `Detail`
options **and** write the option-ID → canonical mappings to Supabase. Notion +
Supabase only — no Gemini/OpenAI keys:

```powershell
python -m src.main --sync-hierarchy --verbose
```

From empty option lists this converges in **one run** (every canonical row is a
*create*, which PATCHes the option and seeds its mapping immediately). Run it a
**second time** and confirm the Gonzalo-style line reads `created=0 … errors=0`
(idempotent = healthy).

**Verify the mappings landed** (replace the db_id with the new DB's hyphenated UUID):

```sql
SELECT 'work_area' tbl, count(*) FROM public.work_area_option_mappings WHERE member_db_id = '<db-uuid>'
UNION ALL
SELECT 'detail' tbl, count(*) FROM public.detail_option_mappings WHERE member_db_id = '<db-uuid>';
```

`work_area` should equal the live Tier-0 count; `detail` should equal the live
`detail_rows` count. **If `detail = 0`, the run errored on this DB** — re-read
the `detail_applier_sync` log line for that member.

> **Colors and Notion's no-in-place-recolor rule.** Notion's API cannot change
> the color of an *existing* select / multi-select option — confirmed
> 2026-06-02 against two PATCH shapes:
>   - `{id, color}` → 400 `Cannot update color of select with id …`
>   - `{name, color}` (no id, to add a same-named "twin") → 400 `Cannot update
>     color of select with name …` — Notion **dedupes options by name**, so it
>     matches the existing option and rejects the color change rather than
>     creating a second option.
>
> So **color is only settable at option *creation*** (a brand-new name). The
> rename saga (`_rename_saga.py`) can carry a color because it creates an option
> under a *new* name; it canNOT recolor an option while keeping the same name
> (the twin-create step 400s on the name dedup above).
>
> Practical consequences:
> - A freshly bootstrapped option (CASE D) comes out with the right color.
> - Recoloring an option that already exists (same name) cannot be done by the
>   appliers as written. Options: recolor in the Notion UI, or run a two-phase
>   temp-name saga (rename `X`→`X⟳`(new color)→`X`) — neither implemented today.
>   Do NOT set a non-default `Color` on a `Detail` Settings-DB row expecting the
>   applier to propagate it to *existing* member-DB options — it will 400 every
>   run until the option colors already match.

### 4. Wire the template-injection webhook (the one manual per-DB step)

In the new DB → **⚡ Automations**, add a rule that POSTs to the API Gateway
webhook URL on page creation (copy it from an existing member's DB). This is the
only thing the crons can't auto-discover.

### 5. Make sure pages carry a `meeting_notes` block

Meetings should be created via Notion's **AI Meeting Notes** integration so each
page has a `meeting_notes` block (transcript, AI Summary, calendar attendees,
notes container). Template injection, the transcript path, Supabase sync, and
Meeting Mirrors all read from it.

### 6. Smoke-test extraction (optional but recommended)

`--db-id` polls only this DB and disables the created-time buffer. Use the path
matching the member's `Auto-extract Tasks` flag (`--no-auto-extract-tasks` for
the literal-notes path). Dry-run writes nothing:

```powershell
python -m src.main --sync --db-id <db-uuid-no-dashes> --no-auto-extract-tasks --dry-run --verbose
```

Create a page with a couple of notes first, or it will (correctly) report
`0 unprocessed meetings`.

---

## Why empty option lists matter

The appliers reconcile each member DB against Supabase **mapping tables**
(`work_area_option_mappings`, `detail_option_mappings`) keyed by
`(canonical_row, member_db_id)`. A freshly-**duplicated** DB inherits another
member's options but has **no mapping rows**.

On the first run with no mappings, the planner can only **bootstrap-adopt by
name** (`macro_block_sync.py:429`): it matches an existing option to a canonical
row purely by name and just records the mapping — it does **not** reconcile.
That means:

- **Renames don't apply.** If a canonical row was renamed (e.g. `Value Creation
  for Portfolio` → `FFF Value Creation for Portfolio`), the duplicated option no
  longer name-matches, so instead of renaming in place the applier
  **bootstrap-creates** a second, prefixed option **alongside** the old one. The
  old one becomes an **orphan** the sync will *never* remove (drops only fire for
  options whose canonical row is tombstoned, which requires a mapping — CASE T,
  `macro_block_sync.py:323`).
- **Colors/positions/drops are skipped** on that first pass — they need the
  mappings to already exist.

Starting from **empty** sidesteps all of it: there's nothing to mis-match, every
canonical row is a clean *create*, and the run both populates the options and
seeds the mappings in one pass. Renames thereafter propagate correctly via the
rename saga, because the mappings now exist.

### Recovering a DB that was duplicated with options (the orphan cleanup)

If a DB already went live with copied options and now has orphans / missing
mappings:

1. Empty both option lists in Notion (do this only when the DB has no tagged
   pages, or you'll clear those tags). The stale Macro mappings self-heal on the
   next run via CASE B (`macro_block_sync.py:418`); no manual Supabase delete is
   required.
2. Re-run `--sync-hierarchy --verbose` (twice) and verify per
   [step 3](#3-seed-the-option-lists--supabase-mappings-the-step-people-forget).

See also: [docs/architecture.md](architecture.md) (Hierarchy DB sync),
[docs/notion-schema.md](notion-schema.md) (schemas/IDs).
