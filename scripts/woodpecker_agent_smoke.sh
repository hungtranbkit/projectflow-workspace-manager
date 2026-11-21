#!/usr/bin/env bash
# End-to-end gRPC registration test of official Woodpecker v3.20.1 binaries.
# Strictly loopback; fake OAuth; no incoming webhooks; no user CI pipelines.
# The local agent backend is UNSANDBOXED; use only with this disposable
# isolated server and NEVER attach it to a public GitHub repo.
set -euo pipefail
BIN_DIR="${WOODPECKER_SMOKE_BIN_DIR:-$HOME/.local/share/projectflow-woodpecker-staging/v3.20.1}"
server_bin="$BIN_DIR/woodpecker-server"
agent_bin="$BIN_DIR/woodpecker-agent"
[[ -x "$server_bin" && -x "$agent_bin" ]] || { echo "BLOCKED: verified binaries missing" >&2; exit 2; }
for item in python3 curl openssl ss; do
  command -v "$item" >/dev/null || { echo "BLOCKED: missing $item" >&2; exit 2; }
done
tmp="$(mktemp -d "${TMPDIR:-/tmp}/pf-woodpecker-agent-smoke.XXXXXXXX")"
chmod 700 "$tmp"
server_pid=''
agent_pid=''
cleanup() {
  if [[ -n "$agent_pid" ]]; then kill "$agent_pid" 2>/dev/null || :; wait "$agent_pid" 2>/dev/null || :; fi
  if [[ -n "$server_pid" ]]; then kill "$server_pid" 2>/dev/null || :; wait "$server_pid" 2>/dev/null || :; fi
  rm -rf -- "$tmp"
}
trap cleanup EXIT INT TERM
http_port=8191
grpc_port=9191
if ss -ltn | grep -Eq "127[.]0[.]0[.]1:($http_port|$grpc_port)[[:space:]]"; then
  echo "BLOCKED: loopback smoke port occupied" >&2; exit 2
fi
export WOODPECKER_HOST="http://127.0.0.1:$http_port"
export WOODPECKER_SERVER_ADDR="127.0.0.1:$http_port"
export WOODPECKER_GRPC_ADDR="127.0.0.1:$grpc_port"
export WOODPECKER_GITHUB=true
export WOODPECKER_GITHUB_CLIENT="LOCAL_TEST_NOT_GITHUB_OAUTH_APP"
export WOODPECKER_GITHUB_SECRET="$(openssl rand -hex 32)"
export WOODPECKER_AGENT_SECRET="$(openssl rand -hex 32)"
export WOODPECKER_GRPC_SECRET="$(openssl rand -hex 32)"
export WOODPECKER_DATABASE_DATASOURCE="$tmp/smoke.sqlite"
export WOODPECKER_DISABLE_VERSION_CHECK=true
export WOODPECKER_OPEN=false
"$server_bin" >"$tmp/server.log" 2>&1 &
server_pid=$!
server_ready=0
for attempt in {1..12}; do
  kill -0 "$server_pid" 2>/dev/null || break
  http_code="$(curl --noproxy '*' -fsS -o /dev/null -w '%{http_code}' --max-time 1 "$WOODPECKER_HOST/" 2>/dev/null || :)"
  if [[ "$http_code" == 200 ]]; then server_ready=1; break; fi
  sleep 1
done
if [[ "$server_ready" != 1 ]]; then
  echo "FAIL: loopback server did not become ready" >&2; exit 1
fi
echo "SERVER_HTTP_PASS (200 on loopback)"

export WOODPECKER_SERVER="127.0.0.1:$grpc_port"
export WOODPECKER_BACKEND=local
export WOODPECKER_HOSTNAME=projectflow-agent-smoke-only
export WOODPECKER_MAX_WORKFLOWS=1
export WOODPECKER_CONNECT_RETRY_COUNT=2
"$agent_bin" >"$tmp/agent.log" 2>&1 &
agent_pid=$!
# Agent auto-registers in server's ephemeral database when gRPC connection succeeds.
status=unknown
for attempt in {1..12}; do
  kill -0 "$agent_pid" 2>/dev/null || { status=agent_exited; break; }
  status="$(python3 - "$tmp/smoke.sqlite" <<'PY'
import sqlite3, sys
path = sys.argv[1]
try:
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=0.5) as db:
        tables=[name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND lower(name) LIKE '%agent%'")]
        counts=[db.execute('SELECT COUNT(*) FROM "' + name.replace('"','""') + '"').fetchone()[0] for name in tables]
        print("registered" if any(c > 0 for c in counts) else "waiting")
except (sqlite3.Error, OSError):
    print("waiting")
PY
)"
  [[ "$status" == registered ]] && break
  sleep 1
done
if [[ "$status" == registered ]]; then
  echo "AGENT_GRPC_REGISTRATION_PASS"
  echo "NO_PUBLIC_WEBHOOK_OR_PIPELINE_WAS_ENABLED"
else
  echo "AGENT_REGISTRATION_NOT_CONFIRMED ($status)" >&2
  # No full logs or secret-bearing environment values printed.
  exit 1
fi
