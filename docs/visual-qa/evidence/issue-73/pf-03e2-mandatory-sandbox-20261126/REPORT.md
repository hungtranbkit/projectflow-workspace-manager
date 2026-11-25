# PF-03e2 — mandatory Docker sandbox visual run (2026-11-26 ICT)

## Result

The `AUTH_MODE=required` application path was exercised through real app wiring, `TestRunner.start()`, `SandboxedCommandRunner`, `SandboxRuntimeService`, Docker, pinned UIFix/browser, SQLite, `GitWorkspaceService`, and `TaskDecisionService`. Docker and CLI results were not mocked. A test observer delegated to the actual runner and copied artifacts before TestRunner cleanup.

- ProjectFlow source tested: commit `878c5a5f11f3a312cd6b11ae57dcf0847048969a`; tree `9b7d7d94af6081eb0a237963cbafa2092a195b5f`.
- PR #74 exact remote head before this publication: `78e4ddf8d35036e3511163ff2b6e996e00b3a486`; base `ac288928ab559799e6a6f34ff9be1de5fdfa5af3`.
- Runner: Python 3.14.4 / pytest 9.2.0. Official image digest: `sha256:83192064c7510f7ee73dd63dc5f22a5e01a92c81a2e6a9c715d9e3fe55471fd9`, linux/amd64, Node 24.15.0, Python 3.12.3, Chromium rev1223 / Chrome for Testing 148.0.7778.96. UIFix 1.2.6 pin `97c4b9d83684c7d607cb8c4b146e3f8aa5356e53`.
- Mandatory integration JUnit: **1 passed / 0 failed / 0 errors / 0 skipped**, exit 0, 11.269s. Start/end: 2026-11-26T00:43:40+0700 to 2026-11-26T00:43:53+0700. Related visual contract/execution, TaskDecisionService, source identity, and network-isolated sandbox selection: **56 passed / 0 failed / 0 errors / 0 skipped**, exit 0, 20.019s. Separate numeric UID sandbox probe: 1 passed, exit 0.

## Root cause reproduced and fixed

Before the patch, the real required-mode TestRunner passed its ordinary test stage, then the visual sandbox failed because its fixture server could not create `.runtime/sandbox-fixture-server.log` in the coordinator's private `0700` run directory. The image default ran as UID 0 with all Linux capabilities dropped, so it could not write to the host-owned bind mount. Red run exit: 1. Observed stderr: `sh: 1: cannot create .runtime/sandbox-fixture-server.log: Permission denied`.

`SandboxRuntimeService.run_ephemeral()` now adds `--user <worktree-owner-uid>:<gid>`. This keeps the private directory mode intact and avoids granting capabilities. Existing network-none, resource limits, cap-drop, no-new-privileges, one-worktree mount, and explicit environment are unchanged. The basic sandbox test now checks numeric UID with `id -u`, because an arbitrary numeric UID need not have a username entry in the container.

## Browser evidence and TaskDecisionService results

Reference SHA-256: `b89caa21bde04741f0909b0e9e62a1d33fa90c4c1d927ead91d7d8a0b119237e`. The reference is a synthetic 320×240 fixture at DPR 1 and fixed 8% threshold; a same-container loopback server serves either its matching color or an intentional whole-page color change. No host loopback, external request, production service, or provider was used.

| Case | UIFix | Mismatch | TaskDecisionService |
|---|---|---:|---|
| Positive matching fixture | PASS | 0% (0/76800) | PASS |
| Intentional full-frame color change | FAIL | 100% (76800/76800) | FAIL |
| Tamper coordinator PNG after positive | n/a | Manifest/hash invalid | FAIL |
| Advance fixture commit after positive | n/a | Stale SHA | NOT_RUN |
| Missing reference | n/a | Rejected before Docker dispatch | NOT_RUN |
| Command exits 0 without evidence | no evidence | Fail closed | FAIL |
| Docker startup rejects invalid memory | visual stage skipped after required test startup fails | no orphan | NOT_RUN |
| Visual command timeout | TIMEOUT / exit 124 | kill + remove observed | FAIL |

The test recorded 9 actual `docker run` calls. Each used the pinned image, worktree owner UID:GID, network none, 3g memory (the startup-negative intentionally sends invalid memory), 2 CPUs, 256 PID limit, all capabilities dropped, no-new-privileges, one fixture worktree mount and only explicit Visual QA variables. Every task-owned container was absent after its command; final `docker ps -a --filter name=wm-exec-` returned no rows.

## Verification limits

- `python3 tools/visual_qa/verify_uifix.py .runtime/uifix-v1.2.6`: PASS.
- `git diff --check`: PASS.
- One host-only legacy test in `tests/test_visual_qa_runner_subprocess.py` failed because the Dell host cache lacks Chromium headless-shell rev1223. It was not retried; this is not evidence for or against the mandatory Docker path.
- Full suite, provider invocation, preflight/build, and CI were not run. This is synthetic mandatory-sandbox evidence, not NovaRetail acceptance and not Gate A/B/C/E completion.
- Rollback: revert only source commit `878c5a5f11f3a312cd6b11ae57dcf0847048969a` and its evidence follow-up if needed. No production, main/WIP, shared Docker config/resources, CI settings, merge, or deployment changes.

## Replay

Run `sh ./replay.sh` from a clean checkout of the tested source commit, with Docker and the pinned UIFix runtime already provisioned. `.runtime/` is ignored and contains the test's evidence output.
