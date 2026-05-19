# Transcript pipeline

The transcript pipeline modules (`src/transcript_pipeline/`) handle transcript-based task extraction. These modules are integrated into the main pipeline orchestrator (`src/pipeline.py`) and are also available as a standalone CLI for diagnostics.

## Pipeline steps

Two modes, selected by `TRANSCRIPT_MERGED_EXTRACTION`:

**Merged (default, `TRANSCRIPT_MERGED_EXTRACTION=true`):**
1. **Fetch** raw transcript from Notion `meeting_notes` block
2. **Load context** from Terminology DB + Org Chart DB + Google Calendar attendees
3. **Merged correction + extraction** — single Gemini call reads the raw transcript and emits tasks directly. Domain corrections + speaker resolutions surface as scratch fields (`domain_corrections`, `speaker_resolutions`) — no full corrected transcript is produced.
4. **Classify** tasks via LLM (category, parent, assignee, deal mapping)
5. **Write** classified tasks to Team Task Tracker (via `TeamTaskTrackerWriter`)

**Legacy (`TRANSCRIPT_MERGED_EXTRACTION=false`, also the rollback path):**
1. **Fetch** raw transcript from Notion `meeting_notes` block
2. **Load context** from Terminology DB + Org Chart DB + Google Calendar attendees
3. **Correct** transcript via LLM (`TranscriptCorrector` — fix domain terms, speaker identification, emit full corrected transcript)
4. **Extract** action items via LLM on the corrected transcript (commitment-aware prompting)
5. **Classify** tasks via LLM (category, parent, assignee, deal mapping)
6. **Write** classified tasks to Team Task Tracker (via `TeamTaskTrackerWriter`)

In the merged path each task carries an extra `commitment_type` field (`hard|conditional|soft|group`) and a verbatim `context` quote from the raw transcript; a soft check warns when the quote is not found in the transcript (never drops the task — the check is whitespace-lossy).

`TRANSCRIPT_MERGED_EXTRACTION=true` (default) collapses correction + extraction into a single Gemini call (no separate corrected-transcript output — domain corrections and speaker resolutions are reported as scratch fields). Saves ~60-70% per meeting on the transcript path. Set `TRANSCRIPT_MERGED_EXTRACTION=false` to roll back to the legacy 2-call flow.

## Cost reductions on the merged extraction call

Two optimisations are wired into the merged path. Both fire automatically when the extraction model is `gemini-*`.

- **Deterministic transcript cleanup** (`src/transcript_pipeline/transcript_cleaner.py`) — regex-only artefact removal applied before the transcript reaches the LLM. Strips pure-timestamp lines, bare speaker labels, blank-line runs; collapses consecutive same-speaker utterances; drops adjacent identical sentences. Typically shaves 10–25% off transcript chars with no semantic loss. Logged per call as `Transcript cleaned: N → M chars (X% kept)`. No NLP / spaCy.
- **Native Gemini SDK with explicit context caching + `response_schema`** (`src/transcript_pipeline/task_extractor.py`) — when the extraction model starts with `gemini-`, the merged call uses `google-genai` directly instead of the OpenAI-compat shim. The stable system prefix (`MERGED_SYSTEM_PROMPT` + terminology + org chart) is uploaded once via `caches.create` and reused across meetings until its 1h TTL expires; cached input tokens are billed at ~25% of the standard rate. Output shape is enforced via the `MergedExtractionOutput` Pydantic schema in `src/transcript_pipeline/schemas.py` (portable across providers). Module-level `_GEMINI_CACHE_REGISTRY` keyed by SHA256 of the system prefix means changes to the org chart or terminology naturally produce a fresh cache. Non-Gemini extraction models keep the existing OpenAI-compat JSON-object path.

## Validating the merge (shadow diff)

Before flipping `TRANSCRIPT_MERGED_EXTRACTION=true` in production, run both paths on a fixed set of historical meetings and diff:

```powershell
# Both paths use Gemini (heavy) → GEMINI_API_KEY
../venv/Scripts/python scripts/shadow_diff_extraction.py `
    --pages <page_id_1> <page_id_2> <page_id_3> `
    --out shadow-diff.json
```

`shadow-diff.json` contains `legacy.tasks` vs `merged.tasks` per page. Passing thresholds (per design doc §6): ≥90% legacy tasks have a semantic match in merged, ≥90% `internal_assignees` agreement on matched pairs, ≥85% `priority`/`due_date` agreement, task-count delta within ±15%.

