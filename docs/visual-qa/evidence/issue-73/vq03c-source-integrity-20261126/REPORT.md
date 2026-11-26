# VQ-03c1 — bind Visual QA inputs to committed source

## Result

The regression was reproduced and fixed in the existing ProjectFlow task worktree. A clean `git status` had reported no changes while an ignored reference existed only in the worktree, and the Task panel still emitted its preview URL when `source_state()` raised. After the change, Visual QA configuration and reference bytes are accepted only when they match regular Git blobs in the exact tested commit and the index has no `assume-unchanged` or `skip-worktree` override. A source-state exception leaves the panel invalid and without a reference link.

This is a targeted source-integrity milestone, not proof of a browser run, full-suite pass, NovaRetail approval, or overall issue acceptance.

## Source identity

- PR #74 expected head before this work: `d530e2979c119c8f01d47c0f3de45abfb249b070`, tree `51d17b4c00718f176f7397cde582e21c007a116b`.
- Tested source commit: `4d88237e0611034d86df0d769442309f0f8e15d6`.
- Tested source tree: `f411335d50518e62370d68e0bd6170963e3dd4e7`.
- Status was clean at the tested source commit before and after the green run. Evidence files are added separately after this run.
- Source paths changed: Git blob reader and committed Visual QA contract loading; prompt, preview, runner and decision-gate consumers; Vietnamese guide and regression tests. No production instance, shared Docker daemon, provider, browser, CI, merge or deploy was used.

## Red / green evidence

RED baseline was local commit `edf92d08fd80212689921b12d1fb91f0da8ccebe`, tree `51d17b4c00718f176f7397cde582e21c007a116b`; this tree matches the expected PR head tree, while the commit SHA differs.

RED command (three focused regressions, before implementation):

```sh
DOCKER_HOST=unix:///tmp/pf-vq03c-no-daemon.sock \
  "$TASK_VENV/bin/python" -m pytest -q -ra tests/test_visual_qa_task_panel.py \
  -k 'ignored_reference_not_in_head or source_state_failure or skip_worktree_reference' \
  --junitxml="$EVIDENCE_TMP/red.xml"
```

Result: exit 1; 3 tests, 3 failed, 0 errors, 0 skipped. Failures showed an ignored reference absent from `HEAD` was linked; a source-state exception still left a link; and modified bytes hidden by `skip-worktree` were linked. The JUnit records these failures. Its original run timestamp is `2026-11-26T20:52:50.105459+07:00` (13:52:50 UTC). The command's complete raw console output was not retained; `red.xml` is the preserved test evidence.

GREEN command (run on the tested source commit above):

```sh
DOCKER_HOST=unix:///tmp/pf-vq03c-no-daemon.sock \
  "$TASK_VENV/bin/python" -m pytest -q -ra \
  tests/test_visual_qa_task_panel.py \
  tests/test_visual_qa_execution.py \
  tests/test_visual_qa_contract.py \
  tests/test_visual_qa_evidence.py \
  tests/test_task_decision_required_stages.py \
  tests/test_test_runner_source_identity.py \
  tests/test_visual_qa_runner_subprocess.py \
  -k 'not test_visual_gate_runs_through_real_test_runner_subprocess_and_pins_current_head' \
  --junitxml="$EVIDENCE_TMP/source-commit-focused.xml"
```

Result: exit 0; **107 passed, 0 failed, 0 errors, 0 skipped**. JUnit suite time is 83.239 seconds and testcase-time sum is 82.983 seconds (JUnit timestamp `2026-11-26T21:32:49.335501+07:00`, or 14:32:49 UTC). The command wrapper recorded process start 14:32:47 UTC, exit 0 and end 14:34:15 UTC (88 seconds wall elapsed). Python 3.14.4 and pytest 9.2.0 came from an already-existing task environment. The one browser subprocess test was explicitly excluded because the pinned Chromium executable is unavailable in this worktree; no install or browser retry was performed. This scoped exclusion is not a full-suite result.

`DOCKER_HOST` pointed at a nonexistent task-local socket so these tests could not use the shared daemon. The published XML/log are sanitized copies (host/path details removed); the original command output and JUnit remain under the task-owned local artifact directory. This evidence directory does not include the raw red console transcript because it was not captured.

## Behavior and limits

- `PROJECT.yaml` and each configured reference are read from regular blobs at the exact tested commit; actual bytes are checked against the Git object and SHA-256. Symlinks, absent/untracked/ignored content, index flags, dirty/stale source and read races fail closed.
- The same committed contract reader is used by preview, prompt, TestRunner stage selection/dispatch, evidence validation and TaskDecisionService stage/gate calculations. Non-UI tasks remain outside the Visual QA stage.
- Panel source-state errors now return `INVALID`/blocked immediately; they cannot fall through and emit a preview link.
- This does not attest to an untrusted test process, validate a real Playwright capture in this run, or fix the separate browser-runtime prerequisite. A–F overall status is unchanged.

## Prior VQ-03c timing correction

The prior VQ-03c raw JUnit at `vq03b3c-preoptin-reference-20261126/focused-tests.xml` is unchanged. Its suite `time` is 75.230 seconds and testcase-time sum is 74.935 seconds. The recorded start/end timestamps yield 80.298051 seconds wall elapsed. The earlier report's 57.746 seconds was incorrect/stale; the summary and checksum manifest now state the verified values.

## Replay and rollback

Replay the GREEN command in a clean checkout of source commit `4d88237e0611034d86df0d769442309f0f8e15d6` with the existing project-compatible Python environment and a nonexistent `DOCKER_HOST` socket. Do not include the browser subprocess test unless an authorized compatible runner is provisioned.

Rollback the source change by reverting only the source commit on the task branch. The evidence correction is documentation-only and does not change runtime behavior.
