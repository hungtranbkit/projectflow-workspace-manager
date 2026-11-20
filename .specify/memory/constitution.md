# ProjectFlow Constitution — draft for Spec Kit adoption

**Date:** 2026-11-20  
**Status:** PROPOSED — review alongside AGENTS.md, specs/SPEC.yaml, and PROJECT.yaml before treating this as an approved constitution.

## Principle I — One source of truth, no parallel orchestrator
The existing ProjectFlow domain, Spec Layer V1, task decision gates, Harness SQLite store, and Terminal MCP receipts remain authoritative for their respective scopes. A new observability feature MUST NOT introduce a second task scheduler, unbounded timer, second business-acceptance database, or a competing definition of DONE. The canonical behavior contract continues to reside in specs/SPEC.yaml and specs/features/*.yaml.

## Principle II — Evidence above activity
Distinguish command submission, tool event, live process proof, output progress, verified test, code review, merge, deployment, and independent business acceptance. A tool count, token count, timestamp, process heartbeat, or agent self-report MUST NOT increase business completion percentage. Missing/stale provenance is UNKNOWN, never assumed successful.

## Principle III — Isolated and reversible execution
Preserve ONE AGENT → ONE BRANCH → ONE WORKTREE; integrate separately and test the exact commit before declaring readiness. Never automatically push, merge main, deploy, resolve conflicts, or destroy an unrelated/dirty session. Any hook added to CLI settings requires explicit opt-in, backup, and rollback.

## Principle IV — Fail closed, bounded recovery
Agent starts and retries must be idempotent, tied to one immutable task/workspace identity and valid authorization. Repair operations may act only on resources owned by ProjectFlow and supported by verified proof. Unknown node, missing receipt, unsafe cleanup, or ambiguous ownership blocks automated recovery. A child task must have a time bound no longer than one day; a parent implementation slice no longer than two days.

## Principle V — Privacy and safe publishing
Internal project data, prompts, terminal output, source paths, secrets, tokens, host addresses, and agent commands stay in operator-only views. A public dashboard, if independently approved, may expose only explicitly allowlisted, sanitized read-only aggregates through a separate access-controlled or reviewed public surface; no control endpoints. Do not assume that a public route is approved simply because a read-only page exists.

## Principle VI — Acceptance and non-regression
Every behavior-changing task must link approved FeatureSpec requirement and acceptance IDs through SpecGate before execution. Changes require unit/integration tests, negative cases, evidence provenance, and rollback strategy. The latest passing CI, a draft PR, and a technical canary must not be confused with business acceptance.

## Amendment / use
This constitution is a proposed Spec Kit document, not an automatic amendment to the approved machine-readable Spec Layer. Review any inconsistency with existing AGENTS.md and architecture decisions before approval. The Spec Kit CLI scaffolding and agent integration have NOT been initialized by this document alone.
