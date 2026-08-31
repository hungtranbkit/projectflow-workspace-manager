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
        # E10.28/E10.29: wired by main.py AFTER construction (same
        # additive-attribute pattern as workflow_service.review_gate) --
        # IntegrationService/ReleaseService are built later in create_app()
        # since they depend on worktree_manager/review_fix_orchestrator/
        # deployer. None-safe throughout: every method below falls back
        # to the exact honest "not linked yet" behavior when unwired.
        self.integration_service = None
        self.release_service = None
        self.product_acceptance_service = None  # E11: wired by main.py once ProductAcceptanceService exists

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
        ov = build_change_overview(
            change=change, work_products=self.work_products.list_for_change(change_id), workflow_state=workflow_state,
            architecture_status=self.architecture_design_service.status(change_id, project_policy=policy),
            design_status=self.architecture_design_service.status(change_id, project_policy=policy),
            test_design_status=self.test_design_lifecycle_service.status(change_id),
            spec_proposals=self.spec_lifecycle_service.list_proposals(change_id),
            human_decisions_pending=self.human_decisions.list_pending_for_change(change_id),
            agents_completed=agents_completed, agents_running=agents_running, spec_drift=spec_drift)
        ov["release_deploy_summary"] = self.release_deploy_summary(change_id)  # E10.29
        return ov

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

    # ---- E7.5.15 / E10.28 Release / Deploy (honest, reuses the exact
    # join WorkflowService._gate_deploy_verified already uses for the
    # legacy DEV-only rows, plus real Release/Deploy state once
    # release_service/integration_service are wired -- never a new,
    # independently-derived status) ------------------------------------
    def _deployment_with_previous_version(self, deployment_id: int | None) -> dict | None:
        """Attaches `previous_version` -- the artifact_version of the
        deployment this one rolled back TO (deployments.
        rollback_to_deployment_id, set by DeploymentService.rollback())
        -- so the Deploy tab can show it without a template-side query."""
        if not deployment_id:
            return None
        d = self.db.one("SELECT * FROM deployments WHERE id=?", (deployment_id,))
        if not d:
            return None
        d["previous_version"] = None
        if d.get("rollback_to_deployment_id"):
            prev = self.db.one("SELECT artifact_version FROM deployments WHERE id=?", (d["rollback_to_deployment_id"],))
            d["previous_version"] = prev["artifact_version"] if prev else None
        return d

    def _change_release_ids(self, change_id: int, task_ids: list[int]) -> list[int]:
        if not task_ids:
            return []
        placeholders = ",".join("?" * len(task_ids))
        rows = self.db.all(
            f"SELECT DISTINCT release_id FROM release_tasks WHERE task_id IN ({placeholders})", tuple(task_ids))
        return sorted((r["release_id"] for r in rows), reverse=True)

    def deploy_tab(self, change_id: int) -> dict:
        change = self._change(change_id)
        tasks = self.changes.list_tasks_for_change(change_id)
        task_ids = [t["id"] for t in tasks]
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

        # E10.28: the same Releases this Change's Tasks belong to --
        # composition only, reading ReleaseService's own already-computed
        # release/test/production state, never a second one.
        releases = []
        if self.release_service:
            for rid in self._change_release_ids(change_id, task_ids):
                r = self.release_service.get(rid)
                if r:
                    r["test_deployment"] = self._deployment_with_previous_version(r["test_deployment_id"])
                    r["production_deployment"] = self._deployment_with_previous_version(r["production_deployment_id"])
                    releases.append(r)
        return {"change": change, "linked": bool(deployments) or bool(releases), "deployments": deployments,
                "merge_records": merge_records, "releases": releases}

    def release_tab(self, change_id: int) -> dict:
        change = self._change(change_id)
        tasks = self.changes.list_tasks_for_change(change_id)
        task_ids = [t["id"] for t in tasks]

        # Per-Task integration state: readiness (E9's own
        # integration_readiness(), via IntegrationService.
        # preflight_integration -- never re-derived) plus whether it's
        # already been integrated (merge_records.merge_status='MERGED',
        # the exact row IntegrationService.integrate_task() itself
        # writes).
        integrations = []
        integrated_task_ids: list[int] = []
        repository_id = None
        for t in tasks:
            merged = self.db.one(
                "SELECT * FROM merge_records WHERE task_id=? AND required=1 ORDER BY id DESC LIMIT 1", (t["id"],))
            integrated = bool(merged and merged["merge_status"] == "MERGED")
            readiness = None
            if self.integration_service and not integrated:
                try:
                    readiness = self.integration_service.preflight_integration(t["id"])
                except Exception:
                    readiness = None
            integrations.append({
                "task": t, "merge_record": merged, "integrated": integrated, "readiness": readiness,
                "can_integrate": bool(readiness and readiness.get("ready") and not integrated)})
            if integrated:
                integrated_task_ids.append(t["id"])
                repository_id = repository_id or merged["repository_id"]

        releases = []
        if self.release_service:
            for rid in self._change_release_ids(change_id, task_ids):
                r = self.release_service.get(rid)
                if r:
                    releases.append(r)

        manifests = self._wps(change_id, "RELEASE_MANIFEST", current_only=False)
        return {"change": change, "linked": bool(releases) or bool(manifests), "manifests": manifests,
                "integrations": integrations, "releases": releases,
                "can_create_release": bool(self.release_service and integrated_task_ids and repository_id and not releases),
                "repository_id": repository_id, "integrated_task_ids": integrated_task_ids}

    # ---- E11.17: Acceptance tab -- composition only over
    # ProductAcceptanceService's own already-computed truth, no second
    # eligibility/applicability/checklist logic here. -------------------
    def acceptance_tab(self, change_id: int) -> dict:
        change = self._change(change_id)
        if not self.product_acceptance_service:
            return {"change": change, "wired": False, "eligibility": None, "acceptance": None,
                    "checklist": [], "context": None, "history": [], "children": []}
        eligibility = self.product_acceptance_service.eligibility(change_id)
        pa = self.product_acceptance_service.get_current_for_change(change_id)
        return {
            "change": change, "wired": True, "eligibility": eligibility, "acceptance": pa,
            "checklist": self.product_acceptance_service.checklist(pa["id"]) if pa else [],
            "context": self.product_acceptance_service.context(change_id),
            "history": self.product_acceptance_service.list_for_change(change_id),
            "children": self.changes.list_children(change_id),
        }

    # ---- E10.29: compact Integration/Release/TEST/PRODUCTION summary,
    # for the Change Overview page -- every field is read straight off
    # IntegrationService/ReleaseService's own already-computed state
    # (merge_records.merge_status, releases.status), reserving the
    # BLOCKED/failed states for cases genuinely worth strong-red
    # attention. Returns None fields throughout when no evidence exists
    # yet (a Change with no Tasks, or the services unwired) -- never a
    # fabricated "ready" default. ------------------------------------
    _RELEASE_BUCKET = {
        "DRAFT": "building", "BUILDING": "building", "BUILT": "building", "QUALIFYING": "building",
        "READY": "ready", "DEPLOYING_TEST": "ready", "TEST_VERIFIED": "ready",
        "WAITING_PRODUCTION_APPROVAL": "ready", "DEPLOYING_PRODUCTION": "ready", "PRODUCTION_VERIFIED": "ready",
        "FAILED": "failed", "ROLLED_BACK": "failed",
    }
    _TEST_VERIFIED_STATUSES = ("TEST_VERIFIED", "WAITING_PRODUCTION_APPROVAL", "DEPLOYING_PRODUCTION",
                                "PRODUCTION_VERIFIED", "ROLLED_BACK")

    def release_deploy_summary(self, change_id: int) -> dict:
        tasks = self.changes.list_tasks_for_change(change_id)
        task_ids = [t["id"] for t in tasks]
        summary = {"integration": None, "release": None, "test": None, "production": None, "acceptance": None}
        if self.product_acceptance_service:
            # E11.18: PENDING/ACCEPTED/CHANGE_REQUESTED/REJECTED/SUPERSEDED
            # or NOT_APPLICABLE, read straight from ProductAcceptanceService's
            # own already-computed truth -- never re-derived here.
            try:
                summary["acceptance"] = self.product_acceptance_service.overview_status(change_id)
            except Exception:
                summary["acceptance"] = None
        if not tasks:
            return summary

        merged_task_ids = {row["task_id"] for row in self.db.all(
            "SELECT task_id FROM merge_records WHERE required=1 AND merge_status='MERGED' "
            "AND task_id IN (SELECT id FROM tasks WHERE change_id=?)", (change_id,))}
        if merged_task_ids and set(task_ids) <= merged_task_ids:
            summary["integration"] = "INTEGRATED"
        elif self.integration_service:
            any_ready, any_blocked = False, False
            for t in tasks:
                if t["id"] in merged_task_ids:
                    continue
                try:
                    readiness = self.integration_service.preflight_integration(t["id"])
                except Exception:
                    continue
                if readiness.get("ready"):
                    any_ready = True
                elif readiness.get("blockers"):
                    any_blocked = True
            if any_ready:
                summary["integration"] = "READY"
            elif any_blocked:
                summary["integration"] = "BLOCKED"

        if self.release_service:
            release_ids = self._change_release_ids(change_id, task_ids)
            releases = [r for r in (self.release_service.get(rid) for rid in release_ids) if r]
            if releases:
                latest = releases[0]  # _change_release_ids sorts newest-first
                summary["release"] = self._RELEASE_BUCKET.get(latest["status"], "building")
                if latest["test_deployment_id"]:
                    summary["test"] = "verified" if latest["status"] in self._TEST_VERIFIED_STATUSES else "failed"
                if latest["production_deployment_id"]:
                    if latest["status"] == "WAITING_PRODUCTION_APPROVAL":
                        summary["production"] = "waiting_approval"
                    elif latest["status"] == "PRODUCTION_VERIFIED":
                        summary["production"] = "verified"
                    elif latest["status"] == "ROLLED_BACK":
                        summary["production"] = "rolled_back"
                    elif latest["status"] == "FAILED":
                        summary["production"] = "failed"
        return summary
