# Output-token reduction plan — merged extraction call

This document plans every realistic lever for reducing **output tokens** on the merged Gemini extraction call (`TaskExtractor.extract_from_raw`). Output is the dominant cost once Gemini context caching is active (cached input drops to ~25% of standard rate; output stays at full rate).

Baseline reference run (page `35e83e67e2e780cc89f9d79b123ad412`, "Int.call IA daily catch up", 2026-05-12, 6 attendees, 30 min):

- 12 tasks extracted
- **1,684 output tokens** (~140 tokens / task)
- 15,412 input tokens, 0 cached (free tier)
- Model: `gemini-3-flash-preview`

All edits are local to:

- `src/transcript_pipeline/schemas.py` (Pydantic shapes + `CANDIDATE_SCHEMAS`)
- `src/transcript_pipeline/task_extractor.py` (`MERGED_SYSTEM_PROMPT`, `_TASK_FIELD_MAP`, `_unpack_merged_response`)
- `scripts/estimate_output_savings.py` (offline transform `CANDIDATES`)

Use the existing measurement harness for every change:

```powershell
# Free, offline ranking against the saved baseline.json
../venv/Scripts/python scripts/estimate_output_savings.py baseline.json

# Real-call validation of the top candidate (~10 Gemini calls)
../venv/Scripts/python scripts/compare_candidate.py --candidate <name> --pages-file pages-corpus.txt --out cand.json
../venv/Scripts/python scripts/compare_runs.py baseline.json cand.json
```

---

## How to read each plan entry

Each section follows the same shape:

- **Mechanism** — what changes in the model's output and why it saves tokens.
- **Files touched** — exact files + functions to edit.
- **Schema change** — new Pydantic class name + JSON shape diff.
- **Prompt change** — exact paragraphs to insert / replace in `MERGED_SYSTEM_PROMPT`.
- **Decode path** — what `_unpack_merged_response` (or a new helper) does to restore the production task dict shape the rest of the pipeline expects.
- **Estimated savings** — per typical 10-task meeting, holding everything else constant.
- **Risks** — what could break, how to detect it on the shadow-diff run.
- **Validation** — pass thresholds for `compare_runs.py` (target: ≥90% task overlap, ≥90% `internal_assignees` agreement, ±15% task-count delta).

---

## Group A — Drop fields from the schema

The cheapest, lowest-risk wins. Each removed field saves `N × (key + value + delimiters)` tokens.

### A1. Drop `sr` (speaker_reasoning)

- **Mechanism**: `sr` is diagnostic-only; no production code reads it. Removing it cuts ~25 tokens per task.
- **Files touched**:
  - `schemas.py`: already exists as `ExtractedTaskNoSR` / `MergedExtractionOutputNoSR`. Promote to default.
  - `task_extractor.py`: drop the `"sr"` entry from `_TASK_FIELD_MAP`; remove the `## Speaker & Assignee Resolution → speaker_reasoning` paragraph + the `"sr"` line in the Output section of `MERGED_SYSTEM_PROMPT`.
- **Schema change**: per-task object loses one string field.
- **Prompt change**: replace the `For each task, include a "speaker_reasoning" field …` paragraph with a one-liner `Apply the assignee rules above silently — do not emit reasoning text.`
- **Decode path**: no change (already absent from `_TASK_FIELD_MAP` after edit).
- **Estimated savings**: ~25 × N tasks → **~250 tokens / meeting**.
- **Risks**: loss of debuggability when an assignment is questioned. Mitigation: keep the `--save-run` raw payload — and if needed, re-add a coded reason (see C3).
- **Validation**: `compare_candidate.py --candidate no-sr` already supported. Diff `internal_assignees` agreement — should match baseline within 1–2 tasks across the corpus.

### A2. Drop `a` (assignee display string)

