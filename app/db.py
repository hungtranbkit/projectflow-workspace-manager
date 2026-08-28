from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS repositories(id INTEGER PRIMARY KEY, repo_name TEXT NOT NULL, repo_path TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1, default_branch TEXT NOT NULL DEFAULT 'main', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS agent_workspaces(id INTEGER PRIMARY KEY, repository_id INTEGER NOT NULL REFERENCES repositories(id), agent TEXT NOT NULL, task_name TEXT NOT NULL, branch TEXT NOT NULL UNIQUE, worktree_path TEXT NOT NULL UNIQUE, base_branch TEXT NOT NULL, base_commit TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'CREATED', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_commit TEXT, ready_for_integration INTEGER NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '', closed_at TEXT);
CREATE TABLE IF NOT EXISTS integration_workspaces(id INTEGER PRIMARY KEY, repository_id INTEGER NOT NULL REFERENCES repositories(id), name TEXT NOT NULL, branch TEXT NOT NULL UNIQUE, worktree_path TEXT NOT NULL UNIQUE, base_branch TEXT NOT NULL, base_commit TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'CREATED', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, ready_for_main INTEGER NOT NULL DEFAULT 0, verified_commit TEXT, verified_at TEXT, closed_at TEXT);
CREATE TABLE IF NOT EXISTS integration_sources(integration_id INTEGER NOT NULL REFERENCES integration_workspaces(id), workspace_id INTEGER NOT NULL REFERENCES agent_workspaces(id), merged_commit TEXT, merged_at TEXT, PRIMARY KEY(integration_id, workspace_id));
CREATE TABLE IF NOT EXISTS test_runs(id INTEGER PRIMARY KEY, workspace_type TEXT NOT NULL, workspace_id INTEGER NOT NULL, command TEXT NOT NULL, stage TEXT NOT NULL, started_at TEXT, finished_at TEXT, exit_code INTEGER, status TEXT NOT NULL DEFAULT 'QUEUED', stdout_tail TEXT NOT NULL DEFAULT '', stderr_tail TEXT NOT NULL DEFAULT '', tested_commit TEXT);
CREATE TABLE IF NOT EXISTS workspace_events(id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_agent_active ON agent_workspaces(status);
CREATE INDEX IF NOT EXISTS idx_test_entity ON test_runs(workspace_type, workspace_id);
"""


class Database:
    def __init__(self, path: Path): self.path = path
    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(1)")
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
