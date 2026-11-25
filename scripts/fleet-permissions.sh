#!/bin/sh
# Linux/macOS: no installation, privilege escalation or shell profile edits.
set -eu
PF83_SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PYTHONPATH="$PF83_SOURCE${PYTHONPATH:+:$PYTHONPATH}"
exec "${PROJECTFLOW_PYTHON:-python3}" -m app.services.fleet_permissions "$@"
