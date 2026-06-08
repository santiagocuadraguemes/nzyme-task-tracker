# Step 1 — Finish & ship the Sync program (Notion → Supabase read surface)

> Implementation brief for the agent. Companion to
> [docs/architecture-lambda-split.md](../docs/architecture-lambda-split.md).
> Branch: `external-orgs-db-sync`.

## Goal

Complete and ship **Program 1 (Sync)** from the architecture plan. The remaining
feature is the **config mirror**: two new sub-syncs that copy the Org Chart and the
Meeting Rules into Supabase every 5 minutes, making Supabase the *complete* read
surface (meetings + member config + routing rules) for the downstream worker Lambdas.
A heartbeat CloudWatch alarm is added so a stalled sync is noticed.

The code is **already written and locally test-run** (the Supabase tables exist and
are populated). Your job is to **verify it is correct and complete, get the test
suite + lint green, commit it on the branch, and hand Santiago an exact deploy +
verification runbook.** You do **not** deploy and you do **not** run the pipeline (see
Hard constraints).

## What is already done (verified 2026-06-08 — do not redo)

- **Supabase tables already exist in the Neo project (`yphbrpbwpakjduhmoimw`) and are
  populated** — no migration work:
  - `public.org_chart_rows` — 24 rows. PK `notion_page_id (uuid)`. Columns: `name`,
    `email`, `meeting_notes_db_id (uuid)`, `active (bool, default true)`,
    `auto_extract_tasks (bool, default false)`, `default_mirror_visibility (text)`,
    `seniority (text)`, `synced_at (timestamptz default now())`, `deleted_at
    (timestamptz)`. RLS enabled (service-role key bypasses).
  - `public.meeting_rule_rows` — 6 rows. PK `notion_page_id (uuid)`. Columns: `label`,
    `match_property`, `match_value`, `action`, `target_db_id (uuid)`, `active (bool,
    default true)`, `synced_at`, `deleted_at`. RLS enabled.
  - Both PKs are `notion_page_id`, so the `on_conflict=notion_page_id` upserts in
    `config_mirror_sync._upsert` resolve correctly.
- `LoggingConfig: LogFormat: JSON` is set on the function (`template.yaml:252-254`), so
  the JSON metric filter on `$.message` will match the heartbeat line.
- Config fields are wired: `config.org_chart_db_id` (`ORG_CHART_DB_ID`) and
  `config.meeting_rules_db_id` (`MEETING_RULES_DB_ID` → falls back to
  `TOPIC_MIRROR_ROUTES_DB_ID`) — `src/config.py`.

## The uncommitted working tree (what you are shipping)

New files:
- `src/config_mirror_sync.py` — `sync_org_chart` → `org_chart_rows`, `sync_meeting_rules`
  → `meeting_rule_rows`. Stable identity `notion_page_id`; `deleted_at` tombstones rows
  that vanish from Notion (revived on reappearance via the `deleted_at: None` in each
  upsert). All I/O via the stdlib `_http` helper reused from
  `src.hierarchy.canonical_mirror_sync`.
- `tests/test_config_mirror_sync.py` — unit tests (Supabase `_http` patched, Notion
  client mocked, no network).

Modified files:
- `src/supabase_sync.py` — `_sync_config_mirrors()` runs both mirrors at the end of
  `run_incremental` (and should be confirmed for the full-sweep path too — see Tasks).
  Each mirror is wrapped in its own try/except so a config-mirror failure never blocks
  the meeting sync.
- `src/meeting_row.py` — `extract_row` now also emits `external_org`, `confidential`,
  `created_by_id`, `created_by_name` into `meeting_transcripts`.
- `src/webhook/lambda_handler.py` — `_handle_supabase_sync` now always logs
  `supabase sync heartbeat: upserted=%d` at INFO (the load-bearing heartbeat line).
- `template.yaml` — new `AlertEmail` param + `AlertsEnabled` condition; `AlertTopic`
  (SNS), `SupabaseSyncHeartbeatFilter` (metric filter on the heartbeat line), and
  `SupabaseSyncStalledAlarm` (45-min silence → ALARM).
- `scripts/deploy.sh` — passes `AlertEmail` through from `$ALERT_EMAIL`.
- `docs/architecture.md` — new "Supabase mirror" section documenting the three tables.
- `tests/test_supabase_sync.py`, `tests/test_meeting_row.py` — tests for the new wiring
  and the new `meeting_row` fields.

**Do NOT commit `.claude/settings.local.json`** (local editor settings — unrelated).
`docs/architecture-lambda-split.md` and `specs/step-1-finish-sync-lambda.md` are part
of this change and SHOULD be committed.

## Tasks (in order)

1. **Read the new/changed code end to end** — `src/config_mirror_sync.py`,
   `src/supabase_sync.py` (the `_sync_config_mirrors` + `run_incremental`/`run_full`
   region), and the three changed test files. Build a mental model before changing
   anything.

2. **Confirm both config mirrors run on the weekly full sweep too, not just the
   incremental tick.** Check `run_full` in `src/supabase_sync.py`. The 5-min path
   (`run_incremental`) calls `_sync_config_mirrors`; the Sunday safety-net
   (`run_full` → `_handle_supabase_sync_full`) is the backstop for missed ticks and
   should also refresh the config mirrors (otherwise a member/rule edit dropped by every
   incremental tick that week is never caught). If `run_full` does **not** call
   `_sync_config_mirrors`, add the call (same isolated-failure pattern). If it already
   does, leave it. Either way, state what you found.

