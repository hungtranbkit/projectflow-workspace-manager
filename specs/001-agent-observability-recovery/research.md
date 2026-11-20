# Research — What ProjectFlow should learn from CrommaCode

**Reviewed 2026-11-20. Source:** https://cromma.ai/code (official product page and FAQ).
**Method:** Feature-level comparison; no application binaries installed, no source-code access assumed, no license to copy implementation inferred.

| Verified CrommaCode behavior | ProjectFlow opportunity | Priority |
| --- | --- | --- |
| CLI detection and tool hooks for Claude Code, Codex and other agent CLIs | Build versioned adapters, opt-in hook activation with backups; accept unsupported sources as UNKNOWN | P0 |
| Tool stream identifies Read/Edit/Bash/Task and subagent activity | Event normalization with source timestamp, causal task identity, replay-safe event IDs | P0 |
| Color-coded multi-agent / subagent lanes | Per-task agent tree and cross-node activity timeline | P0 |
| Live file tree and Git branch history | Read-only changed-files and branch/worktree visualization; no source paths in public feeds | P1 |
| Bees run in separate worktrees/branches and require merge | Reuse the existing ProjectFlow one-agent/branch/worktree invariant; do not implement a second Bees scheduler | P1 |
| Multiple Game View modes (Westworld, Loom, Factory, Chips, Rhythm) | Optional visualization after activity correctness and mobile usability are verified | P2 |
| Local data and settings backup before CLI hooks | Privacy-first event storage, consent-based settings modification, undo support | P0 |

## Distinguish observation from control
CrommaCode is described as a desktop terminal for visualizing coding agents. Its official page does not establish a general-purpose cross-node orchestration, verified business acceptance, stall-recovery engine, or public fleet dashboard. These are proposed ProjectFlow extensions, not copied CrommaCode features.

## Existing ProjectFlow assets to reuse
- specs/SPEC.yaml + specs/features/spec-layer.yaml: canonical behavior and SpecGate
- specs/features/agent-execution.yaml: existing session lifecycle and non-duplication invariant
- docs/HARNESS_MONITOR_INTEGRATION.md: central-attested receipts, WIP limits, immutable goals, STALLED discipline, fail-closed cleanup
- docs/HARNESS_PROGRESS_DASHBOARD.md: gate-verified completion and time-consumed meters are separate; no arbitrary percentages
- app/services/spec_registry.py: baseline digest reads YAML only; SDD Markdown under specs/ does not itself change canonical YAML digest
- README.md: local-only workspace manager and explicit PR/CI safeguards

## Spec Kit source and adoption notes
Official docs: https://github.github.com/spec-kit/quickstart.html and https://github.github.com/spec-kit/how-to/existing-projects.html
Recommended flow: constitution → specify → clarify → plan → checklist → tasks → analyze → implement → converge. Existing projects must review generated files before integrating; specify init in a nonempty repository may affect agent skill/command paths. This proposal writes reviewable SDD artifacts first, deliberately avoids force-initializing or overwriting .agents/skills and .claude/skills.

## Unknowns requiring spike, not invention
1. Exact event hooks available and stable in each installed CLI version and the safe backup/rollback path.
2. Whether current Terminal MCP central receipts can be read from every node without treating attested status as cryptographic proof.
3. Real operator-facing read-model/API location, and whether existing Harness materialized readbacks are sufficient.
4. Approved public-view security boundary and field allowlist, if any; never publish raw logs.
5. Actual scale and latency under disconnected-node and replay conditions.

**Decision:** Prioritize evidence correctness and stalled-work visibility before game visuals. Do not add another monitor timer.
