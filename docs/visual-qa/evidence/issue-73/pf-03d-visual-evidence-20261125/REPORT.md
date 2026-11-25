# PF-03d — run-bound Visual QA evidence

Date: 2026-11-25 (ICT). PR #74 remains draft. This is a scoped milestone, not P0 completion.

## Source and behavior

- Tested source commit: `be4ec9a89bb29f0b0af55dc5f50172f97d2ab124`; tree `f8b66c323b60ea4dd7ed891b4b68e3056d52c484`.
- Expected published parent at start: PR #74 head `75fc1a263a567ca89a22f313c5378eaf5f6f7bc8`, tree `ecef647afe33b48b1c7f058722ed0dd1798455be`. Local pre-change commit had that same tree.
- ProjectFlow now requires a successful `visual_qa` stage to carry a complete run-specific UIFix artifact set. The CLI binds its manifest to run ID, tested commit, pinned UIFix 1.2.6, reference hash, route, viewport/DPR, threshold, and artifact names/sizes/hashes. TestRunner checks the clean source and current contract, copies the artifacts into coordinator `state_dir/visual-qa-evidence/<workspace-type>/<workspace-id>/<run-id>`, revalidates the copy, then appends the digest to the existing `test_runs.stdout_tail`. There is no DB migration or second gate authority.
- TaskDecisionService rechecks the marker, current commit/contract/reference and every artifact hash whenever it reads a visual PASS. Missing, stale, changed, malformed, or invalid evidence cannot pass; visual failures/timeouts remain non-waivable. Backend tasks remain opt-out. The CLI's coordinator output override is restricted to `.runtime/projectflow-visual-qa`; sandbox runs use `/workspace/.runtime/...` so files remain on the one mounted worktree.
- No claim of cryptographic attestation: this binds and detects accidental/stale/tampered files but does not independently recompute UIFix pixels or prove a malicious repo command did not forge a self-consistent manifest. Artifact retention has no automatic cleanup; operator-managed state retention remains necessary.

## Verification actually run

- `python -m pytest -q -ra tests/test_visual_qa_evidence.py tests/test_visual_qa_contract.py tests/test_visual_qa_execution.py tests/test_task_decision_required_stages.py tests/test_test_runner_source_identity.py tests/test_b06_sandbox.py tests/test_integration_test_run_stuck_after_restart.py --junitxml=...`: **77 passed, 0 failed, 0 errors, 0 skipped**, exit 0; 27.881s reported by JUnit. The tests include manifest corruption, mismatched/stale identity, threshold/non-finite metrics, path/symlink rejection, viewport inventory, TestRunner exit-0-without-evidence fail-closed, post-run artifact tamper, non-UI opt-out and no visual waiver.
- `node --check tools/visual_qa/visual-qa.mjs`: exit 0.
- `node --test tests/test_visual_qa_cli.mjs`: 4 passed, 0 failed/skipped, exit 0.
- `VIRTUAL_ENV=<existing test venv> ./scripts/preflight.sh`: `preflight PASS`, exit 0.
- `git diff --check`: exit 0 before commit.
- Real pinned UIFix/Chromium positive+negative subprocess replay is **UNVERIFIED/BLOCKED**. The Dell task worktree lacks `.runtime/uifix-v1.2.6/package.json` and `dist/diff/index.js`; the real browser subprocess test could not create its reference using pinned `sharp`. The authorized `ssh hp` read-only prerequisite check also found no `node`, `npm`, Chrome/Chromium command or UIFix install under `/home`. HP reports 6.5 GiB free on `/tmp`, 8.1 GiB available RAM, but 3.9/4.0 GiB swap used; no install or browser launch was attempted. See `hp-prerequisite.log`. No fake browser result was substituted. The 77 passing tests are contract/unit/sandbox regressions, not screenshot acceptance.
- No full suite, provider, CI, merge, deploy, production write, or shared daemon operation was run.

## Replay

From the repository root with its supported Python venv active:

```sh
python -m pytest -q -ra \
  tests/test_visual_qa_evidence.py \
  tests/test_visual_qa_contract.py \
  tests/test_visual_qa_execution.py \
  tests/test_task_decision_required_stages.py \
  tests/test_test_runner_source_identity.py \
  tests/test_b06_sandbox.py \
  tests/test_integration_test_run_stuck_after_restart.py \
  --junitxml=/tmp/pf-03d-targeted.xml
node --check tools/visual_qa/visual-qa.mjs
node --test tests/test_visual_qa_cli.mjs
./scripts/preflight.sh
```

The raw local logs are preserved outside the repository under the task artifact directory; repository copies are sanitized and portable. `hp-prerequisite.log` records the authorized runner preflight. `SHA256SUMS` covers all evidence files except itself.

## Gates and next action

A full suite remains FAIL from the previously recorded 1,371 pass / 194 fail / 20 errors / 6 skip run; it was not rerun here. B/C remain blocked on approved equivalent Nova reference/pilot. D's earlier UI opt-in/guide evidence is unchanged; this milestone adds source tests but does not independently prove the whole provider dispatch. E remains NOT PASS (workflow authorization/state and exact-head checks remain unresolved); F remains PASS for this scoped local work, with dirty main/WIP and production untouched.

Next: provision the already authorized pinned UIFix 1.2.6 and matching supported Playwright browser in a task-owned runtime, then run the real TestRunner positive/negative subprocess suite; review the non-attestation boundary; continue PF-04 only after that exact-source evidence is available. Rollback this milestone by reverting source commit `be4ec9a...`; remove only its evidence commit separately if needed.
