#!/usr/bin/env bash
set -euo pipefail
systemctl --user stop workspace-manager.service
echo "workspace-manager.service stopped"
