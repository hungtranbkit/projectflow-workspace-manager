from __future__ import annotations
import json

from app.services.workflow_engine import STAGE_ORDER

"""Change Overview (first UI surface for the E1-E7 engineering domain):
a pure VIEW-MODEL builder, exactly the same discipline user_state_view.py
already established for Task -- every field here is read from an
EXISTING service's own already-computed truth (WorkflowService,
ArchitectureDesignLifecycleService, TestDesignLifecycleService,
SpecLifecycleService, HumanDecisionService, PlannerService,
agent_sessions), never a second, independently-derived status
calculation. This module decides nothing; it only arranges what those
services already decided into a checklist a human can read at a glance,
the same wf-checklist/status-hero visual language task_detail.html
already uses (see AGENTS.md "Workflow Summary UX")."""

# Workflow status -> status-hero state class (same 4 states
# task_detail.html's CSS already defines: working/waiting/
# action_required/complete).
_STATE_CLASS = {
    "PENDING": "working", "ACTIVE": "working",
    "WAITING_HUMAN": "action_required", "BLOCKED": "action_required", "FAILED": "action_required",
    "COMPLETE": "complete",
}
_HEADLINE = {
    "PENDING": "Not started yet",
    "ACTIVE": "In progress",
    "WAITING_HUMAN": "Waiting on a human decision",
    "BLOCKED": "Blocked",
    "FAILED": "A deployment failed",
    "COMPLETE": "Complete",
}


def _item(label: str, state: str, note: str | None = None) -> dict:
    return {"label": label, "state": state, "note": note}


def stage_timeline(workflow_state: dict | None, status: str) -> list[dict]:
    """E7.5.4: the full STAGE_ORDER lifecycle stepper (ANALYSIS -> ... ->
    HUMAN_ACCEPTANCE), visual state derived ONLY from WorkflowService.
    evaluate_workflow()'s own already-computed stages/current_stage/
    status -- never a frontend heuristic. A stage this Change's
    WorkflowProfile never declared a requirement for at all (e.g. VIBE
    omits SPEC/ARCHITECTURE/DESIGN/PLANNING/RELEASE/HUMAN_ACCEPTANCE
    entirely) is honestly NOT_APPLICABLE, not silently hidden."""
    by_key = {sr["stage"]: sr for sr in ((workflow_state or {}).get("stages") or [])}
    current = (workflow_state or {}).get("current_stage")
    out = []
    for stage in STAGE_ORDER:
        sr = by_key.get(stage)
        if sr is None or sr["requirement"] == "NOT_APPLICABLE":
            visual = "NOT_APPLICABLE"
        elif sr["complete"]:
            visual = "COMPLETE"
        elif stage == current:
            visual = {"WAITING_HUMAN": "WAITING_HUMAN", "BLOCKED": "BLOCKED", "FAILED": "FAILED"}.get(status, "ACTIVE")
        else:
            visual = "WAITING"
        unmet = [gk for gk, met in ((sr or {}).get("gates") or {}).items() if not met] if sr else []
        out.append({"stage": stage, "visual": visual, "unmet_gates": unmet})
    return out


def _latest(rows: list[dict], kind: str, status: str | None = None) -> dict | None:
    matches = [r for r in rows if r["kind"] == kind and (status is None or r["status"] == status) and r["status"] != "SUPERSEDED"]
    return matches[-1] if matches else None


