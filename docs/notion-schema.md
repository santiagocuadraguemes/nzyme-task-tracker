# Notion Schema Reference

## Database IDs

| Resource | ID | Notes |
|----------|----|-------|
| Meeting Notes DB | `b07976472620499fa4b89be7b03c07d0` | Source of meeting pages |
| Team Task Tracker DB | `32f83e67e2e7803f9662f43125603afa` | Destination for extracted tasks |
| Playbook page | `2a283e67-e2e7-806c-9768-f51c5146e60b` | In "PRUEBAS SANTI" section |

Workspace: `kiboventures.notion.so`

## Meeting Notes DB

| Property | Type | Values / Notes |
|----------|------|----------------|
| Meeting | title | Meeting title text |
| Date | date | ISO date; used with 2-hour buffer filter |
| Attendees | people | List of Notion users (returns id + name) |
| Meeting type | select | Standup, 1:1, Deal review, Portfolio review, Team sync, External, Other (as of 2026-03-27; editable in Notion) |
| Processed | checkbox | `false` = unprocessed, `true` = already synced |
| Task - Relation | relation | Relation to Team Task Tracker (auto-populated when tasks set "Meeting - Relation") |
| Created | created_time | Auto-set by Notion |
| Created by | created_by | Auto-set by Notion |

**Query pattern** for unprocessed meetings (from `single_source.py`):
```
filter: Processed = false AND Date < (now - buffer_hours)
sort: last_edited_time descending
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
Category (root level)
  └── Entity (company, project, fund)
        └── Task (leaf node — not loaded by hierarchy)
```

`HierarchyLoader` queries all non-Done items, builds the tree, and prunes to 2 levels. The resulting JSON is passed to the AI extractor so it can set `parent_task_id` on new tasks.

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
Use `notion-fetch` with page ID `2a283e67-e2e7-806c-9768-f51c5146e60b`.

**Archive a page (for cleanup):**
Use `notion-update-page` on the page and set `archived: true`.
