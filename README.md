# Nzyme Task Tracker

AI-driven sync engine that extracts action items from Notion meeting notes and writes them to a Team Task Tracker database. Built for [Kibo Ventures](https://kiboventures.com) (PE/VC fund, ~10-20 people).

Uses OpenAI-compatible LLMs with function calling, guided by a natural-language **playbook** stored as a Notion page. Meeting notes may be in English, Spanish, or mixed.

## How It Works

```
main.py -> pipeline.run_sync() -> for each unprocessed meeting:
  1. playbook_loader  -> fetch extraction rules from Notion page
  2. hierarchy_loader -> snapshot Team Task Tracker parent-child tree
  3. single_source    -> fetch meeting content as plain text
  4. ai_extractor     -> call LLM with playbook + hierarchy + content
  5. team_writer      -> create task pages in Team Task Tracker
  6. single_source    -> mark meeting page as Processed
```

**Key design:** The playbook, hierarchy, and category options are all read dynamically from Notion at runtime. To change how tasks are extracted, edit the playbook page in Notion -- no code changes needed.

**Deduplication:** Two layers prevent duplicate tasks:
- *Meeting-level:* Title + date fingerprinting skips meetings already processed (handles Notion's `(1)` suffixes on duplicates)
- *Task-level:* Existing task titles are cached on startup; new tasks with matching titles are skipped

**Meeting relations:** Each per-member Meeting Notes DB has a one-way `Task - Relation` pointing into the Team Task Tracker. After tasks are written, the pipeline patches the source meeting page's `Task - Relation` with the new task IDs. (No reverse property on the tracker side -- a single relation can't span N member DBs.)

## Setup

1. **Create the shared venv** (lives one directory up):

   ```bash
   cd ..
   python -m venv venv
   source venv/Scripts/activate  # Windows/Git Bash
   # or: source venv/bin/activate  (Linux/macOS)
   pip install -e "nzyme-task-tracker/.[dev]"
   ```

2. **Configure environment:**

   ```bash
   cp .env.example .env
   # Fill in credentials (see Configuration below)
   ```

3. **Run the engine:**

   ```bash
   python -m src.main                    # full sync
   python -m src.main --dry-run --verbose # preview without writing
   bash scripts/run_once.sh              # wrapper that loads .env
   ```

## Configuration

All configuration is via environment variables (`.env` file). Never commit `.env`.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `NOTION_API_TOKEN` | Yes | -- | Notion integration token |
| `OPENAI_API_KEY` | Yes | -- | OpenAI API key (or compatible provider) |
| `MEETING_NOTES_DB_ID` | Yes | -- | Meeting Notes database ID |
| `TEAM_TRACKER_DB_ID` | Yes | -- | Team Task Tracker database ID |
| `PLAYBOOK_PAGE_ID` | Yes | -- | Notion page ID containing extraction rules |
| `OPENAI_MODEL` | No | `gpt-5-mini` | Model name |
| `OPENAI_BASE_URL` | No | -- | Override for OpenAI-compatible endpoints (e.g., Gemini) |
| `BUFFER_HOURS` | No | `2` | Hours to wait after meeting date before processing |
| `LOG_LEVEL` | No | `INFO` | Logging level |
| `DRY_RUN` | No | `false` | Log extracted tasks without writing to Notion |
| `LOGFIRE_TOKEN` | No | -- | [Logfire](https://logfire.pydantic.dev) write token for OpenAI observability |

### Editing the Playbook

The playbook is a regular Notion page that defines extraction rules in natural language: what counts as an action item, how to assign priorities, how to resolve assignees, how to handle bilingual content, etc. Edit it anytime -- changes take effect on the next sync cycle.

## Architecture

| Component | File | Role |
|-----------|------|------|
| Pipeline | `src/pipeline.py` | Orchestrates the sync cycle |
| AI Extractor | `src/ai_extractor.py` | LLM prompt + function calling |
| Playbook Loader | `src/playbook_loader.py` | Fetches playbook from Notion, converts to text |
| Hierarchy Loader | `src/hierarchy_loader.py` | Builds parent-child tree from tracker DB |
| Single Source | `src/sources/single_source.py` | Polls Meeting Notes DB, fetches content |
| Team Writer | `src/tracker/team_writer.py` | Maps task dicts to Notion properties, creates pages |
| Notion Wrapper | `src/notion_client_wrapper.py` | Rate-limited (3 req/s), auto-retry, auto-pagination |

For detailed architecture documentation, see `docs/architecture.md`.

## Known Limitations

1. **One-way sync** -- If someone edits or removes an action item from meeting notes after the engine has synced it, the tracker entry becomes stale. Bidirectional sync is out of scope.

2. **AI model dependency** -- Extraction quality depends on the LLM. Model changes may require playbook adjustments.

3. **Playbook quality matters** -- The AI follows the playbook literally. Vague or contradictory rules lead to inconsistent extraction.

4. **Rate limiting** -- Notion enforces ~3 req/s. The wrapper handles this, but large backlogs (many unprocessed meetings) will take time.

5. **No scheduling built in** -- The pipeline runs on demand. For automated 15-30 min cycles, use an external scheduler (cron, Task Scheduler, cloud function).

6. **Bilingual date parsing** -- Relative dates like "para el viernes" or "end of next week" are resolved by the AI, not a deterministic parser. Accuracy depends on model quality.

## Development

See `CLAUDE.md` for development conventions, commands, and the documentation index (testing guide, Notion schema reference, architecture details).

```bash
# Run tests
../venv/Scripts/python -m pytest tests/ -v

# Lint
../venv/Scripts/python -m ruff check src/ tests/
```
