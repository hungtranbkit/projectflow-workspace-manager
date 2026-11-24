# fast-anything — Performance Optimization Pilot

Status: **installation and runtime verification pending** (2026-11-24). This document is a reproducible runbook, not evidence of a completed installation.

## Upstream

- Source: https://github.com/EL4CTEO/fast-anything
- Package: https://www.npmjs.com/package/@el4cteo/fast-anything
- License: MIT (verify at the pinned tag).
- Purpose: profile an actual workload, select deterministic CPU-hot functions, port only eligible functions to Rust, verify recorded calls, benchmark the whole application, and roll back if needed.

## Deployment policy

Pilot on an online Linux development node with sufficient free space; do not deploy to production or install fleet-wide automatically. HP was reported overloaded during planning, so prefer a healthy node. Check disk and inode availability, node/npm/Python/Rust versions, architecture and CLI compatibility first. Use a dedicated user-owned prefix and isolated test directory; no sudo, global PATH replacement, or unattended toolchain upgrades. Confirm exact stable npm version and platform package against the official release; pin the version rather than installing an unverified latest. Audit source/package metadata, executable provenance and install scripts before installation. No secrets, raw recordings, build caches or generated binaries in Git.

Example (replace VERSION with verified exact version):
```bash
npm view @el4cteo/fast-anything dist-tags versions --json
npm view @el4cteo/fast-anything@VERSION repository dist.integrity engines --json
npm install --prefix "$HOME/.local/share/projectflow-tools/fast-anything" --save-exact @el4cteo/fast-anything@VERSION
FA="$HOME/.local/share/projectflow-tools/fast-anything/node_modules/.bin/fast-anything"
"$FA" --version
"$FA" doctor
"$FA" --help
```

Check the pinned package lifecycle scripts and transitive metadata before running npm install. Rust is needed for compiling ports, not necessarily for profiling. If prerequisites are missing, install only reviewed user-space dependencies or report the actual blocker. Do not circumvent permissions.

## Agent use

Use the absolute binary path when configuring MCP. Check the local CLI help for the supported commands and scope before registration. Example Claude Code registration in a dedicated pilot workspace:
```bash
claude mcp add fast-anything -- "$FA" mcp
```
Use equivalent verified Codex CLI MCP registration; verify the registered server and tool listing via a smoke invocation. Do not overwrite existing server entries or secrets; keep project-level config out of production until reviewed.

## Performance workflow

1. Establish a representative, offline/reproducible workload; collect baseline wall time, CPU, RSS and output hash.
2. Use `fast-anything scan -- python app.py` (replace app.py with the selected workload). A network, DB, I/O or inference bottleneck is not automatically a Rust optimization candidate.
3. Select a deterministic, portable CPU hotspot. Inspect `port` output and generated TASK.md before agent changes.
4. Run `verify`; do not enable a port before successful verification and project regression tests. Never alter recordings to make verification pass.
5. Benchmark original vs optimized on the same machine/input, alternating runs, at least five runs per variant; report median and spread, memory, disk and correctness.
6. Accept only evidence-backed improvement (pilot target >=20% lower end-to-end median runtime, no correctness regression). Use `disable` and rerun the original tests to prove rollback. If not worthwhile, keep the original.
7. Keep generated native code reviewable, keep .fast/ recordings and build caches out of Git, clean only pilot-owned disposable caches after preserving sanitized reports.

First production-relevant candidate: Code Graph RAG offline parsing/indexing or search processing. Do not change a live service. Run only an isolated representative workload with non-sensitive inputs. A successful synthetic smoke test does not prove application acceleration.

## Verification record (to be filled from real execution)

| Field | Status |
|---|---|
| Node / UTC timestamp | Pending |
| Package version / official release / integrity | Pending |
| Binary path / architecture | Pending |
| Disk before/after / toolchain | Pending |
| Doctor / MCP handshake | Pending |
| Profile smoke / output correctness | Pending |
| Actual project workload benchmark | Pending |
| Rollback / cleanup | Pending |

Record exact commands, exit codes, sanitized output and source commit; distinguish PASS, FAIL, NOT RUN and BLOCKED. Save implementation scripts and evidence in a dedicated branch/PR, without credentials, sensitive machine identifiers or private data. Do not mark installation or optimization complete without real output.
