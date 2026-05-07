#!/usr/bin/env bash
# Pause the Nzyme extraction cron without redeploying.
#
# Disables the EventBridge rule that SAM created for the ScheduledExtraction
# event. The webhook (template injection) keeps working — only the 1-minute
# extraction cron stops.
#
# Re-enable with: ./scripts/resume-lambda.sh
#
# Note: this drifts live AWS state from template.yaml (which still says
# Enabled: true). The next full ./scripts/deploy.sh will re-enable the rule.

set -euo pipefail

REGION="${AWS_REGION:-eu-west-1}"

RULE_NAME=$(aws events list-rules \
    --region "$REGION" \
    --query "Rules[?contains(Name,'NzymeFunctionScheduledExtraction')].Name" \
    --output text)

if [ -z "$RULE_NAME" ] || [ "$RULE_NAME" = "None" ]; then
    echo "ERROR: could not find an EventBridge rule matching" \
        "'NzymeFunctionScheduledExtraction' in region $REGION." >&2
    echo "Has the stack been deployed yet?" >&2
    exit 1
fi

echo "Found rule: $RULE_NAME"
aws events disable-rule --region "$REGION" --name "$RULE_NAME"

STATE=$(aws events describe-rule \
    --region "$REGION" --name "$RULE_NAME" \
    --query "State" --output text)

echo "Rule state: $STATE"
if [ "$STATE" != "DISABLED" ]; then
    echo "ERROR: expected DISABLED, got $STATE" >&2
    exit 1
fi

echo "Extraction cron paused. Webhook still active."
echo "Resume with: ./scripts/resume-lambda.sh"
