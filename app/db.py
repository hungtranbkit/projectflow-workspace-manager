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
