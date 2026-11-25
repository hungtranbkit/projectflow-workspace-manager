# PF-03e2 — pinned browser through TestRunner

**Result:** bounded integration milestone PASS on the exact published PR source tree; not a full-suite, NovaRetail approval, or CI claim.

- PR #74 is draft/open. Parent was `d105867005fc7d84a6f4e04a625657eddaa11fba`; change published at `10455bb4b0b52beb2c06fc3fbd9b339183a49fc0`, with exact tested tree `4c9011ec3e0694b87e889077819b2a195e7ce6be`. Changed test blob: `dd5db08eedda8f524a81753a35479f5575325e0f`.
- At 2026-11-25 23:41:46–23:41:54 ICT / 16:41:46–16:41:54 UTC, the exact test node exited 0: **1 passed, 0 failed, 0 errors, 0 skipped**, JUnit 6.384 seconds.
- Runtime: official `mcr.microsoft.com/playwright:v1.60.0-noble` linux/amd64 image digest `sha256:83192064c7510f7ee73dd63dc5f22a5e01a92c81a2e6a9c715d9e3fe55471fd9`; Ubuntu 24.04.4; Node 24.15.0; Python 3.12.3; pytest 9.2.0; UIFix 1.2.6 at source commit `97c4b9d83684c7d607cb8c4b146e3f8aa5356e53`; embedded Playwright 1.60.0; Chrome for Testing 148.0.7778.96 / rev1223. Pinned UIFix verifier passed. Chromium executable SHA-256 is in `runtime.json`.
- Container was `network=none`, read-only root, no mounts, 6 GiB memory, 4 CPUs, PID limit 512, all capabilities dropped, no-new-privileges. Fixture served GET only; no external requests, provider calls, production systems, Docker socket, host mounts, or shared networks were used.

## Real execution results

The test uses actual `TestRunner.start()` background dispatch, temporary SQLite DB, `GitWorkspaceService`, and a real `SandboxedCommandRunner` subprocess. An observer delegates execution to that runner and copies per-run artifacts before coordinator cleanup. It exercises a synthetic fixture, not product design:

| Case | TestRunner/TaskDecisionService result | Browser evidence |
|---|---|---|
| Exact synthetic reference | PASS | UIFix 1.2.6, 0 / 76,800 pixels different, threshold 0, 320×240 DPR 1 |
| Deliberate color deviation | FAIL | 76,800 / 76,800 different (100%), real actual/diff/overlay |
| Wrong dimensions | FAIL | CLI exit 2 |
| Missing reference | NOT_RUN | contract rejected before stage dispatch |
| Unavailable loopback server | FAIL | CLI exit 2 / `SERVER_NOT_READY` |
| Exit 0 without evidence | FAIL | run evidence validation rejects it |
| Current source evidence tampered after PASS | FAIL | current-head PASS established first, then mutation detected |
| Old PASS after source commit changes | NOT_RUN | stale tested commit rejected |
| Backend opt-out | PASS | only ordinary `test` stage dispatched |

Reference SHA-256: `b89caa21bde04741f0909b0e9e62a1d33fa90c4c1d927ead91d7d8a0b119237e`. Positive actual SHA-256: `9f65d312989d0aae40de76358a6c49f2f383a3cc32fd13422c88f5cbac25c713`. Negative actual SHA-256: `c7c3151f51a387a2090d93f61615a1156b21c347860efbfc6d4de803f0fd4e05`.

See `runner-results.json`, `pytest-junit.xml`, sanitized `pytest.log`, positive/negative PNGs and metrics. `SHA256SUMS.txt` lists each evidence file except itself.

## Limits and next gate

This verifies TestRunner + SandboxedCommandRunner **direct-host mode inside the isolated test container**. The mandatory Docker sandbox executor path was not exercised: the container has no Docker socket or host mounts. This is synthetic visual-tool integration, not NovaRetail acceptance or approved design. The full ProjectFlow suite was not rerun (PF-04 is separate); exact-head GitHub CI is a separate gate and no CI setting changed. No source/UI behavior outside the test changed. Rollback is revert only the PF-03e2 test commit `10455bb...`.