def build_change_overview(*, change: dict, work_products: list[dict], workflow_state: dict | None,
                           architecture_status: dict, design_status: dict, test_design_status: dict,
                           spec_proposals: list[dict], human_decisions_pending: list[dict],
                           agents_completed: int, agents_running: int, spec_drift: dict) -> dict:
    """Everything passed in is already the real, computed truth of its
    own owning service -- this function only arranges it."""
    status = workflow_state["status"] if workflow_state else "PENDING"
    state_class = _STATE_CLASS.get(status, "working")
    headline = _HEADLINE.get(status, status.replace("_", " ").title())

    # ---- Spec section -------------------------------------------------
    ra_done = _latest(work_products, "REQUIREMENT_ANALYSIS") is not None
    latest_proposal = spec_proposals[-1] if spec_proposals else None
    feature_spec = _latest(work_products, "FEATURE_SPEC", "APPROVED")
    spec_reviewed = latest_proposal is not None and json.loads(latest_proposal.get("review_result") or "{}").get("verdict") == "PASS"
    spec_items = [
        _item("Requirement Analysis", "done" if ra_done else ("current" if latest_proposal or ra_done else "future")),
        _item("Feature Spec", "done" if latest_proposal else ("current" if ra_done else "future")),
        _item("Spec Review", "done" if spec_reviewed else ("current" if latest_proposal else "future")),
        _item("Approved", "done" if feature_spec else ("current" if spec_reviewed else "future")),
    ]

    # ---- Architecture section ------------------------------------------
    analysis = architecture_status.get("architecture_analysis")
    adrs = [wp for wp in work_products if wp["kind"] == "ADR" and wp["status"] != "SUPERSEDED"]
    arch_ready = bool(architecture_status.get("architecture_ready"))
    arch_reviewed = _latest(work_products, "ARCHITECTURE_REVIEW") is not None
    no_arch_change = analysis is not None and json.loads(analysis.get("content_metadata") or "{}").get("classification") == "NO_ARCHITECTURE_CHANGE"
    architecture_items = [
        _item("Analysis", "done" if analysis else ("current" if feature_spec else "future")),
        _item("ADR", "done" if any(a["status"] == "APPROVED" for a in adrs) else ("skipped" if (no_arch_change or (arch_reviewed and not adrs)) else ("current" if analysis else "future"))),
        _item("Review", "done" if arch_ready else ("current" if analysis else "future")),
    ]

    # ---- Design section --------------------------------------------
    tech_design = design_status.get("technical_design")
    ui_ux = design_status.get("ui_ux_design")
    applicability = design_status.get("ui_ux_applicability") or {"applicable": False}
    design_reviewed = _latest(work_products, "DESIGN_REVIEW") is not None
    design_items = [
        _item("Technical Design", "done" if tech_design and tech_design["status"] == "APPROVED" else ("current" if analysis or feature_spec else "future")),
        (_item("UI/UX Design", "skipped", "Not applicable") if not applicability.get("applicable")
         else _item("UI/UX Design", "done" if ui_ux and ui_ux["status"] == "APPROVED" else ("current" if tech_design else "future"))),
        _item("Design Review", "done" if design_status.get("design_ready") else ("current" if design_reviewed or tech_design else "future")),
    ]

    # ---- Test Design section -------------------------------------------
    test_case_set = test_design_status.get("test_case_set")
    if not test_case_set:
        test_design_items = [_item("Test Design", "future", "Not started")]
    elif test_design_status.get("test_design_ready"):
        test_design_items = [_item("Test Design", "done")]
    else:
        test_design_items = [_item("Test Design", "current", "In review")]

    # ---- Implementation section -----------------------------------------
    tasks = [t for t in (change.get("_tasks") or [])]
    if not tasks:
        impl_items = [_item("Implementation", "future", "Waiting")]
    elif all(t.get("status") == "DONE" for t in tasks):
        impl_items = [_item("Implementation", "done")]
    else:
        impl_items = [_item("Implementation", "current", f"{sum(1 for t in tasks if t.get('status') == 'DONE')}/{len(tasks)} Tasks done")]

    return {
        "change": change, "state_class": state_class, "headline": headline,
        "status": status, "current_stage": workflow_state["current_stage"] if workflow_state else None,
        "timeline": stage_timeline(workflow_state, status),
        "sections": [
            {"title": "Spec", "checks": spec_items},
            {"title": "Architecture", "checks": architecture_items},
            {"title": "Design", "checks": design_items},
            {"title": "Test Design", "checks": test_design_items},
            {"title": "Implementation", "checks": impl_items},
        ],
        "human_decisions_pending": human_decisions_pending,
        "agents_completed": agents_completed, "agents_running": agents_running,
        "spec_drift": spec_drift,
    }