Each per-page entry also carries `input_tokens` / `cached_input_tokens` / `output_tokens` for both paths, plus `merged.raw_data` (the short-key payload incl. scratch fields). This is what the output-token estimator below feeds on.

## Measuring merged-call output tokens (cost-reduction harness)

The merged Gemini call is the most expensive LLM step. To cut its output tokens without losing quality, use this three-script harness:

| Script | Purpose | API cost |
|---|---|---|
| `scripts/shadow_diff_extraction.py` | Produces `baseline.json` — real merged outputs + token counts on a pinned corpus | ~1 real Gemini call per page |
| `scripts/estimate_output_savings.py` | Ranks candidate schema reductions offline by re-serialising the saved outputs and calling Gemini's free `count_tokens` API | **zero** (count_tokens is unmetered, including on free tier) |
| `scripts/compare_candidate.py` | Runs the merged extractor on the same corpus with ONE candidate schema swapped in | ~1 real Gemini call per page |
| `scripts/compare_runs.py` | Diffs two JSON dumps (token deltas + task-overlap + field agreement) | zero |
| `python -m src.transcript_pipeline <page_id> --extract --save-run [--run-note "..."]` | Same diagnostic CLI as before; with `--save-run`, also appends a history entry (tasks + tokens + raw_data + UTC timestamp + optional `--run-note`) to `runs/<page_id>.json`. Re-runs of the same page APPEND a new entry — the file becomes a per-meeting log of how output tokens evolved across prompt/schema tweaks. Default dir overridable with `--save-run-dir`. | ~1 real Gemini call (same as plain `--extract`) |

Free-tier-friendly flow:

```powershell
# Both real-call scripts use Gemini (heavy) → GEMINI_API_KEY.
# 1. One real baseline run (e.g. 10 pages = 10 calls).
../venv/Scripts/python scripts/shadow_diff_extraction.py --pages-file pages-corpus.txt --out baseline.json

# 2. Offline rank ALL candidate schemas — no API spend.
../venv/Scripts/python scripts/estimate_output_savings.py baseline.json

# 3. Validate the top candidate with real calls (~10 calls).
../venv/Scripts/python scripts/compare_candidate.py --candidate no-sr --pages-file pages-corpus.txt --out cand-no-sr.json
../venv/Scripts/python scripts/compare_runs.py baseline.json cand-no-sr.json
```

Candidate schemas live in `src/transcript_pipeline/schemas.py` (`CANDIDATE_SCHEMAS` dict). Current variants:

- `baseline` — current prod schema (control)
- `no-sr` — drops the per-task diagnostic `sr` (speaker_reasoning) field. Nothing downstream reads it.
- `no-scratch` — drops the per-call `domain_corrections` + `speaker_resolutions` scratch fields
- `combined` — drops `sr` + `a` (assignee display) + scratch fields; `a` is re-derived in Python from `ia`+`ea` after parse

To add a candidate: define a new Pydantic model in `schemas.py`, register it in `CANDIDATE_SCHEMAS`, and add a matching offline transform in `scripts/estimate_output_savings.py::CANDIDATES`. Production code paths never read these — `task_extractor.set_response_schema_override()` is what wires a variant in for measurement runs only.

**Important**: the offline estimator gives RELATIVE rankings (same content, different serialisation). For absolute confirmation, always run `compare_candidate.py` against a real corpus before merging a schema change to prod.

## CLI (diagnostics + manual runs)

```bash
# Full pipeline: correct → extract → classify → write
# Routes through pipeline.run_sync_for_page(); honors TRANSCRIPT_MERGED_EXTRACTION.
python -m src.transcript_pipeline <page_id> --write
python -m src.transcript_pipeline <page_id> --write --dry-run

# Force the legacy 2-call path for a single run (overrides env flag).
python -m src.transcript_pipeline <page_id> --write --legacy-2call

# Diagnostic: extract tasks via the merged single call (no write)
python -m src.transcript_pipeline <page_id> --extract

# Diagnostic + measurement: same as above, also append a history entry
# (tasks + token counts + raw payload + UTC timestamp + --run-note) to
# runs/<page_id>.json. Re-runs of the same meeting APPEND a new entry —
# the file is a chronological log of how output_tokens evolved as you
# tweaked prompt / schema. Files are gitignored.
python -m src.transcript_pipeline <page_id> --extract --save-run --run-note "baseline"

# Diagnostic: legacy 2-call extract (correct + extract, no write)
python -m src.transcript_pipeline <page_id> --extract --legacy-2call

# Diagnostic: just correct the transcript (legacy only — requires --legacy-2call to be meaningful)
python -m src.transcript_pipeline <page_id> --correct --legacy-2call

# Override model
python -m src.transcript_pipeline <page_id> --write --model gpt-5-mini

# Force OpenAI endpoint (when OPENAI_BASE_URL points to Gemini)
python -m src.transcript_pipeline <page_id> --write --openai

# Test GCal attendee lookup only
python -m src.transcript_pipeline <page_id> --gcal
```

