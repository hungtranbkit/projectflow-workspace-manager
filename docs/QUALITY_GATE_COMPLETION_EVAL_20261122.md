# Quality Gate completion attempt, preflight deployment and before/after evaluation — 22 Nov 2026

## Decision

**Not finished end-to-end.** A readonly operations preflight utility is now
implemented, tested and installed on the live Dell node. This does **not**
establish a separate QA signing principal, authentic cross-host receipt
transfer or GitHub required-merge protection. No signed production merge
has occurred. Do not mark the P0 objective DONE.

Engineering estimate on the acceptance checklist: **66/100 before → 68/100
after**, +2 points for automated scoped diagnostics and independently
reviewable monitoring output. This percentage is a manual DoD estimate,
NOT an empirical failure-rate or productivity improvement. Signer/
GitHub/real-task acceptance still contributes **zero**.

## Evidence and before/after measurements

| Measure | Before this operation | After | Qualification |
|---|---|---|---|
| Readonly QA gate operator command | No dedicated reliable operator CLI | `scripts/quality_gate_readiness.py`; installed at `/home/dell/.local/bin/projectflow-qg-preflight` | Delivered; no web restart or DB write |
| Dedicated diagnostics regression | 0 | **11 new tests**, all PASS | Tests cover disabled, strict valid/missing/stale/expired, image mismatch, wrong scope, no HMAC/secret leakage |
| Targeted integrated test suite | Prior release **90 PASS** | Current operator checkout **101 PASS**, 749 dependency warnings, 124.49 s | Full 1,614-test suite NOT run; no test failures in targeted suite |
| Production ProjectFlow | active, healthy | active, healthy | No service restart for CLI installation |
| Production QG selection | `PROJECTFLOW_QUALITY_PILOT_TASK_IDS=` | **unchanged, empty** | 0 actual protected tasks |
| Production mandatory isolated QA | `PROJECTFLOW_QUALITY_REQUIRE_ISOLATED_QA=0` | **unchanged, OFF** | 0 strict production merges |
| NEC strict shadow | HTTP healthy, host-direct receipt invalid | preflight explicitly shows **VERIFIER_BLOCKS_GIVEN_INPUT / QUALITY_ATTESTATION_INVALID**, old receipt expired and not Docker QA | Correct fail-closed behavior; no new signed receipt |
| Independent signing principal | Not established | **Not established** | Cannot claim independent QA |
| Native GitHub required gate | Not confirmed | **Not confirmed** | `main` protected flag does not prove a required Quality Gate check |
| GitHub PR / main merge | None | **None** | Avoided triggering existing GitHub Actions workflow |

### Direct verification

**Dell:** The stable `workspace-manager.service` was active on commit
`26c1d3c45322818d30ddff1948684e6be3d095fd`, health OK, about
138 GiB root disk space available, and SQLite integrity previously checked
OK. Production registered repositories remain `mesflow`, `mesflow-app`,
`qa`, plus two disabled synthetic `vrepo` entries. There is NO registered
`projectflow-workspace-manager` production repo/task matching the hardcoded
pilot policy. Never turn the flag on for an unrelated production task.

**Operator deployment:** The clean checkout at
`/home/dell/workspace/projectflow-qg-operator-20261122` points to code
SHA `28e727ab006868634a2a93fde1ddda7633745a98`. The installed
wrapper invokes that checkout through the existing ProjectFlow Python venv,
with no live service edits or restarts. Running it on Dell produced a safe
JSON report with `status=DISABLED`, `enabled=false`,
`strict_isolation_required=false` and
`production_end_to_end_accepted=false`.

**NEC:** New code checkout used the same
`scripts/quality_gate_readiness.py` on the separate strict shadow service
at `127.0.0.1:18769`. With the selected synthetic task #1, approved
immutable Docker image ID and public key, the report showed
`CONFIGURED_NEEDS_EXTERNAL_ACCEPTANCE`, `configuration_issues=[]`,
`probe=VERIFIER_BLOCKS_GIVEN_INPUT`,
`QUALITY_ATTESTATION_INVALID`, `receipt.fresh=false`,
`independent_qa_principal_proven=false` and
`github_required_merge_check_proven=false`. This is expected: the
receipt was host-direct signed, and expired.

