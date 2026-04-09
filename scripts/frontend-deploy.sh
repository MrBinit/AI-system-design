#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="${ROOT_DIR}/frontend"

AWS_REGION="${AWS_REGION:-us-east-1}"
S3_BUCKET="${S3_BUCKET:-unigraph-frontend}"
CLOUDFRONT_DISTRIBUTION_ID="${CLOUDFRONT_DISTRIBUTION_ID:-EILC5TTHBL1C3}"

usage() {
  cat <<EOF
Usage:
  ./scripts/frontend-deploy.sh [--bucket <s3-bucket>] [--distribution <cloudfront-id>] [--region <aws-region>]

Defaults:
  bucket:        ${S3_BUCKET}
  distribution:  ${CLOUDFRONT_DISTRIBUTION_ID}
  region:        ${AWS_REGION}

Examples:
  ./scripts/frontend-deploy.sh
  ./scripts/frontend-deploy.sh --bucket unigraph-frontend --distribution EILC5TTHBL1C3
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bucket)
      S3_BUCKET="${2:-}"
      shift 2
      ;;
    --distribution)
      CLOUDFRONT_DISTRIBUTION_ID="${2:-}"
      shift 2
      ;;
    --region)
      AWS_REGION="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ -z "${S3_BUCKET}" || -z "${CLOUDFRONT_DISTRIBUTION_ID}" || -z "${AWS_REGION}" ]]; then
  echo "Error: bucket, distribution, and region are required."
  usage
  exit 1
fi

command -v aws >/dev/null 2>&1 || { echo "aws CLI not found"; exit 1; }
command -v npm >/dev/null 2>&1 || { echo "npm not found"; exit 1; }

echo "Building frontend..."
cd "${FRONTEND_DIR}"
npm ci
npm run build

echo "Uploading build to s3://${S3_BUCKET}..."
aws s3 sync dist "s3://${S3_BUCKET}" \
  --delete \
  --cache-control "public,max-age=300" \
  --region "${AWS_REGION}"

aws s3 cp dist/index.html "s3://${S3_BUCKET}/index.html" \
  --cache-control "no-cache, no-store, must-revalidate" \
  --content-type "text/html; charset=utf-8" \
  --region "${AWS_REGION}"

if [[ -d "dist/assets" ]]; then
  aws s3 cp dist/assets "s3://${S3_BUCKET}/assets" \
    --recursive \
    --cache-control "public,max-age=31536000,immutable" \
    --region "${AWS_REGION}"
fi

echo "Creating CloudFront invalidation..."
invalidation_id="$(
  aws cloudfront create-invalidation \
    --distribution-id "${CLOUDFRONT_DISTRIBUTION_ID}" \
    --paths "/*" \
    --query "Invalidation.Id" \
    --output text \
    --region "${AWS_REGION}"
)"

echo "Deploy complete. Invalidation ID: ${invalidation_id}"
