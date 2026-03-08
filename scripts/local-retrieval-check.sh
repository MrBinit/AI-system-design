#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"
export LOCAL_REDIS_PORT="${LOCAL_REDIS_PORT:-6380}"
export GRADIO_PORT="${GRADIO_PORT:-7860}"
export ENV_FILE="${ENV_FILE:-.env.local}"

if [[ ! -f "${ROOT_DIR}/${ENV_FILE}" ]]; then
  if [[ -f "${ROOT_DIR}/.env" ]]; then
    echo "[retrieval-check] ${ENV_FILE} not found; falling back to .env"
    ENV_FILE=".env"
  else
    echo "[retrieval-check] Missing env file: ${ENV_FILE}"
    exit 1
  fi
fi

COMPOSE_ARGS=(
  -f docker-compose.yml
  -f docker-compose.local.yml
  --profile local-redis
)

echo "[retrieval-check] Runtime flags from api container:"
docker compose --env-file "${ENV_FILE}" "${COMPOSE_ARGS[@]}" exec -T api sh -lc \
  'env | grep -E "^(POSTGRES_ENABLED|AWS_ACCESS_KEY_ID|AWS_REGION|AWS_DEFAULT_REGION)=" | sort'

echo "[retrieval-check] Postgres connectivity:"
docker compose --env-file "${ENV_FILE}" "${COMPOSE_ARGS[@]}" exec -T api python -m app.scripts.check_postgres

echo "[retrieval-check] Retrieval pipeline smoke:"
docker compose --env-file "${ENV_FILE}" "${COMPOSE_ARGS[@]}" exec -T api python - <<'PY'
from app.services.retrieval_service import retrieve_document_chunks

try:
    result = retrieve_document_chunks("germany ai universities", top_k=1)
    print("retrieval_ok=true")
    print(f"retrieval_strategy={result.get('retrieval_strategy')}")
    print(f"retrieved_count={len(result.get('results', []))}")
except Exception as exc:
    print("retrieval_ok=false")
    print(f"error_type={type(exc).__name__}")
    print(f"error={exc}")
PY