**`--write` routes through `pipeline.run_sync_for_page()`** — the unified pipeline with dedup, classification, and all post-processing. Diagnostic flags (`--correct`, `--extract`) run standalone without the full pipeline. `--legacy-2call` is diagnostic-only — production rollout is controlled by `TRANSCRIPT_MERGED_EXTRACTION`.

## Google Calendar integration

GCal is the **authoritative attendee source** when a matching calendar event exists. The pipeline searches GCal by cleaned meeting title (ISO datetime suffix stripped via `strip_title_datetime()`), and when found, **replaces** Notion's attendee list entirely. Falls back to Notion attendees only when no GCal event is found.

**Auth:** Google Cloud **service account** with Domain-Wide Delegation, scope `https://www.googleapis.com/auth/calendar`. The SA impersonates the Notion page creator per-meeting (resolved via `client.users.retrieve`), falling back to `GCAL_DELEGATED_USER_DEFAULT`. Works identically in CLI and Lambda.

**Name resolution:** Calendar event attendees come back with emails only (no `displayName`), and the `directory.readonly` scope is not authorized. Names are resolved by matching attendee emails against the **Email** property on the Notion Org Chart DB. External attendees (non-Kibo emails) pass through with email-only — the LLM handles them as "external guests."

**Credentials:**
- **Local:** `GOOGLE_SERVICE_ACCOUNT_FILE=.secrets/service-account.json` (gitignored).
- **Lambda:** `GOOGLE_SERVICE_ACCOUNT_SECRET_ARN` pointing at a Secrets Manager secret holding the JSON. SAM template grants `secretsmanager:GetSecretValue` conditionally (only if the ARN parameter is set).

**Known issue:**
- **Date fallback**: The Notion "Date" property is empty for some meeting pages — currently falls back to `created_time` which may not match the actual meeting time.

## Task extraction (commitment-aware prompting)

The `--extract` flag runs a second LLM call on the corrected transcript to extract action items. Uses commitment-aware prompting (hard/conditional/soft/group commitments), org chart context for role-based assignee resolution, and meeting metadata for relative date resolution. Outputs: title, assignee, priority, due date, confidence level, and supporting transcript quote.

## Key files

| File | Responsibility |
|------|---------------|
| `src/transcript_pipeline/__main__.py` | CLI entry point (diagnostics + `--write` routes through pipeline) |
| `src/transcript_pipeline/fetch_transcript.py` | Find meeting_notes block, extract transcript, resolve attendees, page metadata |
| `src/transcript_pipeline/context_loader.py` | Load terminology dictionary + org chart from Notion DBs |
| `src/transcript_pipeline/transcript_corrector.py` | LLM-based transcript correction (legacy 2-call flow + `--legacy-2call` diagnostic only) |
| `src/transcript_pipeline/task_extractor.py` | LLM-based action item extraction. `extract` consumes a corrected transcript (legacy); `extract_from_raw` does correction + extraction in one merged call (merged path) — native `google-genai` SDK with `caches.create` + `response_schema` for Gemini models, OpenAI-compat shim otherwise |
| `src/transcript_pipeline/schemas.py` | Pydantic `MergedExtractionOutput` schema shared by the Gemini-native (`response_schema`) and OpenAI-compat call sites |
| `src/transcript_pipeline/transcript_cleaner.py` | Deterministic regex cleanup (Layers A + B): drops timestamps and bare speaker labels, collapses same-speaker runs, dedupes adjacent identical sentences |
| `src/transcript_pipeline/task_classifier.py` | LLM-based task classification (category, parent, assignee, deal) |
| `src/transcript_pipeline/gcal_attendees.py` | Google Calendar lookup via service account (DWD) — per-meeting impersonation, emails-only; names resolved downstream via Org Chart |

## Notion databases (env vars in `.env`)

- **Terminology DB** (`TERMINOLOGY_DB_ID`): Term, Phonetic Variants, Category, Context, Active
- **Org Chart DB** (`ORG_CHART_DB_ID`): Name, **Email** (required for GCal attendee matching), Role, Department, Seniority, Typical Topics, Active

## Logfire

All LLM calls (correction, extraction, classification) are tracked via logfire. Token usage is automatic via `logfire.instrument_openai()`.
