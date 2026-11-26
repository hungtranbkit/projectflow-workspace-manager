## 2026-11-26 PF-04b2 Test Design fake-runner seam

- Exact PR #74 candidate at start: `624b88ca9fd86db67c1d1f309c41468bacc22f82`, tree `a433f91871680da8d2fd34e5daaa497d2702a3af`; clean task worktree tree matched. Current base `main` was `cbeaffe417765f2778eeb9ad96cd0db21a9acb69`. Candidate/base Test Design source blob before fix was identical (`23522d1a4af7d1b14923191c1b0f03615cf603f0`).
- `tests/test_test_design.py` had the PF-04b1 seam: helper and custom deterministic fakes replaced only `invoker.runner`, while `PlannerAgentInvoker.invoke()` resolves `which('claude')` first. Three nodes reproduced on both base and candidate with restricted PATH: structured design expected READY but got EXECUTION_FAILED; review then returned 404; full coverage lacked both requirements.
- Test-only repair: inject resolver+runner together, assert planner/design/review share invoker, assert READY/REVIEWED in happy helpers, and add unavailable-CLI fail-closed/no-runner-call regression. Changed file blob `7ac58b7fd3bb4534de4a38d22e281936bdfa8fdf`; no production changes/provider invocation.
- Verification on Python 3.14.4/pytest 9.2.0: Test Design 44 passed, 1 real-provider node deselected, exit 0; PF-04b1 + planner envelope + visual contract/execution regressions 58 passed, exit 0; collection-only inventory 45; scoped `git diff --check` exit 0. No full suite/build/CI. Evidence: `docs/visual-qa/evidence/issue-73/pf-04b2-test-design-20261126/REPORT.md`.
- PR #74 remains draft; before publication exact head was `624b88c…`; no merge, CI enablement, provider or production operation. A remains unqualified by targeted tests; B/C blocked; E not PASS; F preserved.

## 2026-11-26 PF-04b1 spec lifecycle deterministic fixture seam

- On exact published PR #74 head `47abf6f147e5f91910b2e4684631adbee2fa9425` (tree-equivalent local task worktree; base parent `78e4ddf8d35036e3511163ff2b6e996e00b3a486`), three representative historical lifecycle failures reproduced identically on base and candidate with `PATH=/usr/bin:/bin`: fake runner was set, but `PlannerAgentInvoker.invoke()` first called real `which('claude')`, so analysis returned `EXECUTION_FAILED`; downstream proposal returned HTML 409.
- Test-only change in `tests/test_spec_lifecycle.py`: deterministic helper now injects both executable resolver and runner, checks shared invoker identity across Planner/Requirement Analysis/Spec Author/Spec Review, and `do_analyze()` asserts `READY`. Added fail-closed test proving unavailable CLI produces no work product and never calls the runner. The real-provider lifecycle test is unchanged and not run.
- Verification: deterministic lifecycle module 38 passed, 1 real-provider test deselected, exit 0 (44.46s); planner failure-envelope + visual contract/execution regression 20 passed, exit 0 (4.45s); `git diff --check` exit 0. No full suite/provider/CI. Evidence/JUnit/red reproductions and replay are in `docs/visual-qa/evidence/issue-73/pf-04b1-spec-lifecycle-20261126/REPORT.md`. Candidate changed-file blob `0fc734168ab7afc0bc6e30b02a1b58b45c510d4a`.
- Published on existing draft PR #74 with normal expected-head fast-forward: remote commit `d7fa2f0cfa0765efa1a4ec2ee0975befaf5085bd`, parent `47abf6f147e5f91910b2e4684631adbee2fa9425`, tree `581b2a9af01d7449d8d4da304296f8f8c84fa905`. Local task branch `agent/codex/issue-73-engineering-fixture-20261125` has equivalent content at local commit `1c7948600e3786aea1cd59483b276e0c5d9c1a4a`; it preserves its existing separate history. No merge/CI activation. A remains unqualified by targeted tests; B/C blocked; E not PASS; F preserved. PF-04b1 milestone is complete; next work requires a separate owner/scope check, with no provider invocation absent authorization.

## 2026-11-26 PF-03e2 mandatory TestRunner sandbox proof

- A real `AUTH_MODE=required` app is wired to the mandatory `SandboxedCommandRunner`; its `TestRunner` test and `visual_qa` stages now run through `SandboxRuntimeService.run_ephemeral` with pinned Noble Playwright image digest `sha256:83192064c7510f7ee73dd63dc5f22a5e01a92c81a2e6a9c715d9e3fe55471fd9`. Root cause reproduced before the patch: the TestRunner creates evidence directories as `0700`, but the image default user with all capabilities dropped could not write them. `run_ephemeral` now starts commands as the bind-mounted worktree owner UID:GID. Network-none, cap-drop, no-new-privileges, resource limits, one-worktree mount, and explicit environment remain in place.
- Added `tests/test_visual_qa_mandatory_sandbox.py` with an in-container-only synthetic loopback server and real UIFix 1.2.6 / Playwright 1.60 execution. Positive 0% is `PASS`; deliberate full-frame color change is 100% and `FAIL`. TaskDecisionService returned PASS for valid evidence, FAIL after artifact tampering, NOT_RUN for stale SHA/missing reference/startup-skip, and FAIL for exit 0 without evidence, negative diff, and timeout. Timeout sends Docker kill/remove; no `wm-exec-*` containers remained. Synthetic evidence is not NovaRetail design approval.
- `tests/test_b06_sandbox.py` now checks numeric owner identity (`id -u`) because a numeric container UID need not have a `/etc/passwd` name. This is a behavior-preserving sandbox probe. Vietnamese guide documents the required-mode runner and same-container loopback requirement.
- Verification: new mandatory sandbox integration test 1/1 passed with JUnit; related visual contract/execution, TaskDecisionService, source identity, and network-isolated sandbox selection 56 passed, 0 failed, 0 skipped. An additional host-only legacy visual subprocess test could not find Chromium headless-shell rev1223 in the host cache; it is an environment limitation and was not retried or used as mandatory-path evidence. No full suite, provider invocation, or CI run in this milestone. Exact pass artifacts are in task-owned evidence `task-artifacts/issue-73-pf03e2-mandatory-20261126-final`; sanitized before-fix output is in `...-red2`.
- Tested source commit: `878c5a5f11f3a312cd6b11ae57dcf0847048969a`, tree `9b7d7d94af6081eb0a237963cbafa2092a195b5f`. The test commit was replayed exactly after commit. Portable before/after report, real screenshots/metrics, JUnit, replay command, and non-self-referential SHA-256 manifest: `docs/visual-qa/evidence/issue-73/pf-03e2-mandatory-sandbox-20261126/REPORT.md`. Remote PR #74 was `78e4ddf8d35036e3511163ff2b6e996e00b3a486` immediately before this publication attempt; local worktree branch is `agent/codex/issue-73-engineering-fixture-20261125`.
- A/D/F historical local state is unchanged; this verifies only the mandatory sandbox path, not the full-suite A gate, approved-design B/C, or exact-head CI E. Next: publish through the GitHub connector only if the PR head remains the verified expected SHA; then continue separately authorized PF-04a provider/full-suite work. No full suite/provider/CI was run here.

