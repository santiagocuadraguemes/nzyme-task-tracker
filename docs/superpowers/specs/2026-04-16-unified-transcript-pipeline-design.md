# Unified Transcript Pipeline Design

## Context

The transcript pipeline (`src/transcript_pipeline/`) was built as an experiment to test whether extracting action items from meeting transcripts (voice recordings) produces better results than extracting from written meeting notes. The experiment succeeded — transcript-based extraction delivers higher quality tasks with better assignee identification and context.

The transcript pipeline is now feature-complete (fetch → correct → extract → classify → write) but lives as a standalone CLI module, separate from the production pipeline that runs on Lambda/cron. This design merges the transcript pipeline into the main orchestrator so all meetings are processed transcript-first by default, with the old notes-based extraction as a fallback for meetings without transcripts.

## Decision

**Approach A: Integrate into existing orchestrator.** Wire transcript modules into the existing `pipeline.py` rather than rewriting from scratch or adding abstraction layers. This preserves the working Lambda/cron infrastructure while making transcript extraction the default path.

## Unified Pipeline Flow

```
_load_sync_context():
  existing context (prompts, hierarchy, categories, users, deals, semantic dedup)
  + terminology dictionary (from TERMINOLOGY_DB_ID)
  + org chart rows (from ORG_CHART_DB_ID)
  + classifier prompt (from CLASSIFIER_PROMPT_PAGE_ID)

for each unprocessed meeting:
  1. Try fetch transcript (find meeting_notes block via Notion API v2026-03-11)

  2a. IF transcript exists (transcript path):
      - Resolve attendees: GCal → Notion meeting_notes → governance fallback
        (GCal skipped in Lambda — CLI only)
      - Build enriched attendee string (merge org chart roles)
      - Correct transcript (LLM call 1: TranscriptCorrector)
      - Extract tasks (LLM call 2: TaskExtractor)
      - Classify tasks (LLM call 3: TaskClassifier)

  2b. ELSE (notes fallback):
      - Fetch page content as text
      - AI extract + classify in one shot (existing AIExtractor)

  3. Semantic dedup (both paths produce list[dict] → check against existing embeddings)
  4. Assignee fallback (default to meeting creator when no assignee)
  5. Write tasks (TeamTaskTrackerWriter.write_batch())
  6. Mark page as Processed
```

Both extraction paths produce the same output shape: `list[dict]` with keys `title`, `assignee_id`, `category`, `parent_task_id`, `deal_page_id`, etc. Everything downstream is path-agnostic.

## Context Loading Changes

**Once per sync cycle** (in `_load_sync_context()`):
- Terminology dictionary — loaded from `TERMINOLOGY_DB_ID` (optional, degrades gracefully)
- Org chart rows — loaded from `ORG_CHART_DB_ID` (optional)
- Classifier prompt — loaded from `CLASSIFIER_PROMPT_PAGE_ID` (required for transcript path)
- All existing context stays: prompts, hierarchy, categories, users, deals, semantic dedup

**Per meeting** (in the processing loop):
- GCal attendees — searched by meeting title + date (CLI only, skipped in Lambda)
- Attendee resolution chain: GCal → Notion meeting_notes → governance property fallback
- Enriched attendee string — org chart roles merged inline with attendee names

## GCal in Lambda

GCal requires OAuth credentials (credentials.json → token.json) with a browser-based auth flow. This doesn't work in Lambda. For now:
- **CLI mode:** GCal works as today (authoritative attendee source when available)
- **Lambda mode:** Skip GCal, fall back to Notion meeting_notes attendees → governance property
- Future: store refresh token in AWS Secrets Manager for Lambda access

## Notion API Version Upgrade

Upgrade the entire project from `2025-09-03` to `2026-03-11`:
- `NotionClientWrapper` passes the new version to `notion-client` SDK
- v2026-03-11 is backwards-compatible (adds `meeting_notes` block support)
- `transcript_client.py`'s `create_transcript_client()` becomes unnecessary
- Risk mitigation: run full test suite + dry-run on known pages after upgrade

## File Changes

### Modified

