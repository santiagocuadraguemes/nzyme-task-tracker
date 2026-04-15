#!/usr/bin/env bash
# Deploy Nzyme Lambda functions using AWS SAM.
#
# Prerequisites:
#   - AWS CLI configured (aws configure)
#   - AWS SAM CLI installed (pip install aws-sam-cli)
#   - .env file with all required variables
#
# Usage:
#   ./scripts/deploy.sh                  # guided deploy (first time)
#   ./scripts/deploy.sh --no-confirm     # non-interactive re-deploy

set -euo pipefail
cd "$(dirname "$0")/.."

# Load .env for parameter values
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

echo "=== Building SAM application ==="
sam build

echo "=== Deploying to AWS ==="
sam deploy \
    --stack-name nzyme-task-tracker \
    --capabilities CAPABILITY_IAM \
    --resolve-s3 \
    --parameter-overrides \
        "NotionApiToken=${NOTION_API_TOKEN}" \
        "OpenAIApiKey=${OPENAI_API_KEY}" \
        "MeetingNotesDbId=${MEETING_NOTES_DB_ID}" \
        "TeamTrackerDbId=${TEAM_TRACKER_DB_ID}" \
        "SystemPromptPageId=${SYSTEM_PROMPT_PAGE_ID}" \
        "UserPromptPageId=${USER_PROMPT_PAGE_ID}" \
        "MeetingTemplatePageId=${MEETING_TEMPLATE_PAGE_ID:-}" \
        "InjectTemplate=${INJECT_TEMPLATE:-true}" \
        "WebhookPathToken=${WEBHOOK_PATH_TOKEN}" \
        "IdleMinutes=${IDLE_MINUTES:-3}" \
        "LogLevel=${LOG_LEVEL:-INFO}" \
        "DryRun=${DRY_RUN:-false}" \
        "IncludeAINotes=${INCLUDE_AI_NOTES:-false}" \
        "BufferHours=${BUFFER_HOURS:-2}" \
        "DealWorkplansDbId=${DEAL_WORKPLANS_DB_ID:-}" \
        "SemanticDedupThreshold=${SEMANTIC_DEDUP_THRESHOLD:-0.85}" \
    "$@"

echo ""
echo "=== Deployment complete ==="
echo "Run 'sam list stack-outputs --stack-name nzyme-task-tracker' to see the webhook URL."
