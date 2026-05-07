#!/usr/bin/env bash
# Deploy Nzyme Lambda functions using AWS SAM.
#
# Prerequisites:
#   - AWS CLI configured (aws configure)
#   - AWS SAM CLI installed (pip install aws-sam-cli)
#   - .env file with all required variables
#
# Usage:
#   ./scripts/deploy.sh                         # guided deploy (first time)
#   ./scripts/deploy.sh --no-confirm-changeset  # non-interactive re-deploy

set -euo pipefail
cd "$(dirname "$0")/.."

# Load .env for parameter values
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Build the --parameter-overrides list. Empty values are skipped — newer SAM
# CLI versions reject `Key=` with no value, and omitting a parameter causes
# CloudFormation to fall back to the Default declared in template.yaml.
PARAMS=()
add_param() {
    local key="$1"
    local value="$2"
    if [ -n "$value" ]; then
        PARAMS+=("${key}=${value}")
    fi
}

# Required parameters (no defaults in template.yaml — must always be set)
add_param "NotionApiToken"    "${NOTION_API_TOKEN}"
add_param "OpenAIApiKey"      "${OPENAI_API_KEY}"
add_param "TeamTrackerDbId"   "${TEAM_TRACKER_DB_ID}"
add_param "SystemPromptPageId" "${SYSTEM_PROMPT_PAGE_ID}"
add_param "UserPromptPageId"  "${USER_PROMPT_PAGE_ID}"
add_param "WebhookPathToken"  "${WEBHOOK_PATH_TOKEN}"

# Optional parameters (template.yaml supplies the Default when omitted)
add_param "MeetingTemplatePageId"          "${MEETING_TEMPLATE_PAGE_ID:-}"
add_param "InjectTemplate"                 "${INJECT_TEMPLATE:-}"
add_param "IdleMinutes"                    "${IDLE_MINUTES:-}"
add_param "LogLevel"                       "${LOG_LEVEL:-}"
add_param "DryRun"                         "${DRY_RUN:-}"
add_param "IncludeAINotes"                 "${INCLUDE_AI_NOTES:-}"
add_param "BufferHours"                    "${BUFFER_HOURS:-}"
add_param "DealWorkplansDbId"              "${DEAL_WORKPLANS_DB_ID:-}"
add_param "SemanticDedupThreshold"         "${SEMANTIC_DEDUP_THRESHOLD:-}"
add_param "GoogleServiceAccountSecretArn"  "${GOOGLE_SERVICE_ACCOUNT_SECRET_ARN:-}"
add_param "GCalDelegatedUserDefault"       "${GCAL_DELEGATED_USER_DEFAULT:-}"
add_param "GeminiApiKey"                   "${GEMINI_API_KEY:-}"
add_param "GeminiModel"                    "${GEMINI_MODEL:-}"
add_param "GeminiBaseUrl"                  "${GEMINI_BASE_URL:-}"
add_param "OpenAIModel"                    "${OPENAI_MODEL:-}"
add_param "TerminologyDbId"                "${TERMINOLOGY_DB_ID:-}"
add_param "OrgChartDbId"                   "${ORG_CHART_DB_ID:-}"
add_param "ClassifierPromptPageId"         "${CLASSIFIER_PROMPT_PAGE_ID:-}"
add_param "LogfireToken"                   "${LOGFIRE_TOKEN:-}"
add_param "FundraisingBranchEnabled"       "${FUNDRAISING_BRANCH_ENABLED:-}"
add_param "AffinityApiKey"                 "${AFFINITY_API_KEY:-}"
add_param "AffinityLpFunnelListId"         "${AFFINITY_LP_FUNNEL_LIST_ID:-}"

echo "=== Building SAM application ==="
sam build

echo "=== Deploying to AWS ==="
sam deploy \
    --stack-name nzyme-task-tracker \
    --capabilities CAPABILITY_IAM \
    --resolve-s3 \
    --parameter-overrides "${PARAMS[@]}" \
    "$@"

echo ""
echo "=== Deployment complete ==="
echo "Run 'sam list stack-outputs --stack-name nzyme-task-tracker' to see the webhook URL."
