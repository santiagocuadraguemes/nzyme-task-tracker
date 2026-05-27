# Transcript pipeline

The transcript pipeline modules (`src/transcript_pipeline/`) handle transcript-based task extraction. These modules are integrated into the main pipeline orchestrator (`src/pipeline.py`) and are also available as a standalone CLI for diagnostics.

## Pipeline steps

1. **Fetch** raw transcript from Notion `meeting_notes` block
2. **Load context** from Terminology DB + Org Chart DB + Google Calendar attendees
3. **Merged correction + extraction** — single Gemini call reads the raw transcript and emits tasks directly. Domain corrections + speaker resolutions surface as scratch fields (`domain_corrections`, `speaker_resolutions`) — no full corrected transcript is produced.
4. **Classify** tasks via LLM (category, parent, assignee, deal mapping)
5. **Write** classified tasks to Team Task Tracker (via `TeamTaskTrackerWriter`)

Each extracted task carries a `commitment_type` field (`hard|conditional|soft|group`) and a verbatim `context` quote from the raw transcript; a soft check warns when the quote is not found in the transcript (never drops the task — the check is whitespace-lossy).

The merged system prompt lives in the Notion page **`🧠 Transcript Extraction Prompt`** (`MERGED_TRANSCRIPT_EXTRACTION_PROMPT_PAGE_ID`, required env var) and is reloaded on every sync tick. There is no in-code fallback: an unset env var raises at config load, and an empty or inaccessible page raises during ctx-load — the pipeline never runs against a silently-degraded prompt.

## Cost reductions on the merged extraction call

Two optimisations are wired into the merged path. Both fire automatically when the extraction model is `gemini-*`.

- **Deterministic transcript cleanup** (`src/transcript_pipeline/transcript_cleaner.py`) — regex-only artefact removal applied before the transcript reaches the LLM. Strips pure-timestamp lines, bare speaker labels, blank-line runs; collapses consecutive same-speaker utterances; drops adjacent identical sentences. Typically shaves 10–25% off transcript chars with no semantic loss. Logged per call as `Transcript cleaned: N → M chars (X% kept)`. No NLP / spaCy.
- **Native Gemini SDK with explicit context caching + `response_schema`** (`src/transcript_pipeline/task_extractor.py`) — when the extraction model starts with `gemini-`, the merged call uses `google-genai` directly instead of the OpenAI-compat shim. The stable system prefix (Notion-loaded system prompt + terminology + org chart) is uploaded once via `caches.create` and reused across meetings until its 1h TTL expires; cached input tokens are billed at ~25% of the standard rate. Output shape is enforced via the `MergedExtractionOutput` Pydantic schema in `src/transcript_pipeline/schemas.py` (portable across providers). Module-level `_GEMINI_CACHE_REGISTRY` keyed by SHA256 of the system prefix means changes to the prompt page, org chart, or terminology naturally produce a fresh cache. Non-Gemini extraction models keep the existing OpenAI-compat JSON-object path.

## Measuring merged-call output tokens (cost-reduction harness)

The merged Gemini call is the most expensive LLM step. To cut its output tokens without losing quality, use this harness:

| Script | Purpose | API cost |
|---|---|---|
| `scripts/compare_candidate.py --candidate baseline` | Produces `baseline.json` — real merged outputs + token counts on a pinned corpus | ~1 real Gemini call per page |
| `scripts/estimate_output_savings.py` | Ranks candidate schema reductions offline by re-serialising the saved outputs and calling Gemini's free `count_tokens` API | **zero** (count_tokens is unmetered, including on free tier) |
| `scripts/compare_candidate.py --candidate <variant>` | Runs the merged extractor on the same corpus with the candidate schema swapped in | ~1 real Gemini call per page |
| `scripts/compare_runs.py` | Diffs two JSON dumps (token deltas + task-overlap + field agreement) | zero |
| `python -m src.transcript_pipeline <page_id> --extract --save-run [--run-note "..."]` | Single-meeting diagnostic; with `--save-run`, appends a history entry (tasks + tokens + raw_data + UTC timestamp + optional `--run-note`) to `runs/<page_id>.json`. Re-runs of the same page APPEND a new entry — the file becomes a per-meeting log of how output tokens evolved across prompt/schema tweaks. Default dir overridable with `--save-run-dir`. | ~1 real Gemini call (same as plain `--extract`) |

Free-tier-friendly flow:

```powershell
# Both real-call scripts use Gemini (heavy) → GEMINI_API_KEY.
# 1. One real baseline run (e.g. 10 pages = 10 calls).
../venv/Scripts/python scripts/compare_candidate.py --candidate baseline --pages-file pages-corpus.txt --out baseline.json

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
# Full pipeline: extract → classify → write
# Routes through pipeline.run_sync_for_page().
python -m src.transcript_pipeline <page_id> --write
python -m src.transcript_pipeline <page_id> --write --dry-run

# Diagnostic: extract tasks via the merged single call (no write)
python -m src.transcript_pipeline <page_id> --extract

# Diagnostic + measurement: same as above, also append a history entry
# (tasks + token counts + raw payload + UTC timestamp + --run-note) to
# runs/<page_id>.json. Re-runs of the same meeting APPEND a new entry —
# the file is a chronological log of how output_tokens evolved as you
# tweaked prompt / schema. Files are gitignored.
python -m src.transcript_pipeline <page_id> --extract --save-run --run-note "baseline"

# Override model
python -m src.transcript_pipeline <page_id> --write --model gpt-5-mini

# Force OpenAI endpoint (when OPENAI_BASE_URL points to Gemini)
python -m src.transcript_pipeline <page_id> --write --openai

# Test GCal attendee lookup only
python -m src.transcript_pipeline <page_id> --gcal
```

