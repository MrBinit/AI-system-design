#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-summary}"
if [[ "${MODE}" != "summary" && "${MODE}" != "--export" ]]; then
  echo "Usage: ./scripts/aws_secrets_to_env.sh [--export]" >&2
  exit 1
fi

SECRET_ID="${AWS_SECRETS_MANAGER_SECRET_ID:-unigraph/prod/app}"
REGION="${AWS_SECRETS_MANAGER_REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}}"

if [[ -z "${SECRET_ID}" ]]; then
  echo "[aws-secrets-to-env] AWS_SECRETS_MANAGER_SECRET_ID is required." >&2
  exit 1
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "[aws-secrets-to-env] aws CLI is required." >&2
  exit 1
fi

PYTHON_BIN="${ROOT_DIR}/venv/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

SECRET_STRING="$(
  aws secretsmanager get-secret-value \
    --secret-id "${SECRET_ID}" \
    --region "${REGION}" \
    --query SecretString \
    --output text
)"

"${PYTHON_BIN}" - <<'PY' "${MODE}" "${SECRET_STRING}"
import json
import re
import shlex
import sys

mode = str(sys.argv[1]) if len(sys.argv) > 1 else "summary"
secret_string = str(sys.argv[2]) if len(sys.argv) > 2 else ""
if not secret_string:
    raise SystemExit("Missing SecretString payload.")

payload = json.loads(secret_string)
if not isinstance(payload, dict):
    raise SystemExit("SecretString payload must be a JSON object.")

key_pattern = re.compile(r"^[A-Z][A-Z0-9_]*$")
loaded = 0
keys: list[str] = []

for key in sorted(payload.keys()):
    if not isinstance(key, str):
        continue
    key = key.strip()
    if not key or not key_pattern.match(key):
        continue
    value = payload.get(key)
    if value is None:
        continue
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, float, str)):
        text = str(value)
    else:
        continue
    if not text.strip():
        continue
    keys.append(key)
    if mode == "--export":
        print(f"export {key}={shlex.quote(text)}")
    loaded += 1

if mode == "--export":
    print(f"export __AWS_SECRET_KEYS_LOADED={loaded}")
else:
    print(f"loaded_keys={loaded}")
    if keys:
        print("keys=" + ",".join(keys))
PY
