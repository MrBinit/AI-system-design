#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"
export GRADIO_PORT="${GRADIO_PORT:-7860}"
export ENV_FILE="${ENV_FILE:-}"

declare -a COMPOSE_CMD=(docker compose)
if [[ -n "${ENV_FILE}" && -f "${ROOT_DIR}/${ENV_FILE}" ]]; then
  COMPOSE_CMD+=(--env-file "${ENV_FILE}")
fi

COMPOSE_ARGS=(
  -f docker-compose.yml
  -f docker-compose.local.yml
  --profile llm-async
  --profile eval-queue
  --profile metrics-queue
)

echo "[local-down] Stopping local stack..."
"${COMPOSE_CMD[@]}" "${COMPOSE_ARGS[@]}" down --remove-orphans
echo "[local-down] Done."
