#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"
export LOCAL_REDIS_PORT="${LOCAL_REDIS_PORT:-6380}"
export GRADIO_PORT="${GRADIO_PORT:-7860}"
export ENV_FILE="${ENV_FILE:-.env.local}"

if [[ ! -f "${ROOT_DIR}/${ENV_FILE}" ]]; then
  if [[ -f "${ROOT_DIR}/.env" ]]; then
    echo "[local-metrics] ${ENV_FILE} not found; falling back to .env"
    ENV_FILE=".env"
  else
    echo "[local-metrics] Missing env file: ${ENV_FILE}"
    exit 1
  fi
fi

COMPOSE_ARGS=(
  -f docker-compose.yml
  -f docker-compose.local.yml
  --profile local-redis
)

echo "[local-metrics] Metrics files in gradio container:"
docker compose --env-file "${ENV_FILE}" "${COMPOSE_ARGS[@]}" exec -T gradio sh -lc "ls -la /app/data/metrics"

echo "[local-metrics] Metrics files on host:"
ls -la "${ROOT_DIR}/data/metrics"

echo "[local-metrics] Last 3 request metrics:"
docker compose --env-file "${ENV_FILE}" "${COMPOSE_ARGS[@]}" exec -T gradio sh -lc "tail -n 3 /app/data/metrics/chat_metrics_requests.jsonl"

echo "[local-metrics] Aggregate metrics snapshot:"
docker compose --env-file "${ENV_FILE}" "${COMPOSE_ARGS[@]}" exec -T gradio sh -lc "cat /app/data/metrics/chat_metrics_aggregate.json"
