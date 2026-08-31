from __future__ import annotations

"""B0.3 -- AuthZ (docs/B0_HOSTED_PLATFORM_SECURITY_FOUNDATION.md). The
general per-route sweep B0.2's own docstring explicitly deferred here:
one `require_role()` FastAPI dependency, applied to every mutating
route across the 143 pre-existing E1-E13 engineering-lifecycle routes
plus B0.1/B0.2's own, resolving the acting user's role in the
organization that OWNS the resource the route mutates.

`repositories.organization_id` (B0.2) is the one tenant-scoping lever
every other table transitively FKs through -- never a second copy of
tenant identity. This module is the resolver: for each of the ~20
distinct entity kinds a mutating route can operate on, walk that
entity's own real foreign keys (grounded directly against the CREATE
TABLE statements in app/db.py, not assumed) back to one or more
`repositories.id`, then to their `organization_id`(s).

A resource can legitimately resolve to MORE than one organization (a
cross-repository Task spanning repos owned by different orgs -- see
AGENTS.md's own "A Task may span many repositories"). `require_role()`
requires the acting user to hold at least `min_role` in EVERY resolved
organization, never just one -- the conservative, fail-closed reading:
touching a multi-org resource requires standing in all of them, not
just one lucky match.

A resource that resolves to NO organization at all (an orgless
Task/Change/repository -- e.g. a still-unlinked repository, or a
BACKLOG Task created with no repo_scope_id yet) is refused outright
under AUTH_MODE=required (fail closed, per this track's own global
rule) rather than silently treated as accessible-by-anyone. The narrow
set of routes that legitimately create such an orgless resource (a
handful of body-based `create` routes -- see app/main.py's own inline
`_require_org_role_for_repo`/`_require_org_role_for_change` call sites)
are the only places that intentionally allow a blank/optional
repo-or-change reference through; every other route in this module
resolves against a resource that must already exist, so "no org" there
really does mean "nothing this user could legitimately reach"."""

ROLE_LEVEL = {"VIEWER": 0, "MEMBER": 1, "ADMIN": 2, "OWNER": 3}


