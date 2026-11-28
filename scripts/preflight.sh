#!/usr/bin/env bash
set -euo pipefail
PYTHON="${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python}"
if [[ -z "$PYTHON" || ! -x "$PYTHON" ]]; then PYTHON="$(pwd)/.venv/bin/python"; fi
if [[ ! -x "$PYTHON" ]]; then PYTHON=python3; fi
if [[ -n "${PROJECTFLOW_RUNTIME_PATH:-}" ]]; then
  IFS=: read -r -a _runtime_dirs <<< "$PROJECTFLOW_RUNTIME_PATH"
  for _dir in "${_runtime_dirs[@]}"; do
    [[ -z "$_dir" || -d "$_dir" ]] || { echo "runtime PATH entry is not a directory: $_dir" >&2; exit 1; }
  done
fi
"$PYTHON" -m compileall -q app tests
"$PYTHON" - <<'PY'
import yaml
d=yaml.safe_load(open('PROJECT.yaml'))
assert d['project']['code']=='workspace-manager'
assert {'preflight','test'}.issubset(d['commands'])
assert d['ci']['required']==['preflight','test']
PY
echo 'preflight PASS'
