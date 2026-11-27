# Golden E2E runtime and TestRunner timeout follow-up (2026-11-25)

## Scope and exact inputs

- ProjectFlow draft PR #74 source before change: `50ce031328cfaad4e976dc5507ff5e9908d18b42` (confirmed from `refs/pull/74/head` and `refs/heads/agent/codex/visual-qa-73` before test).
- Existing full-suite evidence: raw JUnit SHA-256 `b1a8531d6aaa4f01b9e85afe09619eb05922549f112af953ea78f589091b31be`; 1,371 passed, 194 failed, 20 errors, 6 skipped; exit 1. This follow-up did not rerun the full suite.
- The current test-specific artifacts are in this directory. `failure-fingerprints.json` and `.tsv` account for all 194 failures, 20 errors and 6 skips with 120 failure/error and 2 skip status/fingerprint groups (122 total). The 20 setup errors specifically parse the HTML page “No Requirement Analysis exists…” as JSON. Do not infer the separate remaining failures from the Claude CLI issue.

## Failure grouping from full JUnit

| Status | Observed first-cause group | Count |
|---|---|---:|
| failed | `claude CLI not found in PATH` | 39 |
| failed | asserted provider/planner outcome mismatch (separate fingerprints) | 33 |
| failed | requirement-analysis page/prerequisite missing | 24 |
| error | HTML response parsed as JSON (`JSONDecodeError`) | 20 |
| failed | `NoneType` subscripting | 17 |
| failed | missing `test_cases` key | 12 |
| failed | missing technical design | 9 |
| failed | missing test design | 8 |
| failed | missing architecture analysis | 7 |
| failed | `/usr/bin/python3: No module named pytest` | 2 |
| failed | other distinct assertion/contract fingerprints | 43 |
| skipped | real Claude CLI unavailable (recorded per skip node) | 6 |

This is a fingerprint ledger, not a claim that all groups share one cause. `failure-fingerprints.tsv` lists each node ID and message fingerprint.

## Fixes

1. `tests/test_golden_e2e.py` now launches the child pytest using `sys.executable` while retaining the deliberately restricted child environment (`PATH=/usr/bin:/bin`, `PYTHONDONTWRITEBYTECODE=1`). No system PATH or interpreter was modified. This makes child pytest use the interpreter whose test dependency was installed from the parent project test environment.
2. `PROJECT.yaml` raises the bounded `commands.test.timeout_seconds` from 900 to 4500 seconds (75 minutes). Prior full runs took 2,101.29s and 3,137.38s; 4,500 seconds covers the observed 52m17s run with a finite ~22m margin. `TestRunner` already forwards the declared value into the command runner; no runner or gate semantics changed.
3. `tests/test_project_test_timeout.py` verifies the parsed stage timeout reaches `TestRunner` and verifies a real `TimeoutExpired` remains a `TIMEOUT` row with exit code 124 and `TEST_FAIL`.

## Reproduction and verification

- HP, clean disposable checkout at base SHA, before fix: `/usr/bin/python3 -m pytest --version` exited 1 (`No module named pytest`). The two golden tests were run with the repo-declared `.[test]` in task-owned venv; 2 failed at the child invocation in `tests/test_golden_e2e.py:227`, exit 1.
- HP, same disposable checkout after patch: child pytest passed both invocations; both golden end-to-end cases later stopped at CodeReview because this runner has no `claude` CLI. The same command also ran both timeout regression cases: `2 failed, 2 passed`, exit 1. This does not count as full golden PASS or provider success.
- Dell, task branch candidate, using the pre-existing task venv read-only: `tests/test_golden_e2e.py`, `tests/test_project_test_timeout.py`, `tests/test_visual_qa_contract.py`, `tests/test_visual_qa_execution.py` — **21 passed**, exit 0, 73.98s. The golden CodeReview seam is the existing deterministic `set_fake` fixture; no provider request was made. The two visible business assertions remain intact.
- A separate real fixture check confirmed broken stock logic returns child pytest exit 1 (`10 != 7`) and the actual repaired implementation returns 0.
- On HP candidate: `./scripts/preflight.sh` exit 0; `./scripts/build.sh` exit 0; `./scripts/smoke.sh` exit 0; `tests/test_project_test_timeout.py` 2 passed, exit 0.
- `git diff --check` passed. The full 1,591-case suite was not rerun. Provider-specific tests, 20 lifecycle setup errors and the remaining failure groups remain unresolved; A remains FAIL.

## Replay commands

```sh
# Use the repo test environment; the test child itself still keeps PATH restricted.
venv=/path/to/project-test-venv
VIRTUAL_ENV="$venv" "$venv/bin/python" -m pytest -vv -p no:cacheprovider \
  tests/test_golden_e2e.py tests/test_project_test_timeout.py \
  tests/test_visual_qa_contract.py tests/test_visual_qa_execution.py

VIRTUAL_ENV="$venv" ./scripts/preflight.sh
VIRTUAL_ENV="$venv" ./scripts/build.sh
VIRTUAL_ENV="$venv" ./scripts/smoke.sh
```

## Gate / safety

- Gate A: **FAIL** until the full suite is green on the candidate and every remaining failure is resolved. This targeted green does not replace it.
- B/C: unchanged and blocked by approved Nova design/reference equivalence.
- D/F: historical local PASS; no production, dirty `main`, task WIP, shared Docker or CI configuration was changed here.
- E: not passed; PR #74 CI remains disabled/has no exact-head checks. No bypass or merge/deploy.
- Rollback: revert this change commit only; no daemon/runtime resources remain from these tests.
