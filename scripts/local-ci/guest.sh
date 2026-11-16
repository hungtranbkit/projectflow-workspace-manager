#!/usr/bin/env bash
set -euo pipefail
cd /work/repo
mkdir -p /work/evidence
# This script is copied from the installed controller, not taken from PR source.
python --version > /work/evidence/environment.txt
docker version >> /work/evidence/environment.txt
docker compose version >> /work/evidence/environment.txt
docker pull nginx:1.28-alpine
docker pull python:3.12-slim
python -m pip install -e '.[test]'
python -m pip freeze > /work/evidence/packages.txt
./scripts/preflight.sh
./scripts/ci-test.sh fast --maxfail=1 --junitxml=/work/evidence/fast.xml
./scripts/ci-test.sh remaining --maxfail=5 --junitxml=/work/evidence/remaining.xml
