# ProjectFlow LIVE View: read-only SSE lane

## Public interface

- Static site: https://hungtranbkit.github.io/projectflow-workspace-manager/
- Dynamic health: https://terminal-bootstrap.mesflow.net/projectflow-live/health
- Dynamic snapshot: https://terminal-bootstrap.mesflow.net/projectflow-live/snapshot
- Dynamic event stream: https://terminal-bootstrap.mesflow.net/projectflow-live/events

The stream sends sanitized node and session aggregates approximately every 3 seconds from an HP user service on localhost port 18772. Upstream Terminal MCP registry and heartbeat updates may arrive less frequently; do not call this instant real-time execution tracking.

ACTIVE session, terminal output, and heartbeat are not proof of code progress. Independent acceptance percentage, verified RUNNING and task TTL remain UNKNOWN without trusted Harness evidence. Per-session aliases are anonymized and include only agent type and activity-age bucket. Registered node names from an explicit owner-approved allowlist (dell-5530, dell-linux, hp-linux, local, m910, mac17-macbook-pro, macbook, nec1, win) are public by request. Unknown/new node IDs retain anonymous Node NN labels. Never publish hostnames, IP addresses, unapproved IDs, session names, prompts, paths, commands, tokens, or raw logs.

Only GET health, snapshot and events routes exist; all writes are denied. The site has a CSP restricting the network to the fixed public read-only endpoint and the archived raw GitHub fallback. No login or agent-control capability is exposed.

## Fallback / operations

If SSE loses connection, the client tries direct snapshot every ~15 seconds then falls back to the archived GitHub live-data branch, published every ~5 minutes. The archive is labeled not real-time, and observations older than 15 minutes become UNKNOWN.

The systemd user service on HP is projectflow-live-sse-20261121.service. It is scoped to 127.0.0.1:18772. The named cloudflared tunnel service is cloudflared-terminal-mcp-dashboard.service, with an exact public path under terminal-bootstrap.mesflow.net. The other Access-protected dashboard hostname remains protected. A backup of the ingress configuration is at ~/.cloudflared/terminal-mcp-dashboard-config.pfview-20261121.bak.

To rollback, stop/disable only the new SSE service, restore and validate the original ingress config, restart only the named tunnel service, then revert the static page to the original archive-only client. Do not change or disable Terminal MCP, other tunnels, ProjectFlow production, Quality Gate canary or agent sessions.

## Acceptance requirements

- Exact code commit, clean reviewed checkout and independent test suite
- Cloudflared ingress syntax and route selection verified
- Public GET snapshot returns 200, POST returns 405, other routes 404
- Public SSE shows multiple frames ~3 seconds apart
- Existing bootstrap health stays 200, Access-protected dashboard stays protected
- Mobile Chromium 320/390 px: actual SSE data appears, updates twice, no horizontal overflow
- Only approved registered node names are public; no credentials, unapproved IDs, hostnames, IPs, command lines, prompts, paths or private logs in public JSON
- CI fast/remaining, feature tests and static smoke pass, evidence attached to PR
