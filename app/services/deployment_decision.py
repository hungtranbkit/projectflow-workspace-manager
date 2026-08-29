from __future__ import annotations

"""Deployment lifecycle decision -- deliberately separate from
TaskDecisionService (section 23). TaskDecisionService owns the
development workflow (Builder/Review/QA/Integration/Merge -> DONE);
this module owns ONLY "given the latest Deployment row for one
(task, repository, environment), what's the current state and the one
next action" -- Task DONE and DEV NOT_DEPLOYED/DEPLOYING/VERIFIED/
FAILED are independent axes that coexist by design, never merged into
one status column."""

ACTIVE_STATUSES = ("PENDING", "PREPARING", "BUILDING", "DEPLOYING", "VERIFYING")
TERMINAL_STATUSES = ("VERIFIED", "FAILED", "ROLLED_BACK", "ROLLBACK_FAILED")
# A rollback attempt is FAILED-shaped for rollback-eligibility purposes
# (section 7: offer Rollback again after a failed rollback, same as after
# a failed normal deploy) but must never be confused with VERIFIED.
FAILED_STATUSES = ("FAILED", "ROLLBACK_FAILED")

PHASE_LABEL = {
    "PENDING": "Queued...", "PREPARING": "Preparing source...", "BUILDING": "Building...",
    "DEPLOYING": "Deploying...", "VERIFYING": "Verifying...",
}


def deployment_view(deployment: dict | None, target_configured: bool, rollback_target: dict | None = None) -> dict:
    """One normalized view model for the Task DONE page's Deployment
    section -- the ONE thing a template reads, never re-deriving status
    text from raw deployment fields itself. `rollback_target`, when
    given, is the previous VERIFIED deployment DeploymentService itself
    already confirmed is rollback-eligible (section 7) -- this function
    stays pure/DB-free, so callers compute that separately."""
    if not target_configured:
        return {"state": "NOT_CONFIGURED", "label": "DEV target not configured", "running": False, "deployment": None, "rollback_target": None}
    if not deployment:
        return {"state": "NOT_DEPLOYED", "label": "NOT DEPLOYED", "running": False, "deployment": None, "rollback_target": None}
    status = deployment["status"]
    if status in ACTIVE_STATUSES:
        return {"state": status, "label": PHASE_LABEL.get(status, status), "running": True, "deployment": deployment, "rollback_target": None}
    can_rollback = status in FAILED_STATUSES and rollback_target is not None
    if status == "VERIFIED":
        return {"state": "VERIFIED", "label": "DEV VERIFIED", "running": False, "deployment": deployment, "rollback_target": None}
    if status == "FAILED":
        return {"state": "FAILED", "label": "DEV FAILED", "running": False, "deployment": deployment, "rollback_target": rollback_target if can_rollback else None}
    if status == "ROLLBACK_FAILED":
        return {"state": "ROLLBACK_FAILED", "label": "ROLLBACK FAILED", "running": False, "deployment": deployment, "rollback_target": rollback_target if can_rollback else None}
    if status == "ROLLED_BACK":
        return {"state": "ROLLED_BACK", "label": "ROLLED BACK", "running": False, "deployment": deployment, "rollback_target": None}
    return {"state": status, "label": status, "running": False, "deployment": deployment, "rollback_target": None}
