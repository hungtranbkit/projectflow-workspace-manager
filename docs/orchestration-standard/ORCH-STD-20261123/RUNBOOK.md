# ORCH-STD-20261123 Runbook

## Safety envelope

Work only in `/home/dell/workspace/projectflow-orchestrator-release-v1-20261123`, branch `integration/orchestrator-v1-20261123`. The live baseline is the user unit `workspace-manager.service`, bound to 127.0.0.1:8765, public at `https://workspace.mesflow.net/`. Never print environment values. Never edit the strict release checkout, its DB, or service unit before T08 gates pass. Keep `PROJECT.yaml` production deployment disabled.

Use `/home/dell/workspace/projectflow-workspace-manager/.venv/bin/python` with `PYTHONPATH=.` and `VIRTUAL_ENV=/home/dell/workspace/projectflow-workspace-manager/.venv` for acceptance. Stage with a free localhost port other than 8765 and disposable DB, root, state directory, and worktree root. Do not clean other worktrees/services.

## Staging checks

Run the repository's declared preflight, test, build and smoke commands plus focused security/autonomous/completion/recovery/UI suites. Compare failures to the exact strict base in the same environment. Start one staging Uvicorn process only after selecting a free port; capture its PID and shut down only that process. Verify health and task pages over HTTP. Execute one calculator canary only with an approved FeatureSpec link and complete V2 contract. Capture Change/Task IDs, process/session progress, test/report evidence, QA outcome, and uniqueness. Mocks may exercise denials only.

## Conditional live switch and rollback

Proceed only after the staged canary and all safety gates pass. Make an online SQLite backup with `sqlite3.Connection.backup`, verify backup integrity and free space, and preserve the existing user unit plus environment-file metadata with values redacted. Dry-run schema changes on a copy. Change the existing unit in one reversible update to the tested integration release; do not run two owners on 8765. Verify systemd active, local `/health` and `/` HTTP 200, public HTTPS `/` 200, DB integrity, recent logs, visible canary trace, and no duplicate session.

On any failed live check, immediately restore the saved unit and strict release working directory, daemon-reload/restart the same service, then prove active service and local/public HTTP 200. Record every command and timestamp in EVIDENCE.json. Cloudflare and unrelated services are outside this runbook.


## Supervisor lease and recovery advisor

The existing `CleanupWorker.reconcile()` remains the only periodic invocation. It ingests completed session output, then calls the leased `AutonomousExecutionService.scheduled_reconcile_tick()`. Migration V43 adds only `orchestration_leases`; authoritative task/session/review/release state stays in existing tables/events. The global supervisor lease expires after 180 seconds; each Change dispatch has a 120-second lease. Every retry and Builder start rechecks the current lease owner. An expired controller must stop before dispatch; do not lengthen the TTL to compensate for slow providers.

On an ambiguous blocker, the advisor is event-driven and bounded to one call per fingerprint per 15 minutes and three actual provider calls per 24 hours. Preferred `gpt-6.2-astra`/high is sent through `codex exec` only when the installed model cache lists that exact model/effort. Codex runs ephemeral, read-only, with only a temporary schema/output directory and sanitized prompt on stdin. Its model cache and requested model are not runtime receipts: without matching effective-model metadata the result remains UNKNOWN and is rejected. Only then may the configured first-party Claude fallback (`claude-opus-5-5`/high by default) be used, and its JSON response must attest the exact canonical model. Set `PROJECTFLOW_RECOVERY_PREFERRED_MODEL`, `PROJECTFLOW_RECOVERY_PREFERRED_EFFORT`, `PROJECTFLOW_RECOVERY_FALLBACK_MODEL`, or `PROJECTFLOW_RECOVERY_FALLBACK_EFFORT` per service process to change this role; no global CLI configuration or API key is modified. Model proposals are stored as sanitized task events and never execute a command or bypass TaskDecisionService, SpecGate, retry, Review, QA, CI, merge, or deployment controls. Provider errors, malformed proposals, missing receipts, or budget exhaustion return deterministic VERIFY/BLOCKED.
### Recovery advisor provider/tool boundary — 2026-11-25

- Do not invoke Codex CLI for advisory decisions while its supported per-call interface cannot disable `command_execution`; `--sandbox read-only` still permitted command activity in the blind probe. Capability cache entries do not prove the effective model. Keep Codex model UNKNOWN unless an actual trusted receipt says otherwise.
- Claude recovery advice must use the per-invocation tool-less command: `--restricted --strict-mcp-config --no-chrome --disable-slash-commands --no-session-persistence --tools "" --max-turns 1`, without `--json-schema`. This prevents code/shell tools and ignores configured MCP servers/settings for that invocation; it does not change global configuration.
- Accept advice only with `stop_reason=end_turn`, exactly one turn, an empty `permission_denials` list, zero server tool counts, and exact `modelUsage[*].canonicalModel` matching the requested model. Reject any tool event/command execution marker. Validate the JSON proposal locally against the strict action/identity/evidence schema. Missing receipt, invalid JSON or any tool activity falls back deterministically and does not run a model action.
- A tool-less Opus blind run on 2026-11-25 returned canonical model `claude-opus-5-5`, high requested effort, one turn, no permission denials, zero server tools, and end_turn. Its decision body was not valid JSON, so it was NOT scored and NOT accepted; deterministic fallback is the expected safe result. Prior 12/12 CLI evaluations are advisory evidence only: Codex emitted command_execution events, and the earlier schema-mode Claude run reported `tool_use`.