### Security invariants

- Monitor reports are never merge authorization. They do not sign,
  push, accept merges, import receipts or change feature flags.
- Output omits private key, HMAC secret, signature body and file paths.
- It differentiates OUT_OF_SCOPE, DISABLED, CONFIGURATION_BLOCKED,
  missing receipt, invalid proof, expired/stale SHA and verifier
  acceptance **for the supplied input**. It never claims that this
  means a live merge or independent QA identity has been validated.
- The cryptographic server verifier from the prior production release
  remains the only policy entry point on the actual merge boundary.
- Separate protected QA key ownership **cannot** be inferred from file
  permissions or a valid Ed25519 signature alone. The connected
  NEC user has `docker`/container-admin-equivalent group access and
  no dedicated QA account; running a signer under another UID on the
  same host without removing those root-equivalent capabilities is not
  an adequate independent security boundary.

## Evidence and reproducibility

On Dell from the clean operator checkout:

```
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q \
  -o addopts='' -p no:cacheprovider \
  tests/test_real_merge.py tests/test_merge_reconciliation.py \
  tests/test_integration_create_idempotent.py \
  tests/test_quality_attestation_http_e2e.py \
  tests/test_quality_attestation_pilot.py tests/test_quality_ed25519_pilot.py \
  tests/test_quality_isolated_runner.py tests/test_quality_signer_cli.py \
  tests/test_release_pipeline.py tests/test_cross_repo_integration.py \
  tests/test_integration_readiness_orphans.py \
  tests/test_quality_gate_readiness.py
```

Evidence file on the Dell runtime:
`/dev/shm/qg-readiness-full-targeted-20261122.log` —
**101 passed, 749 warnings in 124.49 seconds**;
`wm-*` test containers/networks after run: **0**.

Direct CLI invocation:

```bash
/home/dell/.local/bin/projectflow-qg-preflight
```

When the approved QA signer and production policy are provisioned by a
separately authorized operator, the same CLI supports an exact
`--task-id --repository-id --repository-name --commit` diagnostic.
The report never supersedes the live merge gate.

## Requirements to qualify 100% (operator-privileged dependencies)

1. **Independent QA identity and private-key custody**: allocate a genuinely
   separate account/host or external signing service with an independent
   administrator. Ensure coding-agent users have neither the private key
   nor root-equivalent Docker/LXD control over the signing host. Do not
   export a private key to the verifier process or any coding-agent shell.
   Current NEC Terminal MCP session cannot perform privileged setup:
   `sudo -n` reports no-new-privileges; no dedicated QA principal exists.
2. **Authenticated receipt handoff**: run pinned, clean monitor-report
   pytest in immutable no-network/read-only Docker under the QA principal,
   only sign PASS, transfer **only signed receipt/public verification
   metadata** via an authorized authenticated path. Verify exact SHA,
   full policy, expiry, approved image SHA and the separate signer
   public key on NEC strict staging. Old host-direct/expired receipts
   must remain blocked. Prior connected transfer/signing attempts
   encountered tool safety restrictions; do not circumvent them.
3. **Real scoped production task**: register a reviewed repository and
   actual matching task policy; do not graft
   `PROJECTFLOW-MONITOR-REPORT-PILOT` onto unrelated MESFlow or QA tasks.
   Demonstrate one real blocked and one real accepted merge flow (without
   actually merging main prematurely), then select only that task.
4. **GitHub required check/ruleset**: repository owner/admin must
   configure the signed Quality Gate status as a *required* status
   for the protected target. This is the only way to prevent bypass via
   direct GitHub merge outside ProjectFlow. Current connector cannot
   read/manage branch protection details (403); `main` being marked
   protected is insufficient. Existing `.github/workflows/ci.yml`
   triggers GitHub Actions on PR and main push; do not trigger workflows
   while replacing that CI mechanism.
5. **Final KPI acceptance**: record selected tasks, signed receipt
   generation and expiry, actual rejected merges, accepted signed merges,
   false blocks, QA runtime cost/latency, and a no-regression window.
   Measure benefit relative to a real baseline; do not treat a green
   `/health` or suite PASS as empirical error reduction.

Until these dependencies are resolved, the honest production protection
count remains **zero selected tasks**, regardless of the implementation
readiness score.
