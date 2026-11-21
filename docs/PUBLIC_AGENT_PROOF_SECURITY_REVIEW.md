# Public RUNNING/IDLE proof: bounded privacy and release review

Date: 2026-11-22. Scope: existing read-only GitHub Pages ProjectFlow view and
public SSE routes `GET /projectflow-live/{health,snapshot,events}` only.

## Authorization and scope

The owner has repeatedly requested that the existing *public, view-only*
dashboard show real agent RUNNING, what task category an agent is working on,
and clear IDLE/WAITING states, without a PC control interface. This is not
a grant to expose internal prompts, exact file edits, shell commands,
credentials, paths, customer data or private agent transcripts. The
private, task-bounded `FEAT-AGENT-OBSERVABILITY` feature remains unchanged.

## Public allowlist

The projection publishes only approved registered node labels; safe curated
project/task categories; `claude|codex|shell|other`; states
`RUNNING_VERIFIED|IDLE|WAITING_INPUT|BLOCKED|OPEN_UNVERIFIED`; timestamps
for witnessed execution; proof category; and counts/coverage. Newly discovered
**remote session names are anonymized**, even if their names match task patterns.
Raw task titles are never published: an explicit owner title must map to one
of the fixed code-owned labels. The client writes values with `textContent`,
not `innerHTML`.

## Evidence requirements

`RUNNING_VERIFIED` requires two distinct terminal-output fingerprints while
the trusted node reports an in-flight foreground process, never a heartbeat,
open tmux pane, dispatch ACK or earlier Git commit. An IDLE/WAITING observation
invalidates prior proof. A proof is bound to node, name, stable session
instance and generation, and expires. Source outages fail closed rather than
replaying stale registry ACTIVE rows. The public report never claims a code
diff, test PASS, merge, deploy or percentage from this signal.

## Attack surface and secrets

The producer runs as the existing HP user, reads the pre-existing
`node-agent-tokens.env` file through systemd (no new credential copy) and
uses authenticated read-only Terminal MCP node endpoints. No terminal raw
output is written to SQLite, HTTP response, archive, HTML, client-side log or
disk by the collector. Only SHA-256 fingerprints are retained transiently in
server memory. Public CORS stays on the existing GitHub Pages origin and no
POST/PUT/DELETE endpoint is added.

## Acceptance checks and rollback

- Negative fixtures for secrets, raw commands and cross-node title confusion
- No false RUNNING across session recreation or generation changes
- Missing node/status denied -> UNKNOWN; IDLE -> IDLE; output delta -> verified
- Fleet list replaces duplicated/stale 'local' registry aliases
- End-to-end test: true foreground output delta -> RUNNING; shell prompt -> IDLE
- Python, Node, smoke/preflight/build and local-ci/verify all PASS before merge
- Browser at mobile viewport and public snapshot inspected after release

Rollback: disable `--enable-terminal-proof` in the existing SSE user unit and
restart; switch frontend to prior tested `gh-pages` commit; leave node
registries, agent tasks, volumes, and private files unchanged.

Residual limitation: a silent long-running job cannot be proven RUNNING and
will show unverified; unavailable nodes are listed as uncovered. Published
task categories are intentionally coarse and do not disclose actual file
contents. This review is of the allowlisted public telemetry, not a claim
of independent business acceptance or a general-purpose security audit.
