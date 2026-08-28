#!/usr/bin/env bash
set -euo pipefail
lines="${1:-100}"
if [[ ! "$lines" =~ ^[1-9][0-9]{0,4}$ ]]; then echo "usage: $0 [line-count]" >&2; exit 2; fi
exec journalctl --user-unit workspace-manager.service -n "$lines" --no-pager
