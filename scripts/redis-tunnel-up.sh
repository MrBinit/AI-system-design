#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"
export ENV_FILE="${ENV_FILE:-}"
export AWS_SECRETS_MANAGER_SECRET_ID="${AWS_SECRETS_MANAGER_SECRET_ID:-unigraph/prod/app}"
export AWS_SECRETS_MANAGER_REGION="${AWS_SECRETS_MANAGER_REGION:-us-east-1}"
if [[ -d "${HOME}/.local/bin" ]]; then
  export PATH="${HOME}/.local/bin:${PATH}"
fi

if [[ -z "${ENV_FILE}" || ! -f "${ROOT_DIR}/${ENV_FILE}" ]]; then
  echo "[redis-tunnel] No env file configured; using process env + AWS secrets only."
fi

if [[ "${AWS_SECRETS_AUTO_EXPORT:-1}" == "1" ]]; then
  echo "[redis-tunnel] Loading runtime secrets from AWS Secrets Manager (${AWS_SECRETS_MANAGER_SECRET_ID}, ${AWS_SECRETS_MANAGER_REGION})..."
  if SECRET_EXPORTS="$("${ROOT_DIR}/scripts/aws_secrets_to_env.sh" --export 2>/tmp/redis-tunnel-aws-secrets.err)"; then
    eval "${SECRET_EXPORTS}"
    echo "[redis-tunnel] Loaded ${__AWS_SECRET_KEYS_LOADED:-0} keys from AWS secret payload."
    unset __AWS_SECRET_KEYS_LOADED
  else
    echo "[redis-tunnel] WARNING: failed to load AWS secrets; continuing with existing env."
    cat /tmp/redis-tunnel-aws-secrets.err
    if [[ "${AWS_SECRETS_STRICT:-0}" == "1" ]]; then
      echo "[redis-tunnel] AWS_SECRETS_STRICT=1 so startup is aborted."
      exit 1
    fi
  fi
fi

if REDIS_TUNNEL_DEFAULTS="$(APP_CONFIG_OVERRIDE_DIR="" "${ROOT_DIR}/venv/bin/python" - <<'PY'
import os
import shlex

from app.core.config import get_settings

redis = get_settings().redis
exports = {}
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

if ! command -v aws >/dev/null 2>&1; then
  echo "[redis-tunnel] aws CLI is required."
  exit 1
fi

if ! command -v session-manager-plugin >/dev/null 2>&1; then
  echo "[redis-tunnel] session-manager-plugin is required."
  echo "[redis-tunnel] Install it system-wide or place binary at ~/.local/bin/session-manager-plugin"
  exit 1
fi

INSTANCE_ID="${REDIS_TUNNEL_INSTANCE_ID:-}"
if [[ -z "${INSTANCE_ID}" ]]; then
  echo "[redis-tunnel] REDIS_TUNNEL_INSTANCE_ID is required (EC2 instance managed by SSM in your VPC)."
  exit 1
fi

REMOTE_HOST="$(APP_CONFIG_OVERRIDE_DIR="" "${ROOT_DIR}/venv/bin/python" - <<'PY'
from app.core.config import get_settings
print(get_settings().redis.app.host)
PY
)"
REMOTE_HOST="${REDIS_TUNNEL_REMOTE_HOST:-${REMOTE_HOST}}"
REMOTE_PORT="${REDIS_TUNNEL_REMOTE_PORT:-6379}"
LOCAL_PORT="${REDIS_TUNNEL_LOCAL_PORT:-6380}"

if [[ -z "${REMOTE_HOST}" ]]; then
  echo "[redis-tunnel] Could not resolve Redis remote host."
  exit 1
fi

echo "[redis-tunnel] Starting SSM tunnel: localhost:${LOCAL_PORT} -> ${REMOTE_HOST}:${REMOTE_PORT}"
echo "[redis-tunnel] Keep this session open while running ./scripts/local-up.sh with REDIS_LOCAL_TUNNEL_ENABLED=true"

aws ssm start-session \
  --target "${INSTANCE_ID}" \
  --region "${AWS_SECRETS_MANAGER_REGION}" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"${REMOTE_HOST}\"],\"portNumber\":[\"${REMOTE_PORT}\"],\"localPortNumber\":[\"${LOCAL_PORT}\"]}"
