#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
template="$project_dir/systemd/workspace-manager.service.in"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit="$unit_dir/workspace-manager.service"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

if [[ ! -x "$project_dir/.venv/bin/uvicorn" ]]; then
  echo "MISSING_VENV: install project dependencies before installing the service" >&2
  exit 4
fi

escaped_dir="${project_dir//&/\\&}"
sed "s&__PROJECT_DIR__&$escaped_dir&g" "$template" > "$tmp"
mkdir -p "$unit_dir"
if [[ -f "$unit" ]] && cmp -s "$tmp" "$unit"; then
  echo "UNCHANGED: $unit"
else
  install -m 0644 "$tmp" "$unit"
  echo "INSTALLED: $unit"
fi

systemctl --user daemon-reload
systemctl --user enable workspace-manager.service

if systemctl --user is-active --quiet workspace-manager.service; then
  echo "ACTIVE: workspace-manager.service (left running)"
elif curl --connect-timeout 1 --max-time 2 -fsS http://127.0.0.1:8765/ 2>/dev/null | grep -q '<title>ProjectFlow Workspace Manager</title>'; then
  echo "HEALTHY_UNMANAGED_INSTANCE: port 8765 already has Workspace Manager; service installed and enabled but not started." >&2
  echo "Stop that known instance, then run: systemctl --user start workspace-manager.service" >&2
else
  systemctl --user start workspace-manager.service
  echo "STARTED: workspace-manager.service"
fi

echo "AUTO_START: enabled (user lingering must remain enabled for boot without login)"
