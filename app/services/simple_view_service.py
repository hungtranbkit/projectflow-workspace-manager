from __future__ import annotations

"""Track A1.11-A1.18/A1.23: Simple Mode is a PRESENTATION layer over the
existing E1-E13 engineering lifecycle -- it creates no new engineering
truth (A1.11's own explicit rule). Every field below is read from
ChangeControlSurfaceService (already composition-only over WorkflowService/
ProductAcceptanceService/etc, see that module's own docstring) or one
more read-only query against an existing table (incidents/
workspace_events) -- never a second status calculation, never invented
state independent of WorkflowService (A1.14).

SIMPLE_STRINGS below is Track A1's translation-key seam (A1.20/A1.21):
a flat dict of English copy, looked up by key from both the Python side
(status/lifecycle text) and templates (`{{ t('key') }}`, wired in
app/main.py). Adding a second language later is "add a second dict and
a lang switch on t()", not a template rewrite -- see docs/LIFECYCLE.md's
localization note. No domain logic is ever embedded in a string value."""

from app.services.workflow_engine import STAGE_ORDER

# A1.14: the ~6-step Simple lifecycle, mapped ONLY from WorkflowService's
# own real STAGE_ORDER truth (via ChangeControlSurfaceService.overview()'s
# `timeline`, itself built by change_overview.stage_timeline() straight
# off evaluate_workflow()'s stages/current_stage/status) -- never a
# second, independently-invented stage machine. TEST_DESIGN/Integration
# have no dedicated STAGE_ORDER entry of their own (see workflow_engine.
# py's own GATES/TASK_TYPES comments for why -- TEST_DESIGN_READY is
# attached to VERIFY, integration is part of the Task-level merge ladder,
# not a Change-level workflow stage), so this groups the REAL stages that
# do exist into the buckets A1.14 names.
SIMPLE_STEPS = (
    ("understanding", "Understanding", ("ANALYSIS", "SPEC")),
    ("designing", "Designing", ("ARCHITECTURE", "DESIGN", "PLANNING")),
    ("building", "Building", ("BUILD",)),
    ("checking", "Checking", ("REVIEW", "VERIFY")),
    ("deploying", "Deploying", ("RELEASE", "DEPLOY")),
    ("ready", "Ready", ("HUMAN_ACCEPTANCE",)),
)

# A1.15: never show a raw internal status/gate code as primary text.
SIMPLE_STRINGS = {
    "status.PENDING": "Not started yet",
    "status.ACTIVE": "AI is working on this",
    "status.WAITING_HUMAN": "Waiting for your decision",
    "status.BLOCKED": "Blocked -- needs attention",
    "status.FAILED": "A deployment failed",
    "status.COMPLETE": "Done",
    "attention.none": "No action needed. AI is working.",
    "attention.decision": "Your decision is needed.",
    "attention.product_review": "Please review the live app.",
    "attention.blocked": "This is blocked and needs your attention.",
    "product.no_release_yet": "Nothing built yet",
    "product.not_deployed": "Built, not deployed yet",
    "deploy.test": "Test",
    "deploy.production": "Production",
    "mode.simple": "Simple",
    "mode.advanced": "Advanced",
}


def t(key: str) -> str:
    return SIMPLE_STRINGS.get(key, key)


# A1.17: agent-activity phrasing -- never raw AgentSession
# provider/session/PID/worktree by default (that stays in Advanced).
def _agent_activity_text(agents_running: int, agents_completed: int) -> str:
    if agents_running == 1:
        return "1 AI worker is building"
    if agents_running > 1:
        return f"{agents_running} AI workers are building"
    if agents_completed:
        return "AI finished its work here"
    return "No AI workers active"


# A1.18: release/deploy bucket -> a short, human sentence. Buckets
# themselves come straight from ChangeControlSurfaceService.
# release_deploy_summary() (already composition-only over
# IntegrationService/ReleaseService's own state) -- never re-derived.
_DEPLOY_TEXT = {
    None: "Not deployed yet", "building": "Building the release",
    "ready": "Released, ready to deploy", "failed": "Release failed",
}
_ENV_TEXT = {
    None: "Not deployed", "verified": "Healthy", "failed": "Unhealthy",
    "waiting_approval": "Waiting for approval", "rolled_back": "Rolled back",
}


