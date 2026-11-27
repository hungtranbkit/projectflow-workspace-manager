# PF-04d — deterministic productization and baseline-waiver residuals

## Identity and setup

- Exact PR #74 source: `3a9fb5a94811a1f44dba150e0573116bebc628cf`; tree `8db4112bdfd1f33d039e6974421c24f4dc855fd9`; remote branch matched before testing.
- Existing task-owned worktree was clean; no source/test edits were made. The pre-existing task venv was invoked read-only. Temp/state used task-scoped pytest temp directories. `PATH=/usr/bin:/bin`, `DOCKER_CONTEXT` unset and `DOCKER_HOST=unix:///tmp/pf-04d-no-docker.sock` (no socket).
- Runner preflight observed Python 3.14.4 / pytest 9.2.0 and adequate local disk headroom. No task pytest process was active before execution.

## Commands and outcomes

Productization audit:

```sh
env -u DOCKER_CONTEXT PATH=/usr/bin:/bin TMPDIR="$TASK_TMP" \
  PYTHONPATH="$PWD:$PWD/tests" DOCKER_HOST=unix:///tmp/pf-04d-no-docker.sock \
  "$TASK_VENV/bin/python" -m pytest -p no:cacheprovider -q -ra \
  tests/test_productization_audit.py --basetemp="$TASK_TMP/pytest" \
  --junitxml=productization-audit.xml
```

Started `2026-11-27T08:34:32Z`, finished `08:34:51Z`; **8 passed, 0 failed, 0 errors, 0 skipped; exit 0**; JUnit suite time 15.640s. This included the LOW-risk TaskDecisionService completion case using the existing deterministic review fake, real review/integration service records, real fixture Git worktree/commits and SQLite. The NORMAL-risk QA-required behavior also remained asserted. It was not a real provider invocation.

Baseline-waiver audit (safe next deterministic group; test module uses task-local repos, real subprocess pytest and `sandbox_profile=NONE`, no Docker):

```sh
env -u DOCKER_CONTEXT PATH=/usr/bin:/bin TMPDIR="$TASK_TMP" \
  PYTHONPATH="$PWD:$PWD/tests" DOCKER_HOST=unix:///tmp/pf-04d-no-docker.sock \
  "$TASK_VENV/bin/python" -m pytest -p no:cacheprovider -q -ra \
  tests/test_baseline_waivers.py --basetemp="$TASK_TMP/pytest" \
  --junitxml=baseline-waivers.xml
```

Started `2026-11-27T08:35:35Z`, finished `08:35:54Z`; **4 passed, 0 failed, 0 errors, 0 skipped; exit 0**; JUnit suite time 15.076s. It exercised real baseline reproduction/fingerprint matching, explicit waiver state, stale fingerprint rejection, and the mixed waived/new failure that must remain blocked.

Sanitized logs and JUnit files are alongside this report. Their hashes in `SHA256SUMS` match published bytes. JUnit records 8 and 4 testcases respectively; no tests were skipped or deselected.

## Scope and gates

No source changes, provider calls, Docker daemon use, shared resource mutation, browser execution, or full suite. `git diff --exit-code` showed no code changes before evidence capture. PR #74 remains draft. This advances only two deterministic modules, not Gate A: full suite is still required before A can pass. No CI, merge, deploy, production or main mutation.
