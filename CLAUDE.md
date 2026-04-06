# CLAUDE.md

## Project

Nzyme is an AI-driven sync engine that extracts action items from Notion meeting notes and writes them to a Team Task Tracker database. It uses OpenAI GPT-4.1 with function calling, guided by a natural-language playbook stored as a Notion page.

Built for Kibo Ventures (PE/VC fund, ~10-20 people). Meeting notes may be in English, Spanish, or mixed.

## Setup

```bash
# Shared venv lives one directory up (../venv/)
cd ../  # from project root
python -m venv venv
source venv/Scripts/activate  # Windows/Git Bash
pip install -e "nzyme-task-tracker/.[dev]"
```

Environment: copy `.env.example` to `.env` and fill in credentials. Never commit `.env`.

## Commands

```bash
# Run tests (see docs/testing.md for details)
../venv/Scripts/python -m pytest tests/ -v

# Watch mode — loop continuously (Ctrl+C to stop)
python -m src.main --watch
python -m src.main --watch --dry-run --verbose

# One-shot: run both template injection + AI extraction (default)
python -m src.main
python -m src.main --dry-run --verbose

# One-shot: run only template injection
python -m src.main --inject-templates

# One-shot: run only AI extraction pipeline
python -m src.main --sync

# Lint
../venv/Scripts/python -m ruff check src/ tests/
```

## Architecture

```
main.py → pipeline.run_sync():
  0. template_injector → inject "Your own notes" section into new meeting pages
  then for each unprocessed meeting:
    1. playbook_loader  → fetch playbook rules from Notion page
    2. hierarchy_loader → snapshot Team Task Tracker parent-child tree
    3. single_source    → fetch meeting content as plain text (filtered by INCLUDE_AI_NOTES)
  4. ai_extractor     → call OpenAI with playbook + hierarchy + content
  5. team_writer      → create task pages in Team Task Tracker
  6. single_source    → mark meeting page as Processed
```

Key design: playbook, hierarchy, and category options are all read dynamically from Notion at runtime. Schema changes in Notion require no code changes.

## Key Files

| File | Responsibility |
|------|---------------|
| `src/pipeline.py` | Orchestrates the sync cycle |
| `src/ai_extractor.py` | OpenAI prompt + function calling, parses tool_calls |
| `src/playbook_loader.py` | Fetches playbook Notion page, converts to text, caches per run |
| `src/hierarchy_loader.py` | Queries tracker DB, builds parent-child tree |
| `src/template_injector.py` | Injects "Your own notes" section into new meeting pages |
| `src/sources/single_source.py` | Polls Meeting Notes DB with buffer delay, fetches page content (AI block filtering) |
| `src/tracker/team_writer.py` | Maps task dicts to Notion properties, creates pages |
| `src/utils/blocks_to_text.py` | Converts Notion block arrays to plain text |
| `src/config.py` | Pydantic config from env vars |
| `src/notion_client_wrapper.py` | Rate-limited (3 req/s), auto-retry Notion API client |

## Conventions

- **Python 3.11+** with type hints (`from __future__ import annotations`)
- **Pydantic** for config validation
- **pytest** for testing, all unit tests use mocked Notion/OpenAI clients
- Tests live in `tests/` mirroring src structure (e.g., `test_pipeline.py` tests `src/pipeline.py`)
- `conftest.py` provides a `mock_client` fixture for NotionClientWrapper
- No multi-source/registry mode — single Meeting Notes DB only

## Documentation

When working on specific areas, read the relevant doc:

- **Testing / QA**: Read `docs/testing.md` — unit test patterns, integration testing workflow with Notion MCP
- **Notion integration**: Read `docs/notion-schema.md` — DB schemas, property mappings, IDs, MCP tool reference
- **Architecture deep dive**: Read `docs/architecture.md` — pipeline details, component responsibilities, error handling, prompt construction

**Keep docs in sync:** After any major change (new module, changed pipeline flow, new/renamed Notion properties, modified error handling, new test patterns), update the relevant doc file above. Review which docs are affected before considering the work complete.