## 2026-11-25 PF-03e2 pinned browser through TestRunner

- Published test-only commit `10455bb4b0b52beb2c06fc3fbd9b339183a49fc0` on draft PR #74, tree `4c9011ec3e0694b87e889077819b2a195e7ce6be`; changed test blob `dd5db08eedda8f524a81753a35479f5575325e0f`. The test replaces the prior fake Git adapter with real `GitWorkspaceService`, and delegates TestRunner commands to a real `SandboxedCommandRunner` while observing artifacts before coordinator cleanup.
- Exact-head TestRunner/UIFix subprocess run in official Ubuntu 24.04.4 Playwright image digest `sha256:83192064c7510f7ee73dd63dc5f22a5e01a92c81a2e6a9c715d9e3fe55471fd9`: Node 24.15.0, Python 3.12.3/pytest 9.2.0, UIFix 1.2.6 / Playwright 1.60.0 / Chromium rev1223 Chrome 148.0.7778.96. `verify_uifix.py` passed. JUnit: 1 passed, 0 failed/errors/skips, exit 0.
- Evidence: `docs/visual-qa/evidence/issue-73/pf-03e2-noble-runner-20261125/REPORT.md` (screenshots, metrics, stage ledger, JUnit, replay, and SHA256 manifest). Synthetic reference SHA `b89caa21...`; positive diff 0/76,800; negative color diff 76,800/76,800. TaskDecisionService observed positive PASS, negative FAIL, stale NOT_RUN, tamper FAIL, missing reference NOT_RUN, unavailable server FAIL, backend opt-out PASS.
- Runtime was an isolated network-none, read-only, bounded container with no mounts/socket. `SandboxedCommandRunner` ran its direct-host mode there; mandatory Docker sandbox executor was not tested. This is not NovaRetail design approval or full-suite/CI proof. Full suite not run; PR workflow remains separately gated. No production or shared daemon mutation.
- Gate state unchanged: A not verified by this milestone (historical full-suite state remains FAIL); B/C not accepted; D historical local PASS; E no exact-head required CI PASS; F preserved. Next: PF-04 work only after separately authorized provider/runtime prerequisites; require exact-head suite and CI before any overall completion claim.

## 2026-11-25 PF-03c required test-stage membership

- `TaskDecisionService` now derives required stage names from `load_contract()` and requires a latest current-source row for every named stage; unrelated rows cannot cover missing stages. QUEUED/RUNNING/SKIPPED stays NOT_RUN; required FAIL/TIMEOUT blocks. Exact-fingerprint baseline waivers remain for ordinary stages, while visual_qa FAIL/TIMEOUT cannot be waived. No visual artifact validity is checked here (PF-03d scope).
- Missing/valid zero-stage legacy behavior is retained: integration PASS; builder uses the prior no-contract row semantics. A present malformed contract is no longer treated as legacy PASS. UI task opt-in requires the visual stage; backend opt-out remains intact.
- Real temporary-Git + checked-in contract + SQLite regression: before 10 failed/3 passed, exit 1; after 13/13 passed. Combined required-stage, source identity, baseline waiver, timeout, cross-repo, merge, visual contract/execution, decision and sandbox runner tests: 77 passed, 0 skipped, exit 0; preflight PASS. Full suite/provider/CI not run. Evidence: `docs/visual-qa/evidence/issue-73/pf-03c-required-stage-membership-20261125/REPORT.md`.
- Tested source commit `d9b0cf203d2f5ab4a457cd290673ac415e250259` / tree `07dfaa2662c09373f60e90b4bb9a403b578f67ec` was published as PR #74 source commit `aaf03776f115a191c65de59384b1e12673713567` with the same tree, normal expected-head fast-forward from `dd0c96e40b463e06a7738a29522312e0756449ba`; base `ac288928ab559799e6a6f34ff9be1de5fdfa5af3`. Local branch: `agent/codex/issue-73-engineering-fixture-20261125`. A remains FAIL; B/C blocked; D prior-local only; E not PASS; F preserved.

# Project Context — ProjectFlow visual QA issue #73

## 2026-11-25 PF-03b TestRunner source identity

- `GitWorkspaceService.source_state()` now observes HEAD plus a SHA-256 digest of tracked/index diff; `TestRunner.start()` captures one stable identity around contract loading and uses one `tested_commit` for every stage. Dirty tracked/untracked input remains runnable for feedback but is stored with `tested_commit=NULL` and an explicit `SOURCE_NOT_COMMIT_BOUND` note.
- Before/after runner checks reject observed HEAD/tracked-source drift and newly untracked paths during clean-source runs; shell exit 0 cannot turn that run into PASS. `TaskDecisionService` now treats a dirty current builder/integration source as `NOT_RUN`; zero-required-stage legacy integration behavior is unchanged. The existing single gate authority remains in place.
- Regression uses real temporary Git repos, real DB, actual git state reads and synchronized actual shell commands. Before: 4 failing races / 1 prior stale-commit test passed, exit 1. After: source identity 9/9 PASS; combined runner/sandbox/merge/visual/decision suite 60/60 PASS, exit 0. Evidence/JUnit/checksum: `docs/visual-qa/evidence/issue-73/pf-03b-source-identity-20261125/REPORT.md`.
- Mandatory sandbox test used the existing isolated Docker `--rm` runner, network none; no `wm-exec-*` container remained. No full suite, provider call, or CI run. ABA remains possible if source changes and is restored between observations; see report limits.
- Worktree branch `agent/codex/issue-73-engineering-fixture-20261125` started at `f1d09c48de10a2c41d8dc907c9d1799df467832f`; PR #74 expected head at start was `e95e5d77b189d3774d02b37a9efd1475b4e88d2d`. A stays FAIL; B/C blocked; D is prior local evidence only; E not PASS; F preserved.

## 2026-11-25 PF-03a integration contract source

- In `app/services/integration_service.py`, `verify_integrated_state()` now loads `PROJECT.yaml` from its detached probe at the exact integrated commit, instead of the mutable canonical checkout. This preserves candidate-bound gate selection and Visual QA fail-closed behavior; it does not change merge sequencing or add another decision authority.
- Added `tests/test_integration_contract_probe.py`, using real temporary Git repositories and the actual GitWorkspaceService probe. Before the fix: 4 failed/exit 1, reproducing canonical command substitution, required-visual contract substitution when candidate contract is absent/invalid, and dirty canonical override. After: 4 passed. Combined merge/cross-repo, visual contract/execution, and TaskDecisionService workflow-decision tests: 51 passed, 0 skipped, exit 0.
- Evidence and JUnit/logs: `docs/visual-qa/evidence/issue-73/pf-03a-contract-source-20261125/REPORT.md`. Python 3.14.4 / pytest 9.2.0 using existing task venv; no provider/network, full suite, build, or CI run.
- Tested local commit `57dcc4a431d0c9c5da26ec0c705e925bb13b9fc9` and published PR #74 commit `a92a96b2e222946150f0c4e467b6b1c15382a9cc` have identical tree `acebc5685b06073c006f1e1b17096b70ccebccb3`; the remote commit is a non-force fast-forward from expected head `4164afde07276ebc07de72f22c89fcd289f0a1ad`. PR remains draft/open; exact SHA currently has zero workflow runs.
- A remains FAIL (full suite not rerun), B/C remain blocked by approved-equivalent Nova reference/pilot, E has no qualifying exact-SHA CI proof; F preserved. Next: separately scope PF-03b; do not mix PF-02b/provider work into this patch.

