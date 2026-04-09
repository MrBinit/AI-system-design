#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./scripts/git-branch-push.sh dev "<commit message>"
  ./scripts/git-branch-push.sh main
  ./scripts/git-branch-push.sh

Modes:
  dev   -> git add . && git commit -m "<message>" && git push origin dev
  main  -> git fetch origin && git pull origin main && git merge dev && git push origin main
  (no args) -> interactive prompt for mode and message
EOF
}

require_branch() {
  local expected="$1"
  local current
  current="$(git rev-parse --abbrev-ref HEAD)"
  if [[ "${current}" != "${expected}" ]]; then
    echo "Error: current branch is '${current}'. Switch to '${expected}' and retry."
    exit 1
  fi
}

mode="${1:-}"

if [[ "${mode}" == "-h" || "${mode}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ -z "${mode}" ]]; then
  if [[ ! -t 0 ]]; then
    usage
    exit 1
  fi
  echo "Select mode: dev or main"
  read -r -p "> " mode
fi

case "${mode}" in
  dev)
    commit_message="${2:-}"
    if [[ -z "${commit_message}" ]]; then
      if [[ -t 0 ]]; then
        read -r -p "Commit message: " commit_message
      fi
    fi
    if [[ -z "${commit_message}" ]]; then
      echo "Error: commit message is required for 'dev' mode."
      usage
      exit 1
    fi

    require_branch "dev"
    git add .

    if git diff --cached --quiet; then
      echo "No staged changes found after 'git add .'. Nothing to commit."
      exit 0
    fi

    git commit -m "${commit_message}"
    git push origin dev
    ;;

  main)
    if [[ -t 0 ]]; then
      read -r -p "This will merge dev into main and push. Continue? [y/N] " confirm
      case "${confirm}" in
        y|Y|yes|YES) ;;
        *)
          echo "Canceled."
          exit 0
          ;;
      esac
    fi
    require_branch "main"
    git fetch origin
    git pull origin main
    git merge dev
    git push origin main
    ;;

  *)
    usage
    exit 1
    ;;
esac
