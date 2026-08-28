#!/usr/bin/env bash
set -euo pipefail
PYTHON="${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python}"
if [[ -z "$PYTHON" || ! -x "$PYTHON" ]]; then PYTHON="$(pwd)/.venv/bin/python"; fi
if [[ ! -x "$PYTHON" ]]; then PYTHON=python3; fi
"$PYTHON" -m compileall -q app tests
"$PYTHON" - <<'PY'
import yaml
d=yaml.safe_load(open('PROJECT.yaml'))
assert d['project']['code']=='workspace-manager'
assert {'preflight','test'}.issubset(d['commands'])
assert d['ci']['required']==['preflight','test']
PY
echo 'preflight PASS'
