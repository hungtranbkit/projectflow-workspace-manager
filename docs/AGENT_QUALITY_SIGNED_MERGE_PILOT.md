# ProjectFlow signed QA gate — opt-in merge pilot (20 Nov 2026)

## Purpose

The pilot integrates the previous lint + task-specific test proof into the
**existing** ProjectFlow merge gate, without replacing `RISK_GATES`,
human review, runtime QA, mergeability, or CI checks.

It adds one extra conditional pre-merge blocker for a specifically selected
ProjectFlow task and repository. **Disabled by default**; no production deploy
or GitHub Actions required.

## Trust model

- A **trusted, isolated QA runner** performs the actual pinned tests from
  `config/agent_quality_tasks_pilot.json`, not a Builder agent's self-report.
- QA signs an exact-SHA receipt with HMAC-SHA256 and a key **not available to
  any coding agent**. QA checks a clean source checkout, exact Git commit,
  trusted policy and unchanged test infrastructure before signing.
- The ProjectFlow service only **reads** the receipt. It never executes
  untrusted test code during an HTTP merge request.
- The receipt is bound to ProjectFlow task ID, repository ID/name, tested
  commit SHA, policy task key, pinned test, test result, and issuance time.
- Proof is valid for at most 24 hours and becomes invalid if the source SHA
  changes. Tampering, missing file/key, malformed policy, stale evidence,
  or unsigned `PASS` returns a blocker.

## Activation (NOT active yet)

Choose one actual ProjectFlow task ID and repository ID from the running
Workspace Manager UI. These IDs are **not** the textual QA policy task key
and are specific to the instance. Set the following only on the ProjectFlow
control plane (replace placeholders):

```bash
export PROJECTFLOW_QUALITY_PILOT_TASK_IDS='TASK_NUMERIC_ID'
export PROJECTFLOW_QUALITY_PILOT_REPO='projectflow-workspace-manager'
export PROJECTFLOW_QUALITY_ATTESTATION_DIR='/private/qa-receipts'
# Inject PROJECTFLOW_QUALITY_ATTESTATION_KEY via a protected secret source.
# Do NOT put it in Git, PROJECT.yaml, chat, .env.example, an agent session,
# website JavaScript, shell history, or committed tests.
```

The QA verifier must also receive the same key **only from its protected
secret source**. The receipt directory must be shared with or securely
transferred to the control-plane host; ensure the control-plane user has
read access, and agents have **no write access**.

The trusted QA host must have a reviewed ProjectFlow checkout with
`app/services/quality_attestation_pilot.py`, `scripts/agent_quality_sign_pilot.py`,
and the two existing pilot scripts; a virtualenv with `pip install -e
'.[test]'`; plus an isolated **clean checkout of the target exact commit**.
The source must not be taken from an agent's uncommitted worktree.

### QA host: generate signed machine receipt

```bash
python scripts/agent_quality_sign_pilot.py \
  --worktree /isolated/checkout/at-exact-commit \
  --commit EXACT_40_HEX_GIT_SHA \
  --task-id TASK_NUMERIC_ID \
  --repository-id REPOSITORY_NUMERIC_ID \
  --repository-name projectflow-workspace-manager \
  --receipt-dir /private/qa-receipts
```

Script runs the trusted pinned `tests/test_harness_monitor_report.py`,
checks zero lint violations and a clean checkout, then atomically writes
`task-TASK_NUMERIC_ID-repo-REPOSITORY_NUMERIC_ID.json`.
**A failing test does not produce a PASS receipt**.

The control-plane test (read only) should call its existing
`TaskDecisionService.merge_gate_status` with the same task and exact
commit. Check `eligible` and `blockers`: receipt missing/invalid/stale
must yield a `QUALITY_ATTESTATION_*` blocker. Existing GitHub status,
code review, runtime QA and mergeability still apply independently.

### Independent QA sign-off and rollout

1. Run pilot unit tests and existing merge regression tests on an
   isolated node. Do not run the entire repo's Docker-enabled test suite
   on a live host.
2. Verify signed PASS on the exact commit, then a deliberately tampered
   signature and a different source SHA both return BLOCKED.
3. Verify with feature flag unset that old merges behave identically.
4. Require a separate reviewer to examine code/QA receipt and approve
   enabling **one** numeric task ID; record rollback (unset flag).
5. Do not silently roll out to every ProjectFlow project.
6. Do not create a PR yet if `.github/workflows/ci.yml` still triggers
   GitHub Actions on pull requests and the user's Actions budget is
   exhausted.

## Enforcement points and limits

- GitHub PR merge: `TaskDecisionService.merge_gate_status()` is already
  called by the live `/api/tasks/{tid}/merges/{repository_id}/merge`
  route immediately before `github_merge.merge_pr`. The same gate
  populates the task-detail UI's merge blockers.
- Direct local integration: `IntegrationService.integrate_task()`
  invokes the same opt-in signature check and checks source SHA again
  before `git update-ref` (no silent bypass).
- The pilot is limited to the ProjectFlow monitor-report policy task.
  Other modules/repositories are not covered by its pinned tests. Signed
  automated QA is **not** human visual acceptance or production proof.
- A direct merge **outside ProjectFlow** (for example via GitHub web UI)
  cannot be blocked by ProjectFlow. GitHub branch protection/rulesets or
  a controlled merge account are necessary for organization-wide
  enforcement.
- A QA host or control-plane host compromised by the same agent is not
  an independent trust boundary. Isolate identities, secrets, and
  write permissions before enabling the feature in production.
