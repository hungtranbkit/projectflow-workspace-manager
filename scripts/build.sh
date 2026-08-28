#!/usr/bin/env bash
set -euo pipefail
PYTHON="${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python}"
if [[ -z "$PYTHON" || ! -x "$PYTHON" ]]; then PYTHON="$(pwd)/.venv/bin/python"; fi
if [[ ! -x "$PYTHON" ]]; then PYTHON=python3; fi
"$PYTHON" -m compileall -q app
echo 'build PASS (source distribution app)'
