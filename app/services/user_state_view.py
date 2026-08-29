from __future__ import annotations

"""Mobile-first operational UX (the redesign this implements): a single
normalized `UserTaskState` view model wrapping TaskDecisionService's
already-authoritative decision (status/stage/next_action/blocking_reasons/
current_step) into what a template actually renders. This is deliberately
NOT a second decision engine -- every state_class/headline/primary_action
below is derived from decision.evaluate()'s own fields, never re-inferred
from raw agent_workspaces/review_runs/etc. Templates render this object;
they do not re-decide workflow meaning themselves (section 16).

Four user-facing state classes (section 4) collapse ~20 internal
`next_action.action` codes:
  WORKING          -- system is actively doing something, no button needed
  WAITING          -- system is waiting on an external event (CI, merges)
  ACTION_REQUIRED  -- a human must do something now
  COMPLETE         -- current stage/Task is finished
"""

# Which state_class each next_action.action code implies, absent any
# closer per-code override below. `None` means "look at status/context
# instead" (see user_task_state()).
ACTION_STATE_CLASS: dict[str, str] = {
    "CREATE_BUILDER_WORKSPACE": "ACTION_REQUIRED",
    "VIEW_BUILDER": "WORKING",
    "START_BUILDER": "ACTION_REQUIRED",
    "REVIEW_BUILDER_RESULT": "ACTION_REQUIRED",
    "RETURN_TO_BUILDER": "ACTION_REQUIRED",
    "SUBMIT_FOR_REVIEW": "ACTION_REQUIRED",
    "START_REVIEW": "ACTION_REQUIRED",
    "CREATE_SANDBOX": "ACTION_REQUIRED",
    "SANDBOX_PROVISIONING": "WORKING",
    "REBUILD_SANDBOX": "ACTION_REQUIRED",
    "START_QA": "ACTION_REQUIRED",
    "CREATE_INTEGRATION": "ACTION_REQUIRED",
    "RESOLVE_CONFLICT": "ACTION_REQUIRED",
    "RUN_INTEGRATION_TEST": "ACTION_REQUIRED",
    "FIX_INTEGRATION_FAILURE": "ACTION_REQUIRED",
    "REVIEW_BASELINE_FAILURE": "ACTION_REQUIRED",
    "PUSH_INTEGRATION": "ACTION_REQUIRED",
    "CONFIRM_INTEGRATION_READY": "ACTION_REQUIRED",
    "PREPARE_PR": "ACTION_REQUIRED",
    "WAIT_FOR_CI": "WAITING",
    "WAIT_FOR_MERGES": "WAITING",
    "CLOSE_TASK": "COMPLETE",
    "SELECT_FOR_DEVELOPMENT": "ACTION_REQUIRED",
    "NONE": None,
}

# Vietnamese headline + explanation per action code (section 3/38): plain
# language, never a raw enum. Falls back to next_action.label/reason
# verbatim for any code not covered here (never a blank headline).
_ACTION_COPY: dict[str, tuple[str, str]] = {
    "CREATE_BUILDER_WORKSPACE": ("Chưa có Builder", "Task này chưa có Agent Workspace nào."),
    "VIEW_BUILDER": ("{agent} đang làm việc", "Không cần thao tác gì lúc này."),
    "START_BUILDER": ("Sẵn sàng bắt đầu", "Builder Workspace đã sẵn sàng, agent chưa chạy."),
    "REVIEW_BUILDER_RESULT": ("{agent} đã dừng trước khi hoàn tất", "ProjectFlow chưa nhận được báo cáo hoàn thành."),
    "RETURN_TO_BUILDER": ("Cần sửa lại", "Reviewer yêu cầu chỉnh sửa."),
    "SUBMIT_FOR_REVIEW": ("Sẵn sàng review", "Builder đã xong, chưa bắt đầu review."),
    "START_REVIEW": ("Cần review lại", "Source hoặc yêu cầu đã thay đổi kể từ lần review trước."),
    "CREATE_SANDBOX": ("Cần tạo Sandbox", "Review đã PASS -- cần môi trường chạy thật để kiểm tra."),
    "SANDBOX_PROVISIONING": ("Đang chuẩn bị môi trường kiểm tra", "Không cần thao tác gì lúc này."),
    "REBUILD_SANDBOX": ("Sandbox tạo thất bại", "Cần tạo lại Sandbox."),
    "START_QA": ("Cần kiểm tra trên môi trường thật", "Mọi review đã PASS -- cần mở app và xác nhận PASS/FAIL."),
    "CREATE_INTEGRATION": ("Sẵn sàng tích hợp", "Mọi điều kiện đã PASS -- có thể tạo Integration."),
    "RESOLVE_CONFLICT": ("Có xung đột merge", "Integration đang có conflict chưa resolve."),
    "RUN_INTEGRATION_TEST": ("Cần chạy test tích hợp", "Integration đã có, chưa test hoặc test chưa current."),
    "FIX_INTEGRATION_FAILURE": ("Test tích hợp thất bại", "Cần sửa lỗi rồi chạy lại."),
    "REVIEW_BASELINE_FAILURE": ("Có lỗi cần xem xét", "Một số lỗi cần được phân loại trước khi tiếp tục."),
    "PUSH_INTEGRATION": ("Sẵn sàng đẩy code", "Test đã PASS -- có thể push để tạo Pull Request."),
    "CONFIRM_INTEGRATION_READY": ("Xác nhận sẵn sàng", "Cần xác nhận trước khi tiếp tục."),
    "PREPARE_PR": ("Sẵn sàng tạo Pull Request", "Đã sẵn sàng lên main -- push và tạo PR."),
    "WAIT_FOR_CI": ("Đang chờ GitHub CI", "Không cần thao tác gì lúc này."),
    "WAIT_FOR_MERGES": ("Đang chờ merge", "Không cần thao tác gì lúc này."),
    "CLOSE_TASK": ("Đã hoàn tất", "Toàn bộ repo bắt buộc đã được merge."),
    "SELECT_FOR_DEVELOPMENT": ("Trong Backlog", "Task chưa được chọn để phát triển."),
}

