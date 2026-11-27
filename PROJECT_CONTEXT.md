# ProjectFlow Project Context

## Latest handoff — PR #92, 2026-11-27

- Continue only the existing PR `https://github.com/hungtranbkit/projectflow-workspace-manager/pull/92`, branch `agent/codex/orch-std-20261123-release`. The PR is open, base `cbeaffe417765f2778eeb9ad96cd0db21a9acb69`, current remote head before this checkpoint `b49a217fe6018c9d897201c3b951682145da917f`. Work was performed in the already existing exact-head checkout `/home/dell/.local/state/projectflow-workspace-manager/qa/ORCH-STD-20261123/sidebar-ci-exact-2ul9qtq4/repo`, detached at that head; no task, branch, worktree, or agent was created. `git ls-remote` confirmed the PR branch still pointed to `b49a217` before local changes. Use normal fast-forward push to that ref only after a fresh head check; never force push.
- B18 proposal and receipt hardening is applied to `app/services/recovery_decision_service.py` from the owner-provided patch with verified SHA256 `77d7ecc204fd08ac37f97e6bdb4c29e4917e6ee22b8f665fa3b9298cc22f37ad`. It revalidates cached proposals against current incident/evidence scope and validates provider/model/requested-effort/tool-less receipt fields before accepting output. Existing deterministic VERIFY fallback and blocker remain authoritative; no model action is executed by this validation.
- B17 changes only `_supervisor_lease_is_current()` in `app/services/autonomous_execution_service.py`: non-None scheduled owners must be nonempty strings and lease expiry must be numeric, finite and in the future. Explicit `None` owner compatibility remains. Lease acquisition, production DB state, and task state were not changed.
- Permanent tests: `tests/test_b18_actual_db.py` (owner-provided real Database/migration/RequestMemo suite; source SHA256 `51f3bd7348445c15e459258008befaef2c5197badac02f98d10226344be506d1`) and `tests/test_b17_finite_lease.py` (SQLite finite/expired/infinite/NaN/malformed/missing/wrong-owner/successor/manual compatibility cases). Combined result: 39 passed, exit 0; JUnit SHA256 `2cf8fa8e5ee6ff58324f288d310b2f862927e94d788748595cabc2d36aac568b`. `scripts/preflight.sh`, `scripts/build.sh`, and `scripts/smoke.sh` passed (smoke 1 passed). Broader focused run had 233 passed and one environment-dependent real-Claude e2e failure because HOME was isolated and its fixture requires `~/.claude.json`; no provider credentials were read and no provider run was attempted. Do not claim full repository suite pass.
- `git diff --check` passed before this handoff. A red/green JUnit pair and exact command output are at `/tmp/pf92-b17-b18-green-20261127.xml` and `/tmp/pf92-focused-regression-20261127.xml`. No production service, database, credentials, systemd, Docker, or GitHub workflow was changed.
- On the documentation head `2445fdb`, GitHub reports commit status `pending` with zero contexts at 2026-11-27 check time; the prior source-head `76aaa6c` had `local-ci/verify` pending. The PR review list is empty, so independent review remains pending. Query the exact latest head again before claiming pass. The independent B20 starvation packet reported in PR comment 7207463907 was not present in the accessible private evidence store; do not reconstruct it from prose. No merge or deployment is authorized here (`PROJECT.yaml` production deployment is disabled).

## Architecture constraints

- Keep `TaskDecisionService`, `SpecGate`, existing task/session/evidence tables, Review/QA/CI/merge/deploy gates authoritative. Recovery advice is advisory and bounded; malformed or untrusted provider receipts fall back to deterministic verification and preserve the factual blocker.
- Supervisor lease checks are fencing reads, not a second task status store. Do not weaken exclusive ownership, retry caps, auth, or V2 prompt/spec gates.
- All main changes must go through the existing PR and protected CI. Preserve unrelated work and never deploy the test fixture or change production state under this task.

## Next actions

1. Recheck exact latest PR head and its genuine `local-ci/verify` result.
2. Obtain an independent review; do not self-approve or report earlier-head checks as current.
3. Retrieve the exact B20 patch/test packet through its authorized owner if still required, and separately satisfy the one-shot advisory/release gates. No production deployment until a release owner follows the approved backup, rollback, and runtime verification procedure.
