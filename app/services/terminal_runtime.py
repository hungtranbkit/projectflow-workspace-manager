"""Validated ProjectFlow ↔ Terminal MCP runtime associations.

This deliberately owns no dispatch queue and launches no process.  It records
the receipt returned by Terminal MCP beside the already-validated ProjectFlow
task/worktree so Open Codex/Open Claude views can resolve one source of truth.
"""
from __future__ import annotations

import re


class RuntimeAssociationError(RuntimeError):
    pass


_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,160}$")
_BACKENDS = {"tmux", "herdr-core"}
_STATES = {"working", "blocked", "idle", "done", "error", "unknown", "stale"}


class TerminalRuntimeAssociations:
    def __init__(self, db):
        self.db = db

    def _projection(self, association_id: int) -> dict:
        row = self.db.one(
            "SELECT a.*,w.branch,w.worktree_path AS worktree,r.repo_path AS repository "
            "FROM terminal_runtime_associations a "
            "JOIN agent_workspaces w ON w.id=a.workspace_id "
            "JOIN repositories r ON r.id=w.repository_id WHERE a.id=?",
            (association_id,),
        )
        if not row:
            raise RuntimeAssociationError("ASSOCIATION_NOT_FOUND")
        return row

    def record(self, *, task_id: int, workspace_id: int, backend: str, node_id: str,
               native_session_id: str, durable_task_id: str | None = None) -> dict:
        if backend not in _BACKENDS:
            raise RuntimeAssociationError("BACKEND_REFUSED")
        if not _ID.fullmatch(node_id or "") or not _ID.fullmatch(native_session_id or ""):
            raise RuntimeAssociationError("IDENTITY_REFUSED")
        workspace = self.db.one("SELECT id,task_id FROM agent_workspaces WHERE id=?", (workspace_id,))
        if not workspace or workspace.get("task_id") != task_id:
            raise RuntimeAssociationError("WORKSPACE_TASK_MISMATCH")
        existing = self.db.one(
            "SELECT id,native_session_id,durable_task_id FROM terminal_runtime_associations "
            "WHERE task_id=? AND workspace_id=? AND backend=? AND node_id=?",
            (task_id, workspace_id, backend, node_id),
        )
        if existing:
            if existing["native_session_id"] != native_session_id or existing.get("durable_task_id") != durable_task_id:
                raise RuntimeAssociationError("ASSOCIATION_CONFLICT")
            return self._projection(existing["id"])
        association_id = self.db.execute(
            "INSERT INTO terminal_runtime_associations(task_id,workspace_id,backend,node_id,native_session_id,durable_task_id) "
            "VALUES(?,?,?,?,?,?)",
            (task_id, workspace_id, backend, node_id, native_session_id, durable_task_id),
        )
        return self._projection(association_id)

    def observe(self, association_id: int, *, status: str, source: str, observed_at: str) -> dict:
        if status not in _STATES or not source.strip() or not observed_at.strip():
            raise RuntimeAssociationError("OBSERVATION_REFUSED")
        self.db.execute(
            "UPDATE terminal_runtime_associations SET status=?,status_source=?,observed_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, source[:80], observed_at, association_id),
        )
        return self._projection(association_id)
