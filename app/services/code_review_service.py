from __future__ import annotations
import json

from app.services.review_service import (
    _ReviewerRole, ReviewError, ReviewResultValidator, FindingsStore,
    CODE_REVIEWER_PREAMBLE, REVIEW_JSON_SCHEMA, bounded_diffs, aggregate_verdict, task_chain_ids,
)

"""Independent Code Review (E9.4-E9.6). A CodeReviewService.review_task()
call is the ONLY place Task source is judged against its governing
Spec/Design/Test contract and its REAL diff -- reusing the exact
TaskExecutionContextBuilder sections E8.8 already built (never a
second context assembler), the exact WorktreeManager/GitWorkspaceService
worktree identity E8.5 already built, and the exact PlannerAgentInvoker
E4-E7's own reviewers already use for a fresh, tool-less, single-turn
structured call."""


class CodeReviewService(_ReviewerRole):
    review_kind = "CODE"
    preamble = CODE_REVIEWER_PREAMBLE
    role_key = "REVIEWER"

    def __init__(self, db, changes, work_products, findings_store: FindingsStore, invoker, roles_catalog, git,
                 worktree_manager, task_execution_context_builder, human_decisions, project_policy_resolver=None):
        super().__init__(db, invoker, roles_catalog, self.role_key)
        self.changes = changes
        self.work_products = work_products
        self.findings_store = findings_store
        self.git = git
        self.worktree_manager = worktree_manager
        self.task_execution_context_builder = task_execution_context_builder
        self.human_decisions = human_decisions
        self.project_policy_resolver = project_policy_resolver
        self.validator = ReviewResultValidator()

    def _round_number(self, chain_ids: list[int], review_kind: str) -> int:
        """Counts across the WHOLE fix-chain (task_chain_ids), never a
        single task_id -- a Fix Task's own first review must continue
        the round count where its predecessor left off, or MAX_ROUNDS
        (E9.18) would never actually trigger across multiple fix
        cycles (each new Fix Task's counter would silently reset to 0)."""
        placeholders = ",".join("?" * len(chain_ids))
        row = self.db.one(f"SELECT COUNT(*) c FROM review_runs WHERE task_id IN ({placeholders}) AND review_kind=?", (*chain_ids, review_kind))
        return row["c"] if row else 0

    def _known_ids(self, change_id: int, task_id: int) -> dict:
        plan_item = self.db.one("SELECT * FROM plan_items WHERE materialized_task_id=?", (task_id,))
        req_ids = set(json.loads(plan_item["requirement_ids"] or "[]")) if plan_item else set()
        cases = self.db.all("SELECT * FROM test_case_specs WHERE change_id=?", (change_id,))
        ac, inv, tcs = set(), set(), set()
        for c in cases:
            ac |= set(json.loads(c["acceptance_ids"] or "[]"))
            inv |= set(json.loads(c["invariant_ids"] or "[]"))
            tcs.add(c["item_key"])
            req_ids |= set(json.loads(c["requirement_ids"] or "[]"))
        return {"known_requirement_ids": req_ids, "known_acceptance_ids": ac, "known_invariant_ids": inv, "known_test_case_ids": tcs}

    def _baseline_wp_ids(self, change_id: int) -> dict:
        spec_rows = [wp for wp in self.work_products.list_for_change(change_id) if wp["kind"] == "FEATURE_SPEC" and wp["status"] == "APPROVED"]
        design_rows = [wp for wp in self.work_products.list_for_change(change_id) if wp["kind"] == "TECHNICAL_DESIGN" and wp["status"] == "APPROVED"]
        return {"spec": spec_rows[-1]["id"] if spec_rows else None, "design": design_rows[-1]["id"] if design_rows else None}

    def review_task(self, task_id: int, provider: str = "claude") -> dict:
        review_kind = self.review_kind
        t = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not t:
            raise ReviewError("Task not found")
        change_id = t.get("change_id")
        ws = self.worktree_manager.get_task_worktree(task_id)
        if not ws:
            return {"outcome": "NO_MANAGED_WORKTREE", "verdict": None}
        try:
            self._check_assignment(provider)
        except ReviewError as exc:
            self.db.event("task", task_id, "CODE_REVIEW_ASSIGNMENT_INVALID", str(exc))
            return {"outcome": "ASSIGNMENT_INVALID", "verdict": None, "message": str(exc)}

        head_commit = self.git.head(ws["worktree_path"])
        base_commit = ws["base_commit"]
        changed_files = self.git.changed_files(ws["worktree_path"], base_commit)
        if not changed_files:
            return {"outcome": "NO_CHANGES", "verdict": None}
        diffs, complete = bounded_diffs(self.git, ws["worktree_path"], base_commit, head_commit, changed_files)

        chain_ids = task_chain_ids(self.db, task_id)
        known = self._known_ids(change_id, task_id) if change_id else {
            "known_requirement_ids": set(), "known_acceptance_ids": set(), "known_invariant_ids": set(), "known_test_case_ids": set()}
        context_lines = self.task_execution_context_builder.render_lines(task_id, change_id) if change_id else []
        round_number = self._round_number(chain_ids, review_kind)
        prior_open = self.findings_store.list_for_task(chain_ids, "OPEN") if round_number else []

        base_prompt_parts = [self.preamble, "", f"## TASK: {t['title']}", (t.get("description") or "").strip(),
                              "", "## GOVERNING CONTEXT", *context_lines,
                              "", "## CHANGED FILES", *[f"- {f}" for f in changed_files]]
        if prior_open:
            base_prompt_parts += ["", "## PREVIOUSLY OPEN FINDINGS (confirm resolved, or re-raise)",
                                   *[f"- [{f['category']}/{f['severity']}] {f['title']}: {f['description']}" for f in prior_open]]
        if not complete:
            base_prompt_parts += ["", "## NOTE", "Some changed files could not be diffed -- review what is provided; treat missing coverage as REVIEW_CONTEXT_INCOMPLETE, not PASS."]

        parsed_results = []
        for chunk in diffs:
            prompt = "\n".join(base_prompt_parts + ["", f"## DIFF ({', '.join(chunk['files'])})", "```diff", chunk["diff"], "```"])
            try:
                raw = self.invoker.invoke(provider, prompt, REVIEW_JSON_SCHEMA, ws["worktree_path"])
                parsed = json.loads(raw)
            except Exception as exc:
                self.db.event("task", task_id, f"{review_kind}_REVIEW_FAILED", str(exc)[:500])
                return {"outcome": "EXECUTION_FAILED", "verdict": None, "message": str(exc)}
            problems = self.validator.validate(parsed, changed_files=changed_files, **known)
            if problems:
                self.db.event("task", task_id, f"{review_kind}_REVIEW_OUTPUT_INVALID", "; ".join(problems)[:500])
                return {"outcome": "REVIEW_OUTPUT_INVALID", "verdict": None, "message": problems}
            parsed_results.append(parsed)

        verdict = aggregate_verdict([p["verdict"] for p in parsed_results])
        if not complete:
            verdict = aggregate_verdict([verdict, "FIX_REQUIRED"])  # never silently PASS an incomplete review
        all_findings = [f for p in parsed_results for f in p.get("findings") or []]
        all_hds = [hd for p in parsed_results for hd in p.get("human_decisions") or []]
        summary = " | ".join(p.get("summary", "") for p in parsed_results if p.get("summary"))

        baselines = self._baseline_wp_ids(change_id) if change_id else {"spec": None, "design": None}
        code_change_wp = self.db.one("SELECT id FROM work_products WHERE task_id=? AND kind='CODE_CHANGE' ORDER BY id DESC LIMIT 1", (task_id,))

        wp_id = self.work_products.create(
            kind="REVIEW_REPORT" if review_kind == "CODE" else "SECURITY_REVIEW",
            title=f"{review_kind.title()} Review: {t['title']} ({head_commit[:8]})",
            change_id=change_id, task_id=task_id, status="DRAFT",
            content_metadata={"verdict": verdict, "summary": summary, "findings": all_findings,
                               "complete": complete, "round": round_number})
        review_id = self.db.execute(
            "INSERT INTO review_runs(task_id,workspace_id,reviewer_type,reviewer_agent,reviewed_commit,status,findings,"
            "completed_at,review_kind,verdict,provider,base_commit,worktree_id,code_change_work_product_id,"
            "spec_baseline_work_product_id,design_baseline_work_product_id,work_product_id,independence_note,round_number) "
            "VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, ws["id"], f"{review_kind}_REVIEW_AI", provider, head_commit, verdict, summary,
             review_kind, verdict, provider, base_commit, ws["id"], code_change_wp["id"] if code_change_wp else None,
             baselines["spec"], baselines["design"], wp_id, "SAME_PROVIDER_INDEPENDENT_CONTEXT", round_number))

        # E9.11/E9.17: a re-review's own set of currently-reported
        # findings is the truth about what's STILL open at this new
        # head commit -- every previously-OPEN finding not re-raised
        # below is auto-resolved (SUPERSEDED, with the new commit as
        # its evidence) before persisting whatever's still reported.
        # "Not silently closed" is satisfied because this only ever
        # fires for a genuinely NEW head commit (a real fix attempt),
        # never a re-run against the same commit, and the resolution
        # always carries the commit sha as its resolution_reference.
        if round_number:
            self.findings_store.auto_resolve_stale(chain_ids, head_commit, f"re-reviewed at {review_kind} round {round_number}")

        finding_ids = []
        for f in all_findings:
            fid = self.findings_store.create_or_dedupe(
                change_id=change_id, task_id=task_id, review_id=review_id,
                category=f["category"], severity=f["severity"], title=f["title"], description=f.get("description", ""),
                file_path=f.get("file_path"), line_start=f.get("line_start"), line_end=f.get("line_end"),
                requirement_ids=f.get("requirement_ids"), acceptance_ids=f.get("acceptance_ids"),
                invariant_ids=f.get("invariant_ids"), test_case_ids=f.get("test_case_ids"), dedupe_task_ids=chain_ids)
            finding_ids.append(fid)

        hd_ids = []
        if verdict == "HUMAN_DECISION_REQUIRED" and change_id:
            for hd in all_hds:
                hd_ids.append(self.human_decisions.create(
                    "work_product", wp_id, hd.get("question") or "", hd.get("reason") or "", "NONE"))

        self.db.event("task", task_id, f"{review_kind}_REVIEW_COMPLETED", f"review={review_id} verdict={verdict} findings={len(finding_ids)}")
        return {"outcome": "REVIEWED", "verdict": verdict, "review_id": review_id, "work_product_id": wp_id,
                "finding_ids": finding_ids, "human_decision_ids": hd_ids, "complete": complete, "reviewed_commit": head_commit}