class SimpleViewService:
    def __init__(self, db, change_control_surface, changes):
        self.db = db
        self.change_control_surface = change_control_surface
        self.changes = changes

    def _lifecycle(self, overview: dict, has_workflow: bool) -> dict:
        timeline_by_stage = {row["stage"]: row for row in overview["timeline"]}
        steps = []
        for key, label, stage_keys in SIMPLE_STEPS:
            if not has_workflow:
                # stage_timeline() reads "no WorkflowRun row exists at
                # all yet" the exact same way it reads "this stage is
                # genuinely NOT_APPLICABLE under the chosen profile" --
                # both collapse to visual="NOT_APPLICABLE" (see
                # workflow_engine.py's own stage_timeline()). Without
                # this guard every step reads NOT_APPLICABLE and the
                # branch below (correctly, for the real "profile opts
                # this stage out" case) marks the WHOLE lifecycle "done"
                # for a Change that has not even started -- checked here,
                # first, since "no workflow yet" is unambiguous on its
                # own (never inferred from the visuals).
                steps.append({"key": key, "label": label, "state": "current" if key == "understanding" else "future"})
                continue
            rows = [timeline_by_stage[s] for s in stage_keys if s in timeline_by_stage]
            visuals = {r["visual"] for r in rows}
            if key == "ready":
                state = "done" if overview["status"] == "COMPLETE" else (
                    "current" if visuals - {"NOT_APPLICABLE"} else "future")
            elif not visuals or visuals <= {"NOT_APPLICABLE", "COMPLETE"}:
                state = "done" if visuals & {"COMPLETE"} or not (visuals - {"NOT_APPLICABLE"}) else "future"
            elif visuals & {"BLOCKED", "WAITING_HUMAN", "FAILED", "ACTIVE", "WAITING"}:
                state = "current"
            else:
                state = "future"
            # a bucket every one of whose real stages is NOT_APPLICABLE
            # (e.g. VIBE has no ARCHITECTURE/DESIGN/PLANNING) is done --
            # nothing here was ever required, matching stage_timeline()'s
            # own NOT_APPLICABLE-is-not-blocking convention.
            if rows and all(r["visual"] == "NOT_APPLICABLE" for r in rows) and key != "ready":
                state = "done"
            steps.append({"key": key, "label": label, "state": state})
        return {"steps": steps, "status": overview["status"], "status_text": t(f"status.{overview['status']}"),
                "headline": overview["headline"]}

    def _human_attention(self, overview: dict, header: dict, product_review_pending: bool) -> dict:
        pending = overview["human_decisions_pending"]
        if pending:
            q = pending[0].get("question") or "A decision is needed."
            return {"needs_you": True, "headline": t("attention.decision"), "detail": q,
                    "link": f"/changes/{header['change']['id']}/decisions"}
        if product_review_pending:
            return {"needs_you": True, "headline": t("attention.product_review"), "detail": None,
                    "link": f"/changes/{header['change']['id']}/acceptance"}
        if overview["status"] == "BLOCKED":
            return {"needs_you": True, "headline": t("attention.blocked"), "detail": None,
                    "link": f"/changes/{header['change']['id']}/tasks"}
        return {"needs_you": False, "headline": t("attention.none"), "detail": None, "link": None}

    def _product(self, context: dict | None, deploy_summary: dict) -> dict:
        if not context:
            return {"requested": None, "delivered": [], "live_url": None, "version": None,
                    "health": t("product.no_release_yet"), "review": None}
        release = context.get("release")
        return {
            "requested": context.get("what_you_asked_for"),
            "delivered": context.get("what_changed") or [],
            "live_url": context.get("live_url"),
            "version": release.get("version") if release else None,
            "health": _ENV_TEXT.get(deploy_summary.get("production"), t("product.not_deployed")),
            "review": context.get("acceptance", {}).get("status") if context.get("acceptance") else deploy_summary.get("acceptance"),
        }

    def _deploy(self, deploy_summary: dict) -> dict:
        return {
            "release_text": _DEPLOY_TEXT.get(deploy_summary.get("release")),
            "test": {"label": t("deploy.test"), "text": _ENV_TEXT.get(deploy_summary.get("test"))},
            "production": {"label": t("deploy.production"), "text": _ENV_TEXT.get(deploy_summary.get("production"))},
        }

    def _history(self, change_id: int) -> dict:
        events = self.db.all(
            "SELECT * FROM workspace_events WHERE entity_type='change' AND entity_id=? ORDER BY id DESC LIMIT 20",
            (change_id,))
        incidents = self.db.all("SELECT * FROM incidents WHERE change_id=? ORDER BY id DESC", (change_id,))
        children = self.changes.list_children(change_id)
        change = self.changes.get(change_id)
        parent = self.changes.get(change["parent_change_id"]) if change and change.get("parent_change_id") else None
        return {"events": events, "incidents": incidents, "follow_up_changes": children, "parent_change": parent}

    def build(self, change_id: int) -> dict:
        header = self.change_control_surface.header(change_id)
        overview = self.change_control_surface.overview(change_id)
        acceptance = self.change_control_surface.acceptance_tab(change_id)
        deploy_summary = overview.get("release_deploy_summary") or {}
        product_review_pending = bool(deploy_summary.get("acceptance") == "PENDING")
        has_workflow = bool(self.change_control_surface.workflow_service.get_workflow(change_id))
        return {
            "change": header["change"],
            "lifecycle": self._lifecycle(overview, has_workflow),
            "human_attention": self._human_attention(overview, header, product_review_pending),
            "product": self._product(acceptance.get("context"), deploy_summary),
            "build": {
                "progress_text": next((s["checks"][0]["note"] for s in overview["sections"] if s["title"] == "Implementation"), None)
                                  or ("Done" if overview["status"] == "COMPLETE" else "Not started"),
                "agents_text": _agent_activity_text(overview["agents_running"], overview["agents_completed"]),
                "agents_running": overview["agents_running"], "agents_completed": overview["agents_completed"],
                # overview["change"]["_tasks"] was already computed by
                # change_control_surface.overview() (never header["change"]
                # -- header() builds its own separate `change` row with no
                # _tasks key) -- reuse it rather than re-querying.
                "task_count": len(overview["change"].get("_tasks") or []),
            },
            "review": {
                "integration_text": deploy_summary.get("integration"),
                "human_attention": overview["human_decisions_pending"],
            },
            "deploy": self._deploy(deploy_summary),
            "history": self._history(change_id),
        }
