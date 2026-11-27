# PF-04b7 — B2 release concurrency residuals

## Identity and scope

- PR #74 remote head at test time: `5e4ab28c05b922bccc6e2fd7e207a29d61718b85`, tree `7ea8df1d08e802406d6415dafe22ab931fa36b74`.
- Existing task worktree branch: `agent/codex/issue-73-engineering-fixture-20261125`; local commit `e443d603e3e90f5c43b71a80ab51abc231e693f3`, exact same tree `7ea8df1d08e802406d6415dafe22ab931fa36b74`. Local commit SHA differs because earlier GitHub connector publication created tree-equivalent commits; no branch reconciliation was needed.
- Test file blob at both local and exact remote tree: `a6fc9b1689211ba64ba10e7430d5724a09316df5`.
- No source/test code changed in PF-04b7. Only task-owned evidence and `PROJECT_CONTEXT.md` handoff are added.

## Test run

Command:

```sh
PATH=/usr/bin:/bin /home/dell/workspace/.worktrees/projectflow-visual-qa-73/.venv/bin/python -m pytest -q -ra tests/test_b2_release_concurrency.py --junitxml=docs/visual-qa/evidence/issue-73/pf-04b7-b2-concurrency-20261126/b2-concurrency.xml
```

For portable replay, substitute the already-existing task venv path; no package was installed or changed. Python 3.14.4, pytest 9.2.0. Start 2026-11-26 00:21:03 UTC / 07:21:03 ICT; pytest collection began 07:21:05 ICT; finish 00:21:21 UTC / 07:21:21 ICT. Exit 0. JUnit: **4 tests, 4 passed, 0 failures, 0 errors, 0 skips** (15.275s). Log includes source identity, exact command, version, timestamps, stdout, exit and post-run git status.

The three historical `provider_cli_missing` IDs now map to PASS under the same restricted PATH. They execute through the current deterministic review-fixture seam; the real concurrency itself is not faked. The module creates real threaded release calls against its temporary SQLite database, uses actual Git worktrees/commits for fixture releases, verifies unique versions and no orphan release manifests, checks duplicate explicit version errors and the threaded TOCTOU collision, and renders the real task route for the non-whitelisted-agent XSS case. See `historical-node-resolution.tsv` and the JUnit testcase records.

No base comparison was needed: the exact PR tree passes and its tracked B2 test blob is verified. No changes were made to concurrency implementation or assertions. No provider call, full suite, related suite rerun, CI mutation, merge, deploy, production or shared runner action.

## Gates and rollback

This module result is not a full-suite Gate A pass. Gate A remains FAIL pending a qualifying full suite; E remains NOT PASS absent successful required checks on final exact PR SHA. B/C unchanged; this diagnostic does not requalify D/F. Keep #74 draft and #73 open.

Rollback only the PF-04b7 evidence/context commit; there is no source-code rollback. The task worktree was clean before the run; only this evidence folder was created by the run.
