# ProjectFlow Workspace Manager — Project Context

## Current repository snapshot

- Branch: `docs/fast-anything-pilot-20261124` (PR #82, base `main`).
- This handoff accompanies the fast-anything pilot docs and user-local install helper.
- The pilot installs the pinned npm package only under `$HOME/.local/share/projectflow-tools/fast-anything`; Claude Code and Codex MCP registrations are user-local, with an absolute binary path. No app runtime, schema, or deployment config changed.

## Recent handoff: fast-anything pilot (2026-11-24)

- Added `scripts/fast-anything-pilot.sh` with exact-version `install`, `check`, and marker-guarded, repeatable `uninstall` commands; it uses a user-owned npm prefix, requires Node >=18, and never invokes global npm or sudo.
- Updated `docs/fast-anything.md` with release/tag/integrity provenance, local toolchain and MCP status, actual synthetic smoke/benchmark/rollback evidence, repo checks, and the explicit blocker that no real Code Graph RAG workload was found. The synthetic benchmark is not application performance evidence.
- Added `.fast/` to `.gitignore` for tool recordings, generated ports, and local build output; cleaned the pilot-owned synthetic `.fast/` directory after saving evidence.
- Package: `@el4cteo/fast-anything@0.8.6`; upstream tag commit `ba571858d222836843fcd0bccbd94a4290c36ac2`. Minimal Rust stable was installed user-locally; Cargo is exposed only in the MCP server environment.
- Verification: shell syntax, pinned install/check, safe uninstall refusal/idempotence check, doctor, MCP initialize/tools/list and `fast_status` call passed; synthetic scan/port/verify/enable/bench/report/disable passed; upstream Python source was byte-identically restored; project preflight passed. Full project test script was attempted but stopped at missing FastAPI dependency (exit 4). Avoided project test installation/run because its session fixture can remove real Docker resources with the shared `wm-*` prefix.
- No application behavior, API/schema, deployment, or production service changed. No GitHub Actions run or merge occurred.
- Remaining task: identify a real, safe offline Code Graph RAG workload and repeat baseline/profile/equivalence/benchmark there. Do not describe synthetic smoke as Code Graph RAG acceleration.
