#!/usr/bin/env bash
# Resume the Nzyme extraction cron after a pause-lambda.sh.

set -euo pipefail

REGION="${AWS_REGION:-eu-west-1}"

RULE_NAME=$(aws events list-rules \
    --region "$REGION" \
    --query "Rules[?contains(Name,'NzymeFunctionScheduledExtraction')].Name" \
    --output text)

if [ -z "$RULE_NAME" ] || [ "$RULE_NAME" = "None" ]; then
    echo "ERROR: could not find an EventBridge rule matching" \
        "'NzymeFunctionScheduledExtraction' in region $REGION." >&2
    exit 1
fi

echo "Found rule: $RULE_NAME"
aws events enable-rule --region "$REGION" --name "$RULE_NAME"

STATE=$(aws events describe-rule \
    --region "$REGION" --name "$RULE_NAME" \
    --query "State" --output text)

echo "Rule state: $STATE"
if [ "$STATE" != "ENABLED" ]; then
    echo "ERROR: expected ENABLED, got $STATE" >&2
    exit 1
fi

echo "Extraction cron resumed. Next tick within 1 min."
