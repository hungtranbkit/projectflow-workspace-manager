# Engineering Roles & Capabilities (Phase E2)

A reusable policy/metadata foundation, layered above the existing Task
execution model, answering: what engineering roles exist, what
capabilities does each require, what can each agent provider actually
do, and is a given provider↔role assignment valid.

Implemented in `app/services/engineering_catalog.py`
(`RoleCapabilityService`), migration V19, API under `/api/engineering/*`.

## Four concepts, kept separate on purpose

**Role** — a responsibility in the software-delivery process (`BUILDER`,
`REVIEWER`, `QA_VERIFIER`, ...). Not a person, not a provider.

**Capability** — a specific ability relevant to carrying out a role
(`EDIT_SOURCE`, `REVIEW_DIFF`, `RUN_TESTS`, ...).

**Provider** — the concrete executable/adapter, e.g. `codex` or
`claude` (`app/launchers.py`'s `AGENT_LAUNCHERS`). Role ≠ Provider: one
provider can support multiple roles, one role can be fulfilled by
multiple providers (or by a human).

**Authorization / Approval** — a completely different, pre-existing
layer this catalog never replaces: the trusted `AGENT_LAUNCHERS`
registry (what commands can actually run), `settings.agents` (which
names are allowed at all), and every existing human-clicked action
(Create PR, Merge, Deploy) staying exactly that — a human clicking a
button. **A provider having a capability in this catalog does not mean
it may act on it autonomously.** `claude` having `MERGE_PR` in its
capability catalog does not mean Claude may merge to `main` — it means
Claude could, in principle, participate in that operation, subject to
the workflow/policy/human-approval this app already requires unchanged.
Capability is not permission, and permission is not approval.

## Default role catalog

| Role | Category | Responsibility |
|---|---|---|
| `REQUIREMENTS_ANALYST` | Analysis | Turns a Change's intent into a requirement analysis WorkProduct. |
| `SPEC_ANALYST` | Analysis | Reads/writes FeatureSpecs against the Spec Layer (`specs/`). |
| `PLANNER` | Analysis | Decomposes a Change into Tasks and records the plan. |
| `BUILDER` | Delivery | Produces source changes in an isolated Builder Workspace. |
| `REVIEWER` | Quality | Inspects a Builder's exact commit/diff/evidence; returns PASS/FIX_REQUIRED/BLOCKED. |
| `SECURITY_REVIEWER` | Quality | Reviews for security impact. Catalog-equivalent to `REVIEWER` today — no dedicated security workflow exists yet. |
| `QA_VERIFIER` | Quality | Verifies behavior in a real, commit-pinned Sandbox; records PASS/FAIL evidence. Usually a human operator, not an agent. |
| `INTEGRATOR` | Operations | Combines approved branches, resolves integration conflicts, qualifies integrated source. |
| `RELEASE_MANAGER` | Operations | Builds/promotes/deploys qualified merged source under deployment policy. |

## Default capability catalog (28 keys)

Grouped by the ProjectFlow subsystem each one maps to — full
descriptions live in the seeded `capabilities` table
(`GET /api/engineering/capabilities`):

- **Repository/Spec**: `READ_REPOSITORY`, `READ_SPEC`, `WRITE_SPEC`
- **Planning**: `PLAN_TASK`
- **Build**: `EDIT_SOURCE`, `CREATE_COMMIT`, `RUN_TESTS`, `READ_TEST_RESULTS`
- **Review**: `REVIEW_SOURCE`, `REVIEW_DIFF`, `SUBMIT_REVIEW`
- **Verification**: `RUN_RUNTIME_VERIFICATION`, `RECORD_VERIFICATION`
- **Integration**: `RESOLVE_CONFLICTS`, `CREATE_INTEGRATION`, `PUSH_BRANCH`
- **Release**: `CREATE_PR`, `READ_CI`, `MERGE_PR`\*, `BUILD_ARTIFACT`,
  `DEPLOY_DEV`, `DEPLOY_TEST`, `DEPLOY_PRODUCTION`\*, `ROLLBACK_DEPLOYMENT`\*
- **Terminal**: `USE_INTERACTIVE_TERMINAL`, `USE_BROWSER`
- **Evidence**: `READ_EVIDENCE`, `WRITE_WORK_PRODUCT`

\* `sensitivity: SENSITIVE` — never `REQUIRED` for any role, never
`SUPPORTED` for any provider by default. Policy-controlled/human-gated
regardless of role or catalog entry.

## Role → capability matrix (required only)

| Role | Required capabilities |
|---|---|
| `REQUIREMENTS_ANALYST` | `READ_REPOSITORY`, `WRITE_WORK_PRODUCT` |
| `SPEC_ANALYST` | `READ_REPOSITORY`, `READ_SPEC`, `WRITE_SPEC`, `WRITE_WORK_PRODUCT` |
| `PLANNER` | `READ_REPOSITORY`, `READ_SPEC`, `PLAN_TASK`, `WRITE_WORK_PRODUCT` |
| `BUILDER` | `READ_REPOSITORY`, `EDIT_SOURCE`, `CREATE_COMMIT`, `RUN_TESTS` |
| `REVIEWER` | `READ_REPOSITORY`, `REVIEW_SOURCE`, `REVIEW_DIFF`, `READ_TEST_RESULTS`, `SUBMIT_REVIEW` |
| `SECURITY_REVIEWER` | `READ_REPOSITORY`, `REVIEW_SOURCE`, `REVIEW_DIFF`, `SUBMIT_REVIEW` |
| `QA_VERIFIER` | `READ_EVIDENCE`, `RUN_RUNTIME_VERIFICATION`, `RECORD_VERIFICATION` |
| `INTEGRATOR` | `READ_REPOSITORY`, `RESOLVE_CONFLICTS`, `CREATE_INTEGRATION`, `RUN_TESTS` |
| `RELEASE_MANAGER` | `BUILD_ARTIFACT`, `DEPLOY_DEV`, `READ_EVIDENCE` |

## Provider → capability matrix (summary)

| Provider | Real adapter? | Summary |
|---|---|---|
| `codex` | Yes (`AGENT_LAUNCHERS`) | `SUPPORTED` for repository/source/test/review/terminal/evidence capabilities. `PARTIAL` for QA/Integration/Release capabilities ProjectFlow has no automated launch path for yet (still human-driven routes). `UNSUPPORTED` for `MERGE_PR`/`DEPLOY_TEST`/`DEPLOY_PRODUCTION`/`ROLLBACK_DEPLOYMENT` (policy-controlled). |
| `claude` | Yes (`AGENT_LAUNCHERS`) | Same as `codex`. |
| `gemini` | No | `UNSUPPORTED` for everything — `settings.agents` allows the *name* on a Builder Workspace, but no launcher exists, so ProjectFlow cannot make it actually do anything. |
| `aider` | No | Same as `gemini`. |
| `other` | No | Same as `gemini` — a placeholder label. |

`GET /api/engineering/providers` returns the full, current matrix
(source of truth; this table is a snapshot).

## What "PARTIAL" means

A provider might conceptually be able to do something (a code model can
"review" text), but if ProjectFlow has no adapter/workflow that actually
runs it in that capacity, it is never claimed `SUPPORTED` without
qualification. Example: `codex`/`claude` are `PARTIAL` for
`RESOLVE_CONFLICTS` — `integration_workspaces` has no stored
provider/launch column at all today, so nothing in ProjectFlow ever
launches an agent as an Integrator; a human resolves conflicts,
optionally running the provider's CLI manually outside ProjectFlow's
controlled session.

## Human / System actors

QA and Integration are, in ProjectFlow today, mostly human actions
(`manual_verifications.operator`, `qa_runs.tester_agent` defaulting to
`"qa"`). `review_runs.reviewer_agent` and `qa_runs.tester_agent` stay
free-text fields — no launch happens for either. The catalog never
requires a provider: a value that isn't a known provider name
(`codex`/`claude`/`gemini`/`aider`/`other`) is treated as a HUMAN/SYSTEM
actor and is never checked against the capability catalog at all (no
new `actor_kind` column — this is a query-time judgment against the
same provider list).

## Validation

`RoleCapabilityService.validate_assignment(provider, role_key,
project_policy=None)` returns:

```json
{
  "valid": true,
  "provider": "codex",
  "role": "BUILDER",
  "missing_required_capabilities": [],
  "partial_capabilities": [],
  "policy_blocked": false,
  "warnings": []
}
```

`missing_required_capabilities` lists any REQUIRED capability the
provider is `UNSUPPORTED` for (these make `valid: false`).
`partial_capabilities` lists REQUIRED capabilities the provider is only
`PARTIAL` for — surfaced as information, never on their own a hard
block. `policy_blocked` is set when a repository's `PROJECT.yaml`
`engineering:` policy narrows the role to a provider allow-list the
given provider isn't on (see below) — a policy can only restrict an
assignment the global catalog supports, never grant one it doesn't.

### Where it's actually enforced

- **`_start_builder_session`** (the real Supervisor entry point):
  every Builder Workspace launch is, by definition, the `BUILDER` role.
  An invalid assignment blocks Start with a human-readable message
  (`Action blocked`, HTTP 409) — this is a no-op for every workflow
  that already worked (`codex`/`claude` are `SUPPORTED`); it only turns
  an already-failing case (`gemini`/`aider`/`other`, which never had a
  real launcher) into an earlier, clearer error.
- **`start_review`** / **`start_qa`**: advisory only, never blocking —
  neither route launches a real process for the reviewer/tester name,
  and both accept free text (often a human name) by long-standing
  design. A known-but-unsupported provider name is logged
  (`ROLE_ASSIGNMENT_REJECTED`, advisory) but never rejects the request.
- **`INTEGRATOR`**: no current runtime assignment point exists at all
  (`integration_workspaces` has no provider column; "Open Integrator"
  is a static anchor link, not a launch action) — catalog-only in E2.

## Project/repository policy override

`PROJECT.yaml` may declare an optional `engineering:` block:

```yaml
engineering:
  roles:
    BUILDER:
      allowed_providers: [codex, claude]
```

No block: safe global defaults apply, unchanged. The block can only
**narrow** which providers may hold a role — it can never grant a
provider a capability the global catalog doesn't already say it
supports. Read via `project_contract.load_engineering_policy()` /
`GET /api/repositories/{id}/engineering-policy`.

## Recommended roles for a Change (advisory only)

`GET /api/engineering/recommended-roles?change_type=...&risk_level=...`
returns a plain list of role keys — pure metadata, never creates a
Task, Change, or agent assignment automatically.

| risk_level | Recommended roles |
|---|---|
| `LOW` | `BUILDER`, `REVIEWER` |
| `NORMAL` | `BUILDER`, `REVIEWER`, `INTEGRATOR` |
| `HIGH` | `BUILDER`, `REVIEWER`, `QA_VERIFIER`, `INTEGRATOR`, `RELEASE_MANAGER` |

`change_type=FEATURE` adds `SPEC_ANALYST`; `ARCHITECTURE_CHANGE` adds
`PLANNER`+`SPEC_ANALYST`; `SECURITY_CHANGE` adds `SECURITY_REVIEWER`.

## Not in E2

Automatic agent selection/staffing, planner decomposition, autonomous
Change orchestration, Change lifecycle derivation, automatic role
handoff, production auto-deploy, autonomous PR merge, a new human RBAC
model, and a Role Management UI. This phase makes those possible later
without implementing them prematurely.