## 2026-11-25 ProjectFlow golden-test follow-up

- Follow-up branch: `agent/codex/issue-73-runtime-isolation-20261125`, based on PR #74 published source SHA `50ce031328cfaad4e976dc5507ff5e9908d18b42`. Isolated scratch only; dirty `main` (`PROJECT.yaml` and local package artifacts) and the existing visual QA WIP at `2805bcd...` were preserved.
- `tests/test_golden_e2e.py` uses `sys.executable` for its three child pytest launches, while retaining the child `PATH=/usr/bin:/bin` and bytecode restriction. HP reproduction before fix failed both golden tests because `/usr/bin/python3` lacked pytest; after the fix the child pytest commands proceed. On HP the golden flows then hit the separately missing Claude CLI in CodeReview. On Dell, the existing deterministic `set_fake` review seam allowed both golden cases to pass without a provider request.
- `PROJECT.yaml` test timeout is now 4500 seconds (75 minutes), based on recorded suite durations of 2101s and 3137s. `tests/test_project_test_timeout.py` proves TestRunner forwards the configured value and that timeout remains TIMEOUT/exit 124/TEST_FAIL.
- Candidate targeted verification: 21 tests passed (golden 2, timeout 2, visual contract/execution 17), exit 0, 73.98s; preflight/build/smoke each exit 0 on HP. No full suite rerun. Exact JUnit/fingerprint ledger and sanitized logs: `docs/visual-qa/evidence/issue-73/golden-timeout-repair-20261125/`.
- Historical PR #74 suite remains FAIL at base source: 1371 passed/194 failed/20 errors/6 skips, exit 1. The 194 failures plus 20 errors have 120 status/fingerprint groups (122 including 2 skip groups); only 39 failures explicitly say Claude CLI missing. All 20 errors parse a missing-requirement HTML page as JSON. Remaining failures are not yet attributed. PR #74 has no valid CI PASS; do not treat this targeted result as Gate A.
- Next: review and publish this scoped patch as a fast-forward of PR #74 if the expected parent SHA is unchanged; obtain an authorized Claude CLI runner for provider-dependent suite tests; diagnose other failure groups before another full-suite run. B/C visual approval and E CI remain blocked; no production/daemon/CI changes were made.

Updated 2026-11-24 ICT. Recovery is open; this is a handoff, not a completion claim.

## Scope and safety

- ProjectFlow task worktree: `/home/dell/workspace/.worktrees/projectflow-visual-qa-73`, branch `agent/codex/visual-qa-73`; local HEAD `2805bcd37fa9f183f7e38a887fc8891884051961` remains dirty with WIP. Do not reset, clean, or overwrite it. Draft PR #74 current exact head is `d1e0010f98d47e85f258e1c07ed0598f6ddee373`, a documentation/evidence-only fast-forward from `e093d51471bf32481a8319035185199fc8206f4b`; local code WIP remains dirty and unchanged. Code publication and verification used the GitHub connector plus a detached exact-SHA checkout. Preserve dirty `main`, all WIP, and remote history.
- No production writes, merge/deploy, force push, shared Docker cleanup, or workflow bypass occurred. Local `gh auth status` reports its token invalid; GitHub connector is the safe publication route.
- NovaRetail is separate: PR #160 exact head `680dbd434081306c3b360b1ffd643d1bd10ce76a`; POS proposal remains unapproved fixture-only at 5.784358%, original legacy order screen 11.524542% and `REFERENCE_NOT_EQUIVALENT`. Do not claim product visual acceptance or alter its reference in this ProjectFlow-only turn.

## ProjectFlow change and verification

- `tests/test_visual_qa_runner_subprocess.py` exercises actual `TestRunner.start()`, `TaskDecisionService`, temporary SQLite, the default host subprocess command runner, local git fixture, loopback server and pinned UIFix/Chromium. It covers exact positive, intentional color negative, wrong dimensions, missing ref, unavailable server, backend opt-out and stale-SHA evidence. The real visual stage is dispatched; this does not invoke Codex/Claude generation providers.
- `pyproject.toml` has `pythonpath = [".", "tests"]`. With the repository `tests/__init__.py` marker, the old root-only pythonpath reproduced 11 collection errors because legacy tests import `conftest` and sibling test modules by top-level names. The updated exact-SHA collect-only probe sums to 1,575 items without import errors or weakened assertions. Full `./scripts/test.sh` on exact remote SHA `cee006ca53eb36b7be29cf654fb3081844c4cce1` exited 0 with 1,575 passed and 1 skipped in 3,137.38s. The collection sum (1,575) and runtime outcome total (1,576) differ by one and remain unresolved; see the exact-SHA evidence folder. No attempt was made to hide the existing skip.
- Targeted command (authorized browser wrapper and existing Playwright package): `CHROME_PATH=<authorized-chromium-wrapper> VISUAL_QA_PLAYWRIGHT_FROM="$PWD" .venv/bin/python -m pytest -p no:cacheprovider -q tests/test_visual_qa_contract.py tests/test_visual_qa_execution.py tests/test_visual_qa_runner_subprocess.py` — 18 passed, exit 0.
- Fixed the test-only Docker IPAM exhaustion without changing daemon configuration: `tests/conftest.py` assigns a route-checked unique `/28` from test pool `10.252.0.0/16` to each temporary sandbox Compose project through its own `.env`, then calls the existing real Compose runtime method. Direct NGINX Compose fixtures use that test-only network contract. The 14 formerly failing sandbox node IDs pass on the exact SHA. After the full suite, read-only inventory returned to 29 networks and zero `wm-*` containers/networks. No daemon config, production, or unrelated shared Docker state was changed.
- Subprocess evidence with real local command execution is in `docs/visual-qa/evidence/issue-73/runner-integration-subprocess-20261124-02/`; the fixture uses 0 threshold only to prove exact dispatch and failure semantics, not Nova's 8% product gate.
- Actual provider dispatch proof is committed at `docs/visual-qa/evidence/issue-73/projectflow-dispatch-runtime-20261124/` on this PR head. In an isolated Nova POS clone, a seeded CSS regression failed the real TestRunner UIFix stage at 26.680849%; one authenticated Claude Builder invocation read the project-local pinned skill and restored one scoped grid rule; the real TestRunner then passed 5.784358% on the new scratch SHA. TaskDecisionService read FAIL then PASS; a later SHA change read NOT_RUN; backend without the marker ran only `test`. The scratch test command was `true` and is not counted as a real suite test. This is unapproved synthetic proposal evidence only, not a product approval.
- Latest full log is sanitized and stored at `docs/visual-qa/evidence/issue-73/full-suite-runtime-20261124/full-suite.log`. Replay and guide are in `docs/VISUAL_QA_HUONG_DAN.md`.

