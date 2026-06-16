# Deployment & manual runs

## Deploying to AWS Lambda

Two deploy scripts in `scripts/`:

| Script | When to use | What it does | Speed |
|--------|------------|--------------|-------|
| `quick-deploy.sh` | **Code-only changes** (default) | Copies `src/` into the SAM build dir, zips, uploads directly via `aws lambda update-function-code` | ~10 seconds |
| `deploy.sh` | Dependency or infrastructure changes (`requirements.txt`, `template.yaml`) | Full `sam build` + `sam deploy` with CloudFormation | ~2-3 minutes |

**Always use `quick-deploy.sh`** unless you changed dependencies or `template.yaml`. It requires a prior `sam build` (the `.aws-sam/build/` directory must exist with dependencies installed).

**Important:** The script does `rm -rf` then `cp -r` to replace the src directory. A plain `cp -r src/ dest/src/` does NOT overwrite files on Windows/Git Bash — it silently keeps stale code. Always verify a deploy worked by checking that the `CodeSha256` in the output changed.

SAM CLI path (Windows): `C:/Users/Santiago Cuadra/AppData/Local/Packages/PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0/LocalCache/local-packages/Python313/Scripts/sam.exe`

For `sam build`, the venv Python must be on PATH:
```bash
PATH="C:/Users/Santiago Cuadra/vscode_projects/venv/Scripts:$PATH" sam build
```

> **Note (post lambda-split).** This repo now only injects templates and runs the
> Notion → Supabase mirror. Task extraction (and its `pause-lambda.sh` /
> `resume-lambda.sh` cron controls, per-stage model overrides, and provider
> auto-detection) moved to `nzyme-task-extraction`; the hierarchy appliers and the
> Done-task archive moved to `nzyme-housekeeping`. See those projects for their
> deploy/CLI docs and [architecture-lambda-split.md](architecture-lambda-split.md)
> for the overall shape.

## Manual CLI runs

`python -m src.main` accepts these flags:

| Flag | Effect |
|------|--------|
| `--inject-templates` | Inject the meeting-note template into new pages (one-shot). Default when no flag is given. |
| `--supabase-sync` | Run the Notion → Supabase incremental mirror (one-shot). |
| `--watch` | Continuous loop: inject templates every `WATCH_INTERVAL`s, run the Supabase sync every `SYNC_INTERVAL`s. |
| `--db-id <notion_db_id>` | Process exactly one Meeting Notes DB (skips Org Chart discovery). Equivalent to setting `MEETING_NOTES_DB_ID` in `.env`. |
| `--dry-run` | Log actions but don't write to Notion. |
| `--verbose` | DEBUG-level logs for `src.*` (third-party loggers stay at WARNING via `setup_logging`). |

This repo makes **no LLM calls** — it needs only the Notion token plus Supabase
credentials. (Model/provider configuration lives with `nzyme-task-extraction`.)

## Watch vs one-shot mode

```bash
# Watch mode — loop continuously (Ctrl+C to stop)
python -m src.main --watch
python -m src.main --watch --dry-run --verbose

# One-shot: inject templates (default)
python -m src.main
python -m src.main --inject-templates --dry-run --verbose

# One-shot: run only the Notion → Supabase mirror
python -m src.main --supabase-sync
```

## Two functions, one stack (since 2026-06-16)

The stack `nzyme-task-tracker` deploys **two** Lambda functions from the same code
package:

| Function | Handler | Trigger | Job |
|----------|---------|---------|-----|
| `nzyme-webhook` (256 MB / 30 s) | `src.webhook.lambda_handler.webhook_handler` | API Gateway `POST /webhook/{token}` | real-time template injection |
| `nzyme-task-tracker` (512 MB / 300 s) | `src.webhook.lambda_handler.cron_handler` | `SupabaseSync` + `SupabaseWeeklySync` schedules | Notion → Supabase mirror |

Both sit behind the **same** `HttpApi` resource, so the webhook URL (api-id
`9g8txmxkef`) is stable across this split — no Notion automation repointing.
`deploy.sh` (full `sam build` + `sam deploy`) handles both; a `template.yaml` change
(like this split) requires the full deploy, not `quick-deploy.sh`. The heartbeat
alarm is keyed to `/aws/lambda/nzyme-task-tracker`, so the Sync function must keep
that name.

> **`quick-deploy.sh` caveat:** it updates a single function's code by name. After
> this split it targets `nzyme-task-tracker` (Sync) only — to hot-patch the webhook
> code, point it at `nzyme-webhook` or run the full `deploy.sh`.

See `docs/architecture.md` for the Lambda entry points (the webhook handler routes
template injection; the cron handler routes the `supabase_sync` /
`supabase_sync_full` cron jobs).