| File | Change |
|------|--------|
| `src/pipeline.py` | Add `_process_via_transcript()` and `_process_via_notes()`. Add terminology/org chart/classifier prompt to `_load_sync_context()`. Add `_resolve_attendees()` for the GCal → Notion → governance chain. Refactor for readability. |
| `src/notion_client_wrapper.py` | Upgrade API version from `2025-09-03` to `2026-03-11`. |
| `src/webhook/lambda_handler.py` | Update `_init()` to use updated client (API version comes from wrapper now). |
| `src/transcript_pipeline/__main__.py` | Slim down `--write` mode to call `pipeline.py` functions instead of duplicating orchestration. Keep `--correct`, `--extract`, `--gcal` as standalone diagnostic tools. |
| `src/transcript_pipeline/transcript_client.py` | Remove entirely. Both `create_transcript_client()` and `create_main_client()` become unnecessary — the main `NotionClientWrapper` now uses v2026-03-11. CLI diagnostic modes receive the client as a parameter instead of creating their own. |
| `src/main.py` | Minor: ensure watch/one-shot modes use the new unified flow. |

### Unchanged (called from pipeline.py as-is)

| File | Why |
|------|-----|
| `src/transcript_pipeline/fetch_transcript.py` | Clean interface: returns (transcript, attendees, metadata, notes, governance). |
| `src/transcript_pipeline/context_loader.py` | Clean interface: returns terminology + org chart strings + rows. |
| `src/transcript_pipeline/transcript_corrector.py` | Self-contained `.correct()` method. |
| `src/transcript_pipeline/task_extractor.py` | Self-contained `.extract()` method. |
| `src/transcript_pipeline/task_classifier.py` | Self-contained `.classify()` method. |
| `src/transcript_pipeline/gcal_attendees.py` | Called conditionally (CLI only). |
| `src/ai_extractor.py` | Stays as the fallback path for meetings without transcripts. |
| `src/tracker/team_writer.py` | Already used by both paths. |
| `src/semantic_dedup.py` | Applied after both extraction paths. |

### Removed

- Duplicate `_format_deal_context()` in `transcript_pipeline/__main__.py` (use the one in `pipeline.py`)

## Pipeline.py Refactoring

To manage the growth from ~729 to ~900+ lines, split into focused functions:

```python
# Shared context (once per cycle)
_load_sync_context()

# Main loop
run_sync()                    # for each meeting: pick path → dedup → write
run_sync_for_page()           # single-page entry (Lambda + CLI)

# Extraction paths
_process_via_transcript()     # correct → extract → classify (3 LLM calls)
_process_via_notes()          # AIExtractor (1 LLM call, fallback)

# Attendee resolution
_resolve_attendees()          # GCal → Notion → governance chain

# Existing helpers (unchanged)
_apply_assignee_fallback()
_archive_done_tasks()
_substitute_placeholders()
```

## Template Injection

Template injection (`--inject-templates`) stays in the codebase but is togglable via `INJECT_TEMPLATE` env var (currently off in Lambda). No changes needed — it's independent of extraction method.

## CLI Interface

The transcript pipeline CLI stays available for manual runs and diagnostics:

```bash
# Full pipeline via CLI (transcript-first, notes fallback)
python -m src.transcript_pipeline <page_id> --write
python -m src.transcript_pipeline <page_id> --write --dry-run

# Diagnostic modes (unchanged, self-contained in __main__.py)
python -m src.transcript_pipeline <page_id> --correct    # just correction
python -m src.transcript_pipeline <page_id> --extract    # correction + extraction
python -m src.transcript_pipeline <page_id> --gcal       # GCal test

# Main pipeline (watch mode, one-shot)
python -m src.main --watch --dry-run --verbose
python -m src.main --sync
```

## Verification Plan

1. **Unit tests**: `pytest tests/ -v` — existing tests should pass (mocked clients)
2. **API version upgrade**: Dry-run on a known meeting page to verify Notion queries work with v2026-03-11
3. **Transcript path**: Process a meeting with a transcript via `--write --dry-run`
4. **Notes fallback**: Process a meeting without a transcript to verify the old AIExtractor path works
5. **Lambda deploy**: `quick-deploy.sh` + verify CloudWatch cron processes meetings
6. **Dedup**: Process same meeting twice, verify no duplicate tasks created
7. **Watch mode**: `python -m src.main --watch --dry-run --verbose` to verify continuous polling
