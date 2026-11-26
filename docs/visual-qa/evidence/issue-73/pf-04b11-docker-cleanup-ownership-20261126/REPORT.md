# PF-04b11 — scope pytest Docker cleanup to resources created by this run

Date: 2026-11-26 (ICT). ProjectFlow test-harness scope only.

## Source and ownership

- New isolated branch: `agent/codex/issue-73-docker-test-ownership-20261126`.
- Exact PR #74 starting head/tree: `7fad14ffe02dd8303a59a4e00ac31344e31cfbfd` /
  `850de8112d673f20c4802752292cc6634fc6fd63`; this matched the fetched
  remote branch before edits.
- Only changed application repository files are under `tests/`, plus the
  required `PROJECT_CONTEXT.md` and this task evidence. No production module,
  project runtime naming, CI, branch protection, main/WIP, or provider config
  was changed.

## Root cause and repair

The old session fixture enumerated every Compose-labelled container and all
networks, then deleted any resource whose name began `wm-`. The production
SandboxManager itself uses that shared prefix. A name prefix therefore did
not establish pytest ownership.

`tests/docker_compose_ownership.py` adds a per-pytest-run registry. Before
Compose `up`, it reserves the exact project and refuses if that project
already has labeled containers, networks, or volumes. After `up`—including
partial-create exceptions—it inspects and records resource IDs/names,
creation identity, Compose project labels, and available worktree/config
labels. The session cleanup removes only those recorded IDs after current
identity verification. It never enumerates by name prefix and never uses
prune commands. Incomplete inventory, spoofed/unregistered IDs, inspection
errors, collisions, and failed cleanup commands raise visibly and leave
unverified resources untouched. Existing exact IDs/labels are rechecked
before project reuse.

`tests/conftest.py` registers the Compose up call before creation and guards
the application's real `compose_stop` and `compose_down`/reset operations
against the same run registry and source paths. Direct test-source search
found no other raw `docker compose` invocations; test cleanup and reset paths
call the SandboxManager APIs through `build_client`. The remaining direct
Docker commands in tests are read-only inventory/assertions or the separate
`docker run --rm` visual sandbox path.

## Red/green evidence

The old prefix-only criterion is reproduced safely by
`legacy_prefix_red.py`; it only processes a synthetic in-memory inventory
and has no Docker/socket access. It exited 1 as expected because its target
set included two resources owned by other projects/runs.

The updated ownership tests use a fake Docker command runner and were run
with the actual updated `tests/conftest.py` loaded:

```sh
PATH=/usr/bin:/bin "$VENV/bin/python" -m pytest -q -ra \
  tests/test_docker_compose_ownership.py \
  --junitxml=green.xml
```

Final run: 2026-11-26 06:01:15–06:01:17 UTC (13:01:15–13:01:17 ICT),
exit 0. JUnit: **18 passed, 0 failures, 0 errors, 0 skips**, Python 3.14.4,
pytest 9.2.0; test time 0.100s. `green.xml` preserves the earlier 16-case run;
`final-green.xml` is the final 18-case result. The 18 cases cover production-like and
sibling `wm-*` resources,
exact IDs, volume/network/container identity, pre-existing namespace
collision, partial creation, unexpected later resource, mismatched worktree
label, missing listed ID, inspect/inventory/remove failures, concurrent duplicate reservation, protected down/stop source identity, and a
second pytest registry refusing a project whose exact resources belong to
another run, duplicate resource names across projects, and a spoofed Compose
project-filter ID. All Docker
behavior in these unit cases is fake; the session registry was empty and
created no real resources. A read-only before/after inventory stayed at
zero `wm-` containers and zero `wm-` networks.

`py_compile` for the helper, regression module, and `conftest.py` exited 0;
`git diff --check` exited 0. Final exit evidence is in `py_compile.exit` and
`diff-check.exit`.

## Safety incident disposition

The read-only audit is recorded in `incident-audit.md`. The historical
container/network destroy event queries returned zero rows for the recorded
time window; current inventory has no listed `wm-` resources. This does not
establish the pre-test inventory, so whether the prior exploratory cleanup
removed an existing resource remains **unknown**. No recovery, deletion,
restart, recreate, daemon configuration change, or host-side cleanup was
performed. Gate F is therefore not re-certified by this patch.

There is no isolated Docker daemon approved for a real Compose create/failure/
cleanup integration run. The shared daemon was deliberately not used for a
destructive integration test. Thus real Docker integration remains
**BLOCKED**; unit/fake-runner PASS is not represented as Docker integration
PASS. The full suite was not run; provider auth remains a separate blocker.

## Rollback and gates

Rollback only by reverting this task's test-harness commit. The PR stays
draft. Do not run full suite until a task-owned/approved isolated daemon and
authorized provider runtime are available. A remains unqualified/FAIL; B/C
are unchanged; D is historical only; E is NOT PASS until exact-SHA checks
pass; F remains under incident review.