## Gate state and blockers

- A PASS locally on exact PR source SHA: `./scripts/test.sh` exit 0, 1,575 passed / 1 skipped; Docker IPAM fixed in test fixtures. The collect-only count is one less than runtime outcomes and is still being investigated. Exact-SHA GitHub CI is a separate E gate.
- B FAIL: approved Nova legacy reference mismatch 11.524542%; POS proposal 5.784358% is unapproved fixture-only.
- C FAIL for acceptance: React preview interactions/responsive smoke were previously exercised, but approved equivalent visual reference is absent.
- D PASS for UI-only opt-in, Vietnamese guide, and actual provider→TestRunner lifecycle on a disposable proposal fixture. This does not establish approved-design equivalence.
- E NOT PASS: PR #74 exact head `d1e0010f98d47e85f258e1c07ed0598f6ddee373` has zero workflow runs/statuses because the workflow is disabled. Nova #160 exact head `680dbd434081306c3b360b1ffd643d1bd10ce76a` has Audit `45401064093` and Application `45401064066` queued with no status contexts at last check. Maintainer action is required; do not bypass.
- F PASS so far: task changes stayed isolated, no production/shared-runner/main mutation.

## Next actions

1. Diagnose why collect-only sums to 1,575 but pytest runtime reports 1,575 passes plus one skip, without changing test selection or assertions.
2. Ask the ProjectFlow maintainer to authorize/re-enable the disabled required Actions workflow and run checks on the latest PR #74 head; E remains blocked until checks run and pass.
3. Continue independent Nova visual equivalence/provider work in its existing owned scope; keep POS proposal unapproved and legacy order reference `REFERENCE_NOT_EQUIVALENT`.

## Latest exact-SHA handoff

- Evidence: `docs/visual-qa/evidence/issue-73/full-suite-runtime-20261124-ipam-fix/REPORT.md`, logs, host inventory, and `SHA256SUMS.txt`.
- Full suite on exact clean detached checkout: 1,575 passed, 1 skipped, exit 0. Preflight/build, 14 recovered sandbox tests, 18 visual runner tests, CLI 4/4, and smoke passed. The runtime integration remains independent of external Claude/Codex provider invocation.
- Full suite remains verified on source SHA `cee006ca53eb36b7be29cf654fb3081844c4cce1`; descendant `e093d514...` and `d1e0010...` add docs/evidence only. Collection/runtime count delta remains unresolved.
- Latest runtime artifact commit `d1e0010f98d47e85f258e1c07ed0598f6ddee373` contains actual dispatch logs, screenshots, metrics, source patch, and updated VN guide. Exact-head ProjectFlow Actions still has zero runs. No CI bypass or maintainer setting change was made.

## 2026-11-25 engineering lifecycle fixture follow-up

- Isolated candidate worktree `agent/codex/issue-73-engineering-fixture-20261125` started clean at PR #74 head `8212795c1932ce3292fcd11259c1d3d2b2e7fd55`; the published branch was unchanged during this verification. Dirty main and the separate visual QA WIP were not touched.
- `tests/test_engineering_lifecycle_ui.py` deterministic fake-provider seam now injects both `invoker.which` and `invoker.runner`. `seeded_change()` asserts JSON/status/outcome/work-product contracts for requirement analysis and spec proposal before consuming results. Production route behavior and provider tests are unchanged.
- Before the patch, one test reproduced the reported JSONDecodeError on both PR head and base `ac288928ab559799e6a6f34ff9be1de5fdfa5af3`: with Claude excluded from PATH, analysis returned JSON `EXECUTION_FAILED` (`claude CLI not found in PATH`), then proposal correctly returned HTML 409 because no analysis existed. The helper had patched `runner` only, while `PlannerAgentInvoker` checks `which('claude')` first. This is a deterministic fixture/provider-availability mismatch, not a PR regression.
- After patch, full `tests/test_engineering_lifecycle_ui.py` passed 27/27, 0 skips/errors, exit 0 in 62.67s with PATH `/usr/bin:/bin`; JUnit and logs are in task artifacts `issue-73-engineering-lifecycle-20261125`. Visual contract/execution regression tests passed 17/17. One architecture-design representative test still fails under the same no-Claude PATH because its separate helper also patches only `runner`; that group was not changed in this scoped milestone.
- PF-01 source repair was published at commit `178a8e96a1b6be86c6c61a6719da666e5d162e0e` (test blob `9644b73abe47d9bfdca54df43e5f69b056a68901`); a handoff-only commit followed. Full suite was not rerun; Gate A remained FAIL, visual B/C remained blocked, CI E not PASS. The architecture/design seam is tracked separately below; do not infer all requirement/design fingerprints share one cause.


## 2026-11-25 PF-02a architecture/design fake-runner seam

- Exact PR #74 head at task start was `14e1aba3442a9296ccc82cdd6a505642689b7ff5`, base `ac288928ab559799e6a6f34ff9be1de5fdfa5af3`. Clean detached base/candidate snapshots reproduced the same two failures with restricted `PATH=/usr/bin:/bin`: architecture analysis and technical design returned HTTP 200 JSON `EXECUTION_FAILED` because `claude` was unavailable. `tests/test_architecture_design.py` fake setup replaced only the subprocess runner, while the invoker checks executable availability first. No provider call occurred.
- Test-only change in `tests/test_architecture_design.py`: `install_fake_runner()` asserts the architecture/design services share the planner invoker and injects both availability resolver and deterministic runner. The helper is used for all deterministic fakes, including two direct-runner tests. The real-provider lifecycle test remains unchanged and was not run.
- Local tested code commit `51ed141588da4533abaa530bdcae37bd73cea92b`. With Python 3.14.4/pytest 9.2.0, combined deterministic architecture/design, provider-envelope, and visual contract/execution run: 72 passed, 1 deliberately deselected real-provider test, exit 0, 66.00s. Separate provider-unavailable fail-closed probe exited 0 before runner invocation. Full suite was not run; Gate A remains FAIL.
- Evidence folder: `docs/visual-qa/evidence/issue-73/pf-02a-architecture-design-20261125/` contains sanitized base/candidate reproductions, logs, JUnit, replay command, and hash manifest. Runtime preflight found local Claude Code 2.1.332 but did not probe auth/quota or invoke the provider. UIFix pin was verified; no browser was launched. PR #74 publication is a normal expected-head fast-forward from parent `14e1aba3442a9296ccc82cdd6a505642689b7ff5`; the resulting remote SHA is recorded in the latest #73 checkpoint. Next: continue PF-02b/PF-03 as separate milestones.

## 2026-11-25 PF-03d visual evidence validation

