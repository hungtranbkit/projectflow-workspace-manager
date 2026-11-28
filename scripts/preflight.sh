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
_runtime_primary="${PROJECTFLOW_RUNTIME_PRIMARY:-}"
_runtime_fallback="${PROJECTFLOW_RUNTIME_FALLBACK:-}"
_runtime_primary_ok=0
_runtime_fallback_ok=0
if [[ -n "$_runtime_primary" && -x "$_runtime_primary" ]]; then _runtime_primary_ok=1; fi
if [[ -n "$_runtime_fallback" && -x "$_runtime_fallback" ]]; then _runtime_fallback_ok=1; fi
if [[ -n "$_runtime_primary" && "$_runtime_primary_ok" -eq 0 && -z "$_runtime_fallback" ]]; then
  echo "runtime executable pin is not executable: $_runtime_primary" >&2
  exit 1
fi
if [[ -n "$_runtime_fallback" && "$_runtime_fallback_ok" -eq 0 && -z "$_runtime_primary" ]]; then
  echo "runtime executable pin is not executable: $_runtime_fallback" >&2
  exit 1
fi
if [[ -n "$_runtime_primary" && -n "$_runtime_fallback" && "$_runtime_primary_ok" -eq 0 && "$_runtime_fallback_ok" -eq 0 ]]; then
  echo "neither pinned runtime executable is available" >&2
  exit 1
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
