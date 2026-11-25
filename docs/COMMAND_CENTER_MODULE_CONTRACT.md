# Command Center curated module contract — R1-01 draft

This contract complements, not replaces, the native Architecture Center snapshot
schema. It defines a private reviewed logical hierarchy per canonical owner/repo
and exact source commit. Module identity is (project, stable ID), independent
of label or file path. Parent edges must form a forest; dependency cycles may
exist in real architectures and remain graph facts. Cross-project dependencies
are out of scope in v1. Missing/target-only/observed are not runtime health or
QA verdicts. No live manifest import, endpoint, database, scanner, scheduler, or
task-status writer is added by this slice. The next R1-02 slice must pin actual
tracked paths and obtain review of the real Zalo Hub manifest; R1-03 maps files.
Authorization, source freshness, provenance and release acceptance remain at
the existing Steward/Architecture/TaskDecisionService boundaries.

Only synthetic tests are included. Independent review and protected CI remain
required; this draft does not mark R1-01 accepted or change production.
