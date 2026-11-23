# Codex CLI disposable builder probe — 2026-11-23 (Asia/Bangkok)

Purpose: check whether the locally available Codex CLI can complete a provider-independent builder task in an isolated temporary git repository. This is not the ProjectFlow-managed Builder E2E and does not substitute for it.

- CLI: `codex-cli 0.171.0`; provider reported `openai`; sandbox `workspace-write`; ephemeral session; workdir `/tmp/issue-73-codex-builder.8yHJwt`.
- Seed: committed `calc.py` returning 41 and `test_calc.py` expecting 42. Baseline `python3 -m pytest -q test_calc.py`: **1 failed** as expected.
- One argument-compatibility attempt with both `--sandbox workspace-write` and `--approve-for-me` was rejected by Codex CLI (`--sandbox` cannot be used with `--approve-for-me`); no files changed.
- Corrected bounded invocation used `codex exec --sandbox workspace-write --ephemeral -C <temp-dir> ...`. Codex reported `exec_command` exit 159 for basic `pwd` and `ls`, then said it could not inspect/edit/run/commit; no files changed. Post-run pytest remained **1 failed**, working-tree change was only Python `__pycache__` created by our diagnostic pytest.
- No `--dangerously-bypass-approvals-and-sandbox` or other sandbox weakening was attempted. This proves the Codex CLI builder path is blocked by local command sandbox startup in this session; it does not prove the product Builder works or fails with Codex.