# Raw-enum -> plain-language technical explanation (section 3), shown as
# the card's supporting line -- distinct from the headline above.
ENUM_COPY: dict[str, str] = {
    "ACTIVE": "Đang thực hiện",
    "EXITED": "Đã dừng",
    "FAILED": "Gặp lỗi",
    "NONE": "Chưa có",
    "NOT_CONFIGURED": "Chưa cấu hình",
    "SOURCE_STALE": "Source đã thay đổi sau lần kiểm tra trước",
    "STALE": "Đã lỗi thời -- cần làm lại",
    "CI_PENDING": "GitHub đang chạy kiểm tra",
    "UNKNOWN_MERGEABILITY": "GitHub chưa xác nhận PR có thể merge",
    "PASS": "PASS",
    "FAIL": "FAIL",
    "BLOCKED": "Bị chặn",
    "PENDING": "Đang chờ",
    "RUNNING": "Đang chạy",
    "FIX_REQUIRED": "Cần sửa lại",
    "CONFLICT": "Có xung đột",
    "MERGED": "Đã merge",
    "NOT_STARTED": "Chưa bắt đầu",
}


def humanize_enum(value: str | None) -> str:
    """Never render a raw enum as the primary user-facing word (section
    3) -- falls back to a title-cased, underscore-stripped version for
    anything not in the table above, so a future enum never renders as
    a literal SCREAMING_SNAKE_CASE string."""
    if not value:
        return "—"
    return ENUM_COPY.get(value, value.replace("_", " ").capitalize())


def _copy_for(action_code: str, next_action: dict) -> tuple[str, str]:
    headline, explanation = _ACTION_COPY.get(action_code, (next_action.get("label") or "Cần thao tác", next_action.get("reason") or ""))
    agent = next_action.get("_agent") or ""
    return headline.replace("{agent}", agent.capitalize()), explanation


def task_state_class(decision: dict) -> str:
    """The Task-level state_class (section 4), derived only from fields
    decision.evaluate() already computed -- never re-inferred."""
    status = decision["status"]
    if status == "DONE":
        return "COMPLETE"
    if status == "CANCELLED":
        return "COMPLETE"
    na = decision["next_action"]
    cls = ACTION_STATE_CLASS.get(na["action"])
    if cls is not None:
        return cls
    # action == NONE: look at context. A real, unresolved blocker with no
    # computed recovery action still needs a human to look at it
    # (never silently "waiting" on nothing); an honest "nothing to do
    # but wait for a gate above" is real WAITING.
    if status == "BLOCKED":
        return "ACTION_REQUIRED"
    return "WAITING"