- Source commit `be4ec9a89bb29f0b0af55dc5f50172f97d2ab124` (tree `f8b66c323b60ea4dd7ed891b4b68e3056d52c484`) adds the run-scoped manifest contract and revalidation path. TestRunner injects DB run ID/current clean SHA into the pinned CLI, verifies exact reference/route/viewport/DPR/threshold/UIFix/artifact inventory and hashes, copies to configured coordinator state storage, and appends a manifest digest only after validation. TaskDecisionService rechecks the current evidence at each gate read; missing or mutated proof cannot pass. No schema migration; TaskDecisionService remains the only authority. Backend opt-out, visual non-waiver and timeout/failure semantics remain.
- Verification: targeted set 77 passed, 0 failed/errors/skips, exit 0 (JUnit 27.881s); Node CLI tests 4/4; preflight PASS; node syntax check and `git diff --check` exit 0. Evidence: `docs/visual-qa/evidence/issue-73/pf-03d-visual-evidence-20261125/REPORT.md`.
- PF-03e2 later verified real pinned UIFix/Chromium positive/negative subprocesses through TestRunner and TaskDecisionService in an isolated official Noble container; see the PF-03e2 handoff at the top of this file. Mandatory Docker sandbox executor remains unverified, and synthetic evidence is not NovaRetail approval.
- Next: PF-04 is separate. Do not repeat host install or browser benchmark. Roll back only task-owned source/evidence commits and container resources.

## 2026-11-26 PF-04b3 release-pipeline test fixture seam

- On the existing task branch `agent/codex/issue-73-engineering-fixture-20261125`, a test-only change in `tests/test_release_pipeline.py` replaces the E10 fake setup that patched only `invoker.runner`. The local helper asserts planner/review share the same invoker and injects both test-only `which('claude')` and runner behavior; production and live-provider tests are untouched.
- Red reproduction on both base `cbeaffe417765f2778eeb9ad96cd0db21a9acb69` and candidate tree `184bc0cd20892abf532034d883b6d50858b2501c`: two release-pipeline representatives fail with `EXECUTION_FAILED: claude CLI not found in PATH` under `PATH=/usr/bin:/bin`. Green full affected module: 29 passed, 0 failed/errors/skips, exit 0; Python 3.14.4/pytest 9.2.0. No full suite or provider call.
- The historical fingerprint row contains 21 `provider_cli_missing` release-pipeline node IDs; all 21 map exactly to PASS in the new module JUnit. Two other historical failures in the module are explicitly not attributed to this repair. Evidence and red/green logs/JUnit plus checksum manifest: `docs/visual-qa/evidence/issue-73/pf-04b3-release-test-fixture-20261126/REPORT.md`.
- At verification, PR #74 was still at remote `678980be18d95aec4e7bdfdd765c67d472c0e800`; local pre-commit HEAD `42c93530f4e658462ccac5d7a0b32103601de85a` had the same tree. Publish only by expected-head fast-forward. Gate A (full suite) and E (exact-SHA CI) remain not PASS; no production, main, shared runner, CI configuration, merge, or deployment changes.

## 2026-11-26 PF-04b4 Planner Service deterministic fixture seam

- Test-only change in `tests/test_planner_service.py`: its fake helper now injects both `PlannerAgentInvoker.which` and `.runner`, covering valid, malformed, usage-limit, and retry fake paths. Added an unavailable-Claude fail-closed check (runner not called, no plan persisted). Production behavior and real-provider test are unchanged.
- Reproduced three requested candidate failures under `PATH=/usr/bin:/bin`; all were `claude CLI not found in PATH`. Two common nodes also failed on base. The usage-limit test did not exist on base, so base malformed-envelope test was used as an available negative control; see report.
- Verification on Python 3.14.4/pytest 9.2.0: planner module 42 passed / 1 real-provider skipped, exit 0; 43 collected. Historical ledger mapping confirms all 30 prior failed nodes in this module now PASS; historical provider CLI skip remains SKIP. Related release pipeline + visual contract/execution: 46 passed, exit 0. Full suite not run, no provider called.
- Evidence: `docs/visual-qa/evidence/issue-73/pf-04b4-planner-fixture-20261126/REPORT.md` and `historical-planner-resolution.tsv`; checksum manifest covers raw logs/JUnit and report.
- Local task branch `agent/codex/issue-73-engineering-fixture-20261125`; at task start it was clean and tree-equivalent to PR #74 expected head `5d1f2e0b982dcf4468a1939601040ceb3746b114` / tree `517f1255eba2810edd7a112ba3ff71b47e220cec`. A remains not PASS without full suite; E remains not PASS absent actual exact-head CI. No main/WIP/production/Nova/shared-runner changes.

## 2026-11-26 PF-04b5 Review Fix Loop fixture seam

- Test-only repair in `tests/test_review_fix_loop.py`: shared helper verifies CodeReview, SecurityReview and Planner share one invoker and injects both test-only Claude resolver and runner for deterministic review fakes. Added unavailable-provider fail-closed test: runner not called, no verdict/review row/report.
- Red: three representative code/security review tests failed on base and candidate with missing Claude resolver. Base lacked tracked `tests/__init__.py` and initially hit installed-package shadowing; the base diagnostic used an empty marker only in the disposable base worktree. No production files changed.
- Candidate whole module: 28 pass / 2 fail / 0 error / 0 skip, exit 1; 30 collected. Historical ledger has 17 failed node IDs: 15 map to PASS, 2 remain FAIL because follow-up Codex Builder launch is unavailable (`Codex CLI chưa được cài hoặc không có trong PATH`). These are outside the reviewer resolver seam. Release + visual contract/execution regressions: 46 passed, exit 0. Real-provider module and full suite not run.
- Evidence and hashes: `docs/visual-qa/evidence/issue-73/pf-04b5-review-fix-loop-20261126/REPORT.md`; no provider/CI/production/main changes. At task start branch was clean and tree-equivalent to PR #74 head `4df44ae5a8695ebb836b6b0d5c1401fd4ef904b7`. A and E remain not PASS.

## 2026-11-26 PF-04b6 fix-task launch callback test isolation

- In `tests/test_review_fix_loop.py`, two fix-task orchestration tests now replace only the orchestrator's injected `_start_builder_session` callback with a deterministic test-only callback. It returns a test session ID without creating a process/session record and captures the transferred workspace row. Tests require FIX_BUILDER_LAUNCHED, the fix-task identity, real worktree ownership/branch/path and actual git fix commit/finding resolution. A negative callback regression verifies recoverable transferred worktree and AUTO_EXECUTION_FAILED without AUTO_BUILDER_LAUNCHED.
- Exact candidate-start-tree red reproduced before the callback: both residual tests got `EXECUTION_FAILED` with missing Codex CLI and then `KeyError` for `fix_task_id`. No base comparison is claimed in PF-04b6. Production is untouched.
- Verification: focused tests 3/3 pass; full Review Fix Loop module 31/31 pass, no skips; all 17 historical failed IDs map PASS. Release pipeline + visual regressions 46/46 pass; builder failure/prompt delivery selected regressions 3/3 pass. No full suite or provider call. Evidence: `docs/visual-qa/evidence/issue-73/pf-04b6-fix-builder-callback-20261126/REPORT.md`.
- Existing task branch start tree equalled PR #74 head `59b255557e399cd0b59a12ca1e0018a711224884` / `c2bafb79eca763f08f2629ce2a56d97d498893ae`; only test/evidence/context scope. A and E remain not PASS.

