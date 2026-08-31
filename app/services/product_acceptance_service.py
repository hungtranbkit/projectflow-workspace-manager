from __future__ import annotations
import json

"""Phase E11: Human Product Acceptance & Production Outcome Review.

Product principle (E11's own spec, restated because it drives every
design choice below): this is NOT code review, architecture approval,
test execution, release approval, or deployment health verification --
those already belong to ProjectFlow/agents (E1-E10). Human Product
Acceptance answers exactly one question: "Does the delivered product
behave and feel like what I asked for?" -- so ProductAcceptance binds
to the EXACT deployed artifact/version a human is looking at, never
merely to a Change.

Reuse discipline (E11.0's own instruction: "do not create a second
generic approval system if current infrastructure can be reused"):
  - eligibility reuses ReleaseService's own PRODUCTION_VERIFIED status
    (which already encodes runtime-verify PASS + observed-version-
    matches-release + artifact-digest-matches -- E10's own real
    verification, never re-derived here).
  - applicability reuses E6's UiUxApplicabilityService.detect() (the
    exact deterministic, no-LLM user-facing detector) plus the same
    actor-hint vocabulary it already defines.
  - the checklist is derived from real FeatureSpec AcceptanceCriteria
    (via ArchitectureContextBuilder's own governing_specs, the same
    trace-linked spec context E6/E7 already resolve) and real
    MANUAL_ACCEPTANCE TestCaseSpecs (E7) -- never LLM-invented items
    with no underlying reference.
  - manual checklist evidence reuses the EXISTING test_runs table (its
    workspace_type column is already a free, per-query-filtered string
    -- see evidence_store.py's own workspace_type='agent' filter; a new
    workspace_type='product_acceptance' value cannot collide with any
    existing query) instead of manual_verifications/test_runs' own
    sandbox_id/workspace_id-scoped dev-verification shape, which does
    not fit a live-production review (there is no sandbox for
    production).
  - HumanDecisionService (WHAT-level ambiguity) is a DISTINCT concept,
    never conflated with product acceptance (E11.19's own instruction).
  - follow-up Changes go through ChangeService.create() (E1), never a
    second "feedback task" mechanism (E11.22).

WorkflowService.human_acceptance_gate is wired the same additive-hook
pattern review_gate/deploy_verified_gate already established (E9/E10):
None result/unwired object -> exact legacy fallback, zero behavior
change for anything that does not use ProductAcceptance."""

from app.services.architecture_design_service import UiUxApplicabilityService

ACCEPTANCE_STATUSES = ("PENDING", "ACCEPTED", "CHANGE_REQUESTED", "REJECTED", "SUPERSEDED")
CHECKLIST_STATUSES = ("UNCHECKED", "PASS", "FAIL", "NOT_APPLICABLE")
APPLICABILITY_KINDS = ("USER_FACING", "BACKEND_ONLY", "OPERATIONAL_ONLY", "MIXED")
FOLLOW_UP_CLASSIFICATIONS = ("PRODUCT_ADJUSTMENT", "BUG", "SPEC_CHANGE", "UX_CHANGE")
_OPEN_STATUSES = ("PENDING", "ACCEPTED")


class ProductAcceptanceError(ValueError):
    pass


