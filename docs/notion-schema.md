# Notion Schema Reference

## Database IDs

| Resource | ID | Notes |
|----------|----|-------|
| Meeting Notes DB | `b07976472620499fa4b89be7b03c07d0` | Source of meeting pages |
| Team Task Tracker DB | `32f83e67e2e7803f9662f43125603afa` | Destination for extracted tasks |
| Playbook page | `33083e67-e2e7-8108-bb08-eaeba8b65678` | "Nzyme Playbook - Task Extraction Rules" under Nzyme Home |
| Meeting template page | `32f83e67-e2e7-8086-9863-e276b70cc5a2` | Default "New page" template in Meeting Notes DB — edit in Notion to change injected content |

Workspace: `kiboventures.notion.so`

## Meeting Notes DB

| Property | Type | Values / Notes |
|----------|------|----------------|
| Meeting | title | Meeting title text |
| Date | date | ISO date; informational only — **not used for processing logic** (created_time and last_edited_time are used instead) |
| Attendees | people | List of Notion users (returns id + name) |
| Meeting type | select | Standup, 1:1, Deal review, Portfolio review, Team sync, External, Other (as of 2026-03-27; editable in Notion) |
| Processed | checkbox | `false` = unprocessed, `true` = AI extraction completed |
| Template Injected | checkbox | `false` = template not yet injected, `true` = template applied |
| Processing | checkbox | Concurrency lock — `true` while a Lambda is extracting tasks from this page |
| Task - Relation | relation | Relation to Team Task Tracker (auto-populated when tasks set "Meeting - Relation") |
| Created | created_time | Auto-set by Notion |
| Created by | created_by | Auto-set by Notion |

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
| Priority | select | High, Medium, Low |
| Category | select | Read dynamically from DB schema at runtime (see below) |
| Parent item | relation | Self-relation to Team Task Tracker (hierarchy parent) |
| Sub-item | relation | Self-relation (hierarchy children, inverse of Parent item) |
| Deal Relation (only for deal tasks) | relation | Relation to Deal Workplans DB (set by pipeline when AI identifies a deal-related task) |
| Meeting - Relation | relation | Relation to Meeting Notes DB (set automatically by pipeline on task creation) |

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

`HierarchyLoader` queries all non-Done items, builds the tree, and prunes to 4 levels (categories → sub-categories → entities → deals). At max depth, only organizational nodes (those with children) are kept — leaf tasks are filtered out. The resulting JSON is passed to the AI extractor so it can set `parent_task_id` on new tasks.

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