## 2026-11-26 PF-04b7 B2 release-concurrency residual verification

- On the existing task branch/worktree, remote PR #74 head `5e4ab28c05b922bccc6e2fd7e207a29d61718b85` and local commit `e443d603e3e90f5c43b71a80ab51abc231e693f3` had identical tree `7ea8df1d08e802406d6415dafe22ab931fa36b74`; B2 test blob matched exact remote (`a6fc9b1689211ba64ba10e7430d5724a09316df5`).
- `PATH=/usr/bin:/bin` full `tests/test_b2_release_concurrency.py` run: 4/4 passed, 0 skipped, exit 0 (Python 3.14.4 / pytest 9.2.0). Three historical provider-CLI failure nodes now pass with the PF-04b5/6 test seam; actual threaded SQLite release operations, Git fixture, uniqueness/error/no-orphan assertions, and XSS route assertion were exercised. No source code changed.
- Evidence/log/JUnit/node mapping/checksum: `docs/visual-qa/evidence/issue-73/pf-04b7-b2-concurrency-20261126/REPORT.md`. Full suite and CI not run; A remains FAIL and E NOT PASS.

## 2026-11-26 PF-04b8 Execution Waves deterministic fixture

- On exact PR #74 starting tree `3316adb87418d7a05adba7fbd6cc18a7c14be40f`, the first two historical Execution Waves failures reproduced on both base and candidate: fixture installed a Claude fake while the scheduler selected Codex. `fake_launcher()` merges the map, so the existing Codex launcher still invoked the unavailable real CLI. A corrected runtime diagnostic recorded `EXECUTION_FAILED` with the Codex CLI missing message, `NO_TASKS_LAUNCHED`, and zero sessions; no scheduler race defect was found.
- Only `tests/test_execution_waves.py` fake launcher selection changed: the two tests now assert default provider Codex and install the matching disposable Codex bash/PTY launcher. Assertions for concurrent reservations, sibling isolation, actual PIDs, worktree/Git isolation, review/integration and wave-2 base remain.
- Final module: 22 passed + 1 unchanged real-Claude CLI skip, exit 0; related selected autonomous/review/visual tests 21 passed, exit 0. Historical 4 node IDs map to PASS; no live provider call or full suite. Evidence/checksums in `docs/visual-qa/evidence/issue-73/pf-04b8-execution-waves-20261126/REPORT.md`. A remains FAIL; E not PASS.

## 2026-11-26 PF-04b9 incident/product-acceptance fixture stages

- On the task-owned tree matching PR #74 start head `f86550f6eda4fa2fc467205282ce4fff412d124a` / tree `814c6b2c0c746dec78912d843f6162440d43b200`, the three historical incident/product-acceptance nodes were run with `PATH=/usr/bin:/bin`. Two incident nodes passed. Product acceptance initially failed at `TESTS_PASS`: its repo fixture requires `preflight` and `test`, but the scenario seeded only `test` evidence.
- Test-only correction in `tests/test_product_acceptance.py` seeds both named required stages, pinned to the same fixture commit; no gate logic or assertions changed. The acceptance node then passed and both entire affected modules passed: 45 passed, 0 failures/errors/skips, exit 0 (Python 3.14.4 / pytest 9.2.0).
- Base comparison was inconclusive: a disposable archive needed an empty `tests/__init__.py` marker to avoid installed-package shadowing, then stopped earlier because restricted PATH lacked Claude CLI in the shared review fixture. It did not reach `TESTS_PASS`. Raw logs/JUnit and details: `docs/visual-qa/evidence/issue-73/pf-04b9-incident-acceptance-fixtures-20261126/REPORT.md`.
- This is a targeted test milestone, not a full-suite or CI pass. A remains not PASS; E remains not PASS until required checks run and pass. No provider, production, main/WIP, Nova, shared runner, or CI setting was touched.

## 2026-11-26 PF-04b9 fresh full-suite runner checkpoint

- Reverified published source SHA `70d467ce7532d6f4fdcb9a238157daba3737afa1` / tree `3639037adab78f941f22817b235cb7f43373e579`; ran exact-source preflight/build/smoke and UIFix verification successfully. Fresh deterministic prepass: 6/6 passed; fresh collect-only inventory sums to 1,627 test items plus one module-level skip because this venv lacks Python Playwright. No full-suite result is claimed.
- Full suite was not started: Dell had an unrelated pytest in the main checkout, an active Claude session, and one existing `/tmp/pytest-of-dell/` trust entry; real-provider test cleanup sweeps every such entry in `~/.claude.json`. HP was not viable (13 GiB free, 95% root usage, swap exhausted, 11 provider/agent processes, no Claude CLI). No provider call, shared config edit, provider hiding, dependency installation, Docker mutation, or production change occurred.
- Evidence and exact blocker: `docs/visual-qa/evidence/issue-73/pf-04b9-fresh-full-suite-20261126/REPORT.md`. Gate A remains unqualified until an isolated authorized provider/browser runner can execute unchanged `./scripts/test.sh` with JUnit and `-rs`; E remains not PASS without exact-SHA required CI.
## 2026-11-26 PF-04b10 — isolated Claude trust config (task branch)

- Work is isolated in `agent/codex/issue-73-provider-test-isolation-20261126`,
  based on local commit `b3c0a23a46ee0273dc4954b906700f76bb04654c` (tree
  `205452217c23ba8ff88dfb2c2022c19a2ea9a58a`). At preflight PR #74 remote
  head was `e3755269baa42fb266044a9441f4f8e8979b168e`; local parent was
  tree-equivalent, not the same commit.
- Test-only helper `tests/claude_test_config.py` confines Claude trust state
  to each pytest temp root, sets `CLAUDE_CONFIG_DIR` inherited by PTY children,
  and restores only its exact project key through a finalizer. Real-provider
  tests in the affected modules now fail closed unless isolated `claude auth
  status --json` confirms login. No application/provider launcher changes.
- `tests/test_claude_test_config_isolation.py`: red sibling-trust regression
  reproduced against old helper; after fix 6 passed, exit 0. Affected-module
  collection: 209 items, exit 0; real-provider modules were not run. Isolated
  auth probe was unauthenticated; no provider call. Evidence/report:
  `docs/visual-qa/evidence/issue-73/pf-04b10-provider-config-isolation-20261126/REPORT.md`.
- Safety caveat: one exploratory pytest accidentally loaded the repository
  autouse Docker cleanup fixture with broad `wm-` resource matching. Prior
  inventory was insufficient to prove whether it removed a pre-existing
  resource; a later read-only event query timed out. Current known state had
  no listed `wm-` resources. Do not run repo pytest without reviewing or
  isolating this fixture; do not claim shared Docker was unaffected.
- Not yet a full-suite, provider, visual-equivalence, CI, or issue #73 gate
  pass. Keep #74 draft and report the real-provider authorization and Docker
  fixture safety limitations. Rollback only this task branch commit.
