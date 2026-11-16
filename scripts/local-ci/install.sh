#!/usr/bin/env bash
set -euo pipefail
source_dir="$(cd "$(dirname "$0")" && pwd)"
install_dir="$HOME/.local/share/projectflow-local-ci"
mkdir -p "$install_dir" "$HOME/.config/systemd/user"
install -m 755 "$source_dir/controller.py" "$source_dir/guest.sh" "$install_dir/"
docker build -t projectflow-local-ci:1 "$source_dir"
cat > "$HOME/.config/systemd/user/projectflow-local-ci.service" <<UNIT
[Unit]
Description=ProjectFlow local CI (no GitHub Actions)
After=network-online.target docker.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $install_dir/controller.py --state $install_dir/state
TimeoutStartSec=0
TimeoutStopSec=90
[Install]
WantedBy=default.target
UNIT
cat > "$HOME/.config/systemd/user/projectflow-local-ci.timer" <<'UNIT'
[Unit]
Description=Check ProjectFlow PR revisions every two minutes
[Timer]
OnBootSec=60
OnUnitInactiveSec=120
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now projectflow-local-ci.timer
systemctl --user start --no-block projectflow-local-ci.service
