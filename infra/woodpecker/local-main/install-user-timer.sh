#!/usr/bin/env bash
# Install/upgrade the trusted-main-only Woodpecker CLI worker.
# No GitHub OAuth, no public PRs, no Docker daemon.
set -euo pipefail
update=false
enable=false
for arg in "$@"; do
  case "$arg" in
    --update) update=true ;;
    --enable) enable=true ;;
    *) echo "usage: $0 [--update] [--enable]" >&2; exit 2 ;;
  esac
done
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"
app="$HOME/.local/share/projectflow-woodpecker-main-ci"
state="$HOME/.local/state/projectflow-woodpecker-main-ci"
units="$HOME/.config/systemd/user"
cli="$HOME/.local/share/projectflow-woodpecker-staging/v3.20.1/woodpecker-cli"
service="$units/projectflow-woodpecker-main-ci.service"
timer="$units/projectflow-woodpecker-main-ci.timer"
test -x "$cli" || { echo 'BLOCKED: verified Woodpecker CLI missing' >&2; exit 2; }
test -x "$app/python311" || { echo 'BLOCKED: run install-python311.sh first' >&2; exit 2; }
test -x "$app/venv-py311/bin/python" || { echo 'BLOCKED: Python 3.11 venv missing' >&2; exit 2; }
test -x "$app/venv-py311/bin/docker" || { echo 'BLOCKED: run install-docker-cli.sh first' >&2; exit 2; }
[[ "$("$app/venv-py311/bin/python" --version)" == "Python 3.11.18" ]] || {
  echo 'BLOCKED: incompatible Python runtime (requires pinned 3.11.18)' >&2; exit 2
}
"$cli" lint --strict "$repo/infra/woodpecker/local-main/trusted-main.yaml"
python3 -m py_compile "$repo/scripts/woodpecker_main_poll.py"
mkdir -p "$app" "$state" "$units"
chmod 700 "$app" "$state"

if [[ -e "$service" || -e "$timer" ]]; then
  if [[ "$update" == false ]]; then
    echo 'BLOCKED: existing units preserved; pass --update after reviewing changes' >&2
    exit 3
  fi
  # Upgrade only our own service. Never overwrite an unrelated user unit.
  if ! grep -Fq 'projectflow-woodpecker-main-ci/woodpecker_main_poll.py' "$service" ||
     ! grep -Fq 'Unit=projectflow-woodpecker-main-ci.service' "$timer"; then
    echo 'BLOCKED: unexpected unit contents, manual review required' >&2; exit 3
  fi
  if systemctl --user is-active --quiet projectflow-woodpecker-main-ci.service; then
    echo 'BLOCKED: CI job is active, refusing to update under a running job' >&2; exit 3
  fi
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  cp -p -- "$service" "$service.bak.$stamp"
  cp -p -- "$timer" "$timer.bak.$stamp"
fi

install -m 700 "$repo/scripts/woodpecker_main_poll.py" "$app/woodpecker_main_poll.py"
install -m 600 "$repo/infra/woodpecker/local-main/trusted-main.yaml" "$app/trusted-main.yaml"

cat >"$service" <<'UNIT'
[Unit]
Description=ProjectFlow FAST trusted-main CI via Woodpecker CLI (not PR/full CI)
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
Nice=10
TimeoutStartSec=20min
UNIT
cat >"$timer" <<'TIMER'
[Unit]
Description=Check ProjectFlow protected main every 15 minutes (FAST CI only)

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
if [[ "$enable" == true ]]; then
  systemctl --user enable --now projectflow-woodpecker-main-ci.timer
  echo 'FAST_CI_TIMER_ENABLED'
else
  echo 'FAST_CI_INSTALLED_TIMER_NOT_ENABLED_BY_THIS_CALL'
fi
echo 'Status: ~/.local/state/projectflow-woodpecker-main-ci/status.json'
echo 'Logs: ~/.local/state/projectflow-woodpecker-main-ci/logs/'
echo 'Security: FAST trusted-main only. Full regression/PR check requires isolated Docker CI.'
