from __future__ import annotations
import json
from pathlib import Path

from app.services.spec_registry import SpecError, SpecRegistry

"""Spec Layer V1 (S5): SpecGate is the one place that decides whether a
Task's spec linkage is sufficient for the Supervisor (app/main.py's
_start_builder_session -- the one place an Agent session actually
starts) to proceed. Deterministic, six outcomes, never a guess (REQ-002).

FEAT-SPEC-LAYER REQ-005 / AC-005 (backward compatibility): a Task with
no spec_change_classification at all -- every Task created before this
feature existed, and every Task created through a route that doesn't
ask -- returns NOT_APPLICABLE, never SPEC_REQUIRED. Only a Task
EXPLICITLY classified as changing behavior is ever gated."""

GATED_CLASSIFICATIONS = ("BEHAVIOR_CHANGE", "NEW_FEATURE", "SPEC_CHANGE", "BUG_FIX_TO_EXISTING_SPEC")
ALL_CLASSIFICATIONS = ("NO_BEHAVIOR_CHANGE", *GATED_CLASSIFICATIONS, "AMBIGUOUS")
OUTCOMES = ("PASS", "NOT_APPLICABLE", "SPEC_REQUIRED", "SPEC_NOT_APPROVED", "TRACEABILITY_MISSING", "SPEC_REFERENCE_INVALID")


def spec_id_list(value) -> list[str]:
    """Task.spec_requirement_ids/spec_acceptance_ids/spec_invariant_ids
    are stored as a JSON-encoded list (matching this codebase's existing
    convention for list-shaped columns, e.g. tasks.tags) -- also accepts
    an already-decoded list directly, so callers building a task dict by
    hand (tests, in-process checks) never have to pre-encode it."""
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in decoded] if isinstance(decoded, list) else []


def _result(outcome: str, reason: str) -> dict:
    return {"outcome": outcome, "reason": reason}


class SpecGate:
    def __init__(self, specs_root: Path | str):
        self.specs_root = specs_root

    def evaluate(self, task: dict) -> dict:
        """task: any mapping with the tasks.spec_* columns (a real DB
        row, or a plain dict in tests). Never raises for an ordinary
        classification/linkage problem -- those are all real, expected
        outcomes; only a genuinely broken spec TREE (missing SPEC.yaml,
        duplicate ids) surfaces as SPEC_REFERENCE_INVALID here rather
        than propagating SpecError, so a malformed spec never crashes
        the Supervisor -- it just refuses to let a spec-gated Task
        start, which is the safe direction to fail in."""
        cls = (task.get("spec_change_classification") or "").strip() or None
        if cls is None:
            return _result("NOT_APPLICABLE", "No spec change classification set -- legacy/unclassified Task, spec linkage not required.")
        if cls == "NO_BEHAVIOR_CHANGE":
            return _result("PASS", "Classified NO_BEHAVIOR_CHANGE -- no externally observable behavior change.")
        if cls == "AMBIGUOUS":
            # Never silently treated as safe (S4/S9): AMBIGUOUS is a real
            # blocker, not an escape hatch.
            return _result("SPEC_REQUIRED", "Change classification is AMBIGUOUS -- resolve to a concrete classification before an Agent may be started.")
        if cls not in GATED_CLASSIFICATIONS:
            return _result("SPEC_REQUIRED", f"Unknown change classification '{cls}' -- must be one of {ALL_CLASSIFICATIONS}.")

        feature_id = (task.get("spec_feature_id") or "").strip() or None
        if not feature_id:
            return _result("SPEC_REQUIRED", f"{cls} tasks require a linked feature spec (spec.feature_id).")

        try:
            registry = SpecRegistry(self.specs_root).load()
        except SpecError as exc:
            return _result("SPEC_REFERENCE_INVALID", f"Spec registry failed to load: {exc}")

        feature = registry.feature(feature_id)
        if feature is None:
            return _result("SPEC_REFERENCE_INVALID", f"Unknown feature id: {feature_id}")
        if feature.get("status") != "approved":
            return _result("SPEC_NOT_APPROVED", f"Feature {feature_id} is '{feature.get('status')}', not approved.")

        req_ids = spec_id_list(task.get("spec_requirement_ids"))
        acc_ids = spec_id_list(task.get("spec_acceptance_ids"))
        inv_ids = spec_id_list(task.get("spec_invariant_ids"))
        if not req_ids or not acc_ids:
            return _result("TRACEABILITY_MISSING", "At least one requirement id and one acceptance criterion id must be linked.")

        for rid in req_ids:
            if registry.requirement(rid) is None:
                return _result("SPEC_REFERENCE_INVALID", f"Unknown requirement id: {rid}")
        for aid in acc_ids:
            if registry.acceptance_criterion(aid) is None:
                return _result("SPEC_REFERENCE_INVALID", f"Unknown acceptance criterion id: {aid}")
        for iid in inv_ids:
            if registry.invariant(iid) is None:
                return _result("SPEC_REFERENCE_INVALID", f"Unknown invariant id: {iid}")

        referenced = registry.feature_ids_for(req_ids, acc_ids, inv_ids) - {feature_id}
        if referenced:
            return _result("SPEC_REFERENCE_INVALID", f"Referenced requirement/acceptance/invariant ids belong to a different feature: {sorted(referenced)}")

        return _result("PASS", "Spec linkage valid and approved.")
