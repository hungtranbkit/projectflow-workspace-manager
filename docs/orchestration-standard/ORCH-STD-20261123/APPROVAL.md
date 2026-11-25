# ORCH-STD-20261123 Owner Approval

**Decision date:** 2026-11-23  
**Decision maker:** Human owner in the current ProjectFlow orchestration session  
**Approved feature:** `FEAT-ORCHESTRATION-STANDARD`, version 1  
**Decision:** Approve the proposed feature scope for implementation.

The owner replied **“thống nhất đi”** to the direct request to approve the proposed FeatureSpec. This is affirmative approval of the existing proposed scope. It does not waive implementation, security, test, staging, review, QA, or live deployment gates.

## Approved scope

The approved canonical feature has six requirements (`REQ-OS-001` through `REQ-OS-006`), six acceptance criteria (`AC-OS-001` through `AC-OS-006`), and three invariants (`INV-OS-001` through `INV-OS-003`). It covers the seven-section task contract and SpecGate dispatch barrier, derived goal closure, single-owner bounded continuation, safe blockage under uncertainty, an opt-in reconciliation tick in the existing autonomous service, and the authenticated evidence-focused Change/Task UI. Do not expand this scope or weaken its criteria.

## Canonical FeatureSpec hashes

- Original DRAFT SHA256: `473dec993cbfbec81ec37c862ea3801b290bea6740aa7158d4a6f9a0e615221b`
- Owner-approved SHA256: `fb99357e910a39267aaf774a4e674a10ca6fc5e4fa18e955c46a78f563b639d4`
- Exact canonical change: `status: draft` → `status: approved`; requirements, acceptance criteria, invariants, scope and all other FeatureSpec content are unchanged.

## Locked planning hashes

- Original locked SPEC.md SHA256: `ae81e9b4f97bb4b3d8dd909e28f2b78cb8cc9f3887363fd4c7ba44502a8e5ef1`
- OWNER_APPROVED SPEC.md SHA256 after the required state-header update: `2370e35ca555a0c0aef792066c549ecc2b78cc6000d18af121145153cc17fa54`
- PLAN.md SHA256: `f6ff1542d436b4e0b250c9f53068dc861d3530558a366080e3f11a5b060bb20f`


## Owner-approved continuation scope — 2026-11-25

The owner explicitly directed this same P0 to use a strong reasoning model as the orchestration decision brain, continue the actual recovery loop, and verify durable single-owner fencing before release. This authorizes the additive `REQ-OS-007..008`, `AC-OS-007..008`, and `INV-OS-004` now present in the FeatureSpec: scheduled recovery action through the existing worker under a durable lease; and bounded, sanitized, structured provider advice with deterministic validation/fallback. The owner specified Astra/high as preferred, Opus 5.5/high as fallback after an authenticated exact-model probe, and prohibited new API keys, uncontrolled spend, global CLI changes, duplicate schedulers, and gate bypasses. This is an explicit scope update, not an agent self-approval; it does not waive test, canary, independent Review, CI, merge, DB backup, deployment, or runtime verification.

Probe evidence: Codex CLI 0.171.0 accepted the explicit `gpt-6.2-astra`/high request and returned 12 structured cases, but emitted no trusted model receipt; the model-authored string said only generic “GPT-6”, so Astra remains unverified. Claude Code 2.1.332 was authenticated through its existing first-party Pro login; the bounded tool-less 12-case probe returned `modelUsage.canonicalModel=claude-opus-5-5`, with `--effort high`, 2 turns, 13,164 ms, and CLI-reported estimated cost USD 0.234748. No API key or global model configuration was changed. The recovery advisor defaults to Opus 5.5/high and exposes model/effort as process environment configuration; it is capped at three calls per day per database and 15-minute fingerprint cooldown, with no repeat retry on provider errors during cooldown.