- **Mechanism**: `a` is `", ".join(ia + ea)` 99% of the time. Re-derive in Python.
- **Files touched**:
  - `schemas.py`: `ExtractedTaskNoSRNoA` already exists; `MergedExtractionOutputCombined` already drops it alongside `sr` + scratch fields.
  - `task_extractor.py`: drop `"a"` from `_TASK_FIELD_MAP`. In `_unpack_merged_response`, after the dict mapping, synthesise `assignee = ", ".join((ia or []) + (ea or [])) or "Team"`.
  - `__main__.py` and any consumer that reads `task["assignee"]` is unaffected since we synthesise it.
- **Prompt change**: remove the `"a" (assignee display string …)` line from the Output section.
- **Decode path**: post-process step in `_unpack_merged_response` adds the synthesised `assignee` key for backwards compatibility with the rest of the pipeline (writer, classifier).
- **Estimated savings**: ~10 × N tasks → **~100 tokens / meeting**.
- **Risks**: edge case where the model previously emitted a phrase like `"Santiago (with Reyes on privacy)"` — that nuance is gone. Mitigation: keep `sr` if you also do A1, or accept the loss (the writer concatenates names exactly the same way anyway).
- **Validation**: `compare_runs.py` will flag if `assignee` strings diverge from baseline; ignore divergences that are purely whitespace / ordering.

### A3. Drop `c` (confidence)

- **Mechanism**: `c` is almost entirely a function of `ct`:
  | ct | implied c |
  |----|-----------|
  | hard | high |
  | conditional | medium |
  | soft | medium |
  | group | low |
- **Files touched**:
  - `schemas.py`: new `ExtractedTaskNoSRNoANoC`, new `MergedExtractionOutputLean`, register in `CANDIDATE_SCHEMAS` as `"lean"`.
  - `task_extractor.py`: drop `"c"` from `_TASK_FIELD_MAP`; in `_unpack_merged_response`, derive `confidence` from `commitment_type` via the table above.
- **Prompt change**: remove the `"c" (confidence)` line from the Output section; remove `confidence: high` / `confidence: medium` annotations in the Commitment classification section.
- **Decode path**: lookup table in `_unpack_merged_response` after mapping.
- **Estimated savings**: ~3 × N tasks → **~30 tokens / meeting**.
- **Risks**: the writer doesn't currently use `confidence` for any filtering — verify in `src/tracker/team_writer.py`. If unused, deletion is safe. If used (semantic dedup threshold?), keep derivation.
- **Validation**: confirm baseline `c` matches the table for ≥80% of tasks before adopting; if the model regularly emits `hard → medium`, keep `c` separate.

### A4. Drop `domain_corrections` + `speaker_resolutions` (scratch fields)

- **Mechanism**: scratch fields are diagnostic-only; the merged path no longer emits a corrected transcript. They cost tokens proportional to how chatty the model is — typically 80–200 tokens, occasionally 0.
- **Files touched**:
  - `schemas.py`: `MergedExtractionOutputNoScratch` already exists.
  - `task_extractor.py`: in `_unpack_merged_response`, the `corrections` / `resolutions` log lines become no-ops when the keys are absent — `data.get(..., [])` already handles that gracefully. No code change beyond schema swap.
