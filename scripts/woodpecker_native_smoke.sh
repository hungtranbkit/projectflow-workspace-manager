#!/usr/bin/env bash
# Isolated one-shot HTTP smoke of a real Woodpecker server binary.
# No GitHub OAuth credentials, no Docker, no public listener, no persistent service.
set -euo pipefail
BIN="${1:-$HOME/.local/share/projectflow-woodpecker-staging/v3.20.1/woodpecker-server}"
if [[ ! -x "$BIN" ]]; then echo 'BLOCKED: woodpecker-server binary missing' >&2; exit 2; fi
if ! command -v curl >/dev/null || ! command -v openssl >/dev/null; then
  echo 'BLOCKED: curl and openssl required' >&2; exit 2
fi
PORT=${WOODPECKER_SMOKE_PORT:-8187}
GRPC_PORT=${WOODPECKER_SMOKE_GRPC_PORT:-9187}
if [[ ! "$PORT" =~ ^[0-9]+$ || ! "$GRPC_PORT" =~ ^[0-9]+$ ]]; then
  echo 'BLOCKED: invalid ports' >&2; exit 2
fi
TMPDIR_SAFE=$(mktemp -d "${TMPDIR:-/tmp}/pf-woodpecker-smoke.XXXXXXXX")
chmod 700 "$TMPDIR_SAFE"
pid=''
cleanup() {
  if [[ -n "$pid" ]]; then kill "$pid" 2>/dev/null || :; wait "$pid" 2>/dev/null || :; fi
  rm -rf -- "$TMPDIR_SAFE"
}
trap cleanup EXIT INT TERM
# Fail if another service already owns our loopback port; never kill it.
if command -v ss >/dev/null && ss -ltn | grep -Eq "127[.]0[.]0[.]1:($PORT|$GRPC_PORT) "; then
  echo 'BLOCKED: smoke port already in use' >&2; exit 2
fi
export WOODPECKER_HOST="http://127.0.0.1:$PORT"
export WOODPECKER_SERVER_ADDR="127.0.0.1:$PORT"
export WOODPECKER_GRPC_ADDR="127.0.0.1:$GRPC_PORT"
export WOODPECKER_GITHUB=true
export WOODPECKER_GITHUB_CLIENT="LOCAL_SMOKE_NOT_REAL"
export WOODPECKER_GITHUB_SECRET="$(openssl rand -hex 32)"
export WOODPECKER_AGENT_SECRET="$(openssl rand -hex 32)"
export WOODPECKER_GRPC_SECRET="$(openssl rand -hex 32)"
export WOODPECKER_DATABASE_DATASOURCE="$TMPDIR_SAFE/woodpecker.sqlite"
export WOODPECKER_OPEN=false WOODPECKER_DISABLE_VERSION_CHECK=true
"$BIN" >"$TMPDIR_SAFE/server.log" 2>&1 &
pid=$!
ok=false
for attempt in {1..10}; do
  if ! kill -0 "$pid" 2>/dev/null; then break; fi
  code=$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' --max-time 2 "$WOODPECKER_HOST/" 2>/dev/null || :)
  if [[ "$code" == 200 || "$code" == 302 || "$code" == 303 ]]; then
    ok=true
    break
  fi
  sleep 1
done
if [[ "$ok" == true ]]; then
  echo "PASS: Woodpecker native server HTTP loopback responded (status $code)"
  echo 'NOT_VERIFIED: GitHub OAuth, external HTTPS, webhook, agent and CI workflow'
else
  echo 'FAIL: Woodpecker server did not reach HTTP loopback' >&2
  # Do not print server log to avoid accidental sensitive information leaks.
  exit 1
fi
