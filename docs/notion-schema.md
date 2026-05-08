# Notion Schema Reference

## Database IDs

| Resource | ID | Notes |
|----------|----|-------|
| Meeting Notes DB | `b07976472620499fa4b89be7b03c07d0` | Source of meeting pages |
| Team Task Tracker DB | `32f83e67e2e7803f9662f43125603afa` | Destination for extracted tasks |
| Playbook page | `33083e67-e2e7-8108-bb08-eaeba8b65678` | "Nzyme Playbook - Task Extraction Rules" under Nzyme Home |
| Meeting template page | `32f83e67-e2e7-8086-9863-e276b70cc5a2` | Default "New page" template in Meeting Notes DB — edit in Notion to change injected content |
| Literal-notes extraction prompt | `35183e67-e2e7-813d-ad55-f011624d2e29` | "📝 Literal Notes Extraction Prompt" under Nzyme Prompts. Used when an Org Chart row has `Auto-extract Tasks = false`. |

Workspace: `kiboventures.notion.so`

## Meeting Notes DB

| Property | Type | Values / Notes |
|----------|------|----------------|
| Meeting | title | Meeting title text |
| Date | date | ISO date; informational only — **not used for processing logic** (created_time and last_edited_time are used instead) |
| Attendees | people | List of Notion users (returns id + name) |
| Meeting type | select | Standup, 1:1, Deal review, Portfolio review, Team sync, External, Other, **Fundraising** (Fundraising triggers the Affinity LP Funnel branch when `FUNDRAISING_BRANCH_ENABLED=true`) |
| LP Emails | rich_text | Manual, comma- or semicolon-separated list of external attendee emails, used by the fundraising branch to match an LP in Affinity when GCal is unavailable. Only read for `Meeting type = Fundraising`. |
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

**Category options** (7 values, read dynamically via `_load_categories()`):
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