## 2026-11-26 PF-04b11 — task-owned pytest Docker cleanup

- New isolated branch `agent/codex/issue-73-docker-test-ownership-20261126`
  starts at exact PR #74 head `7fad14ffe02dd8303a59a4e00ac31344e31cfbfd`,
  tree `850de8112d673f20c4802752292cc6634fc6fd63`.
- Replaced session-wide `wm-` prefix teardown in `tests/conftest.py` with
  per-run `tests/docker_compose_ownership.py`: reserve exact Compose project
  before creation, reject existing resources, register and inspect exact
  container/network/volume identities (including partial-up results), guard
  app stop/down/reset, and fail visibly while leaving unverified resources
  untouched. No product runtime naming/policy change.
- Safe synthetic legacy prefix red reproducer exited 1 with two unowned
  `wm-*` resources selected. Updated conftest loaded for ownership regression:
  18 passed, no failures/errors/skips, exit 0; Python 3.14.4/pytest 9.2.0.
  Final JUnit at 2026-11-26 06:01:15–06:01:17 UTC covers cross-run collision,
  duplicate resource names, and spoofed project-filter IDs.
  The suite uses a fake Docker command runner and created no daemon resources.
  Helper/conftest `py_compile` and `git diff --check` passed.
- Read-only audit: Docker server 29.7.2, 62 running/9 stopped, 71 total
  containers, 29 networks; current `wm-` inventory zero. Container/network
  destroy event queries for the earlier window returned zero records, but
  prior inventory/retention is insufficient to establish whether PF-04b10
  removed anything. Impact remains unknown; do not claim a clean bill.
- Real Compose integration was blocked because only the shared daemon is
  available; no real create/down/cleanup was run. Full suite/provider/CI not
  run. Keep PR #74 draft; A not qualified, E not PASS, and F not re-certified.
  Detailed evidence: `docs/visual-qa/evidence/issue-73/pf-04b11-docker-cleanup-ownership-20261126/REPORT.md`.

## 2026-11-26 PF-04b12 — isolated runner readiness

- PR #74 start head/tree `d1f86cb230d936d882071dc77996583a176cf346` /
  `12e0eab92670cd754c662ad1f8543ec0e70a809c`; local task branch source was
  tree-equivalent and clean. No source code changed.
- Safe subset under nonexistent task-local `DOCKER_HOST`:
  `test_visual_qa_contract`, `test_visual_qa_execution`,
  `test_task_decision_required_stages`, `test_test_runner_source_identity`,
  `test_project_test_timeout`: 42/42 passed, no skip, exit 0. This is not
  full-suite or Docker integration evidence.
- No safe daemon was available: Dell only has shared Docker socket; rootless
  helpers `newuidmap`, `newgidmap`, `slirp4netns`, `pasta`, `fuse-overlayfs`
  and a task user socket are absent. HP had 13 GiB free (95% full), nearly
  exhausted swap, and lacks the same helpers; NEC1 SSH alias failed local
  name resolution. No daemon or
  host settings/packages were changed. Python Playwright is absent in the
  existing venv; no provider auth files were read or provider invoked.
- PF-04b10 cleanup impact remains unknown; F is not certified. A remains
  unqualified, E not PASS, B/C unchanged, D historical. Evidence and exact
  replay: `docs/visual-qa/evidence/issue-73/pf-04b12-isolated-runner-readiness-20261126/REPORT.md`.

## 2026-11-26 PF-04b13 — Task Visual QA panel

- On the task-owned PR #74 worktree, starting source was local `ffe2165734835006413a6e285edb0b05a98e89d4`, tree `da59c305a27bceddb746ff308938e821a6be383e`, matching remote PR head `2827b2ba32a6e9f5f84ddc9beb74b846779754d0` by tree. No main/other WIP was changed.
- `app/templates/task_detail.html` now shows a Visual QA section for opt-in and opt-out Tasks. It reuses Task prompt marker, Builder TestRunner action, TaskDecisionService result, and existing authorized run-log route. It shows repo contract/reference SHA, route, viewport, threshold, tested SHA/run status, and only verifier-returned metrics for valid evidence. Missing/invalid/stale evidence is explicit; approval metadata absent in the contract is never inferred. No artifact download/upload/API or second status authority was introduced.
- `TaskDecisionService.builder_view()` includes read-only panel data and the existing builder test readiness result in the same decision view; `visual_qa_evidence.validate_visual_qa_run()` returns metrics it already verified so the panel does not reread unverified metrics files.
- Test-first red observed for missing panel/behavior. Focused route/evidence and related decision/contract/execution/source-identity/read-auth/authz regressions are being recorded with exact JUnit/log below. Final focused regression run: 95 passed, 0 failed/errors/skipped, exit 0; JUnit and sanitized log are in `docs/visual-qa/evidence/issue-73/task-visual-qa-panel-20261126/`. Test execution used a task-local venv and nonexistent task-local `DOCKER_HOST`; shared Docker was not contacted. Browser screenshot was not run because the selected venv has no compatible Playwright package; FastAPI route/template was tested directly.
- PR #74 remains draft. Full suite/A remains unqualified, B/C remain blocked on approved equivalent Nova pilot, E remains blocked without exact-SHA checks, F remains uncertified due PF-04b10 incident uncertainty. No CI, production, merge, deploy, provider or Docker mutation. Replay/report: `docs/visual-qa/evidence/issue-73/task-visual-qa-panel-20261126/REPORT.md`.

## 2026-11-26 VQ-03b2 — authenticated image evidence viewer

- Started from exact published PR #74 head `547d7814998149ef91afedef2027c79097ab416f`, tree `502e26fe46b67d3393f11b7f535b54543e50d67d`; task branch `agent/codex/vq03b2-image-viewer-20261126`.
- Added read-only `GET /test-runs/{rid}/visual/{viewport}/{artifact}` for only the four verifier-approved PNG names. It reuses `require_read_role('test_run','rid')`, validates agent run/workspace/Task/repository identity, task opt-in, clean current SHA, latest visual PASS and existing TaskDecisionService required-stage status. Exact PNG bytes are read with no-follow descriptor, size/dimensions bounded, then the verifier and manifest digest are checked again before response. Headers are `image/png`, `nosniff`, `private, no-store`.
- Existing Task page builds authenticated thumbnail URLs only from valid current evidence; failed/stale/invalid evidence remains text-only. No report/HTML serving, arbitrary paths, upload, command input, or separate gate was introduced.
- Browser availability check: task-local Python environment returned `ModuleNotFoundError: No module named 'playwright'`; Node returned `MODULE_NOT_FOUND` for `playwright`. No global install or browser flags were changed; route/template tests are the available verification path.
- Source commit `16842dd89aad669d26df088a93680d66acb7b8f3`, tree `b15c52ebb0f33713b15a923ba31f6940e0460c10`; exact-clean-SHA targeted run passed 109/109 (0 fail/error/skip), exit 0, 101.935s. JUnit/log/report/checksum are under `docs/visual-qa/evidence/issue-73/vq03b2-image-viewer-20261126/`.
- Overall A/E remain unqualified; B/C design approval remains blocked; F remains uncertified from PF-04b10. No shared Docker, provider, CI, production, merge or deploy was used.

