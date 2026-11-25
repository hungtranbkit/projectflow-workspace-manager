"""Single side-effect-free readiness gate for every coding executor path."""
from __future__ import annotations

from app.services.spec_gate import GATED_CLASSIFICATIONS
from app.services.task_contract import validate_task_contract


class TaskDispatchBlocked(ValueError):
    def __init__(self, readiness: dict):
        self.readiness = readiness
        super().__init__(readiness.get("blockers", [{}])[0].get("message", "Task is not ready for executor dispatch."))


class TaskDispatchGate:
    def __init__(self, spec_gate):
        self.spec_gate = spec_gate

    def evaluate(self, task: dict) -> dict:
        contract = validate_task_contract(task)
        spec = self.spec_gate.evaluate(task)
        version = str(task.get("task_contract_version") or "V1").upper()
        if version == "V1":
            # Historical Tasks retain their original SpecGate semantics
            # and remain readable/operable. V2 is assigned by the current
            # schema default to every new Task and is mandatory for new
            # coding dispatches.
            blockers=[]
            if spec.get("outcome") not in ("PASS","NOT_APPLICABLE"):
                blockers.append({"code":spec.get("outcome","SPEC_REFERENCE_INVALID"),"message":spec.get("reason","SpecGate refused dispatch.")})
            ready=not blockers
            return {"ready":ready,"code":None if ready else blockers[0]["code"],"blockers":blockers,
                    "spec_gate":spec,"contract":{**contract,"ready":True,"legacy_v1":True},
                    "legacy_upgrade_required":True}
        blockers: list[dict] = []

        classification = str(task.get("spec_change_classification") or "").strip().upper()
        feature_id = str(task.get("spec_feature_id") or "").strip()
        if classification not in GATED_CLASSIFICATIONS or not feature_id:
            blockers.append({
                "code": "SPEC_REQUIRED",
                "message": "New coding Tasks require a behavior-change classification and an approved linked FeatureSpec.",
            })
        elif spec.get("outcome") != "PASS":
            blockers.append({"code": spec.get("outcome", "SPEC_REFERENCE_INVALID"), "message": spec.get("reason", "SpecGate refused dispatch.")})

        if not contract["ready"]:
            blockers.append({
                "code": "TASK_CONTRACT_INCOMPLETE",
                "message": "Complete the task-specific seven-section V2 contract before dispatch.",
                "missing_sections": contract["missing_sections"],
                "section_issues": contract["section_issues"],
                "contract_issues": contract["contract_issues"],
            })

        ready = not blockers
        return {
            "ready": ready,
            "code": None if ready else blockers[0]["code"],
            "blockers": blockers,
            "spec_gate": spec,
            "contract": contract,
            "legacy_upgrade_required": False,
        }
