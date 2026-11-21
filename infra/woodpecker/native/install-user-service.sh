#!/usr/bin/env bash
# Safe installer: install user unit in DISABLED state; never start server.
set -euo pipefail
[[ "$(uname -s)" == Linux ]] || { echo 'Linux required' >&2; exit 2; }
src="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
binary="$HOME/.local/share/projectflow-woodpecker-staging/v3.20.1/woodpecker-server"
[[ -x "$binary" ]] || { echo "BLOCKED: binary not verified at expected location" >&2; exit 2; }
cfg="$HOME/.config/projectflow-woodpecker"
unitdir="$HOME/.config/systemd/user"
execdir="$HOME/.local/share/projectflow-woodpecker-staging/native"
state="$HOME/.local/state/projectflow-woodpecker"
mkdir -p "$cfg" "$unitdir" "$execdir" "$state"
chmod 700 "$cfg" "$execdir" "$state"
install -m 700 "$src/run-server.sh" "$execdir/run-server.sh"
if [[ ! -e "$cfg/server.env.example" ]]; then
  install -m 600 "$src/server.env.example" "$cfg/server.env.example"
fi
unit="$unitdir/projectflow-woodpecker.service"
# Do not overwrite an existing unit. Upgrades need explicit review.
if [[ -e "$unit" ]]; then
  echo "EXISTS: $unit (preserved, not replaced)"
else
  cat >"$unit" <<'UNIT'
[Unit]
Description=Woodpecker CI for ProjectFlow (disabled until verified)
Wants=network-online.target
After=network-online.target
ConditionPathExists=%h/.config/projectflow-woodpecker/server.env

[Service]
Type=simple
EnvironmentFile=%h/.config/projectflow-woodpecker/server.env
WorkingDirectory=%h/.local/state/projectflow-woodpecker
ExecStart=%h/.local/share/projectflow-woodpecker-staging/native/run-server.sh
Restart=on-failure
RestartSec=10
UMask=0077
NoNewPrivileges=true
MemoryMax=750M
TasksMax=160

[Install]
WantedBy=default.target
UNIT
  echo "INSTALLED: $unit"
fi
systemctl --user daemon-reload
# DO NOT enable or start. Fail if unexpectedly running to avoid false evidence.
enabled=$(systemctl --user is-enabled projectflow-woodpecker.service 2>/dev/null || :)
active=$(systemctl --user is-active projectflow-woodpecker.service 2>/dev/null || :)
echo "WOODPECKER_UNIT enabled=$enabled active=$active"
if [[ "$active" == active ]]; then
  echo 'NOTICE: service is already active; installer did not modify it'
fi
echo 'NO_START: OAuth, HTTPS, dedicated agent and actual webhook acceptance required'
