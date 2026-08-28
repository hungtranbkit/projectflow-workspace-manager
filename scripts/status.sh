#!/usr/bin/env bash
set -euo pipefail
service_state="$(systemctl --user is-active workspace-manager.service 2>/dev/null || true)"
enabled_state="$(systemctl --user is-enabled workspace-manager.service 2>/dev/null || true)"
echo "service=${service_state:-not-installed} enabled=${enabled_state:-not-installed}"
body="$(mktemp)"; trap 'rm -f "$body"' EXIT
if curl --connect-timeout 1 --max-time 3 -fsS http://127.0.0.1:8765/ -o "$body" 2>/dev/null; then
  if grep -q '<title>ProjectFlow Workspace Manager</title>' "$body"; then
    echo "health=PASS url=http://127.0.0.1:8765/"
    exit 0
  fi
  echo "health=FAIL reason=port-serves-different-application" >&2
  exit 2
fi
echo "health=FAIL reason=not-reachable" >&2
exit 1
