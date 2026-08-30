from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Versioned, additive migrations. Each entry is (version, sql). Applied in
# order, once, tracked in schema_migrations -- a fresh DB applies all of
# them; an existing local dev DB only applies whichever versions it is
# missing. Never edit an already-shipped migration's SQL after it has been
# released; add a new version instead (same discipline as any other
# project's Alembic-style migrations in this workspace).
MIGRATIONS: list[tuple[int, str]] = [
    (1, """
CREATE TABLE IF NOT EXISTS repositories(id INTEGER PRIMARY KEY, repo_name TEXT NOT NULL, repo_path TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1, default_branch TEXT NOT NULL DEFAULT 'main', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS agent_workspaces(id INTEGER PRIMARY KEY, repository_id INTEGER NOT NULL REFERENCES repositories(id), agent TEXT NOT NULL, task_name TEXT NOT NULL, branch TEXT NOT NULL UNIQUE, worktree_path TEXT NOT NULL UNIQUE, base_branch TEXT NOT NULL, base_commit TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'CREATED', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_commit TEXT, ready_for_integration INTEGER NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '', closed_at TEXT);
CREATE TABLE IF NOT EXISTS integration_workspaces(id INTEGER PRIMARY KEY, repository_id INTEGER NOT NULL REFERENCES repositories(id), name TEXT NOT NULL, branch TEXT NOT NULL UNIQUE, worktree_path TEXT NOT NULL UNIQUE, base_branch TEXT NOT NULL, base_commit TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'CREATED', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, ready_for_main INTEGER NOT NULL DEFAULT 0, verified_commit TEXT, verified_at TEXT, closed_at TEXT);
CREATE TABLE IF NOT EXISTS integration_sources(integration_id INTEGER NOT NULL REFERENCES integration_workspaces(id), workspace_id INTEGER NOT NULL REFERENCES agent_workspaces(id), merged_commit TEXT, merged_at TEXT, PRIMARY KEY(integration_id, workspace_id));
CREATE TABLE IF NOT EXISTS test_runs(id INTEGER PRIMARY KEY, workspace_type TEXT NOT NULL, workspace_id INTEGER NOT NULL, command TEXT NOT NULL, stage TEXT NOT NULL, started_at TEXT, finished_at TEXT, exit_code INTEGER, status TEXT NOT NULL DEFAULT 'QUEUED', stdout_tail TEXT NOT NULL DEFAULT '', stderr_tail TEXT NOT NULL DEFAULT '', tested_commit TEXT);
CREATE TABLE IF NOT EXISTS workspace_events(id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_agent_active ON agent_workspaces(status);
CREATE INDEX IF NOT EXISTS idx_test_entity ON test_runs(workspace_type, workspace_id);
"""),
    # V2: Sandbox & Cross-Repo Integration -- Task spans many AgentWorkspaces
    # (each still exactly one repo/branch/worktree) and coordinates many
    # per-repo integration_workspaces (now "repo integrations") under one
    # task_integrations row. Sandbox is the runtime layer, independent of
    # both: an AgentWorkspace may own zero-or-one sandbox, a task_integration
    # owns exactly one (never reuses an agent's).
    (2, """
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'OPEN',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  merged_at TEXT, cleanup_eligible_at TEXT, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS task_integrations(
  id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
  status TEXT NOT NULL DEFAULT 'CREATED',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  ready_for_main INTEGER NOT NULL DEFAULT 0, verified_at TEXT
);
CREATE TABLE IF NOT EXISTS sandboxes(
  id INTEGER PRIMARY KEY,
  task_id INTEGER REFERENCES tasks(id),
  repository_id INTEGER REFERENCES repositories(id),
  owner_type TEXT NOT NULL,
  owner_id INTEGER NOT NULL,
  sandbox_slug TEXT NOT NULL UNIQUE,
  profile TEXT NOT NULL DEFAULT 'NONE',
  runtime_type TEXT NOT NULL DEFAULT 'docker-compose',
  compose_project TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'CREATED',
  worktree_path TEXT,
  environment_path TEXT,
  source_manifest_json TEXT NOT NULL DEFAULT '{}',
  health_status TEXT NOT NULL DEFAULT 'UNKNOWN',
  last_health_check TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT, stopped_at TEXT,
  cleanup_eligible_at TEXT, cleaned_at TEXT,
  error_code TEXT, error_message TEXT
);
CREATE TABLE IF NOT EXISTS sandbox_sources(
  id INTEGER PRIMARY KEY, sandbox_id INTEGER NOT NULL REFERENCES sandboxes(id),
  repository_id INTEGER NOT NULL REFERENCES repositories(id),
  role TEXT NOT NULL, branch TEXT NOT NULL, commit_sha TEXT NOT NULL, worktree_path TEXT NOT NULL,
  source_type TEXT NOT NULL DEFAULT 'AGENT_WORKSPACE',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sandbox_ports(
  id INTEGER PRIMARY KEY, sandbox_id INTEGER NOT NULL REFERENCES sandboxes(id),
  service TEXT NOT NULL, host_port INTEGER NOT NULL, container_port INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  released_at TEXT,
  UNIQUE(sandbox_id, service)
);
CREATE TABLE IF NOT EXISTS sandbox_operations(
  id INTEGER PRIMARY KEY, sandbox_id INTEGER NOT NULL REFERENCES sandboxes(id),
  operation_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'RUNNING',
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT,
  exit_code INTEGER, stdout_tail TEXT NOT NULL DEFAULT '', stderr_tail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS hardware_test_results(
  id INTEGER PRIMARY KEY, sandbox_id INTEGER NOT NULL REFERENCES sandboxes(id),
  result TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sandbox_owner ON sandboxes(owner_type, owner_id);
CREATE INDEX IF NOT EXISTS idx_sandbox_task ON sandboxes(task_id);
CREATE INDEX IF NOT EXISTS idx_sandbox_cleanup ON sandboxes(status, cleanup_eligible_at);
CREATE INDEX IF NOT EXISTS idx_sandbox_source_sandbox ON sandbox_sources(sandbox_id);
CREATE INDEX IF NOT EXISTS idx_sandbox_port_sandbox ON sandbox_ports(sandbox_id);
CREATE INDEX IF NOT EXISTS idx_sandbox_op_sandbox ON sandbox_operations(sandbox_id);
"""),
    # V3: additive columns linking the existing single-repo entities into
    # the new task model. Nullable/zero-default so every pre-V2 row (and
    # every existing test fixture) keeps working unchanged -- a workspace
    # or repo-integration with no task_id is simply not part of a Task yet.
    (3, """
ALTER TABLE agent_workspaces ADD COLUMN task_id INTEGER REFERENCES tasks(id);
ALTER TABLE agent_workspaces ADD COLUMN role TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_workspaces ADD COLUMN sandbox_profile TEXT;
ALTER TABLE integration_workspaces ADD COLUMN task_integration_id INTEGER REFERENCES task_integrations(id);
ALTER TABLE tasks ADD COLUMN default_sandbox_profile TEXT;
CREATE INDEX IF NOT EXISTS idx_agent_task ON agent_workspaces(task_id);
CREATE INDEX IF NOT EXISTS idx_integration_task_integration ON integration_workspaces(task_integration_id);
"""),
    # V4: Verification UX. Two new, purely additive tables -- no existing
    # column gains a second meaning. A verification_reports row is the
    # agent's own completion report (WORK_STATUS/WHAT_CHANGED/HOW_TO_VERIFY/
    # EXPECTED_RESULT/TEST_DATA/RISKS -- see templates/agent-completion-
    # report.md); workspace_id NULL means it is the Task-level note,
    # workspace_id set means a workspace-specific addition. A
    # manual_verifications row is a human's PASS/FAIL against one exact
    # sandbox_id at one exact source_commit -- staleness is never stored,
    # it is recomputed by comparing source_commit to the source branch's
    # current git HEAD at render time (the same way sandbox staleness
    # already works), so there is no second copy of "is it still valid".
    (4, """
CREATE TABLE IF NOT EXISTS verification_reports(
  id INTEGER PRIMARY KEY,
  task_id INTEGER REFERENCES tasks(id),
  workspace_id INTEGER REFERENCES agent_workspaces(id),
  work_status TEXT NOT NULL DEFAULT 'READY',
  what_changed TEXT NOT NULL DEFAULT '',
  automated_tests TEXT NOT NULL DEFAULT '',
  how_to_verify TEXT NOT NULL DEFAULT '',
  expected_result TEXT NOT NULL DEFAULT '',
  test_data TEXT NOT NULL DEFAULT '',
  runtime_requirements TEXT NOT NULL DEFAULT 'NONE',
  risks TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS manual_verifications(
  id INTEGER PRIMARY KEY,
  task_id INTEGER REFERENCES tasks(id),
  workspace_id INTEGER REFERENCES agent_workspaces(id),
  sandbox_id INTEGER NOT NULL REFERENCES sandboxes(id),
  result TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  source_commit TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_verification_task ON verification_reports(task_id);
CREATE INDEX IF NOT EXISTS idx_verification_workspace ON verification_reports(workspace_id);
CREATE INDEX IF NOT EXISTS idx_manual_verif_sandbox ON manual_verifications(sandbox_id);
CREATE INDEX IF NOT EXISTS idx_manual_verif_workspace ON manual_verifications(workspace_id);
"""),
    # V5: task-first control plane. tasks.status now moves through an
    # explicit BACKLOG->PREPARE->DEVELOPMENT->REVIEW->QA->INTEGRATION->
    # READY_FOR_MAIN->MERGED->CLOSED (+CANCELLED) lifecycle -- a Task can
    # sit in BACKLOG with no branch/worktree/sandbox at all until
    # explicitly selected. Old rows (OPEN/IN_PROGRESS/READY_FOR_INTEGRATION/
    # INTEGRATING/TESTING) keep their existing string; app/main.py maps them
    # for display (normalize_task_status) rather than rewriting history.
    # review_status/qa_status/review_commit live on agent_workspaces (a
    # discrete recorded decision, like manual_verifications already is)
    # so per-workspace gate state is never a second guess derived
    # elsewhere. AgentSession is the new real entity behind the web
    # terminal: one row per PTY-backed agent process, cwd always pinned to
    # a registered Agent Workspace worktree, never a browser-supplied path.
    (5, """
ALTER TABLE tasks ADD COLUMN priority TEXT NOT NULL DEFAULT 'NORMAL';
ALTER TABLE tasks ADD COLUMN tags TEXT NOT NULL DEFAULT '';
ALTER TABLE tasks ADD COLUMN repo_scope_id INTEGER REFERENCES repositories(id);
ALTER TABLE tasks ADD COLUMN notes TEXT NOT NULL DEFAULT '';
ALTER TABLE tasks ADD COLUMN risk_profile TEXT NOT NULL DEFAULT 'NORMAL';
ALTER TABLE tasks ADD COLUMN brief_goal TEXT NOT NULL DEFAULT '';
ALTER TABLE tasks ADD COLUMN brief_context TEXT NOT NULL DEFAULT '';
ALTER TABLE tasks ADD COLUMN brief_requirements TEXT NOT NULL DEFAULT '';
ALTER TABLE tasks ADD COLUMN brief_acceptance_criteria TEXT NOT NULL DEFAULT '';
ALTER TABLE tasks ADD COLUMN brief_out_of_scope TEXT NOT NULL DEFAULT '';
ALTER TABLE tasks ADD COLUMN brief_test_plan TEXT NOT NULL DEFAULT '';
ALTER TABLE tasks ADD COLUMN brief_risks TEXT NOT NULL DEFAULT '';
ALTER TABLE tasks ADD COLUMN agent_prompt TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_workspaces ADD COLUMN reviewer_agent TEXT;
ALTER TABLE agent_workspaces ADD COLUMN review_status TEXT;
ALTER TABLE agent_workspaces ADD COLUMN review_notes TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_workspaces ADD COLUMN review_commit TEXT;
ALTER TABLE agent_workspaces ADD COLUMN tester_agent TEXT;
ALTER TABLE agent_workspaces ADD COLUMN qa_status TEXT;
ALTER TABLE agent_workspaces ADD COLUMN qa_notes TEXT NOT NULL DEFAULT '';
ALTER TABLE verification_reports ADD COLUMN files_changed TEXT NOT NULL DEFAULT '';
ALTER TABLE verification_reports ADD COLUMN tests_run TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS agent_sessions(
  id INTEGER PRIMARY KEY,
  task_id INTEGER REFERENCES tasks(id),
  workspace_id INTEGER NOT NULL REFERENCES agent_workspaces(id),
  agent TEXT NOT NULL,
  command_profile TEXT NOT NULL,
  cwd TEXT NOT NULL,
  pid INTEGER,
  status TEXT NOT NULL DEFAULT 'STARTING',
  mode TEXT NOT NULL DEFAULT 'INTERACTIVE',
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_activity_at TEXT,
  exited_at TEXT,
  exit_code INTEGER,
  transcript_tail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_workspace ON agent_sessions(workspace_id);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_status ON agent_sessions(status);
"""),
    # V6: Task Lifecycle & Gate Model Refactor. tasks.status is reduced to
    # three PERSISTED values from here on -- BACKLOG / ACTIVE / CANCELLED.
    # BLOCKED / READY_FOR_MAIN / DONE are never written to this column;
    # TaskDecisionService computes them live from real child evidence on
    # every read (the same fix already applied twice before in this
    # codebase's history to task_integrations.ready_for_main and the V5
    # Kanban column -- a status a route can forget to update is a status
    # that WILL drift). review_runs/qa_runs are real history tables (never
    # overwritten -- old evidence stays queryable) replacing the single
    # mutable reviewer_agent/review_status/... columns V5 added directly
    # on agent_workspaces; those columns are left in place, unused by any
    # new code, and their last known values are copied into one seed
    # review_runs/qa_runs row each so V5 evidence is not silently lost.
    # merge_records makes cross-repo partial-merge tracking real: Task
    # DONE is a fact derived from every REQUIRED repo's MergeRecord being
    # MERGED, never a single "Mark Merged" click for the whole Task.
    (6, """
ALTER TABLE tasks ADD COLUMN brief_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE tasks ADD COLUMN legacy_status TEXT;
ALTER TABLE tasks ADD COLUMN needs_reconciliation INTEGER NOT NULL DEFAULT 0;
-- Section 13: Builder completion is pinned to the exact commit and Brief
-- version at submission time, so a later commit or Brief edit can make a
-- downstream Review/QA PASS stale without rewriting the report itself.
ALTER TABLE verification_reports ADD COLUMN commit_sha TEXT;
ALTER TABLE verification_reports ADD COLUMN brief_version INTEGER;
CREATE TABLE IF NOT EXISTS review_runs(
  id INTEGER PRIMARY KEY,
  task_id INTEGER REFERENCES tasks(id),
  workspace_id INTEGER REFERENCES agent_workspaces(id),
  integration_id INTEGER REFERENCES integration_workspaces(id),
  reviewer_type TEXT NOT NULL DEFAULT 'BUILDER_WORKSPACE',
  reviewer_agent TEXT,
  brief_version INTEGER,
  reviewed_commit TEXT,
  status TEXT NOT NULL DEFAULT 'PENDING',
  findings TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT
);
CREATE TABLE IF NOT EXISTS qa_runs(
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  workspace_id INTEGER REFERENCES agent_workspaces(id),
  brief_version INTEGER,
  source_manifest TEXT NOT NULL DEFAULT '{}',
  sandbox_id INTEGER REFERENCES sandboxes(id),
  tester_agent TEXT,
  status TEXT NOT NULL DEFAULT 'PENDING',
  automated_results TEXT NOT NULL DEFAULT '',
  manual_result TEXT,
  hardware_result TEXT,
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT
);
CREATE TABLE IF NOT EXISTS merge_records(
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  repository_id INTEGER NOT NULL REFERENCES repositories(id),
  required INTEGER NOT NULL DEFAULT 1,
  integration_branch TEXT,
  pr_ref TEXT,
  merge_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
  merged_commit TEXT,
  merged_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(task_id, repository_id)
);
CREATE TABLE IF NOT EXISTS prompts(
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  workspace_id INTEGER REFERENCES agent_workspaces(id),
  prompt_type TEXT NOT NULL,
  brief_version INTEGER NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_review_runs_workspace ON review_runs(workspace_id);
CREATE INDEX IF NOT EXISTS idx_review_runs_task ON review_runs(task_id);
CREATE INDEX IF NOT EXISTS idx_qa_runs_task ON qa_runs(task_id);
CREATE INDEX IF NOT EXISTS idx_merge_records_task ON merge_records(task_id);
CREATE INDEX IF NOT EXISTS idx_prompts_task ON prompts(task_id);

-- Seed review_runs/qa_runs from V5's single-row columns so existing
-- evidence survives as real history instead of being dropped.
INSERT INTO review_runs(task_id,workspace_id,reviewer_type,reviewer_agent,reviewed_commit,status,findings,created_at)
  SELECT task_id,id,'BUILDER_WORKSPACE',reviewer_agent,review_commit,COALESCE(review_status,'PENDING'),COALESCE(review_notes,''),updated_at
  FROM agent_workspaces WHERE reviewer_agent IS NOT NULL;
INSERT INTO qa_runs(task_id,workspace_id,tester_agent,status,notes,created_at)
  SELECT task_id,id,tester_agent,COALESCE(qa_status,'PENDING'),COALESCE(qa_notes,''),updated_at
  FROM agent_workspaces WHERE tester_agent IS NOT NULL AND task_id IS NOT NULL;

-- Required MergeRecord per distinct repository already represented by a
-- Task's Builder Workspaces, so cross-repo merge tracking exists for
-- every Task carried over from before this phase.
INSERT OR IGNORE INTO merge_records(task_id,repository_id,required,merge_status)
  SELECT DISTINCT task_id,repository_id,1,'NOT_STARTED' FROM agent_workspaces WHERE task_id IS NOT NULL;
UPDATE merge_records SET merge_status='MERGED',merged_at=CURRENT_TIMESTAMP
  WHERE task_id IN (SELECT id FROM tasks WHERE status IN ('MERGED','CLOSED'));

-- Collapse the old, much larger status vocabulary into the three values
-- this column holds from now on. BLOCKED/READY_FOR_MAIN/DONE are NEVER
-- written here -- they are always computed live by TaskDecisionService.
-- A legacy MERGED/CLOSED task becomes ACTIVE with its merge_records
-- already marked MERGED above, so the decision engine derives DONE for
-- it on the very next read -- exactly the same evidence-based path a
-- brand new Task reaches DONE through, not a special-cased status value.
UPDATE tasks SET legacy_status=status WHERE legacy_status IS NULL;
UPDATE tasks SET status='BACKLOG' WHERE status IN ('OPEN');
UPDATE tasks SET status='ACTIVE' WHERE status IN ('PREPARE','IN_PROGRESS','READY_FOR_INTEGRATION','INTEGRATING','TESTING','MERGED','CLOSED');
UPDATE tasks SET needs_reconciliation=1 WHERE legacy_status IN ('MERGED','CLOSED','READY_FOR_INTEGRATION','INTEGRATING','TESTING');
-- BACKLOG/ACTIVE/CANCELLED rows were already valid values and pass through unchanged.
"""),
    (7, """
-- Prompt-first Task creation UX: the single Implementation Prompt field
-- that replaces the structured Brief form as the primary way to describe
-- a Task. Deliberately reuses `brief_version` as the version counter for
-- this field too (bumped on every actual content change, same as the old
-- structured brief_* fields already did) instead of adding a second,
-- parallel "prompt_version" column -- TaskDecisionService.builder_view()
-- already treats brief_version as "the version of whatever currently
-- defines this Task's intent" and flips a pinned Review/QA row to STALE
-- the moment it moves, so this staleness cascade needs no code change.
-- Existing rows default to '' (empty), which is exactly what makes
-- render_agent_prompt() take the legacy structured-brief rendering path
-- for every Task that already existed before this migration -- old
-- structured tasks keep behaving exactly as before, unchanged.
ALTER TABLE tasks ADD COLUMN implementation_prompt TEXT NOT NULL DEFAULT '';
"""),
    (8, """
-- Builder execution UX: optional, per-Builder-Workspace extra
-- instructions layered on top of the Task's own effective prompt (Title
-- fallback or Implementation Prompt) -- e.g. "Backend: add API support
-- only." vs "Firmware: consume the new API from ESP." on the same Task.
-- Never required; empty means "use the Task prompt alone". Not
-- separately versioned -- it has no Review/QA staleness implications of
-- its own beyond what brief_version already covers for the shared intent.
ALTER TABLE agent_workspaces ADD COLUMN builder_instructions TEXT NOT NULL DEFAULT '';
"""),
    # V9: real prompt delivery + evidence-backed baseline waiver model
    # (Task #5 demo gaps). agent_sessions gains prompt_status (PENDING/
    # DELIVERED/FAILED) so 'Agent RUNNING' is never conflated with 'prompt
    # actually delivered' -- prompt_version/prompt_sha256/prompt_source
    # are a point-in-time audit snapshot of exactly what was sent, never
    # recomputed after the fact. baseline_failure_evidence is only ever
    # written after a REAL reproduction run against the Task's own base
    # commit (never inferred from "looks unrelated"). gate_waivers is an
    # explicit, audited, per-failure-fingerprint exception -- never a
    # blanket "ignore tests"; a fingerprint mismatch (source changed,
    # failure changed) makes a stored waiver simply not match anymore,
    # rather than needing a second "still valid?" flag to maintain.
    (9, """
ALTER TABLE agent_sessions ADD COLUMN prompt_status TEXT NOT NULL DEFAULT 'PENDING';
ALTER TABLE agent_sessions ADD COLUMN prompt_version INTEGER;
ALTER TABLE agent_sessions ADD COLUMN prompt_sha256 TEXT;
ALTER TABLE agent_sessions ADD COLUMN prompt_source TEXT;
ALTER TABLE agent_sessions ADD COLUMN delivered_at TEXT;
CREATE TABLE IF NOT EXISTS baseline_failure_evidence(
  id INTEGER PRIMARY KEY,
  repository_id INTEGER NOT NULL REFERENCES repositories(id),
  base_commit TEXT NOT NULL,
  gate TEXT NOT NULL,
  test_identifier TEXT NOT NULL,
  failure_fingerprint TEXT NOT NULL,
  baseline_run_id INTEGER REFERENCES test_runs(id),
  first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  evidence TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_baseline_evidence_lookup ON baseline_failure_evidence(repository_id, base_commit, gate, test_identifier);
CREATE TABLE IF NOT EXISTS gate_waivers(
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  integration_id INTEGER REFERENCES integration_workspaces(id),
  gate TEXT NOT NULL,
  test_identifier TEXT NOT NULL,
  failure_fingerprint TEXT NOT NULL,
  baseline_commit TEXT NOT NULL,
  baseline_run_id INTEGER REFERENCES test_runs(id),
  integration_run_id INTEGER REFERENCES test_runs(id),
  reason TEXT NOT NULL DEFAULT '',
  approved_by TEXT NOT NULL DEFAULT '',
  approved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_gate_waivers_task ON gate_waivers(task_id);
CREATE INDEX IF NOT EXISTS idx_gate_waivers_integration ON gate_waivers(integration_id,gate,test_identifier);
"""),
    # V10: real GitHub-backed merge execution. merge_records grows real
    # PR/CI/mergeability tracking fields, all of them a live snapshot
    # from the last successful `gh pr view` (never hand-typed) --
    # `verified_commit`/`source_branch`/`target_branch` are pinned at
    # Create PR time so staleness (a later commit landing on the source
    # branch) is a real, detectable fact, never assumed current.
    # merge_status keeps its existing small vocabulary
    # (NOT_STARTED/PR_OPEN/MERGED/FAILED/CONFLICT) -- pr_state/ci_status/
    # mergeability are the NEW, separate, more granular live signals a
    # route/template reads to compute the exact merge-blocker reason,
    # never re-derived ad hoc.
    (10, """
ALTER TABLE merge_records ADD COLUMN pr_number INTEGER;
ALTER TABLE merge_records ADD COLUMN pr_url TEXT;
ALTER TABLE merge_records ADD COLUMN pr_state TEXT;
ALTER TABLE merge_records ADD COLUMN ci_status TEXT NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE merge_records ADD COLUMN mergeability TEXT NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE merge_records ADD COLUMN merge_state_status TEXT;
ALTER TABLE merge_records ADD COLUMN head_sha TEXT;
ALTER TABLE merge_records ADD COLUMN base_branch TEXT;
ALTER TABLE merge_records ADD COLUMN source_branch TEXT;
ALTER TABLE merge_records ADD COLUMN verified_commit TEXT;
ALTER TABLE merge_records ADD COLUMN merge_strategy TEXT NOT NULL DEFAULT 'MERGE_COMMIT';
ALTER TABLE merge_records ADD COLUMN last_synced_at TEXT;
ALTER TABLE merge_records ADD COLUMN external_merge_reason TEXT;
"""),
    # V11: real "Push Integration Branch" action. integration_workspaces
    # gains its own small, integration-scoped push state -- deliberately
    # NOT a new Task lifecycle status (section 5): local HEAD is always
    # read live via git, never stored; last_pushed_head/push_status/
    # pushed_at/push_error are the only new persisted facts, purely
    # answering "did the CURRENT local HEAD actually reach GitHub yet".
    (11, """
ALTER TABLE integration_workspaces ADD COLUMN last_pushed_head TEXT;
ALTER TABLE integration_workspaces ADD COLUMN push_status TEXT NOT NULL DEFAULT 'NOT_PUSHED';
ALTER TABLE integration_workspaces ADD COLUMN pushed_at TEXT;
ALTER TABLE integration_workspaces ADD COLUMN push_error TEXT;
"""),
    # V12: uniform action-button feedback (IDLE -> RUNNING -> SUCCEEDED/
    # FAILED). One small, generic ledger for the handful of actions that
    # had no existing job/run table of their own -- Merge Latest Changes,
    # Push Integration Branch, Create PR, Merge PR, Mark Ready for Main.
    # Deliberately NOT used for Run Tests (test_runs already tracks
    # QUEUED/RUNNING/PASS/FAIL) or Sandbox provision/reset/cleanup
    # (sandbox_operations already tracks RUNNING/SUCCESS/FAILED) -- both
    # keep their own table as the single source of truth, per the "reuse
    # existing job/run models" rule; `operations` never duplicates them.
    # At most one QUEUED/RUNNING row can exist for a given
    # (entity_type, entity_id, operation_type) at a time -- that is what
    # makes double-click / double-submit protection possible: a second
    # request finds the still-active row and simply reflects it back
    # instead of launching a second real git/GitHub call.
    (12, """
CREATE TABLE IF NOT EXISTS operations(
  id INTEGER PRIMARY KEY,
  operation_type TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'QUEUED',
  requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT,
  completed_at TEXT,
  error TEXT,
  result_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_operations_lookup ON operations(entity_type, entity_id, operation_type, status);
"""),
    # V13: Post-Merge DEV Deployment. A Deployment is a genuinely separate
    # aggregate from Task/MergeRecord (section 23) -- Task lifecycle
    # (DONE) and Deployment lifecycle (NOT_DEPLOYED/.../VERIFIED/FAILED)
    # never share a status column, and a FAILED deployment must never be
    # able to move a Task back out of DONE. `deployments` is append-only
    # evidence: a Redeploy/Retry always INSERTs a new row (rollback_of/
    # redeploy chains via that column), never UPDATEs a finished row's
    # historical result out from under it -- only a still-in-flight row
    # (status not yet terminal) is ever updated, exactly like test_runs/
    # sandbox_operations/operations already do. source_commit is always
    # the exact MergeRecord.merged_commit (section 3/24), never an
    # integration/agent branch -- enforced by the route that creates a
    # row, not by a constraint here (SQLite has no enum type).
    (13, """
CREATE TABLE IF NOT EXISTS deployments(
  id INTEGER PRIMARY KEY,
  task_id INTEGER REFERENCES tasks(id),
  repository_id INTEGER NOT NULL REFERENCES repositories(id),
  environment TEXT NOT NULL DEFAULT 'DEV',
  target_name TEXT,
  source_branch TEXT NOT NULL,
  source_commit TEXT NOT NULL,
  artifact_version TEXT,
  artifact_image TEXT,
  artifact_digest TEXT,
  status TEXT NOT NULL DEFAULT 'PENDING',
  health_status TEXT,
  health_checked_at TEXT,
  smoke_status TEXT,
  deployed_url TEXT,
  error TEXT,
  rollback_of INTEGER REFERENCES deployments(id),
  requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_deployments_lookup ON deployments(task_id, repository_id, environment);
CREATE TABLE IF NOT EXISTS deployment_phases(
  id INTEGER PRIMARY KEY,
  deployment_id INTEGER NOT NULL REFERENCES deployments(id),
  phase TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'RUNNING',
  stdout_tail TEXT NOT NULL DEFAULT '',
  stderr_tail TEXT NOT NULL DEFAULT '',
  exit_code INTEGER,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_deployment_phases_deployment ON deployment_phases(deployment_id);
"""),
    (14, """
ALTER TABLE deployments ADD COLUMN artifact_filename TEXT;
ALTER TABLE deployments ADD COLUMN artifact_sha256 TEXT;
ALTER TABLE deployments ADD COLUMN rollback_to_deployment_id INTEGER REFERENCES deployments(id);
ALTER TABLE deployments ADD COLUMN rollback_started_at TEXT;
ALTER TABLE deployments ADD COLUMN rollback_finished_at TEXT;
ALTER TABLE deployments ADD COLUMN rollback_status TEXT;
ALTER TABLE deployments ADD COLUMN rollback_error TEXT;
"""),
    (15, """
-- EXITED-without-report recovery: distinguishes a report that came from
-- the agent itself (detected/confirmed from its own terminal, or the old
-- manual paste form) from one a human wrote from scratch as a recovery
-- fallback after the agent process is gone and cannot be asked again.
-- Never changes what READY means -- both still require the exact same
-- clean-worktree + exact-commit-pinning discipline commit_sha already
-- enforces; this only records which path produced the report.
ALTER TABLE verification_reports ADD COLUMN ready_source TEXT NOT NULL DEFAULT 'AGENT_SUBMITTED';
ALTER TABLE verification_reports ADD COLUMN operator TEXT;
"""),
    (16, """
-- QA Center sandbox spec section 16: a recorded manual verification
-- must pin exactly who recorded it, matching verification_reports'
-- existing `operator` column (this app is single-operator/local-only --
-- 'ui' means "recorded through the web UI", same convention V15 already
-- established; never a real multi-user identity system). Runtime
-- dependency source commit(s) are NOT duplicated onto this row -- they
-- stay queryable from sandbox_sources(sandbox_id) via the same sandbox_id
-- already recorded here, one source of truth, never a second copy that
-- could drift.
ALTER TABLE manual_verifications ADD COLUMN operator TEXT;
"""),
    (17, """
-- Spec Layer V1: the canonical specification is the file tree under
-- specs/ (SpecRegistry reads it fresh every time -- these columns are
-- an INDEX/trace pointer into that tree, never a second copy of spec
-- content itself). All nullable/optional -- a Task created before this
-- feature existed, or through a route that doesn't ask, keeps
-- spec_change_classification NULL ("not yet classified", distinct from
-- the explicit AMBIGUOUS value) and SpecGate treats that as
-- NOT_APPLICABLE (passes unchanged) -- required for backward
-- compatibility with every existing Task/test/workflow. Only a Task
-- explicitly classified BEHAVIOR_CHANGE/NEW_FEATURE/SPEC_CHANGE/
-- BUG_FIX_TO_EXISTING_SPEC/AMBIGUOUS is ever gated.
ALTER TABLE tasks ADD COLUMN spec_change_classification TEXT;
ALTER TABLE tasks ADD COLUMN spec_feature_id TEXT;
ALTER TABLE tasks ADD COLUMN spec_version INTEGER;
ALTER TABLE tasks ADD COLUMN spec_requirement_ids TEXT NOT NULL DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN spec_acceptance_ids TEXT NOT NULL DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN spec_invariant_ids TEXT NOT NULL DEFAULT '[]';
-- Evidence trace metadata on the existing verification_reports table
-- (the EvidenceStore's real backing store -- see
-- app/services/evidence_store.py -- never a second, parallel evidence
-- table). Snapshotted from the Task at report-creation time so evidence
-- stays traceable to the exact spec slice it was produced against, even
-- if the Task's own linkage is edited afterward.
ALTER TABLE verification_reports ADD COLUMN spec_feature_id TEXT;
ALTER TABLE verification_reports ADD COLUMN spec_version INTEGER;
ALTER TABLE verification_reports ADD COLUMN spec_requirement_ids TEXT NOT NULL DEFAULT '[]';
ALTER TABLE verification_reports ADD COLUMN spec_acceptance_ids TEXT NOT NULL DEFAULT '[]';
ALTER TABLE verification_reports ADD COLUMN spec_invariant_ids TEXT NOT NULL DEFAULT '[]';
-- Release <-> spec baseline binding (section S10) -- minimal, additive
-- extension point on the one existing Release-shaped table
-- (DeploymentService/`deployments`), never a new Release/Qualification
-- model of its own.
ALTER TABLE deployments ADD COLUMN spec_baseline_sha256 TEXT;
"""),
    # V18: Engineering Domain Foundation (Phase E1). Adds Change and
    # WorkProduct as first-class entities ABOVE the existing Task model,
    # additive only -- every existing Task/Workspace/Spec/Evidence/
    # Deployment table and column is untouched.
    #
    # Project identity (E1.6): ProjectFlow has no separate `projects`
    # table -- `repositories` already IS the project boundary (one row
    # per registered repo, each with its own PROJECT.yaml/specs root,
    # already referenced this same way by tasks.repo_scope_id,
    # sandboxes.repository_id, deployments.repository_id, ...). Reused
    # directly as `project_id -> repositories(id)` rather than inventing
    # a parallel concept; nullable, matching tasks.repo_scope_id's own
    # optionality (a Change/WorkProduct may not be scoped to one repo
    # yet, e.g. an early cross-repo intent).
    #
    # changes: one human/product intent that may produce many Tasks.
    # lifecycle_state is stored, not derived (E1 explicitly defers
    # automatic lifecycle derivation to a later phase) -- an Agent has
    # no route that can set it to DONE in this phase (see change_service.py).
    #
    # tasks.change_id: nullable FK, so every existing Task (and every
    # existing test fixture) keeps working completely unchanged with
    # change_id NULL -- Change is additive, never mandatory in E1.
    #
    # work_products: a generic, typed-kind core table (never one table
    # per WorkProduct kind) for durable engineering outputs (specs,
    # designs, ADRs, code changes, review/verification reports, release
    # manifests, ...). Content itself is never stored inline here --
    # `content_ref` points at where the real content lives (a spec file
    # path, a verification_reports.id, a deployments.id, a free-form
    # URI, ...) and `content_metadata` carries small structured facts;
    # this is a reference/index row, the same discipline the Spec Layer
    # already established for tasks.spec_* (S1: "never a second copy of
    # the real content"). History-friendly: a revision is a NEW row with
    # `supersedes_id` pointing at the one it replaces -- no UPDATE ever
    # overwrites another WorkProduct's historical content/status wholesale.
    #
    # task_work_product_links: Task -> WorkProduct as typed INPUT/OUTPUT
    # references (E1.4) -- never large documents stored on the Task row
    # itself.
    #
    # trace_links: a minimal, TYPED (not polymorphic-blob) source/target
    # relationship table -- added only for the trace edges E1.5 requires
    # that existing columns do not already express (Change -> Spec
    # feature id, WorkProduct -> Release/Deployment). Every other
    # required trace already has a clean, existing, typed mechanism and
    # is deliberately NOT duplicated here: Requirement -> Task is
    # tasks.spec_requirement_ids, Task -> WorkProduct is
    # task_work_product_links, Task -> AgentSession is
    # agent_sessions.task_id, Task -> Evidence is EvidenceStore over
    # verification_reports/review_runs/qa_runs/test_runs (all already
    # task_id-keyed).
    (18, """
CREATE TABLE IF NOT EXISTS changes(
  id INTEGER PRIMARY KEY,
  project_id INTEGER REFERENCES repositories(id),
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  change_type TEXT NOT NULL DEFAULT 'FEATURE',
  risk_level TEXT NOT NULL DEFAULT 'NORMAL',
  lifecycle_state TEXT NOT NULL DEFAULT 'NEW',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_project ON changes(project_id);
CREATE INDEX IF NOT EXISTS idx_changes_lifecycle ON changes(lifecycle_state);

ALTER TABLE tasks ADD COLUMN change_id INTEGER REFERENCES changes(id);
CREATE INDEX IF NOT EXISTS idx_tasks_change ON tasks(change_id);

CREATE TABLE IF NOT EXISTS work_products(
  id INTEGER PRIMARY KEY,
  project_id INTEGER REFERENCES repositories(id),
  change_id INTEGER REFERENCES changes(id),
  task_id INTEGER REFERENCES tasks(id),
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'DRAFT',
  content_ref TEXT,
  content_metadata TEXT NOT NULL DEFAULT '{}',
  content_digest TEXT,
  supersedes_id INTEGER REFERENCES work_products(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_wp_project ON work_products(project_id);
CREATE INDEX IF NOT EXISTS idx_wp_change ON work_products(change_id);
CREATE INDEX IF NOT EXISTS idx_wp_task ON work_products(task_id);
CREATE INDEX IF NOT EXISTS idx_wp_kind ON work_products(kind);

CREATE TABLE IF NOT EXISTS task_work_product_links(
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  work_product_id INTEGER NOT NULL REFERENCES work_products(id),
  direction TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(task_id, work_product_id, direction)
);
CREATE INDEX IF NOT EXISTS idx_twpl_task ON task_work_product_links(task_id, direction);
CREATE INDEX IF NOT EXISTS idx_twpl_wp ON task_work_product_links(work_product_id);

CREATE TABLE IF NOT EXISTS trace_links(
  id INTEGER PRIMARY KEY,
  source_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  relation TEXT NOT NULL DEFAULT 'RELATES_TO',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(source_type, source_id, target_type, target_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_trace_source ON trace_links(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_trace_target ON trace_links(target_type, target_id);
"""),
    # V19: Role & Capability Catalog (Phase E2). Structure only -- the
    # actual catalog ROWS (which roles/capabilities exist, their
    # descriptions, the role<->capability and provider<->capability
    # mappings) are NOT seeded here as literal SQL. Unlike the rest of
    # this file, that catalog is real application data that legitimately
    # evolves (a capability's description gets clearer, a provider
    # gains support) without being a schema change -- baking ~150 rows
    # of it into an append-only, never-editable-after-release migration
    # would make every future wording tweak require a brand new
    # migration. Instead app/services/engineering_catalog.py holds the
    # canonical Python definitions and RoleCapabilityService.seed()
    # upserts them (ON CONFLICT DO UPDATE, keyed by the stable `key`/
    # (provider,capability_id) columns below) every time the app starts
    # -- idempotent and restart-safe by construction, the same
    # discipline AGENT_LAUNCHERS already uses for provider definitions
    # (a Python dict, not a table).
    #
    # GLOBAL vs PROJECT-SCOPED (E2 section 4): every table here is
    # global/canonical -- no project_id anywhere. A repository-level
    # override (allowed_providers per role, etc.) is modeled as a
    # read-time PROJECT.yaml `engineering:` policy overlay
    # (project_contract.load_engineering_policy), never cloned catalog
    # rows -- see that function's docstring.
    (19, """
CREATE TABLE IF NOT EXISTS engineering_roles(
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT 'DELIVERY',
  enabled INTEGER NOT NULL DEFAULT 1,
  system_defined INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS capabilities(
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT 'GENERAL',
  sensitivity TEXT NOT NULL DEFAULT 'NORMAL',
  system_defined INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS role_capabilities(
  id INTEGER PRIMARY KEY,
  role_id INTEGER NOT NULL REFERENCES engineering_roles(id),
  capability_id INTEGER NOT NULL REFERENCES capabilities(id),
  requirement TEXT NOT NULL DEFAULT 'REQUIRED',
  UNIQUE(role_id, capability_id)
);
CREATE TABLE IF NOT EXISTS agent_capabilities(
  id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL,
  capability_id INTEGER NOT NULL REFERENCES capabilities(id),
  support_level TEXT NOT NULL DEFAULT 'UNSUPPORTED',
  source TEXT NOT NULL DEFAULT 'BUILTIN',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(provider, capability_id)
);
CREATE INDEX IF NOT EXISTS idx_role_capabilities_role ON role_capabilities(role_id);
CREATE INDEX IF NOT EXISTS idx_role_capabilities_capability ON role_capabilities(capability_id);
CREATE INDEX IF NOT EXISTS idx_agent_capabilities_provider ON agent_capabilities(provider);
CREATE INDEX IF NOT EXISTS idx_agent_capabilities_capability ON agent_capabilities(capability_id);
"""),
    # V20: Workflow / Process Engine (Phase E3). Structure only -- same
    # discipline as V19: the actual catalog rows (stages/gates/profiles/
    # profile-stage mapping/task types) are seeded idempotently by
    # WorkflowCatalogService.seed() on every app startup
    # (app/services/workflow_engine.py), not baked into this
    # append-only migration.
    #
    # Three responsibilities, three table groups, deliberately never
    # merged (E3's key architectural rule):
    #   DEFINITION: workflow_stages, gate_requirements, workflow_profiles,
    #     workflow_profile_stages, task_types -- declarative, global.
    #   INSTANCE: workflow_runs -- one row per Change (change_id UNIQUE),
    #     records ONLY identity (profile_key/version). No status/stage
    #     column here at all -- WorkflowService.evaluate_workflow()
    #     always derives those fresh from Tasks/gates/evidence, the same
    #     "never a persisted status a route can forget to update"
    #     discipline TaskDecisionService already uses for Task status.
    #   EXECUTION: task_dependencies -- extends the existing Task model
    #     (tasks table, untouched) with a first-class dependency graph;
    #     TaskDecisionService/_start_builder_session remain the sole
    #     source of a Task's own execution truth.
    #
    # tasks.task_type is nullable/optional (E3.1) -- every existing Task
    # keeps working with it NULL, exactly the same backward-compatible
    # pattern tasks.change_id (E1) and tasks.spec_* (Spec Layer) already
    # established.
    (20, """
CREATE TABLE IF NOT EXISTS workflow_stages(
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  order_index INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS gate_requirements(
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  stage_id INTEGER NOT NULL REFERENCES workflow_stages(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS workflow_profiles(
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1,
  system_defined INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS workflow_profile_stages(
  id INTEGER PRIMARY KEY,
  profile_id INTEGER NOT NULL REFERENCES workflow_profiles(id),
  stage_id INTEGER NOT NULL REFERENCES workflow_stages(id),
  requirement TEXT NOT NULL DEFAULT 'OPTIONAL',
  condition_key TEXT,
  UNIQUE(profile_id, stage_id)
);
CREATE TABLE IF NOT EXISTS task_types(
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  stage_key TEXT,
  preferred_role_key TEXT,
  compatible_role_keys TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE tasks ADD COLUMN task_type TEXT;

CREATE TABLE IF NOT EXISTS workflow_runs(
  id INTEGER PRIMARY KEY,
  change_id INTEGER NOT NULL UNIQUE REFERENCES changes(id),
  profile_key TEXT NOT NULL,
  profile_version INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS task_dependencies(
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  depends_on_task_id INTEGER NOT NULL REFERENCES tasks(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(task_id, depends_on_task_id)
);
CREATE INDEX IF NOT EXISTS idx_workflow_profile_stages_profile ON workflow_profile_stages(profile_id);
CREATE INDEX IF NOT EXISTS idx_gate_requirements_stage ON gate_requirements(stage_id);
CREATE INDEX IF NOT EXISTS idx_task_types_stage ON task_types(stage_key);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_change ON workflow_runs(change_id);
CREATE INDEX IF NOT EXISTS idx_task_dependencies_task ON task_dependencies(task_id);
CREATE INDEX IF NOT EXISTS idx_task_dependencies_dep ON task_dependencies(depends_on_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_task_type ON tasks(task_type);
"""),
]


class Database:
    def __init__(self, path: Path): self.path = path
    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            applied = {r[0] for r in db.execute("SELECT version FROM schema_migrations").fetchall()}
            for version, sql in MIGRATIONS:
                if version in applied: continue
                db.executescript(sql)
                db.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))
    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        finally: db.close()
    def all(self, sql, args=()):
        with self.connect() as db: return [dict(r) for r in db.execute(sql, args)]
    def one(self, sql, args=()):
        with self.connect() as db:
            row = db.execute(sql, args).fetchone()
            return dict(row) if row else None
    def execute(self, sql, args=()):
        with self.connect() as db:
            cur = db.execute(sql, args)
            return cur.lastrowid
    def event(self, kind, entity_id, action, details=""):
        self.execute("INSERT INTO workspace_events(entity_type,entity_id,action,details) VALUES(?,?,?,?)", (kind, entity_id, action, details))
