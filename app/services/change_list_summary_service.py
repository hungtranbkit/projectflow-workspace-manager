from __future__ import annotations

"""Track A1.3: ChangeListSummaryService -- builds everything GET /changes
(E7.5.1/E7.5.2) needs in bounded/batched form. This is composition only,
exactly like ChangeControlSurfaceService (A1.3's own explicit rule: "Use
authoritative existing services/data. Do not create a new source of
truth") -- every field still comes from WorkflowService/HumanDecision
Service/ProductAcceptanceService/ChangeService, the SAME calls
app/main.py's changes_page route made before this service existed. The
one real behavior change is WHEN the expensive call
(WorkflowService.evaluate_workflow(), which fans out into
TaskDecisionService.evaluate() per Task -- the confirmed root cause in
docs/PRODUCTIZATION_AUDIT.md's P0.18 and this track's own
scripts/benchmark_changes_list.py) is made:

  * every row still gets its CHEAP signals (human_decisions_pending,
    task_count, product_review_pending, profile_key -- none of these
    loop over Tasks re-evaluating TaskDecisionService) so Human
    Attention / Recent Activity / the change_type+profile filters all
    stay correct across the FULL Change set, not just one page;
  * workflow_status (the expensive one) is only computed for a row that
    is actually about to be shown WITH it -- the current page, a Human
    Attention entry, or a Recent Activity entry -- never for every row
    on every request once the list grows past one page. Human Attention
    selection itself never depended on workflow_status (it always only
    read human_decisions_pending/product_review_pending), so this is a
    pure "don't compute what nothing reads" cut, not a scope narrowing.
  * the one honest exception: filtering by `status` needs workflow_status
    for every row before it can filter+paginate correctly, so that path
    still pays the full cost -- documented in the A1 final report, not
    hidden.

Wrapped in one db.memoize() scope (A1.2/A1.5/A1.6): the remaining N+1
fan-out inside evaluate_workflow()/overview_status() for the rows that
DO get the expensive treatment still collapses its own internal
duplicate reads. Also opens project_contract.memoize(): profiling this
route (Track A1) found ProductAcceptanceService.overview_status() (via
its project-policy resolver) re-reading and re-parsing a repository's
PROJECT.yaml from disk with no caching at all -- more wall-clock cost
at 100 Changes than every SQLite query this page makes combined."""

from app.services import project_contract, spec_registry


class ChangeListSummaryService:
    def __init__(self, db, changes, workflow_service, human_decisions, product_acceptance_service):
        self.db = db
        self.changes = changes
        self.workflow_service = workflow_service
        self.human_decisions = human_decisions
        self.product_acceptance_service = product_acceptance_service

    def build(self, *, status: str = "", change_type: str = "", profile: str = "",
              page: int = 1, page_size: int = 25, recent_limit: int = 8) -> dict:
        page = max(1, page)
        page_size = max(1, min(page_size, 200))  # a deliberate, generous upper bound -- never "no limit"
        with self.db.memoize(), project_contract.memoize(), spec_registry.memoize():
            rows = self.changes.list()
            for c in rows:
                run = self.workflow_service.get_workflow(c["id"])
                c["profile_key"] = run["profile_key"] if run else None
                c["human_decisions_pending"] = len(self.human_decisions.list_pending_for_change(c["id"]))
                c["task_count"] = len(self.changes.list_tasks_for_change(c["id"]))
                # E11.19, unchanged from the pre-A1 route: only computed
                # for Changes with at least one Task (matches
                # release_deploy_summary's own scope).
                c["product_review_pending"] = bool(
                    c["task_count"] and self.product_acceptance_service.overview_status(c["id"]) == "PENDING")
                c["workflow_status"] = None  # filled in by _ensure_status, lazily, below

            def ensure_status(c: dict) -> None:
                if c["workflow_status"] is not None:
                    return
                run = self.workflow_service.get_workflow(c["id"])
                c["workflow_status"] = self.workflow_service.evaluate_workflow(c["id"])["status"] if run else "PENDING"

            if status:
                # A `status` filter needs the real, authoritative
                # workflow_status for every candidate row before it can
                # filter correctly -- no shortcut here without a second,
                # approximate status calculation A1.3 forbids.
                for c in rows:
                    ensure_status(c)

            filtered = rows
            if status: filtered = [c for c in filtered if c["workflow_status"] == status]
            if change_type: filtered = [c for c in filtered if c["change_type"] == change_type]
            if profile: filtered = [c for c in filtered if c["profile_key"] == profile]

            # Cheap-signals-only, exactly like the pre-A1 route -- never
            # gated on workflow_status.
            human_attention = [c for c in rows if c["human_decisions_pending"] or c["product_review_pending"]]
            recent = sorted(rows, key=lambda c: c["updated_at"], reverse=True)[:recent_limit]

            total = len(filtered)
            total_pages = max(1, -(-total // page_size))
            page = min(page, total_pages)
            start = (page - 1) * page_size
            page_rows = filtered[start:start + page_size]

            # human_attention/recent/page_rows all reference the SAME dict
            # objects as `rows` (never copied) -- deduping by identity
            # means a row appearing in more than one of these three lists
            # (common: a page row that also needs attention) only ever
            # gets evaluate_workflow() called once.
            seen: dict[int, dict] = {}
            for c in (page_rows + human_attention + recent):
                seen[id(c)] = c
            for c in seen.values():
                ensure_status(c)

            return {
                "rows": page_rows, "all_changes": rows, "human_attention": human_attention, "recent": recent,
                "total": total, "page": page, "page_size": page_size, "total_pages": total_pages,
            }
