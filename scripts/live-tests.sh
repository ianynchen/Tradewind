#!/usr/bin/env bash
# Run credentialed live/integration tests. Reads secrets from .env (git-ignored).
# Usage: bash scripts/live-tests.sh [pytest args...]   (default: langchain live tests)
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi
args=("$@")
[[ ${#args[@]} -eq 0 ]] && args=(tests/integration/test_langchain_live.py -v)
exec uv run pytest -m integration "${args[@]}"
