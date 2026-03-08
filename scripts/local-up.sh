#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"
export LOCAL_REDIS_PORT="${LOCAL_REDIS_PORT:-6380}"
export GRADIO_PORT="${GRADIO_PORT:-7860}"

COMPOSE_ARGS=(
  -f docker-compose.yml
  -f docker-compose.local.yml
  --profile local-redis
)

echo "[local-up] Building images..."
docker compose "${COMPOSE_ARGS[@]}" build api gradio

echo "[local-up] Starting api/worker/redis/gradio (redis:${LOCAL_REDIS_PORT}, gradio:${GRADIO_PORT})..."
docker compose "${COMPOSE_ARGS[@]}" up -d --no-build api worker redis gradio
echo "[local-up] Started."
echo "[local-up] API:    http://127.0.0.1:${API_PORT:-8000}"
echo "[local-up] Gradio: http://127.0.0.1:${GRADIO_PORT}"
echo "[local-up] Next:   ./scripts/local-smoke.sh --chat"
