#!/usr/bin/env bash
# Fail-closed native Woodpecker server entrypoint. Designed for a systemd USER service.
set -euo pipefail
python3 - <<'PY'
import os, re, sys
from urllib.parse import urlsplit
get = os.environ.get
url = get("WOODPECKER_HOST", "")
p = urlsplit(url)
checks = {
    "HTTPS_HOST": p.scheme == "https" and bool(p.hostname) and p.hostname not in ("ci.example.com", "localhost", "127.0.0.1") and not p.username and not p.password and p.path in ("", "/") and not p.query and not p.fragment,
    "GITHUB_CLIENT": len(get("WOODPECKER_GITHUB_CLIENT","")) >= 10 and "REPLACE" not in get("WOODPECKER_GITHUB_CLIENT","").upper(),
    "GITHUB_SECRET": len(get("WOODPECKER_GITHUB_SECRET","")) >= 20 and "REPLACE" not in get("WOODPECKER_GITHUB_SECRET","").upper(),
    "AGENT_SECRET": bool(re.fullmatch(r"[a-fA-F0-9]{64}", get("WOODPECKER_AGENT_SECRET",""))),
    "GRPC_SECRET": bool(re.fullmatch(r"[a-fA-F0-9]{64}", get("WOODPECKER_GRPC_SECRET",""))),
    "REGISTRATION_CLOSED": get("WOODPECKER_OPEN","false").lower() == "false",
    "GITHUB_PROVIDER": get("WOODPECKER_GITHUB","").lower() == "true",
    "PUBLIC_REPOS_ONLY": get("WOODPECKER_GITHUB_PUBLIC_ONLY","false").lower() == "true",
}
missing = [name for name, valid in checks.items() if not valid]
if missing:
    print("BLOCKED: Woodpecker server configuration incomplete: " + ", ".join(missing), file=sys.stderr)
    sys.exit(2)
print("Woodpecker configuration gate passed; external OAuth/webhook still unverified.")
PY
BIN="$HOME/.local/share/projectflow-woodpecker-staging/v3.20.1/woodpecker-server"
[[ -x "$BIN" ]] || { echo 'BLOCKED: official Woodpecker server binary missing' >&2; exit 2; }
export WOODPECKER_SERVER_ADDR="127.0.0.1:8179"
export WOODPECKER_GRPC_ADDR="127.0.0.1:9000"
export WOODPECKER_DATABASE_DATASOURCE="$HOME/.local/state/projectflow-woodpecker/woodpecker.sqlite"
exec "$BIN"
