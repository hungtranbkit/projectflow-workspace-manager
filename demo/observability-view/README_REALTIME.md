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

## Optional authenticated execution evidence (review required before production)

The separate, draft `specs/features/public-live-proof.yaml` defines a **public**
safe projection of per-node status. It is intentionally not the approved
private, task-bounded Agent Observability feature.

After separate scope/privacy review, the existing SSE server may be started
with `--enable-terminal-proof`. Without that flag its behavior is unchanged.
The module `terminal_proof.py` uses the already-configured Terminal MCP
local / authenticated node readers, observes node states and compares only
SHA-256 hashes of private terminal output **in memory**. No prompt, command,
terminal output, credential, hostname, path or arbitrary task title is emitted.
Only explicit owner task titles mapped to fixed reviewed labels can appear.
A proven RUNNING requires changed output in successive authenticated
observations while the foreground process is active, with a 30s freshness
ceiling. An authenticated IDLE / WAITING_INPUT reports that state without
pretending it is writing code. Source loss falls back to the existing
`OPEN_UNVERIFIED` display. A new task label is not itself evidence of running,
test completion, a git change, review or deployment.

The proof monitor samples at most 100 registered sessions with bounded,
authenticated status reads and a shared eight-second cache across SSE clients.
It does not start sessions or mutate task/agent data. Keep the flag OFF until
the separate public security review and local-ci/verify check have passed.

Staging proof (21 Nov 2026): real HP node readback produced eight IDLE
observations; a running Python foreground probe with output changing across
polls progressed IDLE -> OPEN_UNVERIFIED -> RUNNING_VERIFIED in the
isolated staging service, without touching the public deployment.

## Authenticated fleet inventory and mobile list (22 Nov 2026)

When the opt-in public proof source is enabled, inventory comes from each
reachable node's authenticated `/v1/sessions` readback, not the HP controller's
legacy `session_records` ACTIVE rows. HP's old `local` alias can duplicate
`hp-linux` records; those are canonicalized and then joined only to **currently
listed** sessions. A missing node readback is reported in a bounded coverage
list, never included as a known-open or RUNNING session. The producer keeps
up to 100 safe session entries and sorts verified RUNNING, WAITING, blocked,
IDLE and unverified in that order. The phone view initially renders 24 cards,
with a Show More control and filters able to operate over the full safe set.

The list refreshes from source at most once per ~12s, and proof polling is
bounded by a shared cache. SSE delivery may still emit each ~3s: this cadence
is transport refresh, not a guarantee of new CLI evidence. A single UNKNOWN
foreground tool with output changes may be verified executing, but the site
does not imply that source edits or tests have passed. New remote session
slugs are not exposed; public node labels, finite curated task categories
and the evidence type remain allowlisted. For the audited privacy boundary,
see `docs/PUBLIC_AGENT_PROOF_SECURITY_REVIEW.md`.

## Task identity from remote node ownership (P0 extension)

A Terminal MCP node with the optional authenticated
`GET /v1/sessions/{name}/task-label` endpoint can return the explicit
owner-assigned title of the **current** session instance and its assignment
time. Only the owning node can confirm it; the HP view no longer treats HP's
audit DB as authoritative for unrelated remote nodes. When a node lacks the
new read-only endpoint, denies access, or the task label was derived from a
prompt rather than an explicit title, the collector reports no trusted title
and keeps any existing name-based guess clearly labeled as a guess.

Before any title enters the public SSE projection, the server maps it onto a
fixed, reviewed list of broad project/task categories and drops the original
text and private task ID. No raw task title, prompt, request key or file path
is sent to the public client. Assignment confidence and execution liveness
are separate: an authenticated task may remain OPEN_UNVERIFIED or IDLE.

The UI now reports aggregate counts of sessions with explicit task labels
and sessions whose agent type has been identified, so incomplete coverage
is visible. Proof of a file edit or a successful test is **not** derived
from terminal output growth. Exact file lists/test reports require the
separate, authorization-controlled Task observability view, not an
unauthenticated public feed.

The node extension is opt-in and backward compatible: the ProjectFlow
collector uses only read-only remote calls and a short cache, and ignores
an older node that does not implement the endpoint. No node upgrade is
required merely to keep the current RUNNING/IDLE feed working.

## Verified CLI screen evidence (22 Nov 2026)

The `terminal_mcp.terminal_status` API already captures up to 80 tmux rows
and classifies the actual Claude/Codex UI's busy footer or adapter state.
Previously, ProjectFlow still required **last-20-line output changes** to
confirm RUNNING, causing false negatives when the CLI showed a live busy
footer with no changed text, or only the upper screen changed.

A trusted Claude/Codex busy footer is now a separate
`evidence=agent_ui_busy` proof, requiring a matched agent type and actual
foreground command (no bash scrollback/prose or recent tmux activity alone).
For candidate foreground processes, the collector also takes one optional,
bounded, authenticated read-only 80-line tail per poll and retains only a
SHA-256 digest **in memory**. A witnessed full-screen fingerprint change is
`evidence=terminal_screen_delta`; changing between full-screen and old-tail
read methods cannot itself generate a false change. The current 20-line
redacted status tail remains the fallback if the optional screen read fails.

Explicit IDLE, permission prompts, and WAITING_INPUT always take precedence
over a changing screen. This proves a foreground turn or a terminal process
making visible progress; it **does not** prove file modifications, test
passage, merge/deploy, task completion or accepted results. The frontend
distinguishes the evidence categories without publishing raw terminal lines,
commands, exact task bodies, paths, tokens or user content. No node-agent
upgrade is required; these use existing authenticated GET status and tail
routes. Verification still expires if the node cannot be read.
