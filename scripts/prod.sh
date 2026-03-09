#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

COMPOSE_FILE="docker-compose.prod.yml"
DEFAULT_REGION="us-east-1"
DEFAULT_SECRET_ID="unigraph/prod/app"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/prod.sh up [--gradio]
  ./scripts/prod.sh down
  ./scripts/prod.sh logs [--gradio]
  ./scripts/prod.sh smoke [--gradio]
  ./scripts/prod.sh metrics [N]

Notes:
  - Automatically sets sane defaults for prod env vars.
  - Uses IAM role + aws cli to login to ECR before pull/up.
EOF
}

require_cmd() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "[prod] Missing required command: ${cmd}"
    exit 1
  fi
}

resolve_defaults() {
  export AWS_REGION="${AWS_REGION:-${DEFAULT_REGION}}"
  export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-${AWS_REGION}}"
  export AWS_SECRETS_MANAGER_SECRET_ID="${AWS_SECRETS_MANAGER_SECRET_ID:-${DEFAULT_SECRET_ID}}"
  export AWS_SECRETS_MANAGER_REGION="${AWS_SECRETS_MANAGER_REGION:-${AWS_REGION}}"
  export API_WORKERS="${API_WORKERS:-1}"

  require_cmd aws
  local account_id
  account_id="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
  if [[ -z "${account_id}" || "${account_id}" == "None" ]]; then
    echo "[prod] Could not resolve AWS account id via sts. Check IAM role / aws cli."
    exit 1
  fi

  export APP_IMAGE="${APP_IMAGE:-${account_id}.dkr.ecr.${AWS_REGION}.amazonaws.com/unigraph-app:latest}"
  export GRADIO_IMAGE="${GRADIO_IMAGE:-${account_id}.dkr.ecr.${AWS_REGION}.amazonaws.com/unigraph-gradio:latest}"
  export ECR_REGISTRY="${ECR_REGISTRY:-${APP_IMAGE%%/*}}"
}

docker_login_ecr() {
  require_cmd docker
  require_cmd aws
  echo "[prod] Logging into ECR registry ${ECR_REGISTRY}..."
  aws ecr get-login-password --region "${AWS_REGION}" \
    | docker login --username AWS --password-stdin "${ECR_REGISTRY}" >/dev/null
}

ensure_metrics_dir() {
  mkdir -p data/metrics
  chown "$(id -u):$(id -g)" data/metrics 2>/dev/null || true
  chmod 775 data/metrics 2>/dev/null || true
}

cmd_up() {
  local with_gradio="${1:-0}"
  resolve_defaults
  docker_login_ecr
  ensure_metrics_dir

  local services=(api worker)
  local profile_args=()
  if [[ "${with_gradio}" == "1" ]]; then
    services+=(gradio)
    profile_args=(--profile gradio)
  fi

  echo "[prod] Pulling images..."
  docker compose -f "${COMPOSE_FILE}" "${profile_args[@]}" pull "${services[@]}"

  echo "[prod] Starting services: ${services[*]}"
  docker compose -f "${COMPOSE_FILE}" "${profile_args[@]}" up -d "${services[@]}"

  echo "[prod] Service status:"
  docker compose -f "${COMPOSE_FILE}" "${profile_args[@]}" ps

  echo "[prod] API health:"
  if curl --max-time 10 -fsS "http://127.0.0.1:8000/healthz"; then
    echo
  else
    echo "[prod] API health check failed."
  fi

  if [[ "${with_gradio}" == "1" ]]; then
    echo "[prod] Gradio check:"
    if curl --max-time 10 -fsS "http://127.0.0.1:7860" >/dev/null; then
      echo "gradio_ok=true"
    else
      echo "gradio_ok=false"
    fi
  fi
}

cmd_down() {
  echo "[prod] Stopping stack..."
  docker compose -f "${COMPOSE_FILE}" down
}

cmd_logs() {
  local with_gradio="${1:-0}"
  if [[ "${with_gradio}" == "1" ]]; then
    docker compose -f "${COMPOSE_FILE}" --profile gradio logs -f api worker gradio
  else
    docker compose -f "${COMPOSE_FILE}" logs -f api worker
  fi
}

cmd_smoke() {
  local with_gradio="${1:-0}"
  echo "[prod] Checking API health..."
  curl --max-time 10 -fsS "http://127.0.0.1:8000/healthz"
  echo

  echo "[prod] Checking Redis from API container..."
  docker compose -f "${COMPOSE_FILE}" exec -T api python -c "from app.infra.redis_client import app_redis_client as c; print(c.ping())"

  if [[ "${with_gradio}" == "1" ]]; then
    echo "[prod] Checking Gradio endpoint..."
    curl --max-time 10 -fsS "http://127.0.0.1:7860" >/dev/null
    echo "gradio_ok=true"
  fi
}

cmd_metrics() {
  local tail_count="${1:-3}"
  if [[ -x "${ROOT_DIR}/scripts/prod-metrics.sh" ]]; then
    "${ROOT_DIR}/scripts/prod-metrics.sh" "${tail_count}"
    return
  fi

  echo "[prod] Metrics script not found; showing raw files."
  tail -n "${tail_count}" data/metrics/chat_metrics_requests.jsonl || true
  cat data/metrics/chat_metrics_aggregate.json || true
}

main() {
  local cmd="${1:-}"
  shift || true

  case "${cmd}" in
    up)
      if [[ "${1:-}" == "--gradio" ]]; then
        cmd_up 1
      else
        cmd_up 0
      fi
      ;;
    down)
      cmd_down
      ;;
    logs)
      if [[ "${1:-}" == "--gradio" ]]; then
        cmd_logs 1
      else
        cmd_logs 0
      fi
      ;;
    smoke)
      if [[ "${1:-}" == "--gradio" ]]; then
        cmd_smoke 1
      else
        cmd_smoke 0
      fi
      ;;
    metrics)
      cmd_metrics "${1:-3}"
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

main "$@"
