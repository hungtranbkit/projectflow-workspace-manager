# ORCH-STD-20261123 Acceptance

| ID | Criterion | Proof required |
|---|---|---|
| A1 | Strict base/security preserved and release safe. | Base SHA and security diff review; no changes to old live checkout/DB/unit before gates. |
| A2 | Immutable Spec/Plan/Tasks/Acceptance/Runbook exist before code. | Files and pre-code SPEC/PLAN hashes. |
| A3 | Approved Spec plus meaningful V2 contract gates every dispatch with zero side effects on refusal. | API/UI/queue/supervisor negative tests and valid dispatch test. |
| A4 | One goal owner and one active executor; concurrent tick/restart idempotent. | Concurrency and restart tests plus session evidence. |
| A5 | Process exit/idle never means DONE; retry is safe, same-task and bounded, else accurate blocker. | EXITED/no-report and retry/backoff/offline/unknown tests. |
| A6 | QA, tests, merge, deployment stay independent; agent cannot accept itself. | Separate evidence rows/statuses and negative gate tests. |
| A7 | Mobile UI shows real task/node/proof/blockers/next action. | Staging screenshots/DOM at 375px and 428px, auth/XSS checks. |
| A8 | Focused, security, build, preflight, smoke and staged canary pass without new regressions. | Exact commands/exit status, strict baseline comparison, real canary records. |
| A9 | Existing service and public HTTPS 200 after safe deploy, otherwise explicitly BLOCKED. | Unit CWD, service state, localhost and public checks or verified no-change blocker. |
| A10 | Verifiable RESULT/EVIDENCE and commit/rollback proof, no duplicate live sessions. | Artifacts, scoped commit, clean worktree, session/process and rollback evidence. |

Overall acceptance is `LIVE_ACCEPTED` only if A1–A8 and A10 pass and A9 proves successful live deployment. `STAGING_ACCEPTED_LIVE_BLOCKED` requires all implementable staging criteria to pass and a specific externally evidenced deployment blocker. Missing required feature approval is a specification blocker and cannot be reported as an implementation PASS.


## Owner-approved P0 continuation gates (2026-11-25)

| ID | Criterion | Proof required |
|---|---|---|
| A11 | The existing CleanupWorker invokes one actual recovery/DAG action under a durable, fenced leader lease; stale owners cannot act after takeover or restart. | Shared-DB two-controller test, expired-lease takeover regression, stale recovery side-effect test, native cleanup callback retry and blocker canaries. |
| A12 | Ambiguous incidents use sanitized strict-schema advice only when the exact effective model is attested; Astra/high is preferred, otherwise the reason and fallback receipt are recorded. | Codex model-cache + explicit request evidence kept distinct from provider receipt; 12-case identical-input evaluation; Opus/high canonical-model receipt; budget/cooldown, malformed-action and no-shell/no-gate-bypass tests. |

A11/A12 are not release passes until staged tests and a real controlled ProjectFlow canary show the action, current owner/fence, task continuation evidence, and independent Review boundary. A local process marker canary proves dispatch plumbing only; it is not production or CLI-agent acceptance.
