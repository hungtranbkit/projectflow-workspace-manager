#!/usr/bin/env bash
# Install only a small non-destructive SQLite TTL reconciler for the staging Harness.
set -euo pipefail
action="${1:-}"
unit="projectflow-harness-reconcile-staging"
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
state="${HOME}/.local/state/projectflow-harness"
db="${state}/harness-mvp.db"
units="${HOME}/.config/systemd/user"
py="${HARNESS_PYTHON:-/home/dell/workspace/projectflow-workspace-manager/.venv/bin/python}"
case "$action" in
  install)
    [[ -x "$py" && -f "$db" && -f "$repo/scripts/harness_reconcile.py" ]] || {
      echo 'REFUSED: Python, existing state DB or reconciler missing' >&2; exit 2;
    }
    # Do not silently overwrite an unrelated service.
    [[ ! -e "$units/$unit.service" && ! -e "$units/$unit.timer" ]] || {
      echo 'REFUSED: unit already exists (inspect before updating)' >&2; exit 3;
    }
    mkdir -p "$units"
    cat > "$units/$unit.service" <<EOF
[Unit]
Description=ProjectFlow Harness staging expiry reconciliation (no process deletion)
ConditionPathExists=$db

[Service]
Type=oneshot
WorkingDirectory=$repo
ExecStart=$py $repo/scripts/harness_reconcile.py --db $db --apply
NoNewPrivileges=true
UMask=0077
TimeoutStartSec=30
EOF
    cat > "$units/$unit.timer" <<EOF
[Unit]
Description=ProjectFlow Harness staging checkpoint every five minutes

[Timer]
OnCalendar=*:0/5
AccuracySec=1min
Persistent=true
Unit=$unit.service

[Install]
WantedBy=timers.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "$unit.timer"
    systemctl --user start "$unit.service"
    systemctl --user --no-pager status "$unit.timer" | head -10
    ;;
  status)
    systemctl --user --no-pager status "$unit.timer" "$unit.service" || true
    ;;
  remove)
    # Safe removal does not delete the SQLite evidence.
    systemctl --user disable --now "$unit.timer" || true
    rm -f "$units/$unit.service" "$units/$unit.timer"
    systemctl --user daemon-reload
    echo 'UNIT_REMOVED (SQLite evidence retained)'
    ;;
  *)
    echo "Usage: $0 install|status|remove" >&2
    exit 2
    ;;
esac