## 2026-11-26 VQ-03b3 — failed threshold diagnostic images

- On the task-owned VQ-03b3 branch, source commit `2c62565cfb2e962ce6e18c2614e746df20fafdc5` / tree `dc7f5de4726914ba135671050fbfa5d14ae184ce` adds a separate strict threshold-diagnostic verifier and retains artifacts only for exit-1, clean/current-SHA, opted-in comparisons whose only failure is pixel mismatch. The existing row stays `FAIL`; diagnostic digest is distinct from PASS evidence and TaskDecisionService gate behavior is unchanged.
- The existing Task panel and read-authorized four-PNG route show “Không đạt — dùng để sửa” only after revalidation. Missing/invalid/stale/dirty/tampered/symlink/opt-out/cross-tenant cases remain unavailable. Vietnamese guide updated.
- Exact clean source replay: 121 passed, no failures/errors/skips, exit 0, 95.245s. One new TestRunner test uses explicitly synthetic manifest/image fixtures; it is not real browser evidence. UIFix pin verification passed. Browser smoke was attempted with a task-owned Chromium copy but stopped after GPU error 1002 and Playwright auto-emitted `--no-sandbox`; no usable screenshot or real CLI result was produced.
- Rechecked VQ-03b2 red JUnit SHA: committed bytes and checksum manifest match `6b4144be193aa1d60ea528b7c927c5b41a623efa9d600e1301ae90a468165078`; corrected the stale `run-summary.json` hash and refreshed its manifest entry. Details/log/JUnit/hash manifest: `docs/visual-qa/evidence/issue-73/vq03b3-failed-threshold-diagnostics-20261126/REPORT.md`.
- No full suite, provider, CI, shared Docker, production, merge or deploy. Code commit is local pending expected-head PR publication; PR remains draft. Full-suite/A, CI/E, F incident disposition and approved NovaRetail B/C remain unqualified.

## 2026-11-26 VQ-03c — pre-opt-in reference inspection

- Started in the existing task-owned worktree on branch
  `agent/codex/vq03b3b-preoptin-reference-20261126`, based on local commit
  `db941199ce25578ee0b8270f0e588c2ef92743f2`, whose tree
  `7a23f50102febb434be5c19950204b5dc181ae50` matched the then-current draft
  PR #74 head `dfe2be82cfc5b397e7cbe9efd4bb7cd4eef7838c`.
- The Task Visual QA panel now reads the validated repository-local contract
  for both opt-in and opt-out Tasks, and can show configured reference PNGs
  only for a clean current worktree. The preview endpoint binds Task,
  workspace, registered repository fingerprint, current HEAD and contract
  viewport; reads a bounded PNG through no-follow file descriptors and sets
  `nosniff`/`private, no-store`. It does not accept a client path/URL or imply
  design approval.
- Opt-in is a required checkbox in the same Task prompt-save flow. The handler
  adds the canonical marker without replacing user prompt text, preserves an
  existing prompt marker during edits, and leaves brief fields intact. Save
  does not launch Builder or TestRunner; the existing Run Test action remains
  visible only when the canonical TaskDecisionService says Visual QA is
  required.
- Source commit `5bc2a4bc8505bb030c20684c9713dc9a380d35b1`, tree
  `bf0d59c23a6139a789f105cc439dd408d8f052ba`, was retested with clean Git
  status before and after. Python 3.14.4/pytest 9.2.0 focused JUnit: 96 passed,
  0 failed/errors/skips, exit 0 (2026-11-26 12:38:11–12:39:31 UTC). This
  includes panel, configured-image route, tenant/auth denial, prompt
  preservation, CSRF, evidence, contract/execution, required-stage and
  source-identity regressions. Raw sanitized log/JUnit and checksum manifest:
  `docs/visual-qa/evidence/issue-73/vq03b3c-preoptin-reference-20261126/REPORT.md`.
- No browser, provider, Docker, production, CI, merge or deploy was used.
  Overall A–F remain unchanged: full-suite A and exact-CI E unqualified;
  approved Nova B/C blocked; D historical; F still uncertified by PF-04b10
  incident uncertainty. PR #74 remains draft.

## 2026-11-26 VQ-03c1 — committed-source binding

- Task branch `agent/codex/vq03b3b-preoptin-reference-20261126`; source commit `4d88237e0611034d86df0d769442309f0f8e15d6`, tree `f411335d50518e62370d68e0bd6170963e3dd4e7`. This rebased source commit was tested locally after replay onto the exact PR base; evidence is a separate commit.
- Visual QA `PROJECT.yaml` and reference PNG inputs are now read as regular Git blobs from the exact tested commit, with worktree bytes/index flags checked; ignored, untracked, symlinked, stale, hidden-index or changed inputs fail closed. Task panel returns invalid without a preview URL when source-state lookup fails. TestRunner, evidence validation and TaskDecisionService use the committed contract loader; backend opt-out remains unchanged.
- Focused suite on source commit: 107 passed, no failures/errors/skips, exit 0. One pinned-browser subprocess case was excluded because its Chromium executable was unavailable; no browser install/retry, full suite, Docker, provider or CI run.
- Evidence: `docs/visual-qa/evidence/issue-73/vq03c-source-integrity-20261126/REPORT.md`. VQ-03c timing corrected using raw JUnit: suite 75.230s, case sum 74.935s, wall 80.298051s; raw JUnit unchanged.
- No merge/deploy. A–F are unchanged; full suite, compatible isolated browser/provider runner, approved Nova reference and exact-head CI remain blockers. Next: publish source/evidence commits to existing PR #74 only after expected-head recheck, then wait for authorized compatible runner before browser/full-suite work.

## 2026-11-26 VQ-03e1 — private Task Visual QA reference drafts

- Source commits `4c671c1412d256e1b1b8a16ab6edeec6ce97f87a` and `8e70ca21cfec8ea8fbbc02ee4c1bf5c54321169e` are on existing draft PR #74 branch `agent/codex/visual-qa-73`; tested head `8e70ca21cfec8ea8fbbc02ee4c1bf5c54321169e` / tree `17a4e21b503eff0395b961ece07d4cfda7e46a32`. Task page stores bounded PNG uploads as private task-scoped `UI_UX_DESIGN` WorkProducts in `DRAFT`/`UNREVIEWED`; each workspace is capped at 10. Preview/download are workspace scoped, no-follow, hash/dimension checked, and do not affect committed contract, opt-in, TestRunner, or TaskDecisionService.
- Focused TestClient/Git/SQLite suite on exact source SHA: 138 passed, 0 failed/error/skip, exit 0; one real browser subprocess test explicitly deselected because pinned browser runtime is unavailable. `py_compile`, `git diff --check`, `git show --check` passed. Evidence/checksums: `docs/visual-qa/evidence/issue-73/vq03e1-private-reference-draft-20261126/REPORT.md`.
- No browser/provider/Docker/CI/production/merge/deploy. PR #74 stays draft; A/B/C/E/F unchanged and #73 is not complete.
