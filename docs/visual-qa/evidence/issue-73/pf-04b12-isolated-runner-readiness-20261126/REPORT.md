# PF-04b12 — safe runner assessment and deterministic diagnostics

Date: 2026-11-26 ICT. ProjectFlow lane only. This is a bounded diagnostic
milestone, not a full suite or Docker integration result.

## Exact source and worktree

PR #74 was re-read at start: remote head
`d1f86cb230d936d882071dc77996583a176cf346`, tree
`12e0eab92670cd754c662ad1f8543ec0e70a809c`. The task-owned clean worktree
was at local commit `1df1b5e7f12d9d7c88712970f05b195353975499`, the same
source tree. No source files changed during this diagnostic. The published
head was checked again after the run; no PR ref mutation was done in this
milestone yet.

## Runner assessment: no safe Compose daemon available

- Dell (`Linux 7.0.0-43-generic`, x86_64): 114 GiB free, 17 GiB available
  RAM; only Docker context is `default` at `/var/run/docker.sock`, shared
  daemon 29.7.2 (`/var/lib/docker`, 62 running / 9 stopped / 71 total
  containers, 29 networks). Current read-only inventory has 0 `wm-`
  containers and 0 `wm-` networks. The tests below were explicitly given a
  nonexistent task-local socket, with `DOCKER_CONTEXT` unset, so they could
  not use this daemon.
- Dell rootless prerequisites are incomplete: `dockerd-rootless.sh` and
  `rootlesskit` exist, and unprivileged user namespaces/subuid ranges are
  present; `newuidmap`, `newgidmap`, `slirp4netns`, `pasta`, `fuse-overlayfs`
  and a user Docker socket are absent. No task-owned daemon was started.
- HP was reached over the already authorized SSH alias for read-only
  assessment (two bounded inventory calls): 13 GiB free / 95% root usage, 4 GiB swap with 17 MiB free,
  14 GiB RAM with 8.1 GiB available. The same rootless mapping/network
  helpers and user socket are absent. I did not provision or use a daemon.
- NEC1 was attempted once using the existing SSH alias; local name resolution
  failed (`Could not resolve hostname nec1`). Fleet `online` metadata did not
  establish shell access, so no further connection attempt was made.

No daemon, host package, shared cache, service, container, network, or host
configuration was changed. Process inspection found no pytest or Docker CLI
process belonging to this lane after the run. No real provider was invoked. The existing test venv has Node 26.7.0 but no
Python Playwright package. Presence of the Claude
and Codex executables is not provider authorization; no auth files were read.

## Safe deterministic diagnostic

Source inspection selected five modules with temporary Git/SQLite fixtures,
local TestRunner/TaskDecisionService behavior, and fake command executors;
none calls live Claude/Codex or starts Compose. To fail closed if this
selection unexpectedly attempted Docker, pytest received
`DOCKER_HOST=unix:///tmp/pf04b12-disabled-docker.sock` (no socket exists).

Collection was 42 cases: visual contract 14, visual execution 3, required
stage decisions 14, TestRunner source identity 9, and bounded timeout 2.
The exact node list is `node-ids.txt`.

Actual test command (from repository root):

```sh
PATH=/usr/bin:/bin \
DOCKER_HOST=unix:///tmp/pf04b12-disabled-docker.sock \
/home/dell/workspace/.worktrees/projectflow-visual-qa-73/.venv/bin/python \
-m pytest -q -ra \
tests/test_visual_qa_contract.py \
tests/test_visual_qa_execution.py \
tests/test_task_decision_required_stages.py \
tests/test_test_runner_source_identity.py \
tests/test_project_test_timeout.py \
--junitxml=docs/visual-qa/evidence/issue-73/pf-04b12-isolated-runner-readiness-20261126/targeted.xml
```

Run time: 2026-11-26 06:50:10–06:50:17 UTC (13:50:10–13:50:17 ICT).
Result: **42 passed, 0 failed, 0 errors, 0 skipped; exit 0**. Full JUnit,
combined stdout/stderr, collection log, node list, command exits and source
identity are retained here. The early command attempts recorded as exit 127 (wrong
venv path) and exit 4 (newline-separated module arguments) did not collect or
execute tests; they are retained separately and were corrected before the
successful run.

## Gate and incident disposition

This diagnostic does not exercise actual Docker Compose lifecycle, browser
execution, live providers, or the full 209-module suite. It is not a full
suite pass. The next safe execution requires an owner-provisioned task-isolated
Linux daemon/runner with a dedicated data/exec root, private socket, working
rootless UID mapping and isolated Compose networking, plus an authorized
provider identity and supported Python Playwright/browser environment. Then
run real Compose create/partial-failure/cleanup checks before any full suite.

PF-04b10 incident impact remains **unknown**. Zero event rows and the current
empty `wm-` inventory do not reconstruct the missing pre-run inventory; this
report does not certify no impact. A remains FAIL/unqualified, E NOT PASS,
F remains not certified; B/C unchanged and D is historical only. No full suite,
CI activation, merge, deploy, production access, or source patch occurred.