class AuthzService:
    def __init__(self, db):
        self.db = db

    # ---- one small resolver per entity kind, each grounded in the -------
    # ---- real FK chain read straight out of app/db.py's CREATE TABLEs ---
    def _repo_ids_for_change(self, change_id: int) -> set[int]:
        row = self.db.one("SELECT project_id FROM changes WHERE id=?", (change_id,))
        return {row["project_id"]} if row and row["project_id"] else set()

    def _repo_ids_for_task(self, task_id: int) -> set[int]:
        """Three real, independent sources of a Task's repository, unioned
        -- a Task can carry any subset of them depending on how far along
        it is: `repo_scope_id` (the direct scope a BACKLOG Task is given
        at creation, before any Change/Workspace exists -- see
        POST /api/tasks's own repo_scope_id field), `change_id ->
        changes.project_id` (once a Task is spawned from a Change), and
        each of its own agent_workspaces.repository_id (once at least one
        Builder Workspace exists, possibly spanning several repos)."""
        ids: set[int] = set()
        row = self.db.one(
            "SELECT t.repo_scope_id, c.project_id FROM tasks t "
            "LEFT JOIN changes c ON c.id=t.change_id WHERE t.id=?", (task_id,))
        if not row:
            return ids
        if row["repo_scope_id"]:
            ids.add(row["repo_scope_id"])
        if row["project_id"]:
            ids.add(row["project_id"])
        for r in self.db.all(
                "SELECT DISTINCT repository_id FROM agent_workspaces WHERE task_id=? AND repository_id IS NOT NULL",
                (task_id,)):
            ids.add(r["repository_id"])
        return ids

    def _repo_ids_for_workspace(self, workspace_id: int) -> set[int]:
        row = self.db.one("SELECT repository_id FROM agent_workspaces WHERE id=?", (workspace_id,))
        return {row["repository_id"]} if row and row["repository_id"] else set()

    def _repo_ids_for_integration(self, integration_id: int) -> set[int]:
        row = self.db.one("SELECT repository_id FROM integration_workspaces WHERE id=?", (integration_id,))
        return {row["repository_id"]} if row and row["repository_id"] else set()

    def _repo_ids_for_sandbox(self, sandbox_id: int) -> set[int]:
        row = self.db.one("SELECT repository_id, task_id FROM sandboxes WHERE id=?", (sandbox_id,))
        if not row:
            return set()
        if row["repository_id"]:
            return {row["repository_id"]}
        if row["task_id"]:
            return self._repo_ids_for_task(row["task_id"])
        return set()

    def _repo_ids_for_agent_session(self, session_id: int) -> set[int]:
        row = self.db.one("SELECT workspace_id FROM agent_sessions WHERE id=?", (session_id,))
        if not row or not row["workspace_id"]:
            return set()
        return self._repo_ids_for_workspace(row["workspace_id"])

    def _repo_ids_for_deployment(self, deployment_id: int) -> set[int]:
        row = self.db.one("SELECT repository_id FROM deployments WHERE id=?", (deployment_id,))
        return {row["repository_id"]} if row and row["repository_id"] else set()

    def _repo_ids_for_release(self, release_id: int) -> set[int]:
        row = self.db.one("SELECT repository_id FROM releases WHERE id=?", (release_id,))
        return {row["repository_id"]} if row and row["repository_id"] else set()

    def _repo_ids_for_incident(self, incident_id: int) -> set[int]:
        row = self.db.one("SELECT project_id, change_id FROM incidents WHERE id=?", (incident_id,))
        if not row:
            return set()
        if row["project_id"]:
            return {row["project_id"]}
        if row["change_id"]:
            return self._repo_ids_for_change(row["change_id"])
        return set()

    def _repo_ids_for_finding(self, finding_id: int) -> set[int]:
        row = self.db.one("SELECT change_id, task_id, review_id FROM findings WHERE id=?", (finding_id,))
        if not row:
            return set()
        ids: set[int] = set()
        if row["change_id"]:
            ids |= self._repo_ids_for_change(row["change_id"])
        if row["task_id"]:
            ids |= self._repo_ids_for_task(row["task_id"])
        if not ids and row["review_id"]:
            rr = self.db.one(
                "SELECT task_id, workspace_id, integration_id FROM review_runs WHERE id=?", (row["review_id"],))
            if rr:
                if rr["task_id"]:
                    ids |= self._repo_ids_for_task(rr["task_id"])
                if rr["workspace_id"]:
                    ids |= self._repo_ids_for_workspace(rr["workspace_id"])
                if rr["integration_id"]:
                    ids |= self._repo_ids_for_integration(rr["integration_id"])
        return ids

    def _repo_ids_for_plan(self, plan_id: int) -> set[int]:
        row = self.db.one("SELECT change_id FROM plans WHERE id=?", (plan_id,))
        if not row or not row["change_id"]:
            return set()
        return self._repo_ids_for_change(row["change_id"])

    def _repo_ids_for_spec_proposal(self, proposal_id: int) -> set[int]:
        row = self.db.one("SELECT change_id, project_id FROM spec_proposals WHERE id=?", (proposal_id,))
        if not row:
            return set()
        if row["project_id"]:
            return {row["project_id"]}
        if row["change_id"]:
            return self._repo_ids_for_change(row["change_id"])
        return set()

    def _repo_ids_for_product_acceptance(self, pa_id: int) -> set[int]:
        row = self.db.one("SELECT change_id FROM product_acceptances WHERE id=?", (pa_id,))
        if not row or not row["change_id"]:
            return set()
        return self._repo_ids_for_change(row["change_id"])

    def _repo_ids_for_test_case_spec(self, tc_id: int) -> set[int]:
        row = self.db.one("SELECT change_id FROM test_case_specs WHERE id=?", (tc_id,))
        if not row or not row["change_id"]:
            return set()
        return self._repo_ids_for_change(row["change_id"])

    def _repo_ids_for_work_product(self, wp_id: int) -> set[int]:
        row = self.db.one("SELECT project_id, change_id, task_id FROM work_products WHERE id=?", (wp_id,))
        if not row:
            return set()
        if row["project_id"]:
            return {row["project_id"]}
        if row["change_id"]:
            return self._repo_ids_for_change(row["change_id"])
        if row["task_id"]:
            return self._repo_ids_for_task(row["task_id"])
        return set()

    def _repo_ids_for_execution_wave(self, wave_id: int) -> set[int]:
        row = self.db.one("SELECT change_id FROM execution_waves WHERE id=?", (wave_id,))
        if not row or not row["change_id"]:
            return set()
        return self._repo_ids_for_change(row["change_id"])

    def _repo_ids_for_human_decision(self, decision_id: int) -> set[int]:
        row = self.db.one("SELECT subject_type, subject_id FROM human_decisions WHERE id=?", (decision_id,))
        if not row:
            return set()
        dispatch = {
            "change": self._repo_ids_for_change,
            "plan": self._repo_ids_for_plan,
            "spec_proposal": self._repo_ids_for_spec_proposal,
            "work_product": self._repo_ids_for_work_product,
        }
        fn = dispatch.get(row["subject_type"])
        return fn(row["subject_id"]) if fn else set()

    RESOLVERS = {
        "repository": lambda self, rid: {rid} if self.db.one("SELECT id FROM repositories WHERE id=?", (rid,)) else set(),
        "change": _repo_ids_for_change,
        "task": _repo_ids_for_task,
        "workspace": _repo_ids_for_workspace,
        "integration": _repo_ids_for_integration,
        "sandbox": _repo_ids_for_sandbox,
        "agent_session": _repo_ids_for_agent_session,
        "deployment": _repo_ids_for_deployment,
        "release": _repo_ids_for_release,
        "incident": _repo_ids_for_incident,
        "finding": _repo_ids_for_finding,
        "plan": _repo_ids_for_plan,
        "spec_proposal": _repo_ids_for_spec_proposal,
        "product_acceptance": _repo_ids_for_product_acceptance,
        "test_case_spec": _repo_ids_for_test_case_spec,
        "work_product": _repo_ids_for_work_product,
        "execution_wave": _repo_ids_for_execution_wave,
        "human_decision": _repo_ids_for_human_decision,
    }

    def resolve_repository_ids(self, kind: str, entity_id: int) -> set[int]:
        fn = self.RESOLVERS.get(kind)
        if fn is None:
            raise ValueError(f"authz: unknown resource kind {kind!r}")
        return fn(self, entity_id)

    def resolve_organization_ids(self, kind: str, entity_id: int) -> set[int]:
        """Empty set means either the resource doesn't exist, or it
        resolves to zero organizations (orgless/unlinked) -- callers
        must treat both the same way: fail closed, never "no
        organization to check against" == "allowed"."""
        repo_ids = self.resolve_repository_ids(kind, entity_id)
        if not repo_ids:
            return set()
        placeholders = ",".join("?" for _ in repo_ids)
        rows = self.db.all(
            f"SELECT DISTINCT organization_id FROM repositories WHERE id IN ({placeholders}) "
            f"AND organization_id IS NOT NULL", tuple(repo_ids))
        return {r["organization_id"] for r in rows}

    def organization_ids_for_repository(self, repository_id: int) -> set[int]:
        row = self.db.one("SELECT organization_id FROM repositories WHERE id=?", (repository_id,))
        return {row["organization_id"]} if row and row["organization_id"] else set()
