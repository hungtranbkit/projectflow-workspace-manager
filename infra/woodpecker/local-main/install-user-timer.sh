#!/usr/bin/env bash
# Install ProjectFlow trusted-main Woodpecker CLI poller on the current user.
# --enable is explicit; default installs disabled, without running any build.
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"
app="$HOME/.local/share/projectflow-woodpecker-main-ci"
state="$HOME/.local/state/projectflow-woodpecker-main-ci"
units="$HOME/.config/systemd/user"
cli="$HOME/.local/share/projectflow-woodpecker-staging/v3.20.1/woodpecker-cli"
test -x "$cli" || { echo 'BLOCKED: woodpecker-cli v3.20.1 missing' >&2; exit 2; }
"$cli" lint --strict "$repo/infra/woodpecker/local-main/trusted-main.yaml"
python3 -m py_compile "$repo/scripts/woodpecker_main_poll.py"
mkdir -p "$app" "$state" "$units"
chmod 700 "$app" "$state"
install -m 700 "$repo/scripts/woodpecker_main_poll.py" "$app/woodpecker_main_poll.py"
install -m 600 "$repo/infra/woodpecker/local-main/trusted-main.yaml" "$app/trusted-main.yaml"
if test -e "$units/projectflow-woodpecker-main-ci.service" || test -e "$units/projectflow-woodpecker-main-ci.timer"; then
  echo 'BLOCKED: existing Woodpecker main CI unit found (preserved)' >&2; exit 3
fi
cat >"$units/projectflow-woodpecker-main-ci.service" <<'UNIT'
[Unit]
Description=ProjectFlow protected-main Woodpecker CLI check (no GitHub Actions minutes)
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 %h/.local/share/projectflow-woodpecker-main-ci/woodpecker_main_poll.py
WorkingDirectory=%h/.local/share/projectflow-woodpecker-main-ci
NoNewPrivileges=true
UMask=0077
CPUQuota=125%
MemoryMax=4G
TasksMax=200
TimeoutStartSec=55min
UNIT
cat >"$units/projectflow-woodpecker-main-ci.timer" <<'TIMER'
[Unit]
Description=Poll protected GitHub main for new ProjectFlow commits every 15 minutes

[Timer]
OnBootSec=3min
OnUnitInactiveSec=15min
AccuracySec=1min
RandomizedDelaySec=30s
Persistent=true
Unit=projectflow-woodpecker-main-ci.service

[Install]
WantedBy=timers.target
TIMER
systemctl --user daemon-reload
if [[ "${1:-}" == --enable ]]; then
  systemctl --user enable --now projectflow-woodpecker-main-ci.timer
  echo 'TIMER_ENABLED'
else
  echo 'INSTALLED_DISABLED: pass --enable after verifying the account is dedicated/trusted'
fi
systemctl --user is-enabled projectflow-woodpecker-main-ci.timer 2>/dev/null || true
echo 'Logs: journalctl --user -u projectflow-woodpecker-main-ci.service -n 40 --no-pager'
echo 'Result: ~/.local/state/projectflow-woodpecker-main-ci/status.json'
