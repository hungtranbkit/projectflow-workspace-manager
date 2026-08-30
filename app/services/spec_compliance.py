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

# Phase E7.18: additive diagnostic classifications ONLY -- never a
# fifth VERDICT and never a way to reach PASS. A designed-but-unexecuted
# TestCaseSpec is never evidence; these four values only explain WHY a
# non-PASS verdict looks the way it does, when Test Design (E7) exists
# for this Task's Change at all.
TEST_CONTRACT_GAPS = ("TEST_DESIGN_MISSING", "TEST_IMPLEMENTATION_MISSING", "TEST_EVIDENCE_MISSING", "TEST_EVIDENCE_FAIL")


class SpecComplianceVerifier:
    def __init__(self, db, decision, specs_root, test_case_specs=None, executable_mapping=None):
        self.db = db
        self.decision = decision
        self.gate = SpecGate(specs_root)
        self.evidence = EvidenceStore(db)
        # Phase E7.18 additive hook -- both None (every pre-E7 caller's
        # own construction, including every E1-E6 test) means
        # test_contract_gap() always returns None and verify()'s result
        # never gains the extra key at all. Wired in app/main.py once
        # TestCaseSpecStore/ExecutableTestMappingService exist.
        self.test_case_specs = test_case_specs
        self.executable_mapping = executable_mapping

    def test_contract_gap(self, task_id: int) -> str | None:
        """E7.18: distinguishes TEST_DESIGN_MISSING / TEST_IMPLEMENTATION_
        MISSING / TEST_EVIDENCE_MISSING / TEST_EVIDENCE_FAIL -- purely
        diagnostic, never influences verify()'s own verdict. Requires
        both E7 services to be wired and the Task to actually be
        spec-linked with real requirement ids; returns None (no opinion)
        otherwise, same 'never PASS/never claim on missing evidence'
        safe direction the rest of this module already uses."""
        if self.test_case_specs is None or self.executable_mapping is None:
            return None
        t = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not t or not t.get("change_id") or not t.get("spec_requirement_ids"):
            return None
        task_req_ids = set(spec_id_list(t.get("spec_requirement_ids")))
        if not task_req_ids:
            return None
        cases = self.test_case_specs.list_for_change(t["change_id"])
        if not cases:
            return "TEST_DESIGN_MISSING"
        import json
        covering = [tc for tc in cases if task_req_ids & set(json.loads(tc["requirement_ids"] or "[]"))]
        if not covering:
            return "TEST_DESIGN_MISSING"
        mappings = [self.executable_mapping.get(tc["id"]) for tc in covering]
        if not any(m and m["implementation_status"] != "UNIMPLEMENTED" for m in mappings):
            return "TEST_IMPLEMENTATION_MISSING"
        if any(m and m["implementation_status"] == "FAIL" for m in mappings):
            return "TEST_EVIDENCE_FAIL"
        if not any(m and m["implementation_status"] == "PASS" for m in mappings):
            return "TEST_EVIDENCE_MISSING"
        return None

    def verify(self, task_id: int) -> dict:
        t = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not t:
            return {
                "task_id": task_id, "feature_id": None, "spec_version": None,
                "requirement_ids": [], "acceptance_ids": [], "invariant_ids": [],
                "scope": {}, "gate_outcome": None, "gate_reason": "Task not found",
                "traceability": {}, "evidence": {}, "verdict": "INCOMPLETE", "reason": "Task not found",
                "test_contract": None,
            }
        gate = self.gate.evaluate(t)
        d = self.decision.evaluate(task_id)
        # E7.18: computed once, attached to every returned shape below --
        # purely diagnostic, never read by any of the verdict branches
        # that follow, so it can never change PASS/FAIL/INCOMPLETE/
        # SPEC_DRIFT.
        test_contract = self.test_contract_gap(task_id)

        result = {
            "task_id": task_id,
            "feature_id": t.get("spec_feature_id"),
            "spec_version": t.get("spec_version"),
            "requirement_ids": spec_id_list(t.get("spec_requirement_ids")),
            "acceptance_ids": spec_id_list(t.get("spec_acceptance_ids")),
            "invariant_ids": spec_id_list(t.get("spec_invariant_ids")),
            "scope": {"classification": t.get("spec_change_classification")},
            "test_contract": test_contract,
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
