#!/usr/bin/env bash
# Install ONLY the official Docker CLI into the private CI Python venv.
# No daemon, no sudo, no production container changes.
set -euo pipefail
version=29.10.0
base=https://download.docker.com/linux/static/stable/x86_64
root="$HOME/.local/share/projectflow-woodpecker-main-ci"
d="$root/docker-cli"
venv="$root/venv-py311"
[[ -d "$venv/bin" ]] || { echo 'BLOCKED: CI venv not yet initialized' >&2; exit 2; }
mkdir -p "$d"
chmod 700 "$d"
archive="docker-cli_${version}.tgz"
curl -fsSL --retry 2 --connect-timeout 8 --max-time 65 "$base/$archive" -o "$d/$archive"
curl -fsSL --retry 2 --connect-timeout 8 --max-time 25 "$base/$archive.sha256" -o "$d/$archive.sha256"
(cd "$d" && sha256sum -c "$archive.sha256")
tar -xzf "$d/$archive" -C "$d" bin/docker
chmod 700 "$d/bin/docker"
ln -sfn "$d/bin/docker" "$venv/bin/docker"
"$venv/bin/docker" --version
if "$venv/bin/docker" info >/dev/null 2>&1; then
  echo 'WARNING: a Docker daemon is available; review runner isolation before executing container tests.'
else
  echo 'CLI_ONLY: no Docker daemon; Docker-dependent tests may be skipped by their explicit markers.'
fi
