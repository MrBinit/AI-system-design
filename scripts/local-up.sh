#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"
export GRADIO_PORT="${GRADIO_PORT:-7860}"
export ENV_FILE="${ENV_FILE:-.env.local}"
export AWS_SECRETS_MANAGER_SECRET_ID="${AWS_SECRETS_MANAGER_SECRET_ID:-unigraph/prod/app}"
export AWS_SECRETS_MANAGER_REGION="${AWS_SECRETS_MANAGER_REGION:-us-east-1}"
export APP_CONFIG_OVERRIDE_DIR="${APP_CONFIG_OVERRIDE_DIR:-${ROOT_DIR}/app/config/local}"
mkdir -p "${ROOT_DIR}/data/metrics"

if [[ ! -f "${ROOT_DIR}/${ENV_FILE}" ]]; then
  if [[ -f "${ROOT_DIR}/.env" ]]; then
    echo "[local-up] ${ENV_FILE} not found; falling back to .env"
    ENV_FILE=".env"
  else
    echo "[local-up] Missing env file: ${ENV_FILE}"
    exit 1
  fi
fi

if [[ "${AWS_SECRETS_AUTO_EXPORT:-1}" == "1" ]]; then
  echo "[local-up] Loading runtime secrets from AWS Secrets Manager (${AWS_SECRETS_MANAGER_SECRET_ID}, ${AWS_SECRETS_MANAGER_REGION})..."
  if SECRET_EXPORTS="$("${ROOT_DIR}/scripts/aws_secrets_to_env.sh" --export 2>/tmp/local-up-aws-secrets.err)"; then
    eval "${SECRET_EXPORTS}"
    echo "[local-up] Loaded ${__AWS_SECRET_KEYS_LOADED:-0} keys from AWS secret payload."
    unset __AWS_SECRET_KEYS_LOADED
  else
    echo "[local-up] WARNING: failed to load AWS secrets; continuing with existing env."
    cat /tmp/local-up-aws-secrets.err
    if [[ "${AWS_SECRETS_STRICT:-0}" == "1" ]]; then
      echo "[local-up] AWS_SECRETS_STRICT=1 so startup is aborted."
      exit 1
    fi
  fi
fi

if REDIS_TUNNEL_DEFAULTS="$("${ROOT_DIR}/venv/bin/python" - <<'PY'
import os
import shlex

from app.core.config import get_settings

redis = get_settings().redis
exports = {}
if os.getenv("REDIS_LOCAL_TUNNEL_ENABLED", "") == "":
    exports["REDIS_LOCAL_TUNNEL_ENABLED"] = "true" if redis.local_tunnel_enabled else "false"
if os.getenv("REDIS_TUNNEL_LOCAL_PORT", "") == "":
    exports["REDIS_TUNNEL_LOCAL_PORT"] = str(redis.tunnel_local_port)
if os.getenv("REDIS_TUNNEL_INSTANCE_ID", "") == "" and redis.tunnel_instance_id.strip():
    exports["REDIS_TUNNEL_INSTANCE_ID"] = redis.tunnel_instance_id.strip()

for key, value in exports.items():
    print(f"export {key}={shlex.quote(value)}")
PY
)"; then
  if [[ -n "${REDIS_TUNNEL_DEFAULTS}" ]]; then
    eval "${REDIS_TUNNEL_DEFAULTS}"
  fi
fi

case "$(printf '%s' "${REDIS_LOCAL_TUNNEL_ENABLED:-0}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on)
    export REDIS_TUNNEL_LOCAL_PORT="${REDIS_TUNNEL_LOCAL_PORT:-6380}"
    export REDIS_APP_HOST="${REDIS_APP_HOST:-host.docker.internal}"
    export REDIS_WORKER_HOST="${REDIS_WORKER_HOST:-host.docker.internal}"
    export REDIS_APP_PORT="${REDIS_APP_PORT:-${REDIS_TUNNEL_LOCAL_PORT}}"
    export REDIS_WORKER_PORT="${REDIS_WORKER_PORT:-${REDIS_TUNNEL_LOCAL_PORT}}"
    export REDIS_APP_TLS="${REDIS_APP_TLS:-true}"
    export REDIS_WORKER_TLS="${REDIS_WORKER_TLS:-true}"
    export REDIS_APP_SSL_CERT_REQS="${REDIS_APP_SSL_CERT_REQS:-required}"
    export REDIS_WORKER_SSL_CERT_REQS="${REDIS_WORKER_SSL_CERT_REQS:-required}"
    echo "[local-up] Redis local tunnel mode enabled (host=${REDIS_APP_HOST}, port=${REDIS_APP_PORT})."
    echo "[local-up] Ensure tunnel is running: ./scripts/redis-tunnel-up.sh"
    ;;
esac

echo "[local-up] Validating Redis connectivity..."
if ! "${ROOT_DIR}/venv/bin/python" - <<'PY'
import socket
import sys

from app.core.config import get_settings

settings = get_settings()
local_tunnel_enabled = bool(settings.redis.local_tunnel_enabled)
allow_local_redis = bool(settings.redis.allow_local_redis)
targets = [
    (str(settings.redis.app.host).strip(), int(settings.redis.app.port)),
    (str(settings.redis.worker.host).strip(), int(settings.redis.worker.port)),
]

if allow_local_redis and not local_tunnel_enabled:
    print("[local-up] Local Redis mode enabled via YAML override; skipping host preflight.")
    sys.exit(0)

for host, port in sorted(set(targets)):
    probe_host = host
    if host.strip().lower() in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}:
        probe_host = "127.0.0.1"
    try:
        with socket.create_connection((probe_host, port), timeout=4):
            pass
        print(f"[local-up] Redis reachable: {host}:{port}")
    except Exception as exc:
        print(f"[local-up] ERROR: cannot reach Redis {host}:{port} -> {type(exc).__name__}: {exc}")
        if local_tunnel_enabled:
            print(
                "[local-up] Hint: start local tunnel first (./scripts/redis-tunnel-up.sh) "
                "and keep it running in another terminal."
            )
        else:
            print(
                "[local-up] Hint: check Redis host/port/TLS settings. "
                "ElastiCache is VPC-private and needs tunnel/VPN/peering from laptop."
            )
        sys.exit(1)
PY
then
  exit 1
fi

COMPOSE_ARGS=(
  -f docker-compose.yml
  -f docker-compose.local.yml
  --profile llm-async
  --profile eval-queue
  --profile metrics-queue
)

echo "[local-up] Building images..."
docker compose --env-file "${ENV_FILE}" "${COMPOSE_ARGS[@]}" build api gradio

echo "[local-up] Starting redis/api/worker/llm-worker/eval-worker/metrics-worker/gradio (gradio:${GRADIO_PORT})..."
docker compose --env-file "${ENV_FILE}" "${COMPOSE_ARGS[@]}" up -d --no-build redis api worker llm-worker eval-worker metrics-worker gradio
echo "[local-up] Started."
echo "[local-up] API:    http://127.0.0.1:${API_PORT:-8000}"
echo "[local-up] Gradio: http://127.0.0.1:${GRADIO_PORT}"
echo "[local-up] Metrics: ${ROOT_DIR}/data/metrics"
echo "[local-up] ENV:    ${ENV_FILE}"
echo "[local-up] Next:   ./scripts/local-smoke.sh --chat"