class ProductAcceptanceService:
    def __init__(self, db, changes, work_products, release_service, architecture_design_service,
                 test_case_specs, human_decisions, workflow_service, project_policy_resolver=None):
        self.db = db
        self.changes = changes
        self.work_products = work_products
        self.release_service = release_service
        self.architecture_design_service = architecture_design_service
        self.test_case_specs = test_case_specs
        self.human_decisions = human_decisions
        self.workflow_service = workflow_service
        self.project_policy_resolver = project_policy_resolver

    # ---- shared helpers -------------------------------------------------
    def _policy(self, change_id: int) -> dict | None:
        if not self.project_policy_resolver:
            return None
        change = self.changes.get(change_id)
        return self.project_policy_resolver(change) if change else None

    def _policy_override(self, change_id: int) -> bool | None:
        policy = self._policy(change_id) or {}
        return (policy.get("human_acceptance") or {}).get("required")

    def _profile_key(self, change_id: int) -> str | None:
        run = self.workflow_service.get_workflow(change_id) if self.workflow_service else None
        return run["profile_key"] if run else None

    def _current_release(self, change_id: int) -> dict | None:
        """The latest Release (by id) that includes any Task of this
        Change -- same 'newest release among this Change's Tasks' shape
        change_control_surface._change_release_ids() already uses."""
        if not self.release_service:
            return None
        task_ids = [t["id"] for t in self.changes.list_tasks_for_change(change_id)]
        if not task_ids:
            return None
        placeholders = ",".join("?" * len(task_ids))
        rows = self.db.all(f"SELECT DISTINCT release_id FROM release_tasks WHERE task_id IN ({placeholders})", tuple(task_ids))
        for rid in sorted((r["release_id"] for r in rows), reverse=True):
            r = self.release_service.get(rid)
            if r:
                return r
        return None

    # ---- E11.4: applicability (reuses E6's UiUxApplicabilityService) ----
    def classify_applicability(self, change_id: int) -> str:
        policy = self._policy(change_id)
        ui_result = self.architecture_design_service.detect_ui_ux(change_id, project_policy=policy) \
            if self.architecture_design_service else {"applicable": False}
        ra = None
        if self.architecture_design_service and self.architecture_design_service.requirement_analysis_lookup:
            ra = self.architecture_design_service.requirement_analysis_lookup(change_id)
        actors = [str(a).lower() for a in ((ra or {}).get("actors") or [])]
        has_user_actor = any(
            any(h in a for h in UiUxApplicabilityService._USER_ACTOR_HINTS)
            and not any(h in a for h in UiUxApplicabilityService._SYSTEM_ACTOR_HINTS)
            for a in actors)
        has_system_actor = any(any(h in a for h in UiUxApplicabilityService._SYSTEM_ACTOR_HINTS) for a in actors)
        if ui_result["applicable"] and has_system_actor:
            return "MIXED"
        if ui_result["applicable"]:
            return "USER_FACING"
        if has_system_actor:
            return "OPERATIONAL_ONLY"
        return "BACKEND_ONLY"

    def backend_only_excused(self, change_id: int) -> bool:
        """E11.4: 'For BACKEND_ONLY: human acceptance MAY BE
        NOT_APPLICABLE IF POLICY ALLOWS' -- read literally, the default
        is NOT excused (a project must explicitly opt in via
        engineering.human_acceptance.backend_only_not_applicable). This
        only matters for CONTROLLED, whose own stage requirement stays
        flatly REQUIRED (never conditional) precisely so it stays the
        strict profile -- AGENTIC_STANDARD's 'don't ask about a DB
        index change' behavior is already satisfied by its own
        REQUIRED_IF/HUMAN_ACCEPTANCE_APPLICABLE condition (_required()
        below), with no carve-out needed there at all."""
        policy = self._policy(change_id) or {}
        return bool((policy.get("human_acceptance") or {}).get("backend_only_not_applicable", False))

    def _required(self, change_id: int, applicability: str | None = None) -> bool:
        """E11.3: VIBE optional (off by default, policy opt-in only);
        AGENTIC_STANDARD recommended/default only when user-facing
        behavior changed; CONTROLLED required outright (its own
        BACKEND_ONLY carve-out is a GATE-level pass, not a requirement
        change -- see gate_status(), which keeps
        test_controlled_requires_stronger_gates' literal 'REQUIRED'
        stage requirement string exactly as E10/E9-era tests expect)."""
        override = self._policy_override(change_id)
        if override is not None:
            return bool(override)
        profile_key = self._profile_key(change_id)
        if profile_key == "VIBE":
            return False
        if profile_key == "CONTROLLED":
            return True
        applicability = applicability or self.classify_applicability(change_id)
        return applicability in ("USER_FACING", "MIXED")

    def applicable_for_condition(self, change_id: int) -> bool:
        """WorkflowStateEvaluator._condition_met's HUMAN_ACCEPTANCE_APPLICABLE
        key, used by VIBE/AGENTIC_STANDARD's REQUIRED_IF stage row."""
        return self._required(change_id)

    # ---- E11.2: eligibility ----------------------------------------------
    def eligibility(self, change_id: int) -> dict:
        release = self._current_release(change_id)
        if not release:
            return {"eligible": False, "reason": "NO_PRODUCTION_RELEASE", "release": None, "applicability": None}
        if release["status"] != "PRODUCTION_VERIFIED" or not release.get("production_deployment_id"):
            # PRODUCTION_VERIFIED already IS "runtime verification PASS +
            # observed version matches release + artifact digest matches
            # + no critical runtime failure" (ReleaseService.
            # sync_production_result's own real evidence) -- never
            # re-derived here.
            return {"eligible": False, "reason": "PRODUCTION_NOT_VERIFIED", "release": release, "applicability": None}
        if self.human_decisions.list_pending_for_change(change_id):
            return {"eligible": False, "reason": "UNRESOLVED_HUMAN_DECISION", "release": release, "applicability": None}
        applicability = self.classify_applicability(change_id)
        if applicability == "BACKEND_ONLY" and self.backend_only_excused(change_id):
            return {"eligible": False, "reason": "NOT_APPLICABLE", "release": release, "applicability": applicability}
        if not self._required(change_id, applicability):
            return {"eligible": False, "reason": "NOT_REQUIRED_BY_POLICY", "release": release, "applicability": applicability}
        existing = self.get_current_for_change(change_id)
        if existing and existing["status"] in _OPEN_STATUSES and not self._is_stale(existing):
            reason = "ALREADY_ACCEPTED" if existing["status"] == "ACCEPTED" else "ALREADY_REQUESTED"
            return {"eligible": False, "reason": reason, "release": release, "applicability": applicability,
                    "existing_id": existing["id"]}
        return {"eligible": True, "reason": None, "release": release, "applicability": applicability}

    # ---- E11.6: checklist derivation (real trace only) --------------------
    def _build_checklist(self, change_id: int) -> list[dict]:
        items: list[dict] = []
        seen_keys: set[str] = set()

        def add(key, title, expected, source_type, source_ref, test_case_spec_id=None):
            key = key[:120]
            if key in seen_keys:
                return
            seen_keys.add(key)
            items.append({"item_key": key, "title": title[:500], "expected_behavior": (expected or "")[:2000],
                           "source_type": source_type, "source_ref": source_ref, "test_case_spec_id": test_case_spec_id})

        # Real FeatureSpec AcceptanceCriteria for this Change's governing
        # feature(s) -- the exact trace-linked spec context E6's own
        # ArchitectureContextBuilder already resolves.
        if self.architecture_design_service:
            policy = self._policy(change_id)
            context = self.architecture_design_service.context_builder.build(change_id, project_policy=policy)
            for spec in context.get("governing_specs") or []:
                for ac in spec.get("acceptance_criteria") or []:
                    add(f"ac:{ac.get('id')}", ac.get("text", "")[:120] or ac.get("id", "acceptance criterion"),
                        ac.get("text", ""), "ACCEPTANCE_CRITERION", ac.get("id"))

            # UI/UX Design flows, when this Change has an approved one --
            # E11.6's own "UI/UX Design flows/states".
            ui = self.architecture_design_service.current_ui_ux_design(change_id) \
                if hasattr(self.architecture_design_service, "current_ui_ux_design") else None
            if ui and ui.get("status") == "APPROVED":
                content = json.loads(ui.get("content_metadata") or "{}")
                for i, flow in enumerate(content.get("user_flows") or []):
                    add(f"flow:{i}", str(flow)[:120], str(flow), "UI_UX_FLOW", None)

        # Real MANUAL_ACCEPTANCE TestCaseSpecs (E7) -- never fake
        # automation, never a second manual-test evidence system: this
        # IS that evidence, linked by test_case_spec_id so a checked
        # item writes into the existing test_runs table (see
        # _record_manual_evidence below).
        if self.test_case_specs:
            for tc in self.test_case_specs.list_for_change(change_id):
                if tc["test_type"] == "MANUAL_ACCEPTANCE" or tc["test_level"] == "MANUAL_ACCEPTANCE":
                    add(f"tcs:{tc['id']}", tc["title"], tc.get("expected_results") or tc.get("purpose") or "",
                        "MANUAL_TEST_CASE", tc.get("item_key"), test_case_spec_id=tc["id"])
        return items

    # ---- E11.1/E11.5: request -----------------------------------------
    def request(self, change_id: int, requested_by: str = "system", force: bool = False) -> dict:
        change = self.changes.get(change_id)
        if not change:
            raise ProductAcceptanceError("Change not found")
        elig = self.eligibility(change_id)
        if not elig["eligible"] and not force:
            raise ProductAcceptanceError(f"Not eligible for a Product Acceptance request: {elig['reason']}")
        release = elig["release"] or self._current_release(change_id)
        if not release:
            raise ProductAcceptanceError("No production Release evidence to bind an acceptance request to")

        # Any still-open acceptance for this Change is now superseded --
        # a request always binds to the CURRENT artifact (E11.11).
        for prior in self.list_for_change(change_id):
            if prior["status"] in _OPEN_STATUSES:
                self.db.execute("UPDATE product_acceptances SET status='SUPERSEDED',updated_at=CURRENT_TIMESTAMP WHERE id=?", (prior["id"],))

        ui = None
        if self.architecture_design_service and hasattr(self.architecture_design_service, "current_ui_ux_design"):
            ui = self.architecture_design_service.current_ui_ux_design(change_id)
        spec_baseline = None
        try:
            from app.services.spec_registry import SpecRegistry
            spec_baseline = SpecRegistry(self.architecture_design_service.context_builder.specs_root).load().baseline_digest()
        except Exception:
            spec_baseline = None

        applicability = elig.get("applicability") or self.classify_applicability(change_id)
        pa_id = self.db.execute(
            "INSERT INTO product_acceptances(change_id,release_id,deployment_id,artifact_digest,observed_version,"
            "spec_baseline_digest,ui_ux_design_work_product_id,applicability,status,requested_by) "
            "VALUES(?,?,?,?,?,?,?,?,'PENDING',?)",
            (change_id, release["id"], release.get("production_deployment_id"), release.get("artifact_digest"),
             release.get("artifact_version"), spec_baseline, ui["id"] if ui else None, applicability, requested_by))

        for item in self._build_checklist(change_id):
            self.db.execute(
                "INSERT INTO product_acceptance_checklist_items(product_acceptance_id,item_key,title,expected_behavior,"
                "source_type,source_ref,test_case_spec_id) VALUES(?,?,?,?,?,?,?)",
                (pa_id, item["item_key"], item["title"], item["expected_behavior"], item["source_type"],
                 item["source_ref"], item["test_case_spec_id"]))

        wp_id = self.work_products.create(
            kind="PRODUCT_ACCEPTANCE", title=f"Product Acceptance request for {change['title']}",
            change_id=change_id, status="DRAFT",
            content_metadata={"product_acceptance_id": pa_id, "release_id": release["id"],
                               "deployment_id": release.get("production_deployment_id"),
                               "artifact_digest": release.get("artifact_digest"),
                               "observed_version": release.get("artifact_version"),
                               "applicability": applicability, "checklist_count": len(self._build_checklist(change_id)),
                               "verdict": None})
        self.db.execute("UPDATE product_acceptances SET work_product_id=? WHERE id=?", (wp_id, pa_id))
        self.db.event("change", change_id, "PRODUCT_ACCEPTANCE_REQUESTED", f"product_acceptance={pa_id} release={release['id']}")
        return self.get(pa_id)

    # ---- reads --------------------------------------------------------
    def get(self, pa_id: int) -> dict | None:
        return self.db.one("SELECT * FROM product_acceptances WHERE id=?", (pa_id,))

    def list_for_change(self, change_id: int) -> list[dict]:
        return self.db.all("SELECT * FROM product_acceptances WHERE change_id=? ORDER BY id DESC", (change_id,))

    def get_current_for_change(self, change_id: int) -> dict | None:
        rows = self.list_for_change(change_id)
        return rows[0] if rows else None

    def checklist(self, pa_id: int) -> list[dict]:
        return self.db.all("SELECT * FROM product_acceptance_checklist_items WHERE product_acceptance_id=? ORDER BY id", (pa_id,))

    def _is_stale(self, pa: dict) -> bool:
        """E11.11: a new production deployment always invalidates a prior
        acceptance -- artifact digest A can never authorize artifact
        digest B."""
        current = self._current_release(pa["change_id"])
        if not current:
            return True
        return (current["id"] != pa["release_id"]
                or current.get("production_deployment_id") != pa["deployment_id"]
                or current.get("artifact_digest") != pa["artifact_digest"])

    def _mark_stale_if_needed(self, pa: dict) -> dict:
        if pa["status"] in _OPEN_STATUSES and self._is_stale(pa):
            self.db.execute("UPDATE product_acceptances SET status='SUPERSEDED',updated_at=CURRENT_TIMESTAMP WHERE id=?", (pa["id"],))
            self.db.event("change", pa["change_id"], "PRODUCT_ACCEPTANCE_STALE", f"product_acceptance={pa['id']}")
            return self.get(pa["id"])
        return pa

    # ---- E11.12: manual checklist evidence (reuses test_runs) -------------
    def _record_manual_evidence(self, pa: dict, item: dict, status: str) -> None:
        if not item.get("test_case_spec_id"):
            return
        tr_status = {"PASS": "PASS", "FAIL": "FAIL"}.get(status)
        if not tr_status:
            return
        self.db.execute(
            "INSERT INTO test_runs(workspace_type,workspace_id,command,stage,status,tested_commit,started_at,finished_at) "
            "VALUES('product_acceptance',?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            (pa["id"], f"manual acceptance check: {item['title'][:200]}", "ACCEPTANCE", tr_status, pa.get("artifact_digest") or ""))
        self.db.execute("UPDATE test_runs SET test_case_spec_id=? WHERE id=last_insert_rowid()", (item["test_case_spec_id"],))

    def check_item(self, pa_id: int, item_id: int, status: str, note: str = "", checked_by: str = "") -> dict:
        status = (status or "").strip().upper()
        if status not in CHECKLIST_STATUSES:
            raise ProductAcceptanceError(f"Unknown checklist status: {status} (must be one of {CHECKLIST_STATUSES})")
        pa = self.get(pa_id)
        if not pa:
            raise ProductAcceptanceError("ProductAcceptance not found")
        item = self.db.one("SELECT * FROM product_acceptance_checklist_items WHERE id=? AND product_acceptance_id=?", (item_id, pa_id))
        if not item:
            raise ProductAcceptanceError("Checklist item not found on this ProductAcceptance")
        if pa["status"] not in _OPEN_STATUSES:
            raise ProductAcceptanceError(f"ProductAcceptance is {pa['status']} -- cannot change its checklist")
        self.db.execute(
            "UPDATE product_acceptance_checklist_items SET status=?,note=?,checked_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, (note or "").strip(), item_id))
        self._record_manual_evidence(pa, item, status)
        return self.db.one("SELECT * FROM product_acceptance_checklist_items WHERE id=?", (item_id,))

    def _checklist_satisfied(self, pa_id: int) -> tuple[bool, str | None]:
        policy = self._policy(self.get(pa_id)["change_id"]) or {}
        require_all = bool((policy.get("human_acceptance") or {}).get("require_all_checklist_items", True))
        items = self.checklist(pa_id)
        applicable_items = [i for i in items if i["status"] != "NOT_APPLICABLE"]
        if any(i["status"] == "FAIL" for i in items):
            return False, "A checklist item is marked FAIL"
        if require_all and any(i["status"] == "UNCHECKED" for i in applicable_items):
            return False, "Not every checklist item has been checked"
        return True, None

    # ---- E11.23: evidence WorkProduct on final decision --------------------
    def _record_decision_evidence(self, pa: dict, verdict: str, wp_status: str, note: str, follow_up_change_id: int | None) -> int:
        change = self.changes.get(pa["change_id"])
        wp_id = self.work_products.create(
            kind="PRODUCT_ACCEPTANCE", title=f"Product Acceptance decision for {change['title'] if change else pa['change_id']}",
            change_id=pa["change_id"], status=wp_status, supersedes_id=pa.get("work_product_id"),
            content_metadata={"product_acceptance_id": pa["id"], "release_id": pa["release_id"],
                               "deployment_id": pa["deployment_id"], "artifact_digest": pa["artifact_digest"],
                               "observed_version": pa["observed_version"],
                               "checklist": self.checklist(pa["id"]), "note": note, "verdict": verdict,
                               "follow_up_change_id": follow_up_change_id})
        self.db.execute("UPDATE product_acceptances SET work_product_id=? WHERE id=?", (wp_id, pa["id"]))
        return wp_id

    # ---- E11.8: accept --------------------------------------------------
    def accept(self, pa_id: int, actor: str, note: str = "") -> dict:
        pa = self.get(pa_id)
        if not pa:
            raise ProductAcceptanceError("ProductAcceptance not found")
        pa = self._mark_stale_if_needed(pa)
        if pa["status"] != "PENDING":
            raise ProductAcceptanceError(f"ProductAcceptance is {pa['status']}, not PENDING -- cannot accept")
        ok, reason = self._checklist_satisfied(pa_id)
        if not ok:
            raise ProductAcceptanceError(f"Checklist not satisfied: {reason}")
        self.db.execute(
            "UPDATE product_acceptances SET status='ACCEPTED',decided_at=CURRENT_TIMESTAMP,decided_by=?,note=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            ((actor or "").strip(), (note or "").strip(), pa_id))
        pa = self.get(pa_id)
        self._record_decision_evidence(pa, "ACCEPTED", "APPROVED", note, None)
        self.db.event("change", pa["change_id"], "PRODUCT_ACCEPTED", f"product_acceptance={pa_id} by={actor}")
        return self.get(pa_id)

    # ---- E11.9: request change -- NEW Change, never mutated history -------
    def request_change(self, pa_id: int, actor: str, feedback: str, classification: str = "PRODUCT_ADJUSTMENT") -> dict:
        feedback = (feedback or "").strip()
        if not feedback:
            raise ProductAcceptanceError("Feedback is required to request a change")
        classification = (classification or "PRODUCT_ADJUSTMENT").strip().upper()
        if classification not in FOLLOW_UP_CLASSIFICATIONS:
            raise ProductAcceptanceError(f"Unknown classification: {classification} (must be one of {FOLLOW_UP_CLASSIFICATIONS})")
        pa = self.get(pa_id)
        if not pa:
            raise ProductAcceptanceError("ProductAcceptance not found")
        pa = self._mark_stale_if_needed(pa)
        if pa["status"] != "PENDING":
            raise ProductAcceptanceError(f"ProductAcceptance is {pa['status']}, not PENDING -- cannot request a change")
        change = self.changes.get(pa["change_id"])
        self.db.execute(
            "UPDATE product_acceptances SET status='CHANGE_REQUESTED',decided_at=CURRENT_TIMESTAMP,decided_by=?,note=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            ((actor or "").strip(), feedback, pa_id))
        follow_up_title = f"Product feedback on: {change['title']}" if change else "Product feedback"
        follow_up_id = self.changes.create(
            title=follow_up_title, description=feedback, change_type=classification,
            risk_level=(change or {}).get("risk_level", "NORMAL"), project_id=(change or {}).get("project_id"),
            parent_change_id=pa["change_id"])
        self.db.execute("UPDATE product_acceptances SET follow_up_change_id=? WHERE id=?", (follow_up_id, pa_id))
        try:
            self.changes.set_lifecycle_state(pa["change_id"], "DELIVERED_BUT_CHANGE_REQUESTED")
        except Exception:
            pass  # honest best-effort: lifecycle_state is a separate, human-driven field (E1) -- never block the real outcome on it
        pa = self.get(pa_id)
        self._record_decision_evidence(pa, "CHANGE_REQUESTED", "REJECTED", feedback, follow_up_id)
        self.db.event("change", pa["change_id"], "PRODUCT_CHANGE_REQUESTED", f"product_acceptance={pa_id} follow_up_change={follow_up_id}")
        return {"acceptance": self.get(pa_id), "follow_up_change_id": follow_up_id}

    # ---- E11.10: reject ---------------------------------------------------
    def reject(self, pa_id: int, actor: str, reason: str, classification: str | None = None) -> dict:
        reason = (reason or "").strip()
        if not reason:
            raise ProductAcceptanceError("A reason is required to reject a Product Acceptance")
        pa = self.get(pa_id)
        if not pa:
            raise ProductAcceptanceError("ProductAcceptance not found")
        pa = self._mark_stale_if_needed(pa)
        if pa["status"] != "PENDING":
            raise ProductAcceptanceError(f"ProductAcceptance is {pa['status']}, not PENDING -- cannot reject")
        self.db.execute(
            "UPDATE product_acceptances SET status='REJECTED',decided_at=CURRENT_TIMESTAMP,decided_by=?,note=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            ((actor or "").strip(), reason, pa_id))
        follow_up_id = None
        if classification:
            classification = classification.strip().upper()
            if classification not in FOLLOW_UP_CLASSIFICATIONS:
                raise ProductAcceptanceError(f"Unknown classification: {classification} (must be one of {FOLLOW_UP_CLASSIFICATIONS})")
            change = self.changes.get(pa["change_id"])
            follow_up_id = self.changes.create(
                title=f"Rejected product outcome: {change['title']}" if change else "Rejected product outcome",
                description=reason, change_type=classification, risk_level=(change or {}).get("risk_level", "NORMAL"),
                project_id=(change or {}).get("project_id"), parent_change_id=pa["change_id"])
            self.db.execute("UPDATE product_acceptances SET follow_up_change_id=? WHERE id=?", (follow_up_id, pa_id))
        else:
            # No classification given -- this is a WHAT-level call a
            # product owner must make (what happens next), which is
            # exactly HumanDecisionService's own domain (E4.12's own
            # precedent), never conflated with ProductAcceptance itself.
            self.human_decisions.create(
                "change", pa["change_id"], f"Product rejected: {reason} -- what should happen next?",
                "A human rejected the delivered product outcome; no follow-up classification was given.", "NONE")
        try:
            self.changes.set_lifecycle_state(pa["change_id"], "DELIVERED_BUT_CHANGE_REQUESTED")
        except Exception:
            pass
        pa = self.get(pa_id)
        self._record_decision_evidence(pa, "REJECTED", "REJECTED", reason, follow_up_id)
        # E11.10: PRODUCT REJECTION is distinct from RUNTIME FAILURE --
        # this never touches deployments/releases; a runtime rollback
        # only ever happens through DeploymentService/ReleaseService's
        # own real health/verification evidence, never because a human
        # disliked the UX.
        self.db.event("change", pa["change_id"], "PRODUCT_REJECTED", f"product_acceptance={pa_id} by={actor}")
        return self.get(pa_id)

    # ---- E11.13: WorkflowService.human_acceptance_gate hook ---------------
    def gate_status(self, change_id: int) -> bool:
        applicability = self.classify_applicability(change_id)
        if applicability == "BACKEND_ONLY" and self.backend_only_excused(change_id):
            return True
        if not self._required(change_id, applicability):
            return True
        pa = self.get_current_for_change(change_id)
        if not pa:
            return False
        pa = self._mark_stale_if_needed(pa)
        return pa["status"] == "ACCEPTED" and not self._is_stale(pa)

    # ---- E11.14/E11.18: compact overview status ----------------------------
    def overview_status(self, change_id: int) -> str:
        applicability = self.classify_applicability(change_id)
        if applicability == "BACKEND_ONLY" and self.backend_only_excused(change_id):
            return "NOT_APPLICABLE"
        if not self._required(change_id, applicability):
            return "NOT_APPLICABLE"
        pa = self.get_current_for_change(change_id)
        if not pa:
            return "PENDING" if self.eligibility(change_id)["eligible"] else "NOT_APPLICABLE"
        pa = self._mark_stale_if_needed(pa)
        if pa["status"] == "SUPERSEDED":
            return "PENDING" if self.eligibility(change_id)["eligible"] else "NOT_APPLICABLE"
        return pa["status"]

    # ---- E11.5: ProductAcceptanceContext (the human-facing package) -------
    def context(self, change_id: int) -> dict:
        change = self.changes.get(change_id)
        pa = self.get_current_for_change(change_id)
        release = self._current_release(change_id)
        deployment = self.db.one("SELECT * FROM deployments WHERE id=?", (release["production_deployment_id"],)) \
            if release and release.get("production_deployment_id") else None
        ui_flows = []
        if self.architecture_design_service and hasattr(self.architecture_design_service, "current_ui_ux_design"):
            ui = self.architecture_design_service.current_ui_ux_design(change_id)
            if ui and ui.get("status") == "APPROVED":
                ui_flows = json.loads(ui.get("content_metadata") or "{}").get("user_flows") or []

        confidence = {"production_healthy": None, "tests_passed": None, "review_passed": None, "security_passed": None}
        state = self.workflow_service.evaluate_workflow(change_id) if self.workflow_service else None
        if state:
            for s in state.get("stages") or []:
                gates = s.get("gates") or {}
                if s["stage"] == "DEPLOY" and "DEPLOY_VERIFIED" in gates:
                    confidence["production_healthy"] = gates["DEPLOY_VERIFIED"]
                if s["stage"] == "VERIFY" and "TESTS_PASS" in gates:
                    confidence["tests_passed"] = gates["TESTS_PASS"]
                if s["stage"] == "REVIEW":
                    if "REVIEW_PASS" in gates:
                        confidence["review_passed"] = gates["REVIEW_PASS"]
                    if "SECURITY_PASS" in gates:
                        confidence["security_passed"] = gates["SECURITY_PASS"]

        return {
            "change": change,
            "what_you_asked_for": (change or {}).get("description") or "",
            "what_changed": ui_flows,
            "checklist": self.checklist(pa["id"]) if pa else [],
            "live_url": deployment.get("deployed_url") if deployment else None,
            "runtime_target": {"environment": deployment.get("environment"), "repository_id": deployment.get("repository_id")} if deployment else None,
            "release": release,
            "deployment": deployment,
            "technical_confidence": confidence,
            "acceptance": pa,
            "applicability": self.classify_applicability(change_id),
        }

    def evidence(self, pa_id: int) -> dict:
        pa = self.get(pa_id)
        if not pa:
            raise ProductAcceptanceError("ProductAcceptance not found")
        wp = self.work_products.get(pa["work_product_id"]) if pa.get("work_product_id") else None
        return {"acceptance": pa, "checklist": self.checklist(pa_id),
                "work_product": {**wp, "content": json.loads(wp["content_metadata"] or "{}")} if wp else None}
