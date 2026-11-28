#!/usr/bin/env bash
# Owner-operated aggregate snapshot publisher; never controls any agent.
set -euo pipefail
repo="hungtranbkit/projectflow-workspace-manager"
out_dir="$HOME/.local/state/projectflow-live-view"
src="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$out_dir"
chmod 700 "$out_dir"

# Fail closed: no fresh archive commit when LIVE is stale, incomplete or unavailable.
curl --fail --silent --show-error --proto "=https" --connect-timeout 5 --max-time 25 --max-filesize 1048576 \
  "https://terminal-bootstrap.mesflow.net/projectflow-live/snapshot" | \
  python3 "$src/archive_live_snapshot.py" --output "$out_dir/live-status.json"

# Only the validated aggregate projection is sent; no per-session details.
sha="$(gh api "repos/$repo/contents/live-status.json?ref=live-data" --jq '.sha')"
test -n "$sha"
payload="$(base64 -w0 "$out_dir/live-status.json")"
gh api --method PUT "repos/$repo/contents/live-status.json" \
  -f branch=live-data \
  -f message="data(view): refresh public aggregate without rebuilding Pages" \
  -f sha="$sha" \
  -f content="$payload" \
  --jq '"PUBLISHED_COMMIT="+.commit.sha'
unset payload
