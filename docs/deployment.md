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

## Pausing the extraction cron

`scripts/pause-lambda.sh` disables the EventBridge schedule rule (`*-NzymeFunctionScheduledExtraction-*`) so the 1-minute extraction cron stops firing. The webhook (template injection) keeps working. Use this when you want to run the pipeline manually from the CLI without competing with Lambda for the same meetings.

- `./scripts/pause-lambda.sh` — disables the rule (instant, no redeploy)
- `./scripts/resume-lambda.sh` — re-enables the rule

While paused, live AWS state drifts from `template.yaml` (which still says `Enabled: true`). The next full `./scripts/deploy.sh` resets the rule to enabled, so always resume before deploying — or know that deploy will resume it for you.

## Manual CLI runs (debug / experiment mode)

`python -m src.main --sync` accepts overrides for fine-grained control. Useful when you've paused the Lambda and want to step through a fixed set of meetings.

| Flag | Effect |
|------|--------|
| `--db-id <notion_db_id>` | Process exactly one Meeting Notes DB (skips Org Chart discovery). Equivalent to setting `MEETING_NOTES_DB_ID` in `.env`. |
| `--correction-model <model>` | Override the model for transcript correction. |
| `--extraction-model <model>` | Override the model for task extraction. |
| `--classification-model <model>` | Override the model for task classification. |
| `--auto-extract-tasks` | Force every page in the run onto the transcript pipeline (ignores per-row Org Chart flag). Debugging only. |
| `--no-auto-extract-tasks` | Force every page onto the literal-notes path. Debugging only. |
| `--verbose` | DEBUG-level logs for `src.*` (third-party loggers stay at WARNING via `setup_logging`). Dumps the corrected transcript and raw LLM response payloads. |

**Provider auto-detection** (checked in this order):
- model name contains `/` → OpenRouter slug (e.g. `google/gemini-2.5-flash-preview`, `deepseek/deepseek-chat-v3.1:free`). Routes via `OPENROUTER_API_KEY` + `OPENROUTER_BASE_URL`. Useful as a failover when Google returns 503 UNAVAILABLE on the merged Gemini call. Note: routing through OpenRouter loses Gemini's native context cache (~25% input-token discount).
- model name starts with `gemini-` → Google Gemini direct via `GEMINI_API_KEY` + `GEMINI_BASE_URL`. Only this path can use the native context cache.
- otherwise → OpenAI direct via `OPENAI_API_KEY` + the hardcoded OpenAI base URL.

So `--classification-model gemini-3-flash-preview`, `--correction-model gpt-5-mini`, or `--extraction-model google/gemini-2.5-flash-preview` all Just Work.

Default INFO output reads as a clear pipeline trace: per-meeting framing, then one log line per stage with model + elapsed + token counts. Embedding model (`text-embedding-3-small`) and fundraising-summary model are not exposed as flags — those are not interesting for the dev/test loop.

## Watch vs one-shot mode

```bash
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

# One-shot: run the Done-task archive sweep (mirrors the weekly Sunday Lambda job)
python -m src.main --archive
python -m src.main --archive --dry-run --verbose
```

See `docs/architecture.md` for the weekly Done-task archive sweep behaviour and the Lambda entry points (`webhook_handler`, `extraction_handler`, `_handle_weekly_archive`).
