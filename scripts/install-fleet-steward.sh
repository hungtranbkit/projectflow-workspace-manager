#!/usr/bin/env bash
# Install the user timer from a pinned, persistent ProjectFlow checkout.
set -euo pipefail
umask 077
node=${1:?node ID required}
root=${2:?workspace root required}
origin=${3:?HTTPS ingest URL required}
case "$node" in dell-linux|hp-linux|m910|macbook|nec1) ;; *) exit 2;; esac
code=$(cd "$(dirname "$0")/.." && pwd -P)
for path in "$code" "$root" "$HOME"; do [[ "$path" =~ ^/[a-zA-Z0-9_./-]+$ ]] || exit 2; done
[[ "$origin" =~ ^https://[a-zA-Z0-9.-]+(:[0-9]+)?/api/steward/captures$ ]] || exit 2
keydir="$HOME/.config/projectflow-steward"
state="$HOME/.local/state/projectflow-steward"
units="$HOME/.config/systemd/user"
mkdir -p "$keydir" "$state/spool" "$units"
if [[ ! -s "$keydir/signing.pem" ]]; then
  (set -C; openssl genpkey -algorithm ED25519 > "$keydir/signing.pem")
fi
python_bin=$(command -v python3)
adb_bin=$(command -v adb || true)
service_path="$(dirname "$python_bin"):/usr/local/bin:/usr/bin:/bin"
if [[ -n "$adb_bin" ]]; then service_path="$(dirname "$adb_bin"):$service_path"; fi
[[ "$service_path" =~ ^[a-zA-Z0-9_./:-]+$ ]] || exit 2
cat > "$units/projectflow-steward-collect.service" <<EOF
[Unit]
Description=Collect ProjectFlow private fleet evidence
[Service]
Type=oneshot
WorkingDirectory=$code
Environment=PATH=$service_path
ExecStart=$python_bin -m scripts.fleet_agent run --node $node --root $root --spool $state/spool --key $keydir/signing.pem --url $origin
UMask=0077
TimeoutStartSec=240
EOF
cat > "$units/projectflow-steward-collect.timer" <<EOF
[Unit]
Description=Refresh ProjectFlow private fleet evidence
[Timer]
OnBootSec=1min
OnUnitInactiveSec=5min
RandomizedDelaySec=15s
[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now projectflow-steward-collect.timer
systemctl --user start --no-block projectflow-steward-collect.service
