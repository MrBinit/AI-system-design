#!/usr/bin/env bash
set -euo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"

if ! command -v aws >/dev/null 2>&1; then
  echo "aws CLI is required." >&2
  exit 1
fi

echo "[bedrock-quota-report] region=${REGION}"
echo "[bedrock-quota-report] listing Service Quotas for bedrock..."

aws service-quotas list-service-quotas \
  --service-code bedrock \
  --region "${REGION}" \
  --output table

echo
echo "To request an increase (replace placeholders):"
echo "aws service-quotas request-service-quota-increase --service-code bedrock --quota-code <QUOTA_CODE> --desired-value <VALUE> --region ${REGION}"
