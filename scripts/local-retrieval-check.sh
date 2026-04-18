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
)

echo "[retrieval-check] Runtime flags from api container:"
"${COMPOSE_CMD[@]}" "${COMPOSE_ARGS[@]}" exec -T api sh -lc \
  'env | grep -E "^(POSTGRES_ENABLED|AWS_ACCESS_KEY_ID|AWS_REGION|AWS_DEFAULT_REGION)=" | sort'

echo "[retrieval-check] Postgres connectivity:"
"${COMPOSE_CMD[@]}" "${COMPOSE_ARGS[@]}" exec -T api python -m app.scripts.check_postgres

echo "[retrieval-check] Retrieval pipeline smoke:"
"${COMPOSE_CMD[@]}" "${COMPOSE_ARGS[@]}" exec -T api python - <<'PY'
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