- **Prompt change**: remove the `"domain_corrections"` / `"speaker_resolutions"` lines from the Output section + the Mental correction section (the model still corrects internally, just doesn't report).
- **Decode path**: unchanged — the `.get(..., [])` calls in `_unpack_merged_response` already return empty when missing.
- **Estimated savings**: **~80–200 tokens / meeting** (highly variable).
- **Risks**: harder to debug a bad domain correction post-hoc. Mitigation: keep them gated behind `--verbose` if you want optional emission.
- **Validation**: `compare_candidate.py --candidate no-scratch` already supported. Confirm task quality is identical.

### A5. Make `ea` optional / omit when empty

- **Mechanism**: most internal meetings emit `"ea": []` on every task. Removing the field saves 8 tokens × N when always empty.
- **Files touched**:
  - `schemas.py`: change `ea: list[str] = Field(default_factory=list)` to `ea: Optional[list[str]] = None` and reflect that in the prompt instructions.
  - `task_extractor.py`: `_unpack_merged_response` already handles missing keys via the dict comprehension.
- **Prompt change**: rephrase the Output section: `"ea" (external_assignees) — INCLUDE ONLY IF non-empty.`
- **Decode path**: synthesise `external_assignees = data.get("ea") or []` in the dict mapping.
- **Estimated savings**: **~30 tokens / meeting** for internal-only meetings; ~0 for portfolio calls.
- **Risks**: the model may inconsistently include or omit. The `Optional`-with-instruction approach is fragile on Gemini. Lower priority.

### A6. Omit `dd` when null

- **Mechanism**: `"dd": null` costs 4 tokens; emitting nothing costs 0. Roughly half of tasks lack a due date.
- **Files touched**: `schemas.py` already has `dd: Optional[str] = None`; just update the prompt to say "omit if no deadline" instead of "null".
- **Prompt change**: replace `"dd" (due date): ISO date "YYYY-MM-DD" or null` with `"dd" (due date): ISO date "YYYY-MM-DD" — OMIT this key entirely if no deadline.`
- **Decode path**: `_unpack_merged_response` already maps absent keys to absent dict entries; the writer downstream tolerates a missing `due_date`. Verify in `src/tracker/team_writer.py`.
- **Estimated savings**: ~4 × (N/2) → **~20 tokens / meeting**.
- **Risks**: Gemini's `responseSchema` enforcement may insist on present-but-null. Test before adopting — if rejected, abandon this lever.

---

## Group B — Compress field values

Re-encode values to shorter forms; the model emits the short form, Python expands.

### B1. Single-letter codes for `ct` (commitment type)

- **Mechanism**: `hard` (1 token) vs `h` (1 token) is a wash, but `conditional` (~2 tokens) → `c` saves ~1 token; `soft` and `group` similar.
- **Files touched**:
  - `task_extractor.py`: extend `_TASK_FIELD_MAP` (or add a separate value-decode table): `{"h": "hard", "c": "conditional", "s": "soft", "g": "group"}`. Apply in `_unpack_merged_response`.
- **Prompt change**: rephrase the Commitment classification section to use short codes (`commitment_type: "h"` etc.) and remove the long-name reminders.
- **Estimated savings**: ~2 × N → **~20 tokens / meeting**.
- **Risks**: low; this is a pure encoding swap. Smoke-test that the model honors the short codes.

### B2. Single-letter codes for `p` (priority)

- **Mechanism**: `High` / `Medium` / `Low` are 1 token each; `H` / `M` / `L` are 1 token each. Marginal savings — sometimes Gemini tokenises `Medium` as 2 tokens.
- **Files touched**: same as B1; add `{"H": "High", "M": "Medium", "L": "Low"}` decode.
- **Estimated savings**: **~5–10 tokens / meeting**. Cheap but small.
- **Risks**: trivial; only adopt if bundled with B1 to avoid one-off prompt churn.

### B3. Single-letter codes for `c` (confidence)

- **Mechanism**: `h`/`m`/`l` for confidence if you don't drop the field (A3). Same magnitude as B2.
- **Estimated savings**: **~10 tokens / meeting**.
- **Risks**: nil. Conflicts with A3 — pick one.

### B4. Assignees as indices into the attendee list (highest-ROI in this group)

- **Mechanism**: pre-number attendees in the prompt (`0: Santiago Cuadra | Strategy & Ops`, `1: Jacob Hinz | Value Creation`, …). Model emits `ia: [0, 3]` instead of `["Santiago Cuadra", "Jacob Hinz"]`. Full names average 4–6 tokens each; an index is 1.
- **Files touched**:
  - `task_extractor.py`:
    - In `_build_merged_messages`, replace the `MEETING ATTENDEES` section render with an indexed list. Persist the index → name map for decode.
    - Pass the map into `_extract_from_raw_gemini` / `_extract_from_raw_openai`, store on `self._last_attendee_index`.
    - In `_unpack_merged_response`, replace each `ia` / `ea` integer with the name; fall back to passing strings through (in case the model emits the name anyway).
  - `schemas.py`: change `ia: list[str]` → `ia: list[int | str]` to permit either. (Gemini's `responseSchema` accepts `anyOf` per OpenAPI 3 — verify; if not, use plain `list[int]` and accept the loss of free-form external names.)
- **Prompt change**: replace `=== MEETING ATTENDEES ===` block with `=== ATTENDEES (use these indices for ia) ===` followed by `0: Santiago Cuadra [Intern — Strategy & Operations]` etc. Add `For ia, emit indices from this list. For ea (external people not in this list), emit their full name string.`
- **Decode path**: in `_unpack_merged_response`, walk each task: `task["internal_assignees"] = [attendee_index[i] if isinstance(i, int) else i for i in ia]`.
- **Estimated savings**: avg ~3 assignees-name tokens × N tasks → **~80 tokens / meeting**. Highest non-task-count saving in this whole document.
- **Risks**:
  - Schema mixing `int | str` may break `responseSchema` on Gemini → fall back to plain `list[int]` and never use indices for external names.
  - Model occasionally emits an index outside the range → guard with `if 0 <= i < len(attendee_index)` and drop on miss.
  - Spanish / English naming inconsistency disappears (silver lining).
- **Validation**: run on the corpus, compare `internal_assignees` field-by-field with baseline. Target ≥95% match.

### B5. First names only in `ia`

- **Mechanism**: simpler alternative to B4. `"ia": ["Santiago", "Reyes"]` instead of full names. Resolve to canonical full names by first-name match against the attendee list / Org Chart.
- **Files touched**: only `task_extractor.py` (decode logic) and prompt.
- **Estimated savings**: **~30 tokens / meeting**.
- **Risks**: collisions on common first names ("Juan" matches both Juan Lopez and Juan Alonso-Allende). The Org Chart already has this collision today. B4 is strictly better — only do B5 if `responseSchema` blocks index-based encoding.

---

## Group C — Cap free-form text

Constrain the verbose fields with explicit length budgets in the prompt.

### C1. Cap title length (~80 chars / ≤12 words)

- **Mechanism**: titles in the baseline run hit 19 words (`Asegurar que todas las notas y transcripts de Notion se almacenen automáticamente en Supabase`). Long Spanish titles are a major output-token sink.
- **Files touched**: prompt only.
- **Prompt change**: under `## Rules`, add `Title MUST be ≤12 words / 80 chars. Drop filler ("para que…", "con el objetivo de…", "in order to…"). Imperative voice. Keep the verb + object pattern.`
- **Estimated savings**: avg 3 words trimmed × ~3 tokens/word × N → **~50–100 tokens / meeting**.
- **Risks**: titles become terser and may lose nuance; verify by reading 10 example pairs.
- **Validation**: baseline vs candidate, eyeball the titles; should still be unambiguous to a reader.

### C2. Cap `sr` length (≤12 words)

- **Mechanism**: only if you keep `sr` after A1. Current `sr` runs 20–30 words.
- **Prompt change**: change `For each task, include a "speaker_reasoning" field (1 sentence)` → `"sr" must be ≤12 words. No preamble ("This is because…"). Cite ONE signal.`
- **Estimated savings**: avg 15 words saved × ~1.4 tokens/word × N → **~150 tokens / meeting** (only if you keep `sr`).
- **Risks**: terser justifications may be harder to audit. Reasonable middle ground if A1 (drop entirely) feels too aggressive.

### C3. Replace `sr` with a coded reason

- **Mechanism**: enum field `srk` (speaker reason kind): `{explicit, topic, role, seniority, group}`. One token instead of a sentence.
- **Files touched**:
  - `schemas.py`: new `ExtractedTaskCodedSR` with `srk: str = Field(description="explicit | topic | role | seniority | group")` instead of `sr: str`.
  - `task_extractor.py`: prompt section that explained `speaker_reasoning` becomes a short legend mapping the codes.
- **Estimated savings**: ~22 × N → **~220 tokens / meeting** vs keeping verbose `sr`.
- **Risks**: codes lose nuance for the rare ambiguous case. Acceptable if you can drill into `--save-run` payloads when something looks wrong.
- **Best of the `sr` family**: this is the recommended landing place — it keeps auditability without the prose tax.

---

## Group D — Output fewer tasks

Highest leverage; one fewer task ≈ 140 tokens saved.

### D1. Tighten the "what counts as a task" bar

- **Mechanism**: the baseline run produced 12 tasks for a 30-min standup; reading them, ~3 are weak (e.g. #5 "Elaborar un plan", #6 "Coordinar OKRs" — both implicit), and #12 ("Apoyar a Saki si están bloqueados") is explicitly conditional + soft.
- **Files touched**: prompt only.
- **Prompt change**: in `## Rules`, prepend `BEFORE EXTRACTING: each task needs at least ONE of: (a) a concrete verb + object + named actor in a single transcript turn, (b) an explicit deadline, (c) the note-taker captured it in HUMAN NOTES. Discussion topics, status updates, and "should look into" sentences are NOT tasks.`
- **Estimated savings**: removing 2 marginal tasks × 140 tokens → **~280 tokens / meeting**.
- **Risks**: missing a real task. Mitigation: HUMAN NOTES (rule c) is the safety net — note-taker's bullets always survive.
- **Validation**: shadow-diff against baseline; target ≥90% of high-confidence tasks preserved, ≥30% of low-confidence tasks dropped.

### D2. Hard cap N

- **Mechanism**: `Emit at most 8 tasks per meeting. Rank: hard > conditional > soft > group; ties broken by priority H > M > L. Drop the rest.`
- **Files touched**: prompt only; add an `## Output budget` section.
- **Estimated savings**: zero for normal meetings; **~280 tokens** on over-extracted ones.
- **Risks**: in genuinely action-heavy meetings, valid tasks get dropped. Mitigation: raise the cap or expose it as a flag. Less robust than D1 — only use as a complement.

### D3. Suppress `commitment_type=group` entirely

- **Mechanism**: group commitments are the lowest-quality tier (the model picks 2-3 people based on topic inference). Removing them is a quality + cost win.
- **Files touched**: prompt only.
- **Prompt change**: under Commitment classification, change Group from "extract with confidence: low" → "do NOT extract — log as a discussion topic only."
- **Estimated savings**: highly variable. On retros or all-hands meetings: **~3–6 × 140 tokens**. On 1:1s: zero.
- **Risks**: misses team-wide initiatives in big meetings. Counter-argument: those typically get captured in HUMAN NOTES anyway.

---

## Group E — Structural

Change the wire format itself.

### E1. JSONL output (one task per line)

- **Mechanism**: Drop `{"tasks": [...]}` wrapping; emit `{"t": ..., "ia": [...]}\n{"t": ..., ...}\n`. Saves outer `{}`, `tasks:`, brackets, commas between objects.
- **Files touched**:
  - `task_extractor.py`: parse `response.text.splitlines()` with one `json.loads` per non-empty line; concatenate into the existing list.
  - `schemas.py`: incompatible with `responseSchema` (which enforces JSON, not JSONL). Means turning off structured output for the merged call — measurable input-token win too, since `responseSchema` is sent with every request.
- **Prompt change**: replace `Return a JSON object with three top-level keys` with `Return one JSON object per line (JSONL), one task per line. No outer array, no wrapper.`
- **Estimated savings**: **~15–40 tokens / meeting** (modest). Plus a small input-side win from dropping `responseSchema`.
- **Risks**: parse fragility — a single malformed line breaks the whole batch. Worth the savings only if combined with several other levers.

### E2. TSV / pipe-delimited rows

- **Mechanism**: `t|ia|ea|ct|p|dd\nReviewing FDD|[0,3]||h|H|2026-05-13`. Zero structural overhead. Significant savings.
- **Files touched**: a full parser rewrite in `_unpack_merged_response`.
- **Estimated savings**: **~150–250 tokens / meeting**.
- **Risks**: very fragile (pipes inside titles, escaping, embedded commas). Stop trying once you've shaved the other levers; this is the last 10%.

---

## Group F — Indirect (prompt-level micro-cleanup)

These don't change the output schema but reduce verbosity by removing requirements that produce tokens.

### F1. Remove the "AND why external" clause in `sr`

- **Mechanism**: current prompt: `one sentence covering the assignment AND any external classification`. For internal-only tasks, the second half adds 5–10 tokens.
- **Files touched**: prompt only.
- **Estimated savings**: **~10–30 tokens / meeting**.
- **Risks**: nil. Quick win if you keep `sr`.

### F2. Allow scratch fields to be omitted when empty

- **Mechanism**: pair with A4 — if you don't fully drop scratch fields, at least let them be omitted when `[]`. Variable savings.
- **Estimated savings**: typically 0 (model already emits `[]`) but harmless.

---

## Ranked recommendation

If you want the highest-yield three with the least risk:

| Rank | Change | Savings | Risk |
|------|--------|---------|------|
| 1 | **D1** — tighten task bar | ~280 tokens | Low (HUMAN NOTES safety net) |
| 2 | **B4** — assignees as indices | ~80 tokens | Low (decode is mechanical) |
| 3 | **A1 + A4** — drop `sr` + scratch | ~330 tokens | Very low (diagnostic-only) |

Stacked: **~690 tokens / meeting** → 40% reduction on the baseline. Combine with **C1** (title cap) and **B1/B2** (single-letter codes) and you're at ~50%.

## Suggested rollout order

1. **A1 + A4** — schema variants already exist; promote `combined` to default after the harness confirms parity. One PR.
2. **D1** — pure prompt tweak; lowest risk. Validate task quality on the 10-page corpus before merging.
3. **B4** — touches encoding; needs careful decode + test. Standalone PR.
4. **C1** — prompt-only title cap. Standalone.
5. Stop here unless cost is still material. **C3 / B1-B2** are the next 10%.

## Measurement protocol per change

For every variant, before merging to prod:

1. Define the new `ExtractedTask*` + `MergedExtractionOutput*` in `schemas.py`; register in `CANDIDATE_SCHEMAS`.
2. Add the matching offline transform in `scripts/estimate_output_savings.py::CANDIDATES`.
3. Run `scripts/estimate_output_savings.py baseline.json` to confirm the predicted ranking.
4. Run `scripts/compare_candidate.py --candidate <name> --pages-file pages-corpus.txt` (~10 real Gemini calls).
5. Run `scripts/compare_runs.py baseline.json cand.json`. Pass thresholds:
   - ≥90% baseline tasks have a semantic match in candidate.
   - ≥90% `internal_assignees` agreement on matched pairs.
   - Task-count delta within ±15%.
6. If pass, promote: swap the default `response_schema` in `_extract_from_raw_gemini` and the OpenAI-compat path, update `_unpack_merged_response`, update `MERGED_SYSTEM_PROMPT`.

The pinned corpus (`pages-corpus.txt`) should cover at least: one 1:1, one team standup, one fundraising/LP call, one portfolio call, one all-hands. That spread catches the per-meeting-type regressions that a single page would mask.
