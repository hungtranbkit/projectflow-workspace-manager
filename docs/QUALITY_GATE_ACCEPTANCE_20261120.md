# Quality Gate — Acceptance Record (2026-11-20)

## Verdict

**PILOT ACCEPTED (technical staging QA).** On isolated NEC Linux QA checkout, all
**80 / 80 targeted tests passed** on the exact code commit
`60947d94f83706727bb5b085c6a8ed60fc40527a`.

This is **not** authorization to enable the feature on production, merge to
`main`, or claim organization-wide GitHub branch protection.

## Source and environment

- GitHub repo: `hungtranbkit/projectflow-workspace-manager`
- Branch: `experiment/agent-quality-proof-gate-20261120`
- **Tested source SHA:** `60947d94f83706727bb5b085c6a8ed60fc40527a`
- QA node: `nec1` (Linux); Python 3.14.4, isolated Python virtualenv,
  pytest 9.2.0 and working Docker engine v29.1.3.
- Trusted verifier checkout:
  `/home/rag/workspace/qualitygate-acceptance-20261120`
- Separate target checkout:
  `/home/rag/workspace/qualitygate-candidate-20261120`
- Verified that both checkouts were clean and initially at the same code SHA.
- Available cached image: `nginx:1.28-alpine`.
- No production deployment, real GitHub merge, or GitHub Actions invocation.

## Final acceptance run — all tests on one exact code SHA

Command (with `-o addopts=''` for an explicit test run):

```bash
python -m pytest -q -o addopts='' \
  tests/test_real_merge.py \
  tests/test_merge_reconciliation.py \
  tests/test_integration_push.py \
  tests/test_integration_create_idempotent.py \
  tests/test_quality_attestation_http_e2e.py \
  tests/test_quality_attestation_pilot.py \
  tests/test_agent_quality_pilot.py \
  tests/test_agent_quality_task_gate.py \
  tests/test_harness_monitor_report.py
```

Result:

```text
ACCEPTANCE_SHA=60947d94f83706727bb5b085c6a8ed60fc40527a
BEFORE_WM_CONTAINERS=0
BEFORE_WM_NETWORKS=0
ACCEPTANCE_EXIT=0
80 passed, 463 warnings in 118.10s (0:01:58)
AFTER_WM_CONTAINERS=0
AFTER_WM_NETWORKS=0
SOURCE_HEAD=60947d94f83706727bb5b085c6a8ed60fc40527a
WORKTREE_STATUS_BEGIN
WORKTREE_STATUS_END
```

The warnings are third-party deprecation warnings, mainly Starlette/httpx
and Python 3.14 `asyncio.iscoroutinefunction`. None failed the tests, but
they should be reviewed in dependency maintenance.

Machine-local log: `/tmp/qualitygate-nec-acceptance-all-20261120.log`.

## Functional acceptance, not just unit mocks

1. **Docker sandbox regression:** the previously blocked
   `test_done_ui_shows_verification_sandbox_action_when_supported` passed
   independently on the NEC Docker host; then the entire existing
   36-case merge/integration regression set passed.
2. **Actual HTTP route behavior:** FastAPI/TestClient accepts a valid
   signed exact-SHA QA receipt and returns a successful merge response
   using a **fake GitHub runner**. It refuses missing and stale receipts
   before merging. Legacy `mark-merged` and external-merge confirmation
   cannot bypass an enabled QA gate.
3. **Signer pipeline:** trusted checkout runs the pinned real
   `tests/test_harness_monitor_report.py` suite against a separate
   clean candidate checkout. Generates an HMAC receipt using a temporary
   **test-only** key. The verifier says `EXACT_SHA PASS`,
   `WRONG_SHA QUALITY_ATTESTATION_STALE`, and
   `OTHER_TASK NOT_GATED`.
4. **Security hardening:** signer refuses to attest its own trusted
   checkout, and runs target tests with a scrubbed environment that does
   not pass through signing keys or other unrelated agent credentials.
   Unit tests cover this behavior.
5. **No resource leakage:** Docker `wm-*` containers and networks were
   zero both before and after the final run. Both Git checkouts were
   clean after acceptance.

## Open operational / security prerequisites

Technical pilot acceptance **does not prove production-safe isolation**.
The acceptance test used one Linux user to operate the two distinct
checkouts. For genuine independent verification, run QA and code agents
under different OS principals or separate machines/containers with
separate filesystem and secret access. The QA signing key must never be
accessible to source-under-test processes or agent-controlled mounts.
HMAC verification also relies on protecting the verifier's copy of
the symmetric key.

The feature remains opt-in and disabled unless a specific numeric Task ID,
repository name, receipt directory and protected key are configured.
The current pinned suite is only suitable for the ProjectFlow monitor-report
pilot; other projects need their own independently reviewed test scopes.

A merge executed directly on GitHub, outside ProjectFlow, can bypass the
in-app gate. To claim enforceable protection for `main`, configure
GitHub rulesets/branch protection and a controlled merge identity, as well
as an independent staging deployment + rollback test.

## Next controlled rollout

1. Record an actual non-production Task ID and repository ID in staging.
2. Put signing and verification in distinct privileged identities and
   protect the receipt directory; run the exact-SHA pilot.
3. Verify the staging merge button/blocked UI with a real receipt and an
   intentionally tampered copy; do not merge into `main` during canary.
4. Obtain explicit review/approval before widening scope or deploying.

The docs-only commit containing this acceptance record may advance the
branch head beyond the tested source SHA; the exact acceptance target
remains the commit above.