**`--write` routes through `pipeline.run_sync_for_page()`** — the unified pipeline with dedup, classification, and all post-processing. `--extract` runs the merged single call standalone (no classification, no write).

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

The merged call uses commitment-aware prompting (hard/conditional/soft/group commitments), org chart context for role-based assignee resolution, and meeting metadata for relative date resolution. Outputs: title, assignee, priority, due date, confidence level, and supporting transcript quote.

## Key files

| File | Responsibility |
|------|---------------|
| `src/transcript_pipeline/__main__.py` | CLI entry point (diagnostics + `--write` routes through pipeline) |
| `src/transcript_pipeline/fetch_transcript.py` | Find meeting_notes block, extract transcript, resolve attendees, page metadata |
| `src/transcript_pipeline/context_loader.py` | Load terminology dictionary + org chart from Notion DBs |
| `src/transcript_pipeline/task_extractor.py` | LLM-based action item extraction (`extract_from_raw`): does correction + speaker resolution + extraction in one merged call — native `google-genai` SDK with `caches.create` + `response_schema` for Gemini models, OpenAI-compat shim otherwise |
| `src/transcript_pipeline/schemas.py` | Pydantic `MergedExtractionOutput` schema shared by the Gemini-native (`response_schema`) and OpenAI-compat call sites |
| `src/transcript_pipeline/transcript_cleaner.py` | Deterministic regex cleanup (Layers A + B): drops timestamps and bare speaker labels, collapses same-speaker runs, dedupes adjacent identical sentences |
| `src/transcript_pipeline/task_classifier.py` | LLM-based task classification — emits int tokens for parent + assignees; derives `category` from the parent's Tier-0 ancestor. Reads operator-set `Macro Work Block` / `Detail` / `External Org` from the meeting page as strongest placement signal. Output tokens ~30% leaner than the old UUID-shaped contract. |
| `src/transcript_pipeline/gcal_attendees.py` | Google Calendar lookup via service account (DWD) — per-meeting impersonation, emails-only; names resolved downstream via Org Chart |

## Notion databases (env vars in `.env`)

- **Terminology DB** (`TERMINOLOGY_DB_ID`): Term, Phonetic Variants, Category, Context, Active
- **Org Chart DB** (`ORG_CHART_DB_ID`): Name, **Email** (required for GCal attendee matching), Role, Department, Seniority, Typical Topics, Active

## Observability — token tracking & local capture

Logfire is configured in one place — `configure_logfire(token, service_name=...)` in `src/utils/llm_logging.py` — used by `src.main`, the transcript CLI, and the Lambda. It instruments both `instrument_openai()` and `instrument_google_genai()` and enables GenAI message-content capture.

### Why "output tokens" look too big

The classifier (`gpt-5-mini`) and the extractor (`gemini-3-flash-preview`) are **reasoning/thinking models**. Their billed output includes a hidden chain-of-thought that is *not* part of the visible JSON:

- **OpenAI**: `usage.completion_tokens` already contains the reasoning portion (`usage.completion_tokens_details.reasoning_tokens`). A 17-task classification can emit ~500 visible tokens but bill ~2,400 — the gap is reasoning.
- **Gemini**: reports `thoughts_token_count` *separately* from `candidates_token_count`; both bill at the output rate. `log_usage_genai` records `completion = candidates + thoughts` (the old code counted only `candidates` and under-reported cost).

`log_usage` / `log_usage_genai` now surface this as a `(N reasoning)` suffix on each token line, and the end-of-run `=== LLM USAGE SUMMARY ===` has a **Reason** column (a subset of Output, never added to cost on top).

The classifier defaults to `reasoning_effort="minimal"` (token→UUID mapping needs little deliberation; without it gpt-5-mini burns ~1,500-2,000 reasoning tokens/call). Override per run for quality comparisons:

```bash
# minimal|low|medium|high, or "default" to let the API decide
$env:NZYME_CLASSIFY_REASONING_EFFORT="medium"
```

### Local LLM capture (`--dump-llm`) — the CLI debugging tool

Logfire's scrubber redacts any prompt containing `auth`/`token`/etc. (Gemini system input shows as `[Scrubbed due to 'auth']`), and its google-genai instrumentation only half-captures native calls — so it's the wrong tool for interactive CLI debugging. Instead:

```bash
# OPENAI_API_KEY (gpt-5-mini, classification) + GEMINI_API_KEY (gemini-3-flash-preview, extraction)
python -m src.transcript_pipeline <page_id> --extract --dump-llm
```

`--dump-llm` (env: `NZYME_DEBUG_LLM=1`) writes the **exact, unscrubbed** system prompt, user prompt, raw response, and token usage of every LLM call to a single append-only file — one run = one file: `.llm_logs/<UTC-timestamp>.jsonl`, one JSON line per call. The path is printed at the end of the run. The same flag also turns off Logfire scrubbing on local (non-Lambda) runs so Gemini system input is readable in the Logfire UI. `.llm_logs/` is gitignored. Lambda always keeps scrubbing on. Implementation: `src/utils/llm_dump.py`.
