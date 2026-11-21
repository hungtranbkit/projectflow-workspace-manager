#!/usr/bin/env bash
# Owner-operated aggregate snapshot publisher; never controls any agent.
set -euo pipefail
repo="hungtranbkit/projectflow-workspace-manager"
root="$HOME/.local/state/terminal-mcp"
out_dir="$HOME/.local/state/projectflow-live-view"
src="$HOME/workspace/projectflow-live-view-export-20261121/demo/observability-view"
mkdir -p "$out_dir"
chmod 700 "$out_dir"

python3 "$src/export_live_snapshot.py" \
  --nodes-db "$root/nodes.db" \
  --sessions-db "$root/session_registry.db" \
  --output "$out_dir/live-status.json"

# Only the redacted JSON is sent; the SQLite databases remain local.
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
