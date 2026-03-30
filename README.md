# Nzyme Macro Task Tracker

## Project Purpose

Nzyme Macro Task Tracker is a Python-based sync engine that extracts action items from Notion AI Meeting Notes pages and writes them to a centralized Macro Task Tracker database. It's designed for a PE/VC fund team of 10-20 people who use Notion's AI Meeting Notes feature. The engine polls meeting notes databases on a schedule, extracts action items (to_do blocks, @mentions, deadlines), deduplicates them, and creates task entries in a centralized tracker.

## Shared `.venv` Setup

> **Note:** The virtual environment lives at the parent directory level (`../venv/`) and is shared across all projects in the workspace. Do not create a project-level venv. To install this project's dependencies, activate the root venv and run `pip install -e ".[dev]"` from inside the `nzyme-task-tracker/` folder.

## Architecture Overview

The engine supports two modes of operation, controlled by the `SYNC_MODE` environment variable (`single` or `multi`).

### Mode A -- Single Source

All meeting notes live in one shared Notion database. The engine queries that single DB, extracts action items from each page's child blocks, and writes them to the Macro Task Tracker.

### Mode B -- Multi-Source with Registry

Each team member has their own Notion AI Meeting Notes database (created automatically by Notion Calendar). A separate "source registry" Notion database lists all source database IDs along with per-person schema mappings (e.g., which property holds the date, attendees, etc.). The engine iterates over all active sources in the registry, uses the schema mapping to normalize properties, and writes extracted tasks to the same centralized tracker.

```
Mode A:

  [Shared Meeting Notes DB] --> [Sync Engine] --> [Macro Task Tracker DB]
                                     |
                                     '----------> [Team Task Tracker DB]

Mode B:

  [Source Registry DB]
       |
       |-- [Person 1's Meeting Notes DB] --+
       |-- [Person 2's Meeting Notes DB] --+--> [Sync Engine] --> [Macro Task Tracker DB]
       '-- [Person N's Meeting Notes DB] --+         |
                                                     '----------> [Team Task Tracker DB]
```

Mode selection is via the env var `SYNC_MODE=single|multi`.

### Deduplication & Processed Pages

The engine uses a two-layer dedup strategy:

1. **Primary:** The `Processed` checkbox on Meeting Notes pages. Only pages with `Processed = false` are fetched. After extraction, the engine sets `Processed = true`.
2. **Safety net:** Before writing, the engine queries the Macro Task Tracker for existing tasks linked to the same meeting page via the `Source meeting` relation. Duplicate task titles are filtered out.

### Team Task Tracker Integration

The engine writes action items to both the Macro Task Tracker and the Team Task Tracker. In the TTT, items are placed within an existing hierarchy:

- **Entity matching:** The engine matches action items to existing entities (e.g., company names like "Civislend", "Azenea") using substring matching on the meeting title and item text.
- **Category fallback:** If no entity matches, the item is placed directly under the relevant top-level category (Dealflow, Portfolio, Internal, etc.), inferred from the source meeting's `Meeting type` property.
- **Meeting type → Category mapping:** Deal review → Dealflow, Portfolio review → Portfolio, Standup/Team sync/1:1 → Internal, External/Other → Other.

## Setup Instructions

1. **Clone the repo**

   ```bash
   git clone <repo-url>
   cd nzyme-task-tracker
   ```

2. **Create `.env` from `.env.example`**

   ```bash
   cp .env.example .env
   # Fill in your Notion API token and database IDs
   ```

3. **Activate the shared venv**

   ```bash
   # Linux / macOS
   source ../venv/bin/activate

   # Windows (PowerShell)
   ..\venv\Scripts\Activate.ps1

   # Windows (cmd)
   ..\venv\Scripts\activate.bat
   ```

4. **Install dependencies**

   ```bash
   pip install -e ".[dev]"
   ```

5. **Run the engine**

   ```bash
   python -m src.main
   ```

6. **Run once (script)**

   ```bash
   bash scripts/run_once.sh
   ```

## Architecture Decision Log

### ADR-001: Supporting both single-source and multi-source architectures during evaluation phase

**Context:**
The team uses Notion Calendar's built-in AI Meeting Notes feature. By default, each person's meeting notes are created in their own private database, but Notion Calendar can be reconfigured to write notes to a shared database instead.

**Tradeoffs:**

- **Mode A (single source)** is simpler to implement and operate -- one database to query, one schema to handle. However, it requires every team member to change two settings in their Notion Calendar configuration (default notes database and sharing permissions). This is fragile: the setup breaks every time someone joins or leaves the team, and it depends on every person remembering to configure their calendar correctly.

- **Mode B (multi-source with registry)** adapts to each person's existing setup. A registry database maps each person's meeting notes DB ID to a schema mapping (property name for date, attendees, etc.), so the engine can normalize data across databases with different property names. The downside is added complexity: the engine must iterate over multiple sources, handle per-source schema differences, and deal with partial failures when individual sources become inaccessible.

**Decision:**
Support both modes during the evaluation phase. The `SYNC_MODE` env var (`single` or `multi`) toggles between them. Once the team decides which workflow they prefer, the unused mode can be removed to reduce maintenance surface.

## Known Edge Cases

1. **Schema variations across source databases** -- Different people's Notion Calendar setups may use different property names for date, attendees, or other fields. Mode B's schema mapping handles this, but unmapped properties will be silently skipped.

2. **Pages with no action items** -- The engine must not re-scan pages indefinitely. The `Processed` checkbox on Meeting Notes pages prevents re-scanning: pages are marked as processed even if they contain no action items.

3. **Cross-source duplicate detection** -- The same meeting attended by multiple people generates separate meeting notes pages in each person's database. Without deduplication, the tracker would contain duplicate action items. Dedup must key on a combination of assignee, task text, and source meeting date.

4. **Edited or deleted action items after initial sync** -- If someone edits or removes an action item from the meeting notes after the engine has already synced it, the tracker entry becomes stale. This is a one-way sync by design; bidirectional sync would add significant complexity.

5. **Notion API rate limiting** -- Notion enforces a rate limit of approximately 3 requests per second. With 10-20 source databases (Mode B), each requiring multiple API calls (query DB, fetch child blocks per page), the engine must implement backoff and request pacing.

6. **Permission and access failures per source** -- The Notion integration token needs explicit read access to each person's meeting notes database. If a person hasn't shared their DB with the integration, the engine must log the failure and continue with the remaining sources.

7. **People leaving or databases being archived** -- When a team member leaves or their meeting notes database is archived/deleted, the engine must handle `404` or `403` responses gracefully and mark the source as inactive in the registry rather than crashing.

8. **AI-generated action items with no clear assignee** -- Notion's AI sometimes generates action items without a clear @mention. The fallback strategy is to assign the task to the page creator or the meeting organizer.

9. **Natural language date parsing in Spanish and English** -- Action items may contain deadlines expressed as natural language (e.g., "end of next week", "antes del viernes"). The parser must handle both languages since the team operates bilingually.

10. **Nested blocks and toggle content** -- Action items can be buried inside toggle blocks, callout blocks, or nested bullet lists. Extracting them requires recursive fetching of child blocks, which multiplies API calls.

11. **Multi-language content** -- Keyword-based extraction (e.g., looking for "action item", "TODO", "tarea") needs bilingual patterns to avoid missing tasks written in Spanish or English.

12. **The "meeting about meetings" meta-task problem** -- When the team meets to discuss the task tracker itself, the system will faithfully extract action items about its own development and sync them to the tracker. This is technically correct behavior but produces an entertaining self-referential loop.
