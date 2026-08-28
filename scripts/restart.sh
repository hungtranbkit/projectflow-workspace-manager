#!/usr/bin/env bash
set -euo pipefail
systemctl --user restart workspace-manager.service
for _ in {1..30}; do
  if "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/status.sh" >/dev/null 2>&1; then
    echo "restart=PASS"
    exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/status.sh"
  fi
  sleep 0.2
done
echo "restart=FAIL" >&2
systemctl --user status workspace-manager.service --no-pager >&2 || true
exit 1
