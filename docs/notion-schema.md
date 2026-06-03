# Notion Schema Reference

## Database IDs

| Resource | ID | Notes |
|----------|----|-------|
| Meeting Notes DB | `b07976472620499fa4b89be7b03c07d0` | Source of meeting pages |
| Team Task Tracker DB | `32f83e67e2e7803f9662f43125603afa` | Destination for extracted tasks |
| Playbook page | `33083e67-e2e7-8108-bb08-eaeba8b65678` | "Nzyme Playbook - Task Extraction Rules" under Nzyme Home |
| Meeting template page | `32f83e67-e2e7-8086-9863-e276b70cc5a2` | Default "New page" template in Meeting Notes DB — edit in Notion to change injected content |
| Literal-notes extraction prompt | `35183e67-e2e7-813d-ad55-f011624d2e29` | "📝 Literal Notes Extraction Prompt" under Nzyme Prompts. Used when an Org Chart row has `Auto-extract Tasks = false`. |
| Meeting Mirrors parent page | `36483e67e2e780a0b480ccac6a07ff2b` | "🗂️ Meeting Mirrors" — container page for every topic-mirror DB. |
| Topic Mirror Routes DB | `daa0ef7ac48c40bea82163ebe84ade6b` | Routing config for the Meeting Mirrors feature. Editing rows takes effect on the next cron tick. |
| Meeting Mirrors → AI & Tech | `dc0e537633cb4e8c9c2b97210878d7d2` | First topic mirror DB. Receives pages tagged `Detail = "AI & Tech"`. |
| ~~🏢 External Orgs Settings DB~~ | `36d83e67e2e7807792b4f1f381f12800` | **Deprecated 2026-06-02.** Was a mirror of `ReportingNz_deals`; replaced by `deal_hierarchy_sync` (Hierarchy DB rows) + `external_org_applier_sync` (member-DB `External Org` fan-out). `EXTERNAL_ORGS_DB_ID` is now dead config. |

Workspace: `kiboventures.notion.so`

## Meeting Notes DB

| Property | Type | Values / Notes |
|----------|------|----------------|
| Meeting | title | Meeting title text |
| Date | date | ISO date; informational only — **not used for processing logic** (created_time and last_edited_time are used instead) |
| Attendees | people | List of Notion users (returns id + name) |
| Meeting type | select | Standup, 1:1, Deal review, Portfolio review, Team sync, External, Other, **Fundraising** (Fundraising triggers the Affinity LP Funnel branch when `FUNDRAISING_BRANCH_ENABLED=true`) |
| Confidential | select | `Confidential` / `Shareable` (optional). Meeting Mirrors confidentiality gate: `Confidential` = never mirror, `Shareable` = always mirror, blank = use owner's `Default Mirror Visibility` (Org Chart). Not auto-synced. See [docs/meeting-mirrors.md](meeting-mirrors.md) |
| Processed | checkbox | `false` = unprocessed, `true` = AI extraction completed |
| Template Injected | checkbox | `false` = template not yet injected, `true` = template applied |
| Processing | checkbox | Concurrency lock — `true` while a Lambda is extracting tasks from this page |
| Task - Relation | relation | One-way relation to Team Task Tracker. The pipeline patches this property after creating tasks for the meeting. |
| Created | created_time | Auto-set by Notion |
| Created by | created_by | Auto-set by Notion |

**Fundraising → Affinity outcome**: not stored on the meeting page. Each
fundraising-branch run emits a single grep-friendly CloudWatch log line:

```
fundraising outcome: page=<16-char-prefix> db_owner=<member> status=<enum> detail=<text>
```

Status values: `Posted`, `Skipped: no external attendees`, `Skipped: no LP match`,
`Failed: API error`. Failures log at ERROR; everything else at INFO. To audit
the queue: CloudWatch Logs Insights query
`filter @message like /fundraising outcome:/`.

**Query patterns** (from `single_source.py`):

Polling mode (unprocessed meetings with buffer):
```
filter: Processed = false AND created_time < (now - buffer_hours)
sort: last_edited_time descending
```

Webhook/cron mode (ready for extraction):
```
filter: Processed = false AND last_edited_time < (now - idle_minutes)
sort: last_edited_time descending
```

Template injection:
```
filter: Template Injected = false AND created_time > (now - 12 hours)
sort: created_time descending
```

## Team Task Tracker DB

