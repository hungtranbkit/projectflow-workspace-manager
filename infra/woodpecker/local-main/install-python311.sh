#!/usr/bin/env bash
# Install an isolated Python 3.11 runtime for the trusted-main CI worker.
# Never modifies system Python, production venvs or global PATH.
set -euo pipefail
app="$HOME/.local/share/projectflow-woodpecker-main-ci"
bootstrap="$app/uv-bootstrap"
mkdir -p "$app"
chmod 700 "$app"
if ! test -x "$bootstrap/bin/uv"; then
  python3 -m venv "$bootstrap"
  "$bootstrap/bin/python" -m pip install --disable-pip-version-check --no-cache-dir 'uv>=0.13,<1'
fi
uv="$bootstrap/bin/uv"
"$uv" python install 3.11.18 --install-dir "$app/python-runtimes" --no-progress
py="$app/python-runtimes/cpython-3.11.18-linux-x86_64-gnu/bin/python3.11"
test -x "$py" || { echo 'FAIL: pinned Python 3.11.18 missing' >&2; exit 2; }
"$py" --version
ln -sfn "$py" "$app/python311"
if ! test -x "$app/venv-py311/bin/python"; then
  "$uv" venv --python "$py" --seed "$app/venv-py311"
fi
venv_version="$("$app/venv-py311/bin/python" --version 2>&1)"
[[ "$venv_version" == "Python 3.11.18" ]] || {
  echo 'BLOCKED: existing CI venv has wrong Python; preserve it for audit' >&2
  exit 2
}
echo 'CI_PYTHON311_READY'
