# VQ-03b2 — authenticated Visual QA image viewer

Date: 2026-11-26 (Asia/Bangkok). PR #74 remains draft.

## Source

- Source implementation commit: `16842dd89aad669d26df088a93680d66acb7b8f3`.
- Source tree: `b15c52ebb0f33713b15a923ba31f6940e0460c10`.
- Parent / verified published PR #74 base: `547d7814998149ef91afedef2027c79097ab416f` (tree `502e26fe46b67d3393f11b7f535b54543e50d67d`).
- Work was done in task-owned branch `agent/codex/vq03b2-image-viewer-20261126`; no dirty main or other worktree was changed.

## Implementation

- Added `GET /test-runs/{rid}/visual/{viewport}/{artifact}`. It reuses `require_read_role('test_run','rid')`, then binds the PASS row to its exact agent workspace, task opt-in, registered repo identity, clean current source SHA, latest current visual run, configured viewport/contract, and `TaskDecisionService`'s existing required-stage decision.
- Only `reference.png`, `actual.png`, `diff.png`, and `overlay.png` are allowed. The evidence verifier runs before and after a no-follow bounded file-descriptor read; returned bytes must match both validated manifest hashes and configured image dimensions. Response headers are `image/png`, `X-Content-Type-Options: nosniff`, and `Cache-Control: private, no-store`.
- The existing Task page renders thumbnails only for validated current PASS evidence. Failure/stale/invalid and opt-out paths remain text-only. No artifact path or URL input, HTML/report serving, upload, new app, gate, or status authority was introduced.

## Verification on exact clean source commit

- Exact invocation is recorded in `run-summary.json`; it ran the focused pytest set from the task-local Python 3.14 venv with `DOCKER_HOST` set to a task-owned nonexistent Unix socket.
- Start/end: 2026-11-26 08:32:16–08:33:58 UTC (15:32:16–15:33:58 ICT).
- Exit `0`; JUnit `109` passed, `0` failures, `0` errors, `0` skipped; duration `101.935s`.
- Tests cover byte-exact delivery of all four PNG names, response MIME/cache/nosniff headers, current Task preview URLs/alt labels, same-Task different-workspace binding, required-mode unauthenticated `401` and other-tenant `404`, traversal/name/viewport rejection, symlink, tamper, missing manifest digest, opt-out, stale/dirty source, newer failed run, plus existing visual evidence/contract/execution, TaskDecisionService, source identity and read-auth/authz regressions.
- TDD red sample before route implementation: JUnit `11` cases, `6` failures, exit `1`; the valid image requests returned `404` and the Task panel had no preview URLs. One extra red was a fixture manifest run-id mismatch; it was corrected in the test fixture before the final green run.
- `git diff --check` passed on the source commit. The source worktree was clean before evidence packaging.

## Browser and safety limits

- Browser smoke was **not run**. Node `v26.7.0` could not resolve `playwright` (`MODULE_NOT_FOUND`); the task-local Python venv raised `ModuleNotFoundError: No module named playwright`. No global install, shared browser profile/cache, sandbox bypass, shared Docker daemon, provider, production app or external network was used.
- This route/template milestone does not qualify the full suite, approved Nova pilot, exact-SHA CI, or incident disposition. A/E remain unqualified, B/C remain blocked by design approval/equivalence, and overall F remains uncertified due PF-04b10.

## Files

- `focused-regressions.xml` and `focused-regressions.log`: final exact-source run.
- `pre-route-red.xml`: TDD red sample.
- `run-summary.json`: source identity, exact commands, exit/counts, environment and digests.
- `SHA256SUMS`: hashes for these artifacts and this report; it excludes itself.
