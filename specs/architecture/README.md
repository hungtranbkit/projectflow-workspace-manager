# Architecture notes (spec-relevant only)

This directory holds architecture context that a FeatureSpec may need to
reference (`architecture/<name>.md`) -- it is not a duplicate of the
real, authoritative engineering docs, which stay in `AGENTS.md` and the
source itself:

- Task lifecycle / gate model: `AGENTS.md` "Task Lifecycle & Gate Model",
  `app/services/task_decision_service.py`.
- Sandbox contract + runtime dependencies: `AGENTS.md` "Sandbox &
  Cross-Repo Integration", `app/services/sandbox_manager.py`,
  `app/services/sandbox_contract.py`.
- Workflow Summary UX: `AGENTS.md` "Workflow Summary UX (V3)".
- Spec Layer itself: `AGENTS.md` "Spec-Driven Development (Spec Layer,
  V1)", `app/services/spec_registry.py`, `app/services/spec_gate.py`,
  `app/services/spec_compliance.py`, `app/services/evidence_store.py`.

Keep this file short. If a FeatureSpec needs deeper architecture
context, add one focused file here and reference it by name -- never
duplicate what AGENTS.md/source comments already say correctly.
