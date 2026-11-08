#!/usr/bin/env bash
set -euo pipefail
PYTHON="${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python}"
if [[ -z "$PYTHON" || ! -x "$PYTHON" ]]; then PYTHON="$(pwd)/.venv/bin/python"; fi
if [[ ! -x "$PYTHON" ]]; then PYTHON=python3; fi
if [[ "${PROJECTFLOW_INCLUDE_LIVE_AGENT:-0}" == "1" ]]; then
  "$PYTHON" -m pytest -m live_agent
else
  "$PYTHON" -m pytest -m "not live_agent"
fi
