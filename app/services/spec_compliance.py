from __future__ import annotations

from app.services.evidence_store import EvidenceStore
from app.services.spec_gate import SpecGate, spec_id_list

"""Spec Layer V1 (S7): SpecComplianceVerifier produces a machine-readable
SpecComplianceResult for one Task, built entirely from evidence
ProjectFlow already collects -- TaskDecisionService.evaluate() (review/
QA gate status, the same authoritative decision the whole Workflow
Summary UX reads) and EvidenceStore (the real verification_reports/
review_runs/qa_runs/test_runs/manual_verifications rows). No semantic
theorem-proving (S7: not required for v1) -- this is a deterministic
function of SpecGate's outcome plus whether the required evidence
actually exists and passes, never a second, independently-derived
readiness calculation."""

VERDICTS = ("PASS", "FAIL", "INCOMPLETE", "SPEC_DRIFT")


class SpecComplianceVerifier:
    def __init__(self, db, decision, specs_root):
        self.db = db
        self.decision = decision
        self.gate = SpecGate(specs_root)
        self.evidence = EvidenceStore(db)

    def verify(self, task_id: int) -> dict:
        t = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not t:
            return {
                "task_id": task_id, "feature_id": None, "spec_version": None,
                "requirement_ids": [], "acceptance_ids": [], "invariant_ids": [],
                "scope": {}, "gate_outcome": None, "gate_reason": "Task not found",
                "traceability": {}, "evidence": {}, "verdict": "INCOMPLETE", "reason": "Task not found",
            }
        gate = self.gate.evaluate(t)
        d = self.decision.evaluate(task_id)

        result = {
            "task_id": task_id,
            "feature_id": t.get("spec_feature_id"),
            "spec_version": t.get("spec_version"),
            "requirement_ids": spec_id_list(t.get("spec_requirement_ids")),
            "acceptance_ids": spec_id_list(t.get("spec_acceptance_ids")),
            "invariant_ids": spec_id_list(t.get("spec_invariant_ids")),
            "scope": {"classification": t.get("spec_change_classification")},
            "gate_outcome": gate["outcome"],
            "gate_reason": gate["reason"],
            "traceability": {},
            "evidence": {},
        }

        # S9 condition 2/3/4/6: a broken/invalid spec reference on a Task
        # that otherwise looks like it's claiming compliance is real
        # SPEC_DRIFT, never silently downgraded to just "incomplete".
        if gate["outcome"] == "SPEC_REFERENCE_INVALID":
            result["verdict"], result["reason"] = "SPEC_DRIFT", gate["reason"]
            return result
        if gate["outcome"] in ("SPEC_REQUIRED", "SPEC_NOT_APPROVED", "TRACEABILITY_MISSING"):
            result["verdict"], result["reason"] = "INCOMPLETE", gate["reason"]
            return result

        # gate PASS or NOT_APPLICABLE from here -- check real evidence.
        builders = d["builders"]
        reviews_pass = bool(builders) and all(b["review_status"] == "PASS" for b in builders)
        qa_required = self.decision.requires_qa(d["risk_profile"])
        qa = d["qa"]
        qa_pass = (not qa_required) or (qa is not None and qa["status"] == "PASS" and self.decision.qa_current(qa, t))
        any_failed = any(b["fix_required"] or b["review_status"] == "BLOCKED" for b in builders) or bool(qa and qa["status"] in ("FAIL", "BLOCKED"))

        result["evidence"] = {
            "builders_ready": bool(builders) and all(b["ready"] for b in builders),
            "reviews_pass": reviews_pass,
            "qa_required": qa_required,
            "qa_pass": qa_pass,
        }

        if any_failed:
            result["verdict"], result["reason"] = "FAIL", "Review or Runtime Verification evidence reports a failure."
            return result
        if not builders or not reviews_pass or not qa_pass:
            result["verdict"], result["reason"] = "INCOMPLETE", "Required verification evidence is not yet complete."
            return result

        # Spec-linked task (gate PASS via real feature linkage, not just
        # NO_BEHAVIOR_CHANGE/NOT_APPLICABLE): never PASS on a declared-
        # but-never-produced mapping (S7: "Never emit PASS when required
        # evidence is missing") -- require at least one real evidence
        # row actually stamped with this exact feature_id.
        if gate["outcome"] == "PASS" and t.get("spec_feature_id"):
            reports = self.evidence.for_task(task_id)["verification_reports"]
            traced = [r for r in reports if r.get("spec_feature_id") == t["spec_feature_id"]]
            result["traceability"] = {"linked_evidence_count": len(traced)}
            if not traced:
                result["verdict"], result["reason"] = "INCOMPLETE", "No evidence has been recorded against this Task's spec linkage yet."
                return result

        result["verdict"], result["reason"] = "PASS", "All required evidence present and passing."
        return result
