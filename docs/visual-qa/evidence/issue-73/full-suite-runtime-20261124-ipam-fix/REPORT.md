# Full-suite Docker IPAM recovery — 2026-11-24

## Result

On exact PR #74 source SHA `cee006ca53eb36b7be29cf654fb3081844c4cce1`, `./scripts/test.sh` completed with exit 0: **1,575 passed, 1 skipped**, 12,184 warnings, 3,137.38 seconds (52:17). The prior 14 sandbox-provisioning failures now pass individually on this SHA. The one environment skip is retained; no assertions or test selection were changed.

There is still a count discrepancy: `pytest --collect-only -q` reports 1,575 collected items, while final run outcomes total 1,576 (1,575 passed + 1 skipped). Both commands exit 0. It is recorded as unresolved; it does not alter the successful full-suite exit, but should be diagnosed before claiming every collected/runtime item maps one-to-one.

## Cause and scoped fix

The shared Dell Docker daemon had `DefaultAddressPools=null`, 29 existing networks, and no free predefined address pools. Real Compose calls for sandbox tests therefore failed with `all predefined address pools have been fully subnetted`. A private rootless daemon was not available: required `slirp4netns`/`pasta`/`newuidmap`/`newgidmap` executables were absent and `unshare -Ur` returned `Operation not permitted`. `ssh nec1` could not resolve its host; no remote test node was reachable through the authorized local shell path. No sudo, daemon restart, global prune, or host configuration change was made.

The branch fix is test-only. The nginx Compose fixture now reads `WM_TEST_DOCKER_SUBNET`. A pytest-owned wrapper around the existing `SandboxRuntimeService.compose_up` appends a unique subnet for each sandbox project to that sandbox's temporary `.env`, then invokes the original real Compose method. Each `/28` comes from the test-only pool `10.252.0.0/16`; code verifies the pool against local routes and Docker networks before allocation. Each Compose project retains its own bridge network, port publishing, health checks, and real `up`/`down` behavior. Concurrent projects get distinct subnets. Production code and daemon settings are unchanged.

The first static subnet attempt exposed two integration tests that create two projects from the same Compose document concurrently; Docker rejected the overlap. The final per-project `.env` allocation fixed that actual test interaction. The clean-SHA targeted rerun then passed all 14 previously failing node IDs.

## Exact-source verification

Checkout used a detached clean worktree at `cee006ca53eb36b7be29cf654fb3081844c4cce1`. The long-running task worktree remained dirty and untouched by the suite. Its current local HEAD is an older WIP base; do not reset or synchronize it destructively. Remote branch `agent/codex/visual-qa-73` points at the tested SHA.

| Command | Result |
|---|---|
| `./scripts/preflight.sh` | PASS, exit 0 |
| `./scripts/build.sh` | PASS, exit 0 |
| 14 formerly failing sandbox tests (node IDs from previous `summary.json`) | 14 passed, exit 0, real Docker Compose |
| Disposable `nginx:1.28-alpine` Compose `up`/`down` on `10.252.255.240/28` | Container reached running; network subnet verified; down removed only the named project network; PASS |
| `pytest` visual contract, execution, subprocess integration selection | 18 passed, exit 0 |
| `node --test tests/test_visual_qa_cli.mjs` | 4 passed, exit 0 |
| `node tools/visual_qa/smoke.mjs` | PASS, exit 0; positive, negative, repaired and missing-reference/server/dimensions cases |
| `./scripts/test.sh` | 1,575 passed, 1 skipped, exit 0; 52:17 |
| `pytest --collect-only -q` | exit 0; module collection counts sum to 1,575; differs by one from runtime outcomes |

The browser smoke used the existing authorized Chromium wrapper and pinned local Playwright; no network or provider request was made. Docker returned to 29 networks, with zero `wm-*` networks or containers after suite completion. The `10.252.0.0/16` test pool was absent from Docker allocations after cleanup. A separate final disposable Compose lifecycle probe used the pre-verified unused `10.252.255.240/28`; its recorded output shows a running nginx container and successful project-scoped removal.

## Reproduction

From a clean checkout of the exact SHA, with the repository's already-installed Python environment and authorized browser wrapper:

```sh
export VIRTUAL_ENV=/path/to/existing/projectflow/.venv
export PATH="$VIRTUAL_ENV/bin:$PATH"
export CHROME_PATH=/path/to/authorized-chromium-wrapper
export VISUAL_QA_PLAYWRIGHT_FROM=/path/to/existing/projectflow/task-worktree
unset DOCKER_HOST DOCKER_CONTEXT
./scripts/preflight.sh
./scripts/build.sh
./scripts/test.sh
```

For the isolated previously-failing test set, obtain `failed_node_ids` from the preserved earlier `full-suite-runtime-20261124/summary.json` and pass those 14 IDs to `python -m pytest -q`. The successful exact-SHA output is `logs/targeted-14.log`.

## Scope and gates

- No dirty `main`, unrelated WIP, production service/database, shared Docker network, daemon configuration, NovaRetail code, or other task branch was changed. No merge, deploy, force-push, CI bypass, global cleanup, or second production app.
- A: local full suite PASS with one existing skip; collection/runtime count delta remains unresolved.
- B: not met here. Nova approved legacy order reference remains 11.524542% mismatch (`REFERENCE_NOT_EQUIVALENT`); POS proposal 5.784358% remains unapproved fixture-only.
- C: not met for approved-design acceptance.
- D: actual ProjectFlow visual command-stage and UI-only opt-in tests pass; provider image-to-code invocation remains unverified.
- E: not met. Exact SHA `cee006ca...` has zero GitHub Actions workflow runs because PR #74's workflow remains disabled pending maintainer authorization.
- F: safety constraints observed.

## Next actions

1. Diagnose why collection lists 1,575 items but pytest reports 1 skipped outcome in addition to 1,575 passes; keep the test and assertions intact.
2. Maintainer to authorize/re-enable the existing required workflow for draft PR #74 and run checks on its exact current SHA.
3. Continue Nova design-equivalence and provider-runtime work in their owned scopes; do not promote the unapproved POS proposal to an approved product result.