3. **Verify the heartbeat alarm is wired correctly** (no code change expected — this is
   a correctness check, report findings):
   - The metric filter pattern in `template.yaml`
     (`{ $.message = "supabase sync heartbeat*" }`) must match the JSON log line emitted
     by `_handle_supabase_sync` (`message` = `"supabase sync heartbeat: upserted=N"`).
     JSON logging is on, so this should match — confirm the field name is `message` for
     Lambda's JSON format and the wildcard placement is right.
   - `TreatMissingData: breaching` + `EvaluationPeriods: 3` × `Period: 900` = 45 min of
     silence → ALARM. Confirm the sync's own EventBridge schedule is `rate(5 minutes)`
     so ≥3 heartbeats are expected per 15-min period.
   - Confirm `AlarmActions`/`OKActions` degrade cleanly when `AlertEmail` is empty
     (`!If [AlertsEnabled, [...], []]`).

4. **Run the test suite and lint** (commands below). All green. If the new tests are
   thin, you may add focused cases for: tombstone-then-revive (a row disappears then
   reappears in Notion), the legacy Affinity action normalization in `sync_meeting_rules`,
   and a Mirror-to-DB rule with an unparseable Target DB being skipped. Keep new tests in
   the existing mocked style (no network).

5. **Self-review the diff** for anything that would break the running prod sync:
   the config mirrors must never raise out of `run_incremental` (they're the newest,
   least-proven code riding the most critical cron). Confirm the try/except isolation
   holds for both mirrors.

6. **Commit** on `external-orgs-db-sync` (do not push). Stage only the files listed in
   "The uncommitted working tree" above plus the two new docs — explicitly exclude
   `.claude/settings.local.json`. Suggested message:

   ```
   feat(supabase-sync): mirror Org Chart + Meeting Rules → Supabase; heartbeat alarm

   Completes the Notion → Supabase read surface (Program 1 of the Lambda split):
   - config_mirror_sync: sync_org_chart → org_chart_rows, sync_meeting_rules →
     meeting_rule_rows; notion_page_id identity, deleted_at tombstones w/ revive.
     Rides every 5-min sync tick (+ weekly sweep); isolated failure never blocks
     the meeting sync.
   - meeting_row: also mirror external_org, confidential, created_by_id/_name.
   - heartbeat: _handle_supabase_sync always logs "supabase sync heartbeat:" →
     CloudWatch metric filter + nzyme-supabase-sync-stalled alarm (45-min silence).
   - docs: architecture-lambda-split.md roadmap + architecture.md mirror section.

   Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
   ```

7. **STOP and write the hand-off runbook** for Santiago (below). Do not deploy, do not
   push, do not run any pipeline command.

## Test & lint commands

These use the shared venv one directory up (`../venv/`). The agent runs these itself —
they are local, no external side effects.

```
../venv/Scripts/python -m pytest tests/test_config_mirror_sync.py tests/test_supabase_sync.py tests/test_meeting_row.py -v
../venv/Scripts/python -m pytest tests/ -q
../venv/Scripts/python -m ruff check src/ tests/
```

## Acceptance criteria

- [ ] `config_mirror_sync` read end-to-end; behavior matches this spec.
- [ ] Decision on `run_full` config-mirror coverage made and applied (or confirmed
      already present).
- [ ] Heartbeat alarm wiring confirmed correct (filter matches JSON message; missing-data
      + email-empty behavior verified).
- [ ] Full test suite passes; `ruff check` clean.
- [ ] Committed on `external-orgs-db-sync`, `.claude/settings.local.json` NOT included.
- [ ] Hand-off runbook produced.

## Hand-off runbook (the agent produces this; Santiago runs it)

This change touches `template.yaml` (new SNS topic, metric filter, alarm, parameter), so
it is a **full** deploy — `./scripts/deploy.sh`, **not** `quick-deploy.sh`.

1. Set the alert email in `.env` (optional but recommended; empty = alarm exists but
   notifies nobody): `ALERT_EMAIL=santiago.cuadra@nzyme.com`.
2. Full deploy: `./scripts/deploy.sh` (Santiago runs this).
3. If `ALERT_EMAIL` was set: confirm the SNS subscription email and click the confirm
   link (one-time).
4. Post-deploy verification (the agent should write these out concretely):
   - CloudWatch: the `Nzyme/SupabaseSyncHeartbeat` metric increments every ~5 min; the
     `nzyme-supabase-sync-stalled` alarm settles to OK within ~15 min.
   - Supabase: `select count(*), max(synced_at) from public.org_chart_rows;` and the same
     for `meeting_rule_rows` — `synced_at` should advance on each tick.
   - Edit one Org Chart row (or toggle a rule's Active) in Notion, wait one tick, confirm
     the change lands in the corresponding Supabase table (and that a deleted row gets a
     `deleted_at`, a re-added one clears it).

## Hard constraints (project rules — non-negotiable)

- **Do NOT run the pipeline** (`python -m src.main`, `src.transcript_pipeline`, the sync
  CLIs). Santiago runs all pipeline/deploy commands himself. Provide commands; never
  execute them.
- **Do NOT deploy and do NOT push.** Commit on the branch only; the deploy is Santiago's.
- **No silent failures** in any code you add — let errors surface; do not `except: pass`.
  (The config-mirror try/except is the intended exception: it logs via
  `logger.exception` and continues — that is explicit and logged, not silent.)
- **No @mentions** in any Notion content.
- Any run/deploy command you put in the runbook must carry the **two-key model split** so
  Santiago knows which key to check on failure: heavy calls = `gemini-3-flash-preview`
  via `GEMINI_API_KEY`; light calls = `gpt-5-mini` via `OPENAI_API_KEY`. (Step 1 itself
  makes no LLM calls, but keep the convention in any command you surface.)
- After the work, update the relevant docs if anything changed materially (the mirror
  section in `docs/architecture.md` already exists — adjust only if your `run_full`
  decision changes the described behavior).