| Property | Type | Values / Notes |
|----------|------|----------------|
| Task | title | Task title (max 2000 chars in code) |
| Status | status | Not Started, In Progress, Done |
| Assignee (edit access) | people | Single Notion user |
| Governance: View Access | people | View-only access (not used by pipeline) |
| Due Date | date | ISO date (YYYY-MM-DD) |
| Priority | select | High, Medium, Low, **[DETAILS INSIDE]** — architecture/hierarchy rows carry `[DETAILS INSIDE]`; extracted tasks use `High`/`Medium`/`Low` or no value |
| Category | select | Read dynamically from DB schema at runtime (see below) |
| Parent item | relation | Self-relation to Team Task Tracker (hierarchy parent) |
| Sub-item | relation | Self-relation (hierarchy children, inverse of Parent item) |
| Deal Relation (only for deal tasks) | relation | Relation to Deal Workplans DB (set by pipeline when AI identifies a deal-related task) |

**Meeting linkage:** Meeting → task is one-way only, owned by each per-member Meeting Notes DB via its `Task - Relation`. The previous reverse `Meeting - Relation` on the tracker was deleted when the meeting database split into one DB per member.

**Category options** (7 values, derived by the classifier from each task's chosen parent Tier-0 ancestor — the writer copies whatever string the loader attached to that node in `HierarchyLoader._get_category`):
- Sourcing / Investing / Divesting
- Value Creation (Portfolio)
- Nzyme Growth
- Operations
- Investor Relations & Fundraising
- Recruiting & Talent Management
- Other

**Property name exact-match requirement:** Property names in code must match Notion exactly, including parentheticals. For example, `"Assignee (edit access)"` — not `"Assignee"`.

## Hierarchy Structure

The Team Task Tracker uses the "Parent item" self-relation to form a tree:

```
Category (root level, e.g. "Sourcing / Investing / Divesting")
  └── Sub-category (e.g. "Investing")
        └── Group (e.g. "Active Dealflow")
              └── Entity (e.g. "Citadel" — only if it has children)
                    └── Task (leaf node — not loaded by hierarchy)
```

`HierarchyLoader` queries non-Done items where **`Priority = [DETAILS INSIDE]`**, builds the tree, and prunes to 4 levels (categories → sub-categories → entities → deals). At max depth, only organizational nodes (those with children) are kept. The resulting JSON is passed to the classifier so it can set `parent_task_id` on new tasks.

The marker is the single source of truth for "this row is part of the architecture, not a real task." It also cleanly splits the two consumers of tracker state: the **classifier (LLM)** sees architecture rows for parent assignment, and the **deduper** (`TeamTaskTrackerWriter._load_existing_titles` + `SemanticDedup`) sees only task rows (`Priority != [DETAILS INSIDE]`) for skip-or-create. Two scopes, no overlap. To add a new architecture row in Notion, set `Priority = [DETAILS INSIDE]` — no code change needed.

## Meeting Notes & Task Tracker Hierarchy DB

Source of truth for the firm's work-block taxonomy as edited by humans, but the **authoritative canonical** lives in Supabase (`public.hierarchy_rows` — written daily by `canonical_mirror_sync`). Lives under **Nzyme Settings**. Tier 0 rows propagate to the `Macro Work Block` select on every member Meeting Notes DB via `macro_block_sync`, which reads the Supabase canonical and pairs each `(hierarchy_page_id, member_db_id)` to a Notion `Macro Work Block` option id via `public.work_area_option_mappings` — so renames are id-preserving PATCHes (no orphaned options). Notion's API forbids commas in select option names, so the option name written into each member DB goes through `_sanitize_option_name` (commas → spaces, whitespace collapsed); the Hierarchy DB and Supabase canonical keep commas verbatim.

| Property | Type | Values / Notes |
|----------|------|----------------|
| Name | title | Display name (e.g. `Investor Relations & Fundraising`, `Marketing`, `Citadel`) |
| Tier | select | `0. Macro Work Block` / `1. Project` / `2. Workstream` |
| Active | checkbox | `true` = synced live; `false` = downstream targets renamed to `(archived) X` (never deleted) |
| Parent item | relation | Self-relation to Hierarchy DB (parent row) |
| Sub-item | relation | Self-relation to Hierarchy DB (children, inverse of Parent item) |
| Tracker Node | relation | Human-readable cache of the matching `[DETAILS INSIDE]` row in the Team Task Tracker. **Authoritative mapping lives in Supabase `public.hierarchy_rows.tracker_node_page_id`** — `tracker_applier_sync` writes there first and updates this Notion column on a best-effort basis. Don't clear by hand; the next sync run heals divergence. |
| Notes | text | One-sentence description (read by the classifier LLM to disambiguate similarly-named nodes) |
| Deal ID | text | **Auto-managed by `deal_hierarchy_sync`** — the Supabase `ReportingNz_deals.id` UUID for rows it creates from the deal pipeline (dealflow opportunities + PortCos). A row carrying a `Deal ID` is owned by the sync (never hand-edit/delete it; it's archived via `Active=false` when the deal leaves the tracked stages). Rows with an empty `Deal ID` are hand-made and never touched by the deal sync. Hidden in the default view. |

The canonical mirror of this DB lives in Supabase (`public.hierarchy_rows` in the Neo project). The daily `canonical_mirror_sync` writes Notion's current state to that table and surfaces structured `created` / `edited` / `deleted` / `reactivated` events in `public.hierarchy_sync_runs` for queryable history. The companion `tracker_applier_sync` then reads that canonical to keep the Team Task Tracker `[DETAILS INSIDE]` rows aligned (rename / soft-archive / create + back-fill — never delete), and `macro_block_sync` reads it to keep each member DB's `Macro Work Block` select aligned (id-preserving renames via `public.work_area_option_mappings`).

## Detail Options Settings DB

Source of truth for the `Detail` multi-select on every member Meeting Notes DB. Lives in the **Nzyme Settings** page alongside the Hierarchy DB. Edited by operators in Notion; canonical mirrored to Supabase by `detail_canonical_mirror_sync` and propagated by `detail_applier_sync` (id-preserving renames via `public.detail_option_mappings`). Color is canonical-driven — recoloring a Settings row recolors that option on every member DB on the next tick.

| Property | Type | Values / Notes |
|----------|------|----------------|
| Name | title | Display name (e.g. `Legal DD`, `Investor Relations`, `AI & Tech`) |
| Color | select | One of Notion's 10 standard colors (`default`, `gray`, `brown`, `orange`, `yellow`, `green`, `blue`, `purple`, `pink`, `red`). Convention: matches the parent Macro Work Block's color. |
| Parent Work area | relation | Hierarchy DB Tier 0 row this Detail belongs to. Documents intent; not yet used by the applier to drive color (operator picks color explicitly in the Color column). |
| Active | checkbox | `true` = synced live; `false` = member-DB option renamed to `(archived) <sanitized name>` (never deleted). |

Like the Hierarchy DB, `Detail Options` rows are mirrored to Supabase by `detail_canonical_mirror_sync` (table `public.detail_rows`, audit trail `public.detail_sync_runs`). The Notion side keeps commas verbatim; the Notion-option-side comma stripping happens in the applier via the same `_sanitize_option_name` rule as Macro Work Block.

## External Org (member-DB select, deal-driven)

`External Org` is a `select` property on every member Meeting Notes DB whose option list is fanned out from the deal pipeline (`public."ReportingNz_deals"`, Affinity → Supabase) by `external_org_applier_sync` — the same applier pattern as `Macro Work Block` / `Detail`:

- **Tracked stages → options.** Deals in `Portfolio` + `DD phase` + `Working on a deal (significant effort)` + `Under analysis (team assigned, moderate effort)` become options.
- **Color is canonical-driven:** `Portfolio` → orange, the three dealflow stages → blue.
- **Order:** stage priority (Portfolio → DD phase → Working → Under analysis) then alphabetical by name; `(archived) X` options sink to the bottom.
- **Stage-out → soft-archive** (`X` → `(archived) X` via the rename saga); re-entry un-archives in place. Never deletes an option a meeting has been tagged on.
- Mapping table `public.external_org_option_mappings` pins `(deal_id, member_db_id) → option_id` so renames are id-preserving. Don't edit the option list by hand — manual edits are overwritten on the next tick.

The same tracked deals are written into the Hierarchy DB as rows by `deal_hierarchy_sync` (see the Hierarchy DB `Deal ID` property above), so each opportunity/PortCo is also a fileable `[DETAILS INSIDE]` tracker node.

> **Deprecated:** the old single `🏢 External Orgs` Settings DB (id `36d83e67e2e7807792b4f1f381f12800`) + its `external_org_db_sync` sub-sync were retired 2026-06-02 when the member-DB fan-out + Hierarchy writes were rebuilt. `EXTERNAL_ORGS_DB_ID` is now dead config; the Settings DB page can be archived in Notion (nothing reads it).

## Org Chart DB

Source of truth for which Meeting Notes DBs the pipeline polls and how each
member's pages are processed. One row per active team member.

| Property | Type | Values / Notes |
|----------|------|----------------|
| Name | title | Member full name |
| Email | email | Used to match Google Calendar attendees → Org Chart names |
| Active | checkbox | `true` = polled by registry; `false` removes the member from the run without deleting the row |
| Meeting Notes DB | url | Member's personal Meeting Notes DB. Required — rows without a URL are skipped |
| **Auto-extract Tasks** | checkbox | **`true`** = run the full transcript pipeline (correct → extract → classify). **`false`** = literal-notes path: a single light LLM call (gpt-5-mini) on the page's notes content, using the Notion-hosted prompt at `LITERAL_NOTES_EXTRACTION_PROMPT_PAGE_ID` that instructs the model to keep titles verbatim and split assignees into internal/external. The same classifier as the transcript path resolves category/parent/`assignee_id` afterwards. Defaults to `true` when the column is missing or unset. |
| Default Mirror Visibility | select | `Private` / `Shared` (optional). The member's Meeting Mirrors default for meetings left untagged by the per-meeting `Confidential` column. Blank/unset ⇒ `Shared` (mirror as before); `Private` holds back blank meetings. Read into `MeetingDB.default_mirror_visibility`. See [docs/meeting-mirrors.md](meeting-mirrors.md) |
| Role | rich_text | Free-text role used by the transcript pipeline for speaker identification |
| Department | select | Used by the transcript pipeline |
| Seniority | select | Used by the transcript pipeline |
| Typical Topics | multi_select | Used by the transcript pipeline |

**Joiner / leaver workflow:** create the row in Notion, paste the Meeting
Notes DB URL into `Meeting Notes DB`, set `Active = true`. To remove a
member without losing history, flip `Active = false`. No code change or
redeploy needed in either direction.

**Auto-extract Tasks override (CLI):** `python -m src.main --sync
--auto-extract-tasks` and `--no-auto-extract-tasks` force the path for
every page in the run, ignoring the per-row Org Chart flag. Useful for
debugging without touching Notion.

## Topic Mirror Routes DB

`TOPIC_MIRROR_ROUTES_DB_ID` (`daa0ef7ac48c40bea82163ebe84ade6b`). Routes are
config-as-data — each active row maps a meeting tag to a target DB. The
pipeline reloads them once per cron tick.

| Property | Type | Notes |
|----------|------|-------|
| Route | title | Human-readable label (e.g. `Detail = AI & Tech`). Only used in logs. |
| Match Property | select | One of: `Meeting type`, `Detail`, `External Org`. Rows with any other value are skipped at load. |
| Match Value | rich_text | Exact tag value to match (e.g. `AI & Tech`). Matching is case-sensitive — keep this in sync with the Meeting Notes DB select/multi-select option name. |
| Target DB | url | Notion DB URL. The pipeline extracts the 32-char hex id from the last URL segment, stripping any `?v=…` view suffix. |
| Active | checkbox | `false` (or unset) hides the route from the pipeline without losing the row. |
| Notes | rich_text | Optional free-form description. |

**Add a new topic mirror in two steps:**
1. Create the destination DB under the Meeting Mirrors parent page with the agreed property convention (Meeting, Date, Meeting type, Detail, External Org, AI Summary, Tasks, Files & media, Contributors, Primary Source URL). Anything missing on the destination is silently dropped at clone time.
2. Add a row to Topic Mirror Routes: pick `Match Property`, type the `Match Value`, paste the new DB's URL, check `Active`.

## Meeting Mirror DBs

Each topic mirror DB shares the same property convention. Pipeline-control
columns (`Processed`, `Processing`, `Template Injected`, `Task - Relation`)
intentionally don't exist here — they're silently dropped by Notion at clone
time because they're absent from the destination schema.

| Property | Type | Notes |
|----------|------|-------|
| Meeting | title | Re-passed at clone time by the writer (the `template_id` mechanism does NOT carry over the source's DB property values — only the body). |
| Date | date | Re-passed at clone time (template_id otherwise resets it). |
| Meeting type | select | Explicitly copied from the source by the writer. Define the same select options as the source Meeting Notes DB to keep names valid. |
| Detail | multi_select | Explicitly copied. Same options as the source. |
| External Org | select | Explicitly copied. Same options as the source. |
| AI Summary | rich_text | Explicitly copied at clone time; Notion AI may later regenerate it from the cloned `meeting_notes` block. |
| Files & media | files | Carried over via the body clone. |
| Owner | people | Multi-person. First contributor is the meeting page's `created_by` user. Subsequent contributors are appended by the merge path; dedup key is the Notion user UUID, not display name. |
| Governance: Edit & View Access | people | Explicitly copied from the source's `Governance: Edit & View Access` people property so the mirror inherits the same access list. |
| Primary Source URL | url | Backlink to the source page the mirror was cloned from. |

`Tasks` (rich_text) is intentionally not part of this convention. The action
items already live in the Team Task Tracker, linked from the source page
via `Task - Relation`. Reintroducing it on a topic mirror DB is harmless —
Notion would carry through the AI-populated value — but the writer no
longer re-passes it explicitly.

**Notes-merge layout** (Option B). Inside the cloned `meeting_notes` block's
`notes_block_id`:
```
## Action Items   (from template; unchanged)
[the first contributor's action items, if any]
## Notes          (from template; unchanged)
[the first contributor's notes, unlabeled]
### <Contributor 2>'s Notes   ← appended when contributor 2's page is processed
[contributor 2's notes content]
### <Contributor 3>'s Notes   ← appended when contributor 3's page is processed
[contributor 3's notes content]
```

The first contributor's notes stay unlabeled because Notion's
`blocks.children.append` has no atomic prepend — retroactive labeling
would require delete+rebuild on AI-cloned content.

## Deal Workplans DB (Investment Team)

Used when `DEAL_WORKPLANS_DB_ID` is set. Contains one page per active deal.

| Property | Type | Notes |
|----------|------|-------|
| Name | title | Deal name (e.g., "Citadel") |
| 🖇️ Team Task Tracker | relation | Links to the deal's entry in Team Task Tracker |

Each deal page contains inline databases discovered by title pattern:
- **`{Deal} Workplan`** — workstreams with Status, Type (multi_select), Adviser (multi_select), Owner, Start/End dates
- **`{Deal} Action Items`** — granular tasks with Assigned To, Deadline, Status, Workstream (relation)

The `DealContextLoader` queries this DB, discovers inline databases per deal, and loads active workstreams for AI prompt enrichment.

## Playbook Page

The playbook is a regular Notion page containing natural-language rules for task extraction. It is:
- Fetched once per sync cycle by `PlaybookLoader`
- Converted to plain text via `blocks_to_text`
- Injected into the system prompt as `{playbook}`

To update extraction behavior, edit the playbook page in Notion. No code changes needed.

## MCP Tools Reference

These are the Notion MCP tools available for interacting with the databases directly (e.g., for integration testing or manual inspection).

| Tool | Use Case |
|------|----------|
| `mcp__claude_ai_Notion__notion-search` | Find pages/databases by title keyword |
| `mcp__claude_ai_Notion__notion-fetch` | Read a specific page's content, properties, and blocks |
| `mcp__claude_ai_Notion__notion-query-data-sources` | Query a database with filters (equivalent to DB query) |
| `mcp__claude_ai_Notion__notion-create-pages` | Create new pages in a database with specified properties |
| `mcp__claude_ai_Notion__notion-update-page` | Update page properties (e.g., set Processed checkbox, archive) |
| `mcp__claude_ai_Notion__notion-get-comments` | Read comments on a page |
| `mcp__claude_ai_Notion__notion-create-comment` | Add a comment to a page |

### Common MCP Patterns

**Query unprocessed meetings:**
Use `notion-query-data-sources` with database ID `b07976472620499fa4b89be7b03c07d0`.
Filter: property "Processed" checkbox equals `false`.

**Read meeting content:**
Use `notion-fetch` with the page URL or page ID.

**Create a task in Team Task Tracker:**
Use `notion-create-pages` with database ID `32f83e67e2e7803f9662f43125603afa`.
Include properties: Task (title), Status, Assignee (edit access), Due Date, Priority, Category, Parent item.

**Read playbook:**
Use `notion-fetch` with page ID `33083e67-e2e7-8108-bb08-eaeba8b65678`.

**Archive a page (for cleanup):**
Use `notion-update-page` on the page and set `archived: true`.
