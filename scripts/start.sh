#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${WORKSPACE_MANAGER_ENV_FILE:-$project_dir/.env}"
if [[ -f "$env_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
fi

host="${WORKSPACE_MANAGER_HOST:-127.0.0.1}"
port="${WORKSPACE_MANAGER_PORT:-8765}"
if [[ "$host" != "127.0.0.1" ]]; then
  echo "REFUSED: WORKSPACE_MANAGER_HOST must be 127.0.0.1 (got: $host)" >&2
  exit 2
fi
if [[ "$port" != "8765" ]]; then
  echo "REFUSED: WORKSPACE_MANAGER_PORT must be 8765 (got: $port)" >&2
  exit 2
fi

health_url="http://127.0.0.1:8765/"
body="$(mktemp)"
trap 'rm -f "$body"' EXIT
if curl --connect-timeout 1 --max-time 2 -fsS "$health_url" -o "$body" 2>/dev/null; then
  if grep -q '<title>ProjectFlow Workspace Manager</title>' "$body"; then
    echo "ALREADY_RUNNING: ProjectFlow Workspace Manager is healthy at $health_url"
    exit 0
  fi
  echo "PORT_CONFLICT: port 8765 serves a different HTTP application" >&2
  exit 3
fi

if ! "$project_dir/.venv/bin/python" - 2>/dev/null <<'PY'
import socket
s = socket.socket()
try:
    s.bind(("127.0.0.1", 8765))
except OSError:
    raise SystemExit(1)
finally:
    s.close()
PY
then
  echo "PORT_CONFLICT: 127.0.0.1:8765 is occupied by a non-responsive or non-HTTP process" >&2
  exit 3
fi

if [[ ! -x "$project_dir/.venv/bin/uvicorn" ]]; then
  echo "MISSING_VENV: run 'python3 -m venv .venv && .venv/bin/pip install -e .'" >&2
  exit 4
fi

cd "$project_dir"
exec "$project_dir/.venv/bin/uvicorn" app.main:app --host 127.0.0.1 --port 8765