def user_task_state(decision: dict) -> dict:
    """The Task Status Hero's data (section 2/16). `primary_action` is
    None for WORKING/WAITING/COMPLETE states that don't need a button --
    templates show it only for ACTION_REQUIRED (and the few COMPLETE
    states that still have one real next step, e.g. Close Task)."""
    na = dict(decision["next_action"])
    builders = decision["builders"]
    live_builder = next((b for b in builders if b["agent_status"] in ("STARTING", "RUNNING", "WAITING_FOR_INPUT") or b["agent_status"] in ("EXITED", "FAILED")), None)
    na["_agent"] = live_builder["agent"] if live_builder else ""
    state_class = task_state_class(decision)
    headline, explanation = _copy_for(na["action"], na)
    primary_action = None
    if state_class == "ACTION_REQUIRED" or (state_class == "COMPLETE" and na["action"] not in ("NONE",) and na.get("target")):
        if na.get("target"):
            primary_action = {"label": na["label"] or headline, "target": na["target"], "method": na.get("method", "GET")}
    # EXITED-without-report recovery (section 18/28): the real primary
    # action here is [Mark Ready for Review] / [Resume Codex] -- both
    # already exist as real, validated forms in the per-workspace panel
    # right on this same page, never re-implemented here. The Hero
    # anchors down to that panel rather than duplicating the git-source
    # validation or the completion-summary form.
    recovery_extra_secondary = []
    if live_builder and live_builder.get("recovery_state") == "COMPLETION_REQUIRED":
        check = live_builder.get("manual_ready_check") or {}
        anchor = f"#agent-panel-{live_builder['id']}"
        if check.get("ok"):
            headline, explanation = _copy_for("REVIEW_BUILDER_RESULT", na)
            primary_action = {"label": "Mark Ready for Review", "target": anchor, "method": "GET"}
            recovery_extra_secondary = [{"label": "Resume " + live_builder["agent"].capitalize(), "target": anchor}]
        else:
            headline = f"{live_builder['agent'].capitalize()} đã dừng trước khi hoàn tất"
            explanation = check.get("detail") or "Cần thêm thao tác trước khi có thể đánh dấu sẵn sàng review."
            primary_action = {"label": f"Resume {live_builder['agent'].capitalize()}", "target": anchor, "method": "GET"}
            recovery_extra_secondary = [{"label": "Mark Blocked", "target": anchor}]
    blocking = decision.get("blocking_reasons") or []
    primary_blocker = blocking[0] if blocking else None
    extra_blockers = len(blocking) - 1 if len(blocking) > 1 else 0
    return {
        "state_class": state_class,
        "headline": headline,
        "explanation": explanation,
        "blocker": primary_blocker,
        "extra_blocker_count": extra_blockers,
        "primary_action": primary_action,
        "secondary_actions": recovery_extra_secondary + _secondary_actions(decision, live_builder),
        "current_step": decision.get("current_step"),
        # Sections 2/4/11: the Workflow Summary card's other three
        # ingredients, straight from decision.evaluate() -- never
        # recomputed here or in a template.
        "checklist": decision.get("checklist") or [],
        "missing": decision.get("missing_requirements") or [],
        "previous_step_summary": decision.get("previous_step_summary"),
    }


def _secondary_actions(decision: dict, live_builder: dict | None) -> list[dict]:
    """Always-available, lower-priority links (section 7/9) -- never
    compete visually with the one primary_action. "Open Live Agent" only
    when the session is genuinely live -- a builder_view() `session` row
    can exist (and be non-None) for an already-EXITED session too, which
    has no live terminal to attach to (matches the same
    LIVE_SESSION_STATUSES guard task_detail.html's own template uses)."""
    out = []
    if live_builder and live_builder.get("session") and live_builder.get("agent_status") in ("STARTING", "RUNNING", "WAITING_FOR_INPUT"):
        out.append({"label": "Open Live Agent", "target": f"/workspaces/{live_builder['id']}/sessions/{live_builder['session']['id']}"})
    if live_builder:
        out.append({"label": "View Details", "target": f"/workspaces/{live_builder['id']}"})
    return out


PROGRESS_STEPS = [
    ("TASK", "Task"), ("SETUP", "Cấu hình"), ("AGENT_RUNNING", "Agent"), ("REVIEW", "Review"),
    ("TEST_QA", "Kiểm tra"), ("INTEGRATION", "Integration"), ("READY_FOR_MAIN", "Sẵn sàng merge"), ("DONE", "Hoàn tất"),
]


def progress_summary(current_step: str | None, qa_required: bool, integration_required: bool) -> dict:
    """Compact mobile stepper data (section 10/11): only current/previous/
    next, never the full 8-label row squeezed into a narrow screen. Skips
    steps the same way the desktop stepper already does (qa_required/
    integration_required), so mobile and desktop never disagree about
    which steps exist."""
    keys = [k for k, _ in PROGRESS_STEPS if not ((k == "TEST_QA" and not qa_required) or (k == "INTEGRATION" and not integration_required))]
    labels = dict(PROGRESS_STEPS)
    if current_step not in keys:
        return {"current": None, "previous": None, "next": None, "index": -1, "total": len(keys)}
    idx = keys.index(current_step)
    return {
        "current": labels[current_step],
        "previous": labels[keys[idx - 1]] if idx > 0 else None,
        "next": labels[keys[idx + 1]] if idx < len(keys) - 1 else None,
        "index": idx,
        "total": len(keys),
    }
