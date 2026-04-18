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

HOST_METRICS_DIR="${ROOT_DIR}/data/metrics"
REQ_FILE="${HOST_METRICS_DIR}/chat_metrics_requests.jsonl"
AGG_FILE="${HOST_METRICS_DIR}/chat_metrics_aggregate.json"
TAIL_COUNT="${TAIL_COUNT:-3}"

section() {
  local title="${1}"
  printf "\n========== %s ==========\n" "${title}"
}

section "Metrics Files (Container)"
"${COMPOSE_CMD[@]}" "${COMPOSE_ARGS[@]}" exec -T gradio sh -lc "ls -lah /app/data/metrics || true"

section "Metrics Files (Host)"
ls -lah "${HOST_METRICS_DIR}" || true

if [[ ! -f "${REQ_FILE}" && ! -f "${AGG_FILE}" ]]; then
  section "No Metrics Yet"
  echo "[local-metrics] No metrics files found in ${HOST_METRICS_DIR}."
  echo "[local-metrics] Send a chat request first, then re-run."
  exit 0
fi

if command -v jq >/dev/null 2>&1; then
  if [[ -f "${REQ_FILE}" ]]; then
    section "Last ${TAIL_COUNT} Requests (Summary)"
    tail -n "${TAIL_COUNT}" "${REQ_FILE}" \
      | jq -s '
          [
            .[] | {
              timestamp,
              request_id,
              outcome,
              overall_ms: .timings_ms.overall_response_ms,
              llm_ms: .timings_ms.llm_response_ms,
              retrieval_strategy: .retrieval.strategy,
              retrieval_results: .retrieval.result_count,
              question
            }
          ]
        '
  fi

  if [[ -f "${AGG_FILE}" ]]; then
    section "Aggregate Metrics (Readable)"
    jq '
      {
        updated_at,
        total_requests,
        outcomes,
        latency_ms: (
          .latency_ms
          | with_entries(.value |= {
              count,
              average,
              p95,
              p99,
              max
            })
        ),
        token_usage,
        latest_request
      }
    ' "${AGG_FILE}"
  fi
else
  section "jq Not Found"
  echo "[local-metrics] jq is not installed; falling back to raw output."
  if [[ -f "${REQ_FILE}" ]]; then
    section "Last ${TAIL_COUNT} Requests (Raw)"
    tail -n "${TAIL_COUNT}" "${REQ_FILE}"
  fi
  if [[ -f "${AGG_FILE}" ]]; then
    section "Aggregate Metrics (Raw)"
    cat "${AGG_FILE}"
  fi
fi
