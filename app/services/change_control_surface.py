from __future__ import annotations
import json

"""Change Control Surface (Phase E7.5): the human-facing Engineering
Lifecycle UI's ONE aggregation layer, over a Change. Same discipline as
change_overview.py (E7.5.19: "composition only, no duplicated truth, no
re-derived competing status logic, call existing services, keep
underlying APIs intact") -- every method here only ASSEMBLES what an
existing E1-E7 service already computed (ChangeService, WorkflowService,
SpecLifecycleService, ArchitectureDesignLifecycleService,
TestDesignLifecycleService, PlannerService, TaskDecisionService,
EvidenceStore, HumanDecisionService, DeploymentService's own
`deployments` table) into one page's worth of view-model. It never opens
a second requirement/coverage/staleness calculation, and it never calls
an LLM (E7.5.20: the Change Detail page must not invoke Claude merely to
render)."""


class ChangeControlSurfaceService:
    def __init__(self, db, changes, work_products, trace, decision, evidence, roles_catalog,
                 workflow_service, spec_lifecycle_service, architecture_design_service,
                 test_design_lifecycle_service, test_case_specs, executable_mapping,
                 planner_service, human_decisions, specs_root, project_policy_resolver):
        self.db = db
        self.changes = changes
        self.work_products = work_products
        self.trace = trace
        self.decision = decision
        self.evidence = evidence
        self.roles_catalog = roles_catalog
        self.workflow_service = workflow_service
        self.spec_lifecycle_service = spec_lifecycle_service
        self.architecture_design_service = architecture_design_service
        self.test_design_lifecycle_service = test_design_lifecycle_service
        self.test_case_specs = test_case_specs
        self.executable_mapping = executable_mapping
        self.planner_service = planner_service
        self.human_decisions = human_decisions
        self.specs_root = specs_root
        self.project_policy_resolver = project_policy_resolver

    # ---- shared helpers -------------------------------------------------
    def _change(self, change_id: int) -> dict | None:
        return self.changes.get(change_id)

    def _policy(self, change: dict | None):
        if not self.project_policy_resolver or not change:
            return None
        return self.project_policy_resolver(change)

    @staticmethod
    def _attach_content(wp: dict | None) -> dict | None:
        if wp is None:
            return None
        wp["content"] = json.loads(wp.get("content_metadata") or "{}")
        return wp

    def _governing_feature_ids(self, change_id: int) -> list[str]:
        return [l["target_id"] for l in self.trace.for_source("change", change_id) if l["target_type"] == "spec_feature"]

    def _wps(self, change_id: int, kind: str, current_only: bool = True) -> list[dict]:
        """Every WorkProduct row here also gets a `content` key -- its
        content_metadata, pre-parsed once -- so no template ever needs a
        JSON-decoding filter; content_metadata itself is left untouched
        for the raw/technical <details> view (_macros.html's raw())."""
        rows = [wp for wp in self.work_products.list_for_change(change_id) if wp["kind"] == kind]
        if current_only:
            rows = [wp for wp in rows if wp["status"] != "SUPERSEDED"]
        for wp in rows:
            wp["content"] = json.loads(wp["content_metadata"] or "{}")
        return rows

    def header(self, change_id: int) -> dict:
        """Header facts every tab route renders (E7.5.3)."""
        change = self._change(change_id)
        run = self.workflow_service.get_workflow(change_id)
        state = self.workflow_service.evaluate_workflow(change_id) if run else None
        baseline = None
        governing = self._governing_feature_ids(change_id)
        if governing:
            try:
                from app.services.spec_registry import SpecRegistry
                baseline = SpecRegistry(self.specs_root).load().baseline_digest()[:12]
            except Exception:
                baseline = None
        return {
            "change": change, "profile_key": run["profile_key"] if run else None,
            "status": state["status"] if state else "PENDING",
            "current_stage": state["current_stage"] if state else None,
            "spec_baseline": baseline,
        }

    # ---- E7.5.4 Overview (delegates to change_overview.py) --------------
    def overview(self, change_id: int) -> dict:
        from app.services.change_overview import build_change_overview
        change = self._change(change_id)
        change["_tasks"] = self.changes.list_tasks_for_change(change_id)
        run = self.workflow_service.get_workflow(change_id)
        change["profile_key"] = run["profile_key"] if run else None
        workflow_state = self.workflow_service.evaluate_workflow(change_id) if run else None
        policy = self._policy(change)
        agent_rows = self.db.all(
            "SELECT status,COUNT(*) c FROM agent_sessions WHERE task_id IN (SELECT id FROM tasks WHERE change_id=?) GROUP BY status", (change_id,))
        from app.services.task_decision_service import LIVE_SESSION_STATUSES
        agents_completed = sum(r["c"] for r in agent_rows if r["status"] == "EXITED")
        agents_running = sum(r["c"] for r in agent_rows if r["status"] in LIVE_SESSION_STATUSES)
        plans = self.planner_service.list_plans(change_id)
        spec_drift = {"stale": False, "reason": None}
        if plans:
            spec_drift = self.planner_service.check_staleness(plans[-1]["id"])
        return build_change_overview(
            change=change, work_products=self.work_products.list_for_change(change_id), workflow_state=workflow_state,
            architecture_status=self.architecture_design_service.status(change_id, project_policy=policy),
            design_status=self.architecture_design_service.status(change_id, project_policy=policy),
            test_design_status=self.test_design_lifecycle_service.status(change_id),
            spec_proposals=self.spec_lifecycle_service.list_proposals(change_id),
            human_decisions_pending=self.human_decisions.list_pending_for_change(change_id),
            agents_completed=agents_completed, agents_running=agents_running, spec_drift=spec_drift)

    # ---- E7.5.6 Spec tab --------------------------------------------------
    def spec_tab(self, change_id: int) -> dict:
        from app.services.spec_registry import SpecRegistry, SpecError
        change = self._change(change_id)
        ra_wp = self.spec_lifecycle_service.get_requirement_analysis(change_id)
        requirement_analysis = json.loads(ra_wp["content_metadata"]) if ra_wp else None
        proposals = self.spec_lifecycle_service.list_proposals(change_id)
        for p in proposals:
            p["review"] = json.loads(p["review_result"]) if p.get("review_result") else {}
        current_features = []
        try:
            registry = SpecRegistry(self.specs_root).load()
            for fid in self._governing_feature_ids(change_id):
                f = registry.feature(fid)
                if f:
                    current_features.append(f)
        except SpecError:
            pass
        plans = self.planner_service.list_plans(change_id)
        staleness = self.planner_service.check_staleness(plans[-1]["id"]) if plans else {"stale": False, "reason": None}
        return {
            "change": change, "original_intent": change.get("description") if change else None,
            "requirement_analysis": requirement_analysis, "requirement_analysis_wp": ra_wp,
            "current_features": current_features, "proposals": proposals,
            "latest_proposal": proposals[-1] if proposals else None,
            "human_decisions": self.human_decisions.list_for_change(change_id) if ra_wp or proposals else [],
            "staleness": staleness,
        }

    # ---- E7.5.7 Architecture tab -------------------------------------------
    def architecture_tab(self, change_id: int) -> dict:
        change = self._change(change_id)
        analyses = self._wps(change_id, "ARCHITECTURE_ANALYSIS", current_only=False)
        current = [a for a in analyses if a["status"] != "SUPERSEDED"]
        adrs = self._wps(change_id, "ADR")
        reviews = self._wps(change_id, "ARCHITECTURE_REVIEW", current_only=False)
        status = self.architecture_design_service.status(change_id, project_policy=self._policy(change))
        return {
            "change": change, "current_analysis": current[-1] if current else None,
            "revision_history": analyses, "adrs": adrs, "reviews": reviews,
            "architecture_ready": status.get("architecture_ready"),
        }

    # ---- E7.5.8 Design tab ------------------------------------------------
    def design_tab(self, change_id: int) -> dict:
        change = self._change(change_id)
        policy = self._policy(change)
        status = self.architecture_design_service.status(change_id, project_policy=policy)
        tech_history = self._wps(change_id, "TECHNICAL_DESIGN", current_only=False)
        ui_history = self._wps(change_id, "UI_UX_DESIGN", current_only=False)
        reviews = self._wps(change_id, "DESIGN_REVIEW", current_only=False)
        return {
            "change": change, "technical_design": self._attach_content(status.get("technical_design")),
            "ui_ux_design": self._attach_content(status.get("ui_ux_design")), "ui_ux_applicability": status.get("ui_ux_applicability"),
            "design_ready": status.get("design_ready"),
            "technical_design_history": tech_history, "ui_ux_design_history": ui_history, "reviews": reviews,
        }

    # ---- E7.5.9 Tests tab -------------------------------------------------
    def tests_tab(self, change_id: int) -> dict:
        change = self._change(change_id)
        status = self.test_design_lifecycle_service.status(change_id)
        coverage = status["coverage"]
        cases = self.test_case_specs.list_for_change(change_id)
        rows = []
        for tc in cases:
            m = self.executable_mapping.get(tc["id"])
            rows.append({
                "id": tc["id"], "key": tc["item_key"], "title": tc["title"], "test_level": tc["test_level"],
                "test_type": tc["test_type"], "status": tc["status"],
                "requirement_ids": json.loads(tc["requirement_ids"] or "[]"),
                "acceptance_ids": json.loads(tc["acceptance_ids"] or "[]"),
                "invariant_ids": json.loads(tc["invariant_ids"] or "[]"),
                "automation_candidate": bool(tc["automation_candidate"]),
                "implementation_status": m["implementation_status"] if m else "UNIMPLEMENTED",
                "mapping": m,
            })
        implemented = sum(1 for r in rows if r["implementation_status"] != "UNIMPLEMENTED")
        passed = sum(1 for r in rows if r["implementation_status"] == "PASS")
        failed = sum(1 for r in rows if r["implementation_status"] == "FAIL")
        reviews = self._wps(change_id, "TEST_REVIEW", current_only=False)
        stale = self.test_design_lifecycle_service.staleness(change_id)
        return {
            "change": change, "test_plan": self._attach_content(status["test_plan"]), "test_case_set": self._attach_content(status["test_case_set"]),
            "coverage": coverage, "cases": rows,
            "implemented_count": implemented, "unimplemented_count": len(rows) - implemented,
            "passed_count": passed, "failed_count": failed, "no_evidence_count": implemented - passed - failed,
            "reviews": reviews, "test_design_ready": status["test_design_ready"], "staleness": stale,
        }

    # ---- E7.5.10 Plan tab ---------------------------------------------------
    def plan_tab(self, change_id: int) -> dict:
        change = self._change(change_id)
        plans = self.planner_service.list_plans(change_id)
        current = plans[-1] if plans else None
        items = []
        human_decisions_ = []
        spec_stale = design_stale = test_stale = {"stale": False, "reason": None}
        if current:
            items = self.planner_service.plan_items(current["id"])
            human_decisions_ = self.planner_service.human_decisions(current["id"])
            spec_stale = self.planner_service.check_staleness(current["id"])
            design_stale = self.planner_service.check_design_staleness(current["id"])
            test_stale = self.planner_service.check_test_design_staleness(current["id"])
            current["assumptions_list"] = json.loads(current["assumptions"] or "[]")
        dag = []
        for it in items:
            dag.append({
                "key": it["item_key"], "title": it["title"], "task_type": it["task_type"],
                "preferred_role": it["preferred_role"], "depends_on": json.loads(it["depends_on_keys"] or "[]"),
                "materialized_task_id": it["materialized_task_id"], "optional": bool(it["optional"]),
            })
        return {
            "change": change, "plans": plans, "current_plan": current, "dag": dag,
            "human_decisions": human_decisions_,
            "spec_staleness": spec_stale, "design_staleness": design_stale, "test_design_staleness": test_stale,
        }

    # ---- E7.5.11 Tasks tab (operational) -----------------------------------
    def tasks_tab(self, change_id: int) -> dict:
        change = self._change(change_id)
        tasks = self.changes.list_tasks_for_change(change_id)
        rows = []
        for t in tasks:
            d = self.decision.evaluate(t["id"])
            rows.append({
                "task": t, "status": d["status"], "stage": d["stage"], "next_action": d["next_action"],
                "builders": d["builders"], "spec_feature_id": t.get("spec_feature_id"),
                "outputs": self.work_products.list_for_task(t["id"]),
            })
        return {"change": change, "rows": rows}

    # ---- E7.5.12 Reviews tab (aggregate) -----------------------------------
    def reviews_tab(self, change_id: int) -> dict:
        change = self._change(change_id)
        items = []
        for kind, label in (("SPEC_REVIEW", "Spec Review"), ("ARCHITECTURE_REVIEW", "Architecture Review"),
                             ("DESIGN_REVIEW", "Design Review"), ("TEST_REVIEW", "Test Review")):
            for wp in self._wps(change_id, kind, current_only=False):
                meta = json.loads(wp["content_metadata"] or "{}")
                items.append({"kind": label, "verdict": meta.get("verdict"), "findings": meta.get("findings") or [],
                              "work_product": wp, "created_at": wp["created_at"]})
        # Existing code Review/QA evidence (review_runs/qa_runs), for this
        # Change's own Tasks -- E7.5.12: "prepare the structure so E9
        # Code/Security reviews can appear here without redesign."
        for t in self.changes.list_tasks_for_change(change_id):
            for r in self.evidence.for_task(t["id"])["review_runs"]:
                items.append({"kind": "Code Review", "verdict": r["status"], "findings": r["findings"],
                              "work_product": None, "created_at": r["created_at"], "task": t})
        items.sort(key=lambda x: x["created_at"] or "", reverse=True)
        return {"change": change, "reviews": items}

    # ---- E7.5.13 Decisions tab ----------------------------------------------
    def decisions_tab(self, change_id: int) -> dict:
        change = self._change(change_id)
        all_decisions = self.human_decisions.list_for_change(change_id)
        return {"change": change, "pending": [d for d in all_decisions if not d["resolved"]],
                "resolved": [d for d in all_decisions if d["resolved"]]}

    # ---- E7.5.14 Evidence tab -----------------------------------------------
    def evidence_tab(self, change_id: int) -> dict:
        change = self._change(change_id)
        tasks = self.changes.list_tasks_for_change(change_id)
        by_task = []
        for t in tasks:
            ev = self.evidence.for_task(t["id"])
            if any(ev.values()):
                by_task.append({"task": t, "evidence": ev})
        return {"change": change, "by_task": by_task}

    # ---- E7.5.15 Release / Deploy (honest, reuses the exact join
    # WorkflowService._gate_deploy_verified already uses -- never a new
    # linkage) -------------------------------------------------------------
    def deploy_tab(self, change_id: int) -> dict:
        change = self._change(change_id)
        tasks = self.changes.list_tasks_for_change(change_id)
        repo_ids: set[int] = set()
        merge_records = []
        for t in tasks:
            rows = self.db.all("SELECT * FROM merge_records WHERE task_id=? AND required=1", (t["id"],))
            merge_records.extend(rows)
            repo_ids.update(r["repository_id"] for r in rows)
        deployments = []
        for rid in repo_ids:
            latest = self.db.one(
                "SELECT d.*,r.repo_name FROM deployments d JOIN repositories r ON r.id=d.repository_id "
                "WHERE d.repository_id=? AND d.environment='DEV' AND d.task_id IN (SELECT id FROM tasks WHERE change_id=?) "
                "ORDER BY d.id DESC LIMIT 1", (rid, change_id))
            if latest:
                deployments.append(latest)
        return {"change": change, "linked": bool(deployments), "deployments": deployments,
                "merge_records": merge_records}

    def release_tab(self, change_id: int) -> dict:
        """No ReleaseService/artifact-versioning concept exists yet in
        this codebase (E1 only reserved the RELEASE_MANIFEST WorkProduct
        kind) -- honest 'not linked yet' rather than inventing one
        (E7.5.15's own explicit instruction)."""
        change = self._change(change_id)
        manifests = self._wps(change_id, "RELEASE_MANIFEST", current_only=False)
        return {"change": change, "linked": bool(manifests), "manifests": manifests}
