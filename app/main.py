from __future__ import annotations
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from fastapi import FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.config import load_settings
from app.db import Database
from app.repositories import discover_repositories
from app.services.git_workspace import GitWorkspaceService, GitSafetyError, GitCommandError, slugify
from app.services.test_runner import TestRunner
from app.services.terminal_launcher import TerminalLauncherService, LauncherError
from app.services.cleanup_worker import CleanupWorker
from app.services.port_allocator import PortAllocatorService
from app.services.sandbox_contract import (
    SandboxContractError, hardware_build_command, load_sandbox_contract, resolve_profile,
)
from app.services.sandbox_manager import SandboxError, SandboxManager, SourceSpec
from app.services.sandbox_runtime import SandboxRuntimeService
from app.services.agent_session_manager import AgentSessionManager, SessionError
from app.services.task_decision_service import TaskDecisionService, RISK_PROFILES as TDS_RISK_PROFILES, effective_task_prompt, prompt_source, LIVE_SESSION_STATUSES
from app.services.user_state_view import user_task_state, progress_summary, humanize_enum
from app.services.gate_waiver_service import GateWaiverError, GateWaiverService
from app.services.github_merge_service import GitHubIntegrationError, GitHubMergeService, MERGED_STATES
from app.services.operations import OperationInProgress, OperationService
from app.services.deployment_service import DeploymentService, DeploymentError
from app.services.deployment_decision import deployment_view
from app.services.repository_contract_editor import RepositoryContractEditor, ContractEditError
from app.services.completion_report_parser import parse_completion_report, strip_ansi

def create_app(settings=None):
    settings = settings or load_settings(); db = Database(settings.db_path); db.init()
    git = GitWorkspaceService(settings.root, worktree_root=settings.worktree_root); runner = TestRunner(db, git); launcher = TerminalLauncherService(settings, git)
    sandbox_runtime = SandboxRuntimeService(); ports = PortAllocatorService(db)
    sandboxes = SandboxManager(db, sandbox_runtime, ports, settings.state_dir, settings.max_running_sandboxes, settings.sandbox_retention_hours)
    cleanup_worker = CleanupWorker(db, sandboxes, settings.cleanup_poll_seconds)
    agent_sessions = AgentSessionManager(db)
    decision = TaskDecisionService(db, git)
    gate_waivers = GateWaiverService(db, git)
    github_merge = GitHubMergeService()
    ops = OperationService(db)
    deployer = DeploymentService(db, git)
    contract_editor = RepositoryContractEditor(git)
    app = FastAPI(title="ProjectFlow Workspace Manager", docs_url=None, redoc_url=None)
    base = Path(__file__).parent; templates = Jinja2Templates(directory=base / "templates")
    templates.env.filters["humanize"] = humanize_enum
    app.mount("/static", StaticFiles(directory=base / "static"), name="static")
    app.state.settings, app.state.db, app.state.git, app.state.runner, app.state.launcher = settings, db, git, runner, launcher
    app.state.sandboxes, app.state.ports, app.state.sandbox_runtime, app.state.cleanup_worker = sandboxes, ports, sandbox_runtime, cleanup_worker
    app.state.agent_sessions = agent_sessions
    app.state.decision = decision
    app.state.gate_waivers = gate_waivers
    app.state.github_merge = github_merge
    app.state.deployer = deployer
    app.state.ops = ops
    app.state.contract_editor = contract_editor
    cleanup_worker.start(); agent_sessions.reconcile_on_startup()
    def render(request, name, **ctx): return templates.TemplateResponse(request=request, name=name, context={"settings": settings, **ctx})
    def repo(repo_id):
        row = db.one("SELECT * FROM repositories WHERE id=? AND enabled=1", (repo_id,))
        if not row: raise HTTPException(404, "Enabled repository not found")
        git.validate_repo(row["repo_path"]); return row
    def agent_row(wid):
        row=db.one("SELECT w.*,r.repo_name,r.repo_path FROM agent_workspaces w JOIN repositories r ON r.id=w.repository_id WHERE w.id=?",(wid,))
        if not row: raise HTTPException(404); return row
        return row
    def integration_row(iid):
        row=db.one("SELECT i.*,r.repo_name,r.repo_path FROM integration_workspaces i JOIN repositories r ON r.id=i.repository_id WHERE i.id=?",(iid,))
        if not row: raise HTTPException(404)
        return row
    def invalidate(iid): db.execute("UPDATE integration_workspaces SET ready_for_main=0,verified_commit=NULL,verified_at=NULL,status='TESTING',updated_at=CURRENT_TIMESTAMP WHERE id=?",(iid,))
    TEST_RUN_LABELS={"QUEUED":"Starting tests...","RUNNING":"Running tests...","PASS":"Tests PASS","FAIL":"Tests FAILED","TIMEOUT":"Tests timed out","SKIPPED":"Tests skipped"}
    OPERATION_RUNNING_LABELS={"MERGE_LATEST":"Merging latest changes...","PUSH_INTEGRATION":"Pushing...","MARK_READY_FOR_MAIN":"Validating readiness...","CREATE_PR":"Creating PR...","MERGE_PR":"Merging..."}
    def integration_current_action(iid):
        """Button-state-ux section 6: whichever action on this Integration
        is most worth surfacing right now -- a currently RUNNING one if
        any (test run and/or a tracked Operation), else the most recent
        terminal result of any kind. Never a client-side guess: every
        field here comes straight from test_runs/operations."""
        candidates=[]
        tr=db.one("SELECT * FROM test_runs WHERE workspace_type='integration' AND workspace_id=? ORDER BY id DESC LIMIT 1",(iid,))
        if tr:
            running=tr["status"] in ("QUEUED","RUNNING")
            candidates.append({"kind":"Run Tests","started_at":tr["started_at"] or tr["finished_at"] or "","is_running":running,
                                "label":TEST_RUN_LABELS.get(tr["status"],tr["status"]),"is_failed":tr["status"] in ("FAIL","TIMEOUT"),
                                "is_succeeded":tr["status"]=="PASS","url":f"/test-runs/{tr['id']}/log"})
        for optype,kind in (("MERGE_LATEST","Merge Latest Changes"),("PUSH_INTEGRATION","Push Integration Branch"),("MARK_READY_FOR_MAIN","Mark Ready for Main")):
            op=ops.latest("integration",iid,optype)
            if not op: continue
            running=op["status"] in ("QUEUED","RUNNING")
            label=OPERATION_RUNNING_LABELS[optype] if running else (op["result_summary"] if op["status"]=="SUCCEEDED" else (op["error"] or "Failed"))
            candidates.append({"kind":kind,"started_at":op["started_at"] or "","is_running":running,"label":label,
                                "is_failed":op["status"]=="FAILED","is_succeeded":op["status"]=="SUCCEEDED","url":None})
        if not candidates: return None
        running=[c for c in candidates if c["is_running"]]
        return max(running or candidates,key=lambda c:c["started_at"])
    def safe_details(worktree_path):
        path=Path(worktree_path)
        empty={"head":None,"status":[],"modified":[],"untracked":[],"commits":[]}
        if not path.exists(): return empty
        try: return git.details(path)
        except GitSafetyError: return empty
    def task_row(tid):
        row=db.one("SELECT * FROM tasks WHERE id=?",(tid,))
        if not row: raise HTTPException(404,"Task not found")
        return row
    def sandbox_row(sid):
        row=db.one("SELECT s.*,r.repo_name FROM sandboxes s LEFT JOIN repositories r ON r.id=s.repository_id WHERE s.id=?",(sid,))
        if not row: raise HTTPException(404,"Sandbox not found")
        return row
    def task_workspaces(tid): return db.all("SELECT w.*,r.repo_name,r.repo_path FROM agent_workspaces w JOIN repositories r ON r.id=w.repository_id WHERE w.task_id=? ORDER BY w.created_at",(tid,))
    def task_sandboxes(tid): return db.all("SELECT * FROM sandboxes WHERE task_id=? ORDER BY id",(tid,))
    def task_integration_row(task_id):
        return db.one("SELECT * FROM task_integrations WHERE task_id=? ORDER BY id DESC LIMIT 1",(task_id,))
    def format_countdown(iso):
        """Single place that turns a cleanup_eligible_at timestamp into a
        human countdown -- reused by /sandboxes, /sandboxes/{id} and
        /tasks/{id} so the three views can never drift on the wording."""
        if not iso: return None
        try:
            secs=int((datetime.fromisoformat(iso)-datetime.now(timezone.utc)).total_seconds())
        except Exception: return None
        return "due" if secs<=0 else f"{secs//3600}h {secs%3600//60}m"
    def sandbox_view(sb):
        """The one place that turns a `sandboxes` row into everything a
        template needs to render it (outputs/ports/countdown/open URLs) --
        called by /sandboxes, /sandboxes/{id} and /tasks/{id} so all three
        views read the exact same Sandbox/SandboxPort/output state."""
        outputs=sandboxes.outputs(sb["id"]); ports_=ports.ports_for(sb["id"])
        primary=db.one("SELECT s.*,r.repo_name FROM sandbox_sources s JOIN repositories r ON r.id=s.repository_id WHERE s.sandbox_id=? ORDER BY s.id LIMIT 1",(sb["id"],))
        return {"row":sb,"outputs":outputs,"ports":ports_,"primary_source":primary,
                "cleanup_countdown":format_countdown(sb["cleanup_eligible_at"]),
                "backend_url":outputs.get("backend_url"),"frontend_url":outputs.get("frontend_url"),
                "hardware_api_url":outputs.get("hardware_api_url") or outputs.get("lan_url")}
    def sandbox_current_commits(sandbox_id):
        cur={}
        for s in db.all("SELECT * FROM sandbox_sources WHERE sandbox_id=?",(sandbox_id,)):
            try: cur[s["repository_id"]]=git.head(s["worktree_path"])
            except Exception: pass
        return cur
    def integration_readiness(iid):
        """The single readiness computation for one repo's Integration
        Workspace -- backs BOTH the POST ready-for-main gate and the
        Task Detail readiness checklist, so the template never derives a
        second, independently-calculated status. Test-gate PASS/FAIL
        (including baseline-waiver awareness, section 16-17) is delegated
        to TaskDecisionService.integration_gate_status() -- the one
        authoritative place that decision lives, never recomputed here."""
        i=integration_row(iid)
        clean=not git.status(i["worktree_path"]).strip(); no_conflicts=not git.conflict_files(i["worktree_path"])
        sources_current=True
        for s in db.all("SELECT s.*,w.branch FROM integration_sources s JOIN agent_workspaces w ON w.id=s.workspace_id WHERE s.integration_id=?",(iid,)):
            current=git.head(i["repo_path"],s["branch"])
            if current!=s["merged_commit"] or not git.is_ancestor(i["worktree_path"],current): sources_current=False
        ti=db.one("SELECT task_id FROM task_integrations WHERE id=?",(i["task_integration_id"],)) if i["task_integration_id"] else None
        gate=decision.integration_gate_status(i,ti["task_id"] if ti else None)
        head=gate["head"] or git.head(i["worktree_path"])
        return {"integration":i,"head":head,"clean":clean,"no_conflicts":no_conflicts,"sources_current":sources_current,
                "tests_pass":gate["tests_pass"],"tests_status":gate["tests_status"],"failures":gate["failures"],
                "tests_passed":gate.get("tests_passed",0),"tests_required":gate["tests_required"],"summary":gate.get("summary") or {},
                "ready":clean and no_conflicts and sources_current and gate["tests_pass"]}
    def sandbox_for_workspace(wid):
        return db.one("SELECT * FROM sandboxes WHERE owner_type='AGENT_WORKSPACE' AND owner_id=? ORDER BY id DESC LIMIT 1",(wid,))
    def latest_test_status(workspace_type,workspace_id):
        row=db.one("SELECT status FROM test_runs WHERE workspace_type=? AND workspace_id=? ORDER BY id DESC LIMIT 1",(workspace_type,workspace_id))
        return row["status"] if row else "NOT_RUN"
    def manual_verification_status(sandbox_id,worktree_path):
        """Latest manual verification for a sandbox. Staleness is never
        stored -- recomputed here by comparing its recorded source_commit
        to the source branch's CURRENT git HEAD, same rule as sandbox
        staleness. A PASS never survives new commits on the branch."""
        row=db.one("SELECT * FROM manual_verifications WHERE sandbox_id=? ORDER BY id DESC LIMIT 1",(sandbox_id,))
        if not row: return {"status":"NOT_RUN","row":None}
        try: current=git.head(worktree_path)
        except Exception: current=None
        stale=current is not None and current!=row["source_commit"]
        return {"status":"STALE" if stale else row["result"],"row":row}
    def workspace_readiness(w):
        """The one place that reads a workspace's four independent
        verification signals -- sandbox, automated tests, manual
        verification, sandbox-contract presence -- so workspace_detail,
        task_detail and next_action_code never compute a conflicting
        answer for the same workspace."""
        sb=sandbox_for_workspace(w["id"])
        try: configured=load_sandbox_contract(Path(w["repo_path"])) is not None
        except SandboxContractError: configured=True  # misconfigured contract is not "absent"
        manual={"status":"NOT_RUN","row":None}; sb_view=None
        if sb:
            sb_view=sandbox_view(sb)
            manual=manual_verification_status(sb["id"],w["worktree_path"])
        # sandbox_configured = the repo declares a sandbox: contract at all
        # (drives the "not configured for this repository" empty state).
        # sandbox_required = this workspace actually needs a RUNNING
        # sandbox to be considered testable -- false when the user
        # explicitly chose profile NONE, even on a repo that has a
        # contract, so a deliberate no-runtime workspace is never treated
        # as perpetually blocked on "Create Sandbox".
        required=configured and w.get("sandbox_profile")!="NONE"
        return {"agent_ready":w["status"]=="READY","sandbox":sb,"sandbox_view":sb_view,
                "sandbox_configured":configured,"sandbox_required":required,"sandbox_status":sb["status"] if sb else "NOT_CREATED",
                "automated_status":latest_test_status("agent",w["id"]),"manual":manual}
    def task_verification(tid): return db.one("SELECT * FROM verification_reports WHERE task_id=? AND workspace_id IS NULL ORDER BY id DESC LIMIT 1",(tid,))
    def workspace_verification(wid): return db.one("SELECT * FROM verification_reports WHERE workspace_id=? ORDER BY id DESC LIMIT 1",(wid,))
    def effective_verification(tid,workspaces):
        """Task-level note wins; for the common single-workspace task, the
        one workspace's own report stands in for the task's -- never
        invented, only ever what an agent/user actually recorded."""
        tv=task_verification(tid) if tid else None
        if tv: return tv
        if len(workspaces)==1: return workspace_verification(workspaces[0]["id"])
        return None

    NEXT_ACTION_COPY={
        "MARK_READY":("Agent chưa Mark Ready — code chưa được coi là hoàn thành.",None,None),
        "NO_SANDBOX_CONTRACT_WAIT":("Repo này không có sandbox: contract. Verify thủ công qua worktree, sau đó theo dõi Integration.",None,None),
        "NO_SANDBOX_CONTRACT_INTEGRATE":("Repo này không có sandbox: contract. Verify thủ công qua worktree, sau đó tích hợp.","Create Integration","integration"),
        "CREATE_SANDBOX":("Code đã được agent hoàn thành nhưng chưa có runtime verification.","Create Sandbox","create-sandbox"),
        "SANDBOX_FAILED":("Sandbox tạo thất bại.","Xem Logs","sandbox"),
        "SANDBOX_STARTING":("Sandbox đang khởi động, chờ vài giây rồi tải lại.",None,None),
        "SANDBOX_NOT_RUNNING":("Sandbox không chạy.","Start Sandbox","sandbox"),
        "RUN_TESTS":("Sandbox đã sẵn sàng — nên chạy automated tests trước khi kiểm tra thủ công.","Run Tests","run-tests"),
        "TESTS_FAILING":("Automated tests đang FAIL.","Xem log test","tests"),
        "OPEN_APP_VERIFY":("Automated tests PASS — mở app và kiểm tra thủ công theo hướng dẫn.","Open App and Verify","open-app"),
        "MANUAL_FAILING":("Kiểm tra thủ công FAIL — quay lại agent workspace để sửa.",None,None),
        "VERIFIED_STANDALONE":("Agent code đã được verify (sandbox + test).","Create Integration","legacy-integration"),
        "CREATE_INTEGRATION":("Đã verify — sẵn sàng tích hợp.","Create Integration","integration"),
        "VIEW_INTEGRATION":("Integration đang chạy — còn điều kiện chưa đạt, xem Integration Readiness.","Xem Integration","integration"),
        "PREPARE_PR":("Mọi điều kiện đã đạt.","Prepare PR (push + tạo Pull Request)","github-flow"),
    }
    def next_action_code(r,integration_exists=None,ready_for_main=False):
        """Pure state -> action mapping. Deterministic, no AI: same inputs
        always produce the same recommendation."""
        if not r["agent_ready"]: return "MARK_READY"
        if not r.get("sandbox_required",r["sandbox_configured"]):
            if integration_exists is None: return "NO_SANDBOX_CONTRACT_WAIT"
            return "VIEW_INTEGRATION" if integration_exists else "NO_SANDBOX_CONTRACT_INTEGRATE"
        st=r["sandbox_status"]
        if st=="NOT_CREATED": return "CREATE_SANDBOX"
        if st=="FAILED": return "SANDBOX_FAILED"
        if st in ("CREATED","PROVISIONING","STARTING"): return "SANDBOX_STARTING"
        # CLEANUP_ELIGIBLE is a retention countdown, not a runtime state --
        # the real container is still up and usable for the whole grace
        # window (state-consistency audit finding: never conflate the two).
        if st not in ("RUNNING","CLEANUP_ELIGIBLE"): return "SANDBOX_NOT_RUNNING"
        if r["automated_status"]=="NOT_RUN": return "RUN_TESTS"
        if r["automated_status"]=="FAIL": return "TESTS_FAILING"
        if r["manual"]["status"] in ("NOT_RUN","STALE"): return "OPEN_APP_VERIFY"
        if r["manual"]["status"]=="FAIL": return "MANUAL_FAILING"
        if integration_exists is None: return "VERIFIED_STANDALONE"
        if not integration_exists: return "CREATE_INTEGRATION"
        if not ready_for_main: return "VIEW_INTEGRATION"
        return "PREPARE_PR"
    def resolve_next_action(code,*,wid=None,tid=None,sandbox_id=None):
        """Turns a symbolic action tag into a real href/method for the
        context it is shown in (workspace page vs task page)."""
        text,label,tag=NEXT_ACTION_COPY[code]
        href=None; method="GET"
        if tag=="create-sandbox":
            href=f"/api/tasks/{tid}/workspaces/{wid}/create-sandbox" if tid else f"/api/workspaces/{wid}/create-sandbox"; method="POST"
        elif tag=="sandbox" and sandbox_id: href=f"/sandboxes/{sandbox_id}"
        elif tag=="run-tests" and wid: href=f"/api/workspaces/{wid}/test"; method="POST"
        elif tag=="tests" and wid: href=f"/workspaces/{wid}"
        elif tag=="open-app" and sandbox_id: href=f"/sandboxes/{sandbox_id}"
        elif tag=="integration" and tid: href=f"/tasks/{tid}"
        elif tag=="legacy-integration": href="/integrations"
        elif tag=="github-flow": href="/help#github-flow"
        return {"code":code,"text":text,"label":label,"href":href,"method":method}

    # ---------------------------------------------------- Control plane
    RISK_PROFILES=("LOW","NORMAL","HIGH")
    RISK_GATES={"LOW":("REVIEW",),"NORMAL":("REVIEW","INTEGRATION"),"HIGH":("REVIEW","QA","INTEGRATION")}
    def requires_qa(risk_profile): return "QA" in RISK_GATES.get(risk_profile,RISK_GATES["NORMAL"])
    def latest_session_for_workspace(wid):
        return db.one("SELECT * FROM agent_sessions WHERE workspace_id=? ORDER BY id DESC LIMIT 1",(wid,))
    def activity_summary(sid, max_len=200):
        """One clean, human-readable line of 'what is this session doing
        right now' -- reads the LIVE in-process buffer (never the
        possibly-stale/empty DB transcript_tail column, which is only
        refreshed on WS disconnect) and strips ANSI/control sequences
        before ever rendering it outside the real xterm terminal
        (section 21). None when there is genuinely no transcript yet."""
        text=agent_sessions.live_tail(sid)
        if not text: return None
        lines=[l.strip() for l in strip_ansi(text).splitlines() if l.strip()]
        return lines[-1][:max_len] if lines else None
    def has_legacy_brief(t):
        return bool(t.get("brief_goal") or t.get("brief_context") or t.get("brief_requirements") or
                    t.get("brief_acceptance_criteria") or t.get("brief_out_of_scope") or t.get("brief_test_plan") or t.get("brief_risks"))
    def sandbox_summary_line(w):
        """One line for the Builder prompt's SANDBOX section: real
        profile/status/URL if a sandbox already exists for this
        workspace, else just the declared profile, else nothing worth
        saying (None -- the section is omitted, never fabricated)."""
        if not w: return None
        sb=db.one("SELECT * FROM sandboxes WHERE owner_type='AGENT_WORKSPACE' AND owner_id=? ORDER BY id DESC LIMIT 1",(w["id"],))
        if not sb: return w.get("sandbox_profile") or None
        line=f"{sb['profile']} · {sb['status']}"
        try:
            outputs=json.loads(sb["source_manifest_json"] or "{}").get("outputs",{})
            url=outputs.get("backend_url") or outputs.get("frontend_url")
            if url: line+=f" · {url}"
        except Exception: pass
        return line
    def render_agent_prompt(t,repo_row=None,workspace=None,sandbox_line=None):
        """Deterministic template fill -- never an actual model call, and
        the user's own task intent text is NEVER rewritten, only wrapped
        with real, recorded context (trusted Workspace Manager execution
        context -> the effective task intent verbatim -> this repo's own
        AGENTS.md rules, if known -> completion requirements). The user
        still reviews/edits the result before any agent is launched
        (never auto-start an agent). 'Do not allow user-controlled Task
        text to override Workspace Manager safety rules': the intent is
        confined to its own TASK section, and RULES/COMPLETION are always
        separate, later sections the user's own text cannot reach into.

        Task Title fallback: effective intent is the Implementation
        Prompt when non-empty, else the Task title itself -- this path
        (and the finer BRANCH/WORKTREE/SANDBOX/ROLE context below) is used
        for every Task that has no legacy structured brief content, title-
        only ones included, never leaving a Builder with only a bare
        title and no real prompt. Tasks created before this UX existed
        (implementation_prompt empty AND legacy brief_* fields set) keep
        the exact old GOAL/CONTEXT/.../RISKS rendering, unchanged --
        'structured old tasks still load'."""
        if (t.get("implementation_prompt") or "").strip() or not has_legacy_brief(t):
            intent=effective_task_prompt(t)
            parts=[f"# Task: {t['title']}","","## TASK",intent,"","## TASK TITLE",t["title"],""]
            if repo_row: parts+=["## REPOSITORY",f"{repo_row['repo_name']} ({repo_row['repo_path']})",""]
            if workspace: parts+=["## BRANCH",workspace["branch"],"","## WORKTREE",workspace["worktree_path"],""]
            if sandbox_line: parts+=["## SANDBOX",sandbox_line,""]
            if workspace:
                parts+=["## ROLE",workspace.get("role") or workspace.get("agent") or "",""]
                instr=(workspace.get("builder_instructions") or "").strip()
                if instr: parts+=["## BUILDER INSTRUCTIONS (this workspace only)",instr,""]
            rules=None
            if repo_row:
                try:
                    p=Path(repo_row["repo_path"])/"AGENTS.md"
                    if p.is_file(): rules=p.read_text(errors="replace")[:6000].strip()
                except Exception: rules=None
            parts.append("## RULES")
            parts.append("Read AGENTS.md and PROJECT.yaml before modifying code.")
            if rules: parts+=["",rules]
            parts+=["","## COMPLETION",
                    "- implement task","- run appropriate tests","- review diff","- commit changes",
                    "- report WHAT_CHANGED","- report TESTS_RUN","- report HOW_TO_VERIFY","- report EXPECTED_RESULT","- report RISKS",
                    "- Submit for Review (use the format in templates/agent-completion-report.md)."]
            return "\n".join(parts)
        parts=[f"# Task: {t['title']}",""]
        if t["brief_goal"]: parts+=["## GOAL",t["brief_goal"],""]
        if t["brief_context"]: parts+=["## CONTEXT",t["brief_context"],""]
        if t["brief_requirements"]: parts+=["## REQUIREMENTS",t["brief_requirements"],""]
        if t["brief_acceptance_criteria"]: parts+=["## ACCEPTANCE_CRITERIA",t["brief_acceptance_criteria"],""]
        if t["brief_out_of_scope"]: parts+=["## OUT_OF_SCOPE",t["brief_out_of_scope"],""]
        if t["brief_test_plan"]: parts+=["## TEST_PLAN",t["brief_test_plan"],""]
        if t["brief_risks"]: parts+=["## RISKS",t["brief_risks"],""]
        parts.append("When your source change is complete, report back using the format in templates/agent-completion-report.md.")
        return "\n".join(parts)
    def regenerate_agent_prompt(tid,repo_row=None):
        """Recompute the derived, composed TASK-LEVEL agent_prompt (no
        specific Builder Workspace yet) and persist it as a new `prompts`
        row stamped with the exact brief_version it was generated from --
        same discipline generate_prompt already established, just
        callable from anywhere a Task's prompt-relevant state just
        changed (create, prompt edit). Once a Builder Workspace exists,
        its own live per-workspace prompt (role/instructions/sandbox
        included) is what actually matters -- see workspace_agent_prompt()."""
        t=task_row(tid); prompt=render_agent_prompt(t,repo_row)
        db.execute("INSERT INTO prompts(task_id,prompt_type,brief_version,content) VALUES(?,?,?,?)",(tid,"BUILDER",t["brief_version"],prompt))
        db.execute("UPDATE tasks SET agent_prompt=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(prompt,tid))
        db.event("task",tid,"PROMPT_GENERATED",f"brief_version={t['brief_version']}")
        return prompt
    def workspace_agent_prompt(w,t=None,repo_row=None,note_exited_without_report=False):
        """Live per-Builder-Workspace effective prompt -- always freshly
        computed (never stored/cached), so a role/instructions/sandbox
        edit is reflected immediately with no separate staleness to
        track. Only Review/QA evidence is version-pinned; this display
        text is not.

        Return to Builder (spec section 11): when the reviewer most
        recently returned FIX_REQUIRED for this exact workspace, its
        findings are appended as their own section so the next Builder
        session's prompt is the original task intent + what the
        reviewer asked to change -- never a bare 'resume' with no
        context. A PASS/STALE/no-review workspace gets no such section.

        `note_exited_without_report` (section 6) is decided by the
        caller, not re-derived here -- by the time a resumed session's
        prompt is being computed, a brand-new AgentSession row already
        exists and is "the latest session", so re-querying it here would
        always see the wrong (new) one."""
        t=t or task_row(w["task_id"])
        if repo_row is None:
            try: repo_row=repo(w["repository_id"])
            except HTTPException: repo_row=None
        prompt=render_agent_prompt(t,repo_row,workspace=w,sandbox_line=sandbox_summary_line(w))
        review=decision.latest_review(w["id"])
        if review and review["status"]=="FIX_REQUIRED":
            findings=(review["findings"] or "").strip() or "(no written findings)"
            prompt+="\n\n## REVIEW FINDINGS (fix required -- address these, then Submit for Review again)\n"+findings+"\n"
        # EXITED-without-report resume (section 6): the previous process
        # ended without ever getting a completion report into
        # verification_reports -- tell the freshly-resumed agent exactly
        # that, so it inspects what's already there instead of quietly
        # redoing finished work or re-reading the whole task as if this
        # were a first attempt.
        if note_exited_without_report and not (review and review["status"]=="FIX_REQUIRED"):
            prompt+=("\n\n## PREVIOUS SESSION ENDED WITHOUT A COMPLETION REPORT\n"
                      "Your previous session exited without a persisted completion report.\n"
                      "Please inspect the existing workspace state and submit the required completion report for the current HEAD.\n"
                      "Do not redo completed work unnecessarily.\n")
        return prompt
    def render_review_prompt(t,w,report):
        """Deterministic template (section 6): the Task's own prompt/brief,
        source branch/commit, and the Builder's own completion report --
        never a diff computed by an LLM, only real recorded facts."""
        head=git.head(w["worktree_path"])
        parts=[f"# Review: {t['title']}",f"Branch: {w['branch']} @ {head[:12]}",""]
        if (t.get("implementation_prompt") or "").strip() or not has_legacy_brief(t): parts+=["## TASK PROMPT",effective_task_prompt(t),""]
        elif t["brief_acceptance_criteria"]: parts+=["## ACCEPTANCE_CRITERIA",t["brief_acceptance_criteria"],""]
        if report:
            parts+=["## Builder report","WHAT_CHANGED: "+ (report["what_changed"] or "—"),"FILES_CHANGED: "+(report["files_changed"] or "—"),
                     "TESTS_RUN: "+(report["tests_run"] or report["automated_tests"] or "—"),"RISKS: "+(report["risks"] or "—"),""]
        else:
            parts+=["## Builder report","(no completion report submitted yet)",""]
        parts.append("Evaluate correctness, completeness, scope, regressions, missing tests, and unsafe/unexpected changes. Report REVIEW_PASS, FIX_REQUIRED, or BLOCKED. Do not modify source unless explicitly switched to repair mode.")
        return "\n".join(parts)

    @app.exception_handler(GitSafetyError)
    @app.exception_handler(GitCommandError)
    async def git_error(request, exc): return HTMLResponse(f"<h1>Action blocked</h1><pre>{str(exc)}</pre><a href='javascript:history.back()'>Back</a>", status_code=409)

    @app.exception_handler(SandboxError)
    @app.exception_handler(SandboxContractError)
    async def sandbox_error(request, exc):
        code = getattr(exc, "code", "SANDBOX_ERROR")
        if request.url.path.startswith("/api/"): return JSONResponse({"ok":False,"code":code,"message":str(exc)},status_code=409)
        return HTMLResponse(f"<h1>Sandbox action blocked</h1><pre>{code}: {str(exc)}</pre><a href='javascript:history.back()'>Back</a>", status_code=409)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        agents=db.all("SELECT w.*,r.repo_name FROM agent_workspaces w JOIN repositories r ON r.id=w.repository_id WHERE w.status NOT IN ('CLOSED','DONE') ORDER BY w.updated_at DESC")
        ints=db.all("SELECT i.*,r.repo_name FROM integration_workspaces i JOIN repositories r ON r.id=i.repository_id WHERE i.status!='CLOSED' ORDER BY i.updated_at DESC")
        running_sandboxes=sandboxes.running_count()
        cleanup_pending=db.one("SELECT COUNT(*) n FROM sandboxes WHERE status='CLEANUP_ELIGIBLE'")["n"]
        # Dashboard is Task-centric (section 48): every count below comes
        # from TaskDecisionService.evaluate() (via task_card_view), the
        # same source Kanban/List/Detail use -- never a raw worktree
        # count standing in for Task state, never a second dashboard-only
        # tally that could drift from the real gate engine.
        task_rows=db.all("SELECT * FROM tasks WHERE status!='CANCELLED'")
        cards=[task_card_view(t) for t in task_rows]
        by_status={s:sum(1 for c in cards if c["status"]==s) for s in ("BACKLOG","ACTIVE","BLOCKED","READY_FOR_MAIN","DONE")}
        summary={"active":len(agents),"ready":sum(x["status"]=="READY" for x in agents),"testing":sum(x["status"]=="TESTING" for x in ints),"main":sum(bool(x["ready_for_main"]) for x in ints),
                 "tasks":len(task_rows),"sandboxes":running_sandboxes,"cleanup":cleanup_pending,
                 "backlog":by_status["BACKLOG"],"tasks_active":by_status["ACTIVE"],"blocked":by_status["BLOCKED"],
                 "tasks_ready_for_main":by_status["READY_FOR_MAIN"],"done_recent":by_status["DONE"],
                 "builders_running":sum(1 for c in cards for w in c["workspaces"] if not w["ready"]),
                 "review_pending":sum(1 for c in cards if c["decision"]["next_action"]["action"] in ("SUBMIT_FOR_REVIEW","START_REVIEW")),
                 "qa_pending":sum(1 for c in cards if c["decision"]["next_action"]["action"]=="START_QA"),
                 "integrations_running":sum(1 for c in cards if c["stage"] in ("INTEGRATION","MERGING")),
                 "tasks_fix_required":sum(1 for c in cards if c["needs_fix"])}
        return render(request,"dashboard.html",agents=agents,integrations=ints,summary=summary,cards=cards,first_run=not db.one("SELECT id FROM repositories LIMIT 1"))

    @app.get("/help", response_class=HTMLResponse)
    def help_page(request: Request): return render(request, "help.html")

    @app.get("/repositories", response_class=HTMLResponse)
    def repositories(request: Request): return render(request,"repositories.html",repositories=db.all("SELECT * FROM repositories ORDER BY repo_name"),discovered=discover_repositories(settings.root))
    @app.post("/api/repositories")
    def register(repo_path: str=Form(...), repo_name: str=Form(""), default_branch: str=Form("main")):
        path=git.validate_repo(repo_path); default_branch=git.validate_branch(default_branch)
        if not git.base_exists(path,default_branch): raise GitSafetyError("Default branch missing")
        db.execute("INSERT INTO repositories(repo_name,repo_path,default_branch) VALUES(?,?,?) ON CONFLICT(repo_path) DO UPDATE SET enabled=1,repo_name=excluded.repo_name,default_branch=excluded.default_branch",(slugify(repo_name or path.name),str(path),default_branch))
        return RedirectResponse("/repositories",303)
    @app.get("/api/repositories")
    def api_repos(): return db.all("SELECT * FROM repositories")

    # ------------------------------------------- Repository Runtime & Sandbox
    def check_dependency_cycle(self_repo_name, dependency_names):
        """Cross-repo cycle check (section 23) -- needs every OTHER
        registered repo's own contract, so it lives here (main.py already
        has the full repository registry) rather than inside
        RepositoryContractEditor itself. Rejects only a real cycle back to
        self_repo_name; unrelated dependency graphs elsewhere are none of
        this repo's concern."""
        seen = set()
        stack = list(dependency_names)
        while stack:
            name = stack.pop()
            if name == self_repo_name: return True
            if name in seen: continue
            seen.add(name)
            row = db.one("SELECT repo_path FROM repositories WHERE repo_name=? AND enabled=1", (name,))
            if not row: continue
            try: settings_ = contract_editor.read_sandbox_settings(Path(row["repo_path"]))
            except ContractEditError: continue
            stack.extend(d["repo"] for d in settings_.get("runtime_dependencies", []))
        return False
    @app.get("/repositories/{rid}/runtime", response_class=HTMLResponse)
    def repository_runtime(request: Request, rid: int):
        r = repo(rid)
        try:
            current = contract_editor.read_sandbox_settings(Path(r["repo_path"]))
            load_error = None
        except ContractEditError as exc:
            current = None; load_error = "; ".join(exc.errors)
        registered = db.all("SELECT id,repo_name FROM repositories WHERE enabled=1 AND id!=?", (rid,))
        history = db.all("SELECT * FROM workspace_events WHERE entity_type='repository' AND entity_id=? AND action='REPOSITORY_CONTRACT_UPDATED' ORDER BY id DESC LIMIT 10", (rid,))
        diff = request.query_params.get("diff") == "1"
        raw_text = None
        if diff or request.query_params.get("view") == "raw":
            try: raw_text = contract_editor.load(Path(r["repo_path"]))["raw_text"]
            except ContractEditError: pass
        git_dirty = None
        try: git_dirty = bool(git.status(r["repo_path"]).strip())
        except Exception: pass
        test_sandbox = db.one("SELECT * FROM sandboxes WHERE owner_type='REPOSITORY_TEST' AND owner_id=? ORDER BY id DESC LIMIT 1", (rid,))
        return render(request, "repository_runtime.html", r=r, current=current, load_error=load_error,
                      registered=registered, history=history, raw_text=raw_text, git_dirty=git_dirty,
                      test_sandbox=test_sandbox)

    def _parse_runtime_form(form) -> dict:
        enabled = form.get("enabled") == "on"
        dep_repos = form.getlist("dep_repo")
        dep_profiles = form.getlist("dep_profile")
        dep_modes = form.getlist("dep_mode")
        deps = [
            {"repo": repo_.strip(), "profile": (prof or "BACKEND").strip(), "mode": (mode or "KNOWN_GOOD_MAIN").strip()}
            for repo_, prof, mode in zip(dep_repos, dep_profiles, dep_modes) if repo_.strip()
        ]
        services = [s.strip() for s in (form.get("services") or "").split(",") if s.strip()]
        profile_name = (form.get("profile_name") or "").strip().upper()
        own_service = services[0] if services else profile_name.lower()
        # Ports/health/outputs (section 3: "health endpoint if schema
        # supports it... runtime output labels" ARE normal-UI settings,
        # not Advanced-only) -- the user only ever types the container
        # port + health path; the HOST port range is never user-supplied
        # (section 24/26) -- port_specs() already falls back to a wide,
        # safe default range (20000-29999) whenever `range` is omitted,
        # so this deliberately never writes one.
        ports = {}
        own_port = (form.get("own_port") or "").strip()
        if own_port.isdigit():
            ports[own_service] = {"container": int(own_port)}
        health = {}
        health_path = (form.get("health_path") or "").strip()
        if health_path:
            health[own_service] = {"path": health_path}
        outputs = {}
        if services and profile_name:
            outputs[f"{profile_name.lower()}_url"] = {"service": own_service}
        return {
            "enabled": enabled,
            "profile_name": profile_name,
            "services": services,
            "runtime_dependencies": deps,
            "seed_default": (form.get("seed_default") or "").strip() or None,
            "auto_provision": form.get("auto_provision") == "on",
            "ports": ports, "health": health, "outputs": outputs,
        }
    @app.post("/api/repositories/{rid}/runtime-sandbox")
    async def save_runtime_sandbox(request: Request, rid: int):
        r = repo(rid)
        form = await request.form()
        proposed = _parse_runtime_form(form)
        # section 7: production target never accidentally selected --
        # a profile literally named PRODUCTION (or a dependency mode
        # pointed at one) is refused outright, never silently accepted.
        if proposed["enabled"] and proposed["profile_name"] == "PRODUCTION":
            raise GitSafetyError("Refusing to save a sandbox profile named PRODUCTION -- sandboxes are never production targets")
        registered_names = {x["repo_name"] for x in db.all("SELECT repo_name FROM repositories WHERE enabled=1")}
        dep_names = [d["repo"] for d in proposed["runtime_dependencies"]]
        if check_dependency_cycle(r["repo_name"], dep_names):
            raise GitSafetyError(f"Runtime dependency graph would form a cycle back to {r['repo_name']}")
        try:
            diff = contract_editor.write_sandbox_settings(Path(r["repo_path"]), proposed, self_repo_name=r["repo_name"], registered_repo_names=registered_names)
        except ContractEditError as exc:
            raise GitSafetyError("; ".join(exc.errors))
        db.event("repository", rid, "REPOSITORY_CONTRACT_UPDATED",
                  f"repo={r['repo_name']} fields=sandbox before_sha={diff['before_sha256'][:12]} after_sha={diff['after_sha256'][:12]} operator=ui")
        return RedirectResponse(f"/repositories/{rid}/runtime?diff=1", 303)
    @app.post("/api/repositories/{rid}/runtime-sandbox/test")
    def test_runtime_sandbox(rid: int):
        r = repo(rid)
        try:
            current = contract_editor.read_sandbox_settings(Path(r["repo_path"]))
        except ContractEditError as exc:
            raise GitSafetyError("; ".join(exc.errors))
        if not current["enabled"]:
            raise GitSafetyError("Sandbox is not enabled for this repository -- save a configuration first")
        existing = db.one("SELECT * FROM sandboxes WHERE owner_type='REPOSITORY_TEST' AND owner_id=? AND status IN ('CREATED','PROVISIONING','STARTING','RUNNING') ORDER BY id DESC LIMIT 1", (rid,))
        if existing:
            return RedirectResponse(f"/sandboxes/{existing['id']}", 303)
        commit = git.head(r["repo_path"])
        source = SourceSpec(repository_id=rid, role="test-configuration", branch=r["default_branch"], commit_sha=commit,
                             worktree_path=r["repo_path"], repo_path=r["repo_path"], source_type="REPOSITORY_TEST")
        try:
            sid = sandboxes.create(task_id=None, owner_type="REPOSITORY_TEST", owner_id=rid, profile=current["profile_name"], provider=source)
        except SandboxError as exc:
            raise GitSafetyError(str(exc))
        if sid is None:
            raise GitSafetyError("Sandbox profile resolved to NONE -- nothing to test")
        db.event("repository", rid, "REPOSITORY_CONTRACT_TESTED", f"repo={r['repo_name']} profile={current['profile_name']} sandbox={sid}")
        sandboxes.provision(sid)
        return RedirectResponse(f"/sandboxes/{sid}", 303)

    @app.get("/workspaces", response_class=HTMLResponse)
    def workspaces(request: Request): return render(request,"workspaces.html",workspaces=db.all("SELECT w.*,r.repo_name FROM agent_workspaces w JOIN repositories r ON r.id=w.repository_id ORDER BY w.updated_at DESC"),repositories=db.all("SELECT * FROM repositories WHERE enabled=1"),tasks=db.all("SELECT id,title FROM tasks WHERE status NOT IN ('MERGED','CANCELLED') ORDER BY title"))
    @app.post("/api/workspaces")
    def create_workspace(repository_id:int=Form(...),agent:str=Form(...),task_name:str=Form(...),base_branch:str=Form("main")):
        r=repo(repository_id); agent=slugify(agent); task=slugify(task_name)
        if agent not in settings.agents: raise GitSafetyError("Agent is not allowed")
        branch,path,commit=git.create_agent(r["repo_path"],agent,task,base_branch)
        try: wid=db.execute("INSERT INTO agent_workspaces(repository_id,agent,task_name,branch,worktree_path,base_branch,base_commit,last_commit,status) VALUES(?,?,?,?,?,?,?,?,?)",(repository_id,agent,task,branch,str(path),base_branch,commit,commit,"CREATED"))
        except Exception:
            if not git.status(path).strip(): git.close(r["repo_path"],path)
            raise
        db.event("agent",wid,"WORKSPACE_CREATED",branch); return RedirectResponse(f"/workspaces/{wid}",303)
    @app.get("/api/workspaces")
    def api_workspaces(): return db.all("SELECT * FROM agent_workspaces")
    @app.get("/workspaces/{wid}",response_class=HTMLResponse)
    def workspace_detail(request:Request,wid:int):
        """Section 39: Task Status, Workspace Status, Sandbox Status and
        Test Status are shown as four clearly separate values -- Task
        Status/review gate state come only from decision.evaluate() (the
        `task_decision`/`builder` values below); `readiness` stays scoped
        to this workspace's own runtime signals (sandbox/automated tests/
        manual verification), a different concern from the Review/QA
        gate. Neither is allowed to stand in for the other."""
        w=agent_row(wid); details=safe_details(w["worktree_path"])
        runs=db.all("SELECT * FROM test_runs WHERE workspace_type='agent' AND workspace_id=? ORDER BY id DESC",(wid,))
        readiness=workspace_readiness(w)
        report=workspace_verification(wid) or (task_verification(w["task_id"]) if w["task_id"] else None)
        manual_history=db.all("SELECT * FROM manual_verifications WHERE workspace_id=? ORDER BY id DESC",(wid,))
        task_decision=decision.evaluate(w["task_id"]) if w["task_id"] else None
        builder=next((b for b in task_decision["builders"] if b["id"]==wid),None) if task_decision else None
        integration_exists=None if not w["task_id"] else bool(task_decision["task_integration"])
        ready_for_main=bool(task_decision and task_decision["ready_for_main"])
        code=next_action_code(readiness,integration_exists,ready_for_main)
        action=resolve_next_action(code,wid=wid,tid=w["task_id"],sandbox_id=readiness["sandbox"]["id"] if readiness["sandbox"] else None)
        sessions=db.all("SELECT * FROM agent_sessions WHERE workspace_id=? ORDER BY id DESC LIMIT 10",(wid,))
        review_history=decision.review_history(wid)
        live_prompt=workspace_agent_prompt(w,task_row(w["task_id"]),repo(w["repository_id"])) if w["task_id"] else None
        # One authoritative session-state computation for this page,
        # whether or not the workspace belongs to a Task (builder.session/
        # agent_status only exist in the task-scoped case) -- Workspace
        # Detail UX audit section 1/4: the whole "Current Action" block
        # reads only this, never re-derives session state itself.
        session=sessions[0] if sessions else None
        live_session=session if session and session["status"] in LIVE_SESSION_STATUSES else None
        agent_status=session["status"] if session else "NOT_STARTED"
        detected_report=parse_completion_report(agent_sessions.live_tail(session["id"])) if session and w["status"]!="READY" else None
        recovery_state="COMPLETION_REQUIRED" if agent_status in ("EXITED","FAILED") and w["status"]!="READY" and not detected_report else None
        manual_ready_check=validate_manual_ready(w) if recovery_state else None
        return render(request,"workspace_detail.html",w=w,details=details,runs=runs,readiness=readiness,report=report,
                      manual_history=manual_history,next_action=action,sessions=sessions,
                      task_decision=task_decision,builder=builder,review_history=review_history,live_prompt=live_prompt,
                      session=session,live_session=live_session,agent_status=agent_status,detected_report=detected_report,
                      recovery_state=recovery_state,manual_ready_check=manual_ready_check)
    @app.get("/api/workspaces/{wid}")
    def api_workspace(wid:int): return agent_row(wid)
    @app.post("/api/workspaces/{wid}/ready")
    def ready(wid:int):
        w=agent_row(wid); head=git.head(w["worktree_path"]); db.execute("UPDATE agent_workspaces SET status='READY',ready_for_integration=1,last_commit=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(head,wid)); db.event("agent",wid,"READY_MARKED",head)
        recompute_task_status(w.get("task_id"))
        return RedirectResponse(f"/workspaces/{wid}",303)
    @app.post("/api/workspaces/{wid}/test")
    def test_agent(wid:int): w=agent_row(wid); runner.start("agent",wid,Path(w["worktree_path"])); return RedirectResponse(f"/workspaces/{wid}",303)
    @app.post("/api/workspaces/{wid}/create-sandbox")
    def create_workspace_sandbox_standalone(wid:int,profile:str=Form("")):
        w=agent_row(wid)
        task_default=None
        if w["task_id"]:
            t=db.one("SELECT default_sandbox_profile FROM tasks WHERE id=?",(w["task_id"],)); task_default=t["default_sandbox_profile"] if t else None
        sid=auto_create_sandbox(w["task_id"],w["repository_id"],w["repo_path"],"AGENT_WORKSPACE",wid,w["role"] or w["agent"],w["branch"],w["last_commit"],w["worktree_path"],profile.strip().upper() or None,task_default)
        if sid: db.execute("UPDATE agent_workspaces SET sandbox_profile=(SELECT profile FROM sandboxes WHERE id=?) WHERE id=?",(sid,wid))
        return RedirectResponse(f"/workspaces/{wid}",303)
    def _insert_verification_report(w,t,status_upper,head,*,what_changed="",files_changed="",tests_run="",automated_tests="",how_to_verify="",expected_result="",test_data="",runtime_requirements="NONE",risks="",ready_source="AGENT_SUBMITTED",operator=None):
        """Shared by the normal Submit-for-Review path and the manual
        EXITED-without-report recovery fallback -- one insert, one
        status-transition, one audit shape, regardless of which UI action
        produced the report. `ready_source` records which path it was
        (never changes what READY itself requires: commit_sha is always
        the exact pinned HEAD, still validated clean by the caller)."""
        wid=w["id"]
        db.execute("INSERT INTO verification_reports(task_id,workspace_id,work_status,what_changed,files_changed,tests_run,automated_tests,how_to_verify,expected_result,test_data,runtime_requirements,risks,commit_sha,brief_version,ready_source,operator) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (w["task_id"],wid,status_upper,what_changed.strip(),files_changed.strip(),tests_run.strip(),automated_tests.strip(),how_to_verify.strip(),expected_result.strip(),test_data.strip(),runtime_requirements.strip().upper() or "NONE",risks.strip(),head,t["brief_version"] if t else None,ready_source,operator))
        db.event("agent",wid,"VERIFICATION_REPORT_ADDED",f"{status_upper} source={ready_source}")
        if status_upper=="READY" and w["status"]!="READY":
            db.execute("UPDATE agent_workspaces SET status='READY',ready_for_integration=1,last_commit=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(head,wid))
            db.event("agent",wid,"SUBMITTED_FOR_REVIEW",f"{head} source={ready_source}"); recompute_task_status(w.get("task_id"))
    @app.post("/api/workspaces/{wid}/verification-report")
    def submit_workspace_report(wid:int,work_status:str=Form("READY"),what_changed:str=Form(""),files_changed:str=Form(""),tests_run:str=Form(""),automated_tests:str=Form(""),how_to_verify:str=Form(""),expected_result:str=Form(""),test_data:str=Form(""),runtime_requirements:str=Form("NONE"),risks:str=Form("")):
        """Submit for Review (section 14/15): the Builder completion
        report (WHAT_CHANGED/FILES_CHANGED/TESTS_RUN/HOW_TO_VERIFY/
        EXPECTED_RESULT/RISKS) is what workspace becomes READY_FOR_REVIEW
        from. Pinned to the exact commit_sha and brief_version at
        submission time (section 15) -- both are what
        TaskDecisionService.builder_view() later compares against to
        decide a downstream Review/QA/Integration result is still valid.
        WORK_STATUS: READY requires a clean git worktree (section 14) --
        an uncommitted change can never silently become "ready"."""
        w=agent_row(wid); t=task_row(w["task_id"]) if w["task_id"] else None
        status_upper=work_status.strip().upper() or "READY"
        if status_upper=="READY" and git.status(w["worktree_path"]).strip():
            raise GitSafetyError("Git worktree must be clean before submitting for review (uncommitted changes present)")
        head=git.head(w["worktree_path"])
        _insert_verification_report(w,t,status_upper,head,what_changed=what_changed,files_changed=files_changed,tests_run=tests_run,automated_tests=automated_tests,how_to_verify=how_to_verify,expected_result=expected_result,test_data=test_data,runtime_requirements=runtime_requirements,risks=risks,ready_source="AGENT_SUBMITTED")
        return RedirectResponse(f"/workspaces/{wid}",303)

    MANUAL_READY_BLOCKERS={
        "WORKTREE_MISSING":"Worktree không còn tồn tại trên máy này.",
        "HEAD_UNRESOLVED":"Không đọc được HEAD hiện tại của worktree.",
        "BRANCH_MISMATCH":"Worktree đang ở branch khác với branch của Builder này.",
        "MERGE_CONFLICT":"Worktree đang có merge conflict chưa resolve.",
        "UNCOMMITTED_CHANGES":"Worktree có thay đổi chưa commit.",
        "NO_SOURCE_CHANGES":"Chưa có commit nào mới so với base -- chưa có gì để Submit for Review.",
    }
    def validate_manual_ready(w):
        """Section 2 (EXITED-without-report manual fallback): everything
        that must be true about the real git worktree before a human can
        stand in for a missing agent completion report. Never trusts the
        UI state that got the user here -- re-derives every check from
        the actual worktree on disk, in the same order the blockers are
        documented, first failure wins. Returns {"ok","blocker","detail","head"}."""
        path=Path(w["worktree_path"])
        if not path.is_dir():
            return {"ok":False,"blocker":"WORKTREE_MISSING","detail":MANUAL_READY_BLOCKERS["WORKTREE_MISSING"],"head":None}
        try:
            branch_result=git.git(path,"rev-parse","--abbrev-ref","HEAD",check=False)
            current_branch=branch_result.stdout.strip()
        except GitCommandError:
            current_branch=""
        if not current_branch or branch_result.returncode:
            return {"ok":False,"blocker":"HEAD_UNRESOLVED","detail":MANUAL_READY_BLOCKERS["HEAD_UNRESOLVED"],"head":None}
        if current_branch!=w["branch"]:
            return {"ok":False,"blocker":"BRANCH_MISMATCH","detail":f"{MANUAL_READY_BLOCKERS['BRANCH_MISMATCH']} (worktree={current_branch}, builder={w['branch']})","head":None}
        if git.conflict_files(path):
            return {"ok":False,"blocker":"MERGE_CONFLICT","detail":MANUAL_READY_BLOCKERS["MERGE_CONFLICT"],"head":None}
        if git.status(path).strip():
            return {"ok":False,"blocker":"UNCOMMITTED_CHANGES","detail":MANUAL_READY_BLOCKERS["UNCOMMITTED_CHANGES"],"head":None}
        try:
            head=git.head(path)
        except GitCommandError:
            return {"ok":False,"blocker":"HEAD_UNRESOLVED","detail":MANUAL_READY_BLOCKERS["HEAD_UNRESOLVED"],"head":None}
        if head==w["base_commit"]:
            return {"ok":False,"blocker":"NO_SOURCE_CHANGES","detail":MANUAL_READY_BLOCKERS["NO_SOURCE_CHANGES"],"head":head}
        return {"ok":True,"blocker":None,"detail":"","head":head}

    @app.post("/api/workspaces/{wid}/mark-ready-manual")
    def mark_ready_manual(wid:int,what_changed:str=Form(...),how_to_verify:str=Form(...),tests_run:str=Form("Not run"),expected_result:str=Form(""),risks:str=Form("None known")):
        """[Mark Ready for Review] manual fallback (section 1/2/12): shown
        only when AgentSession EXITED (or FAILED) without a persisted or
        detected completion report and the Builder isn't already READY --
        a recovery path for real, already-complete source work, never a
        force-pass button. Re-validates the real worktree from scratch
        server-side regardless of what the page showed (section 12) --
        the same checks that decide whether the button is even shown are
        run again here, because client state is never trusted for
        something this consequential. What changed / How to verify are
        the only two required fields (section 3); Tests run defaults to
        'Not run', Risks to 'None known' so a genuinely simple, already-
        complete change never needs a user to invent boilerplate."""
        w=agent_row(wid); t=task_row(w["task_id"]) if w["task_id"] else None
        session=decision.latest_session(wid)
        db.event("agent",wid,"BUILDER_MANUAL_READY_REQUESTED",f"task={w.get('task_id')} session={session['id'] if session else None} operator=ui")
        if w["status"]=="READY":
            raise GitSafetyError("Builder is already Submitted for Review")
        check=validate_manual_ready(w)
        if not check["ok"]:
            db.event("agent",wid,"BUILDER_MANUAL_READY_BLOCKED",f"task={w.get('task_id')} session={session['id'] if session else None} blocker={check['blocker']} operator=ui")
            raise GitSafetyError(f"Cannot mark ready: {check['detail']}")
        if not what_changed.strip() or not how_to_verify.strip():
            db.event("agent",wid,"BUILDER_MANUAL_READY_BLOCKED",f"task={w.get('task_id')} session={session['id'] if session else None} blocker=MISSING_SUMMARY operator=ui")
            raise GitSafetyError("What changed and How to verify are required")
        head=check["head"]
        _insert_verification_report(w,t,"READY",head,what_changed=what_changed,tests_run=tests_run or "Not run",how_to_verify=how_to_verify,expected_result=expected_result,risks=risks or "None known",ready_source="MANUAL_CONFIRMATION",operator="ui")
        db.event("agent",wid,"BUILDER_MANUAL_READY_SUCCEEDED",f"task={w.get('task_id')} session={session['id'] if session else None} commit={head} operator=ui")
        return RedirectResponse(f"/tasks/{w['task_id']}" if w.get("task_id") else f"/workspaces/{wid}",303)
    @app.post("/api/workspaces/{wid}/close")
    def close_agent(wid:int): w=agent_row(wid); git.close(w["repo_path"],w["worktree_path"]); db.execute("UPDATE agent_workspaces SET status='CLOSED',closed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(wid,)); db.event("agent",wid,"WORKSPACE_CLOSED"); return RedirectResponse("/workspaces",303)
    @app.post("/api/workspaces/{wid}/create-task")
    def create_task_from_workspace(wid:int):
        """Legacy-workspace migration (section 21): wraps an UNASSIGNED
        workspace (task_id IS NULL) in a brand-new Task, title prefilled
        from its own task_name. No branch/worktree/sandbox is touched or
        recreated -- this only sets agent_workspaces.task_id."""
        w=agent_row(wid)
        if w["task_id"]: raise GitSafetyError("Workspace already belongs to a Task")
        title=w["task_name"].replace("-"," ").replace("_"," ").strip() or w["task_name"]
        slug=slugify(title)
        # Section 5: a Task with >=1 Builder Workspace is never BACKLOG --
        # this one already has code underway, so it starts ACTIVE.
        tid=db.execute("INSERT INTO tasks(slug,title,description,status) VALUES(?,?,?,?)",(slug,title,f"Created from existing Agent Workspace #{wid} ({w['agent']}/{w['repo_name']}).","ACTIVE"))
        db.execute("UPDATE agent_workspaces SET task_id=? WHERE id=?",(tid,wid))
        db.event("task",tid,"TASK_CREATED_FROM_WORKSPACE",str(wid)); db.event("agent",wid,"ATTACHED_TO_TASK",str(tid))
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/workspaces/{wid}/attach-task")
    def attach_workspace_to_task(wid:int,task_id:int=Form(...)):
        w=agent_row(wid)
        if w["task_id"]: raise GitSafetyError("Workspace already belongs to a Task")
        t=task_row(task_id)
        db.execute("UPDATE agent_workspaces SET task_id=? WHERE id=?",(task_id,wid))
        db.event("agent",wid,"ATTACHED_TO_TASK",str(task_id)); recompute_task_status(task_id)
        return RedirectResponse(f"/tasks/{task_id}",303)

    # ------------------------------------------------------ Review / QA
    @app.post("/api/workspaces/{wid}/start-review")
    def start_review(wid:int,reviewer_agent:str=Form(...)):
        """[Start Review] (section 6/11): only after Builder submitted
        (status=READY). Inserts a NEW review_runs row -- a fresh review
        cycle after a fix is a new row, never an overwrite of the old
        one's evidence (section 18). Default mode is READ_ONLY: no new
        worktree is created here at all; a reviewer only ever gets a
        worktree if explicitly escalated to repair mode via the normal
        Create Agent Workspace flow."""
        w=agent_row(wid)
        if w["status"]!="READY": raise GitSafetyError("Builder has not submitted for review yet")
        t=task_row(w["task_id"]) if w["task_id"] else None
        report=workspace_verification(wid)
        prompt=render_review_prompt(t,w,report) if t else render_review_prompt({"title":w["task_name"],"brief_acceptance_criteria":""},w,report)
        head=git.head(w["worktree_path"])
        bv=t["brief_version"] if t else None
        rid=db.execute("INSERT INTO review_runs(task_id,workspace_id,reviewer_type,reviewer_agent,brief_version,reviewed_commit,status,findings) VALUES(?,?,?,?,?,?,?,?)",
                        (w["task_id"],wid,"BUILDER_WORKSPACE",slugify(reviewer_agent),bv,head,"RUNNING",prompt))
        if t: db.execute("INSERT INTO prompts(task_id,workspace_id,prompt_type,brief_version,content) VALUES(?,?,?,?,?)",(t["id"],wid,"REVIEWER",bv,prompt))
        db.event("agent",wid,"REVIEW_STARTED",f"run={rid} reviewer={reviewer_agent}")
        return RedirectResponse(f"/workspaces/{wid}",303)
    @app.post("/api/workspaces/{wid}/submit-review")
    def submit_review(wid:int,result:str=Form(...),notes:str=Form("")):
        """Reviewer result (section 6/7/17): REVIEW_PASS/FIX_REQUIRED/
        BLOCKED, completing the most recent RUNNING review_runs row for
        this workspace (never overwriting an earlier, already-completed
        one -- that stays queryable history). FIX_REQUIRED returns the
        Task to the Builder; a later commit or Brief bump independently
        makes this same row STALE at read time (TaskDecisionService),
        never something this route has to remember to invalidate."""
        w=agent_row(wid)
        if result not in ("PASS","FIX_REQUIRED","BLOCKED"): raise GitSafetyError("Invalid review result")
        run=db.one("SELECT * FROM review_runs WHERE workspace_id=? ORDER BY id DESC LIMIT 1",(wid,))
        if not run or run["status"] not in ("PENDING","RUNNING"): raise GitSafetyError("No review in progress for this workspace")
        db.execute("UPDATE review_runs SET status=?,findings=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",(result,notes.strip(),run["id"]))
        db.event("agent",wid,"REVIEW_SUBMITTED",f"run={run['id']} result={result}")
        return RedirectResponse(f"/workspaces/{wid}",303)
    @app.post("/api/tasks/{tid}/start-qa")
    def start_qa(tid:int,tester_agent:str=Form("")):
        """[Start Tester] (section 8/12/19): QA is Task-level (it may
        cover cross-repo behavior), gated on every required Builder's
        review being PASS-and-current. Primarily reads the sandbox +
        exact source manifest already in place -- no new worktree unless
        the tester must change test code/fixtures, which stays a manual,
        explicit Create Agent Workspace outside this route."""
        t=task_row(tid); d=decision.evaluate(tid)
        if d["stage"] not in ("QA","INTEGRATION","MERGING","COMPLETE") or any(b["review_status"]!="PASS" for b in d["builders"]):
            raise GitSafetyError("QA requires every required Builder's review to be PASS and current")
        sbxs=task_sandboxes(tid); sandbox_id=next((s["id"] for s in sbxs if s["status"] in ("RUNNING","CLEANUP_ELIGIBLE")),None)
        manifest=db.one("SELECT source_manifest_json FROM sandboxes WHERE id=?",(sandbox_id,))["source_manifest_json"] if sandbox_id else "{}"
        qid=db.execute("INSERT INTO qa_runs(task_id,brief_version,source_manifest,sandbox_id,tester_agent,status) VALUES(?,?,?,?,?,?)",
                        (tid,t["brief_version"],manifest,sandbox_id,slugify(tester_agent) if tester_agent else "qa","RUNNING"))
        db.event("task",tid,"QA_STARTED",f"run={qid} tester={tester_agent}")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/submit-qa")
    def submit_qa(tid:int,result:str=Form(...),notes:str=Form(""),manual_result:str=Form(""),hardware_result:str=Form("")):
        task_row(tid)
        if result not in ("PASS","FAIL","BLOCKED"): raise GitSafetyError("Invalid QA result")
        run=db.one("SELECT * FROM qa_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",(tid,))
        if not run or run["status"] not in ("PENDING","RUNNING"): raise GitSafetyError("No QA run in progress for this Task")
        db.execute("UPDATE qa_runs SET status=?,notes=?,manual_result=?,hardware_result=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",
                   (result,notes.strip(),manual_result.strip() or None,hardware_result.strip() or None,run["id"]))
        db.event("task",tid,"QA_SUBMITTED",f"run={run['id']} result={result}")
        return RedirectResponse(f"/tasks/{tid}",303)

    # ------------------------------------------------- Agent Sessions (PTY)
    def session_row(sid):
        row=db.one("SELECT s.*,w.worktree_path,w.agent workspace_agent FROM agent_sessions s JOIN agent_workspaces w ON w.id=s.workspace_id WHERE s.id=?",(sid,))
        if not row: raise HTTPException(404,"Session not found")
        return row
    def _deliver_to_session(w,sid,note_exited_without_report=False):
        """Compute the current effective Builder prompt -- the Task's
        intent as usual, or, if the latest review for this workspace is
        FIX_REQUIRED, the repair prompt with findings appended (exactly
        what workspace_agent_prompt() already composes, section 4C) --
        and deliver it into a LIVE session's stdin. Always snapshots a
        `prompts` audit row first, regardless of whether delivery itself
        succeeds, so the exact text that was (attempted to be) sent is
        always recoverable. Returns True only on confirmed delivery.
        `note_exited_without_report` is decided by the CALLER before this
        new session ever existed (section 6) -- workspace_agent_prompt()
        must never re-derive it from "the latest session", because by the
        time this prompt is being computed the new session already is
        the latest one."""
        if not w.get("task_id"): return False
        try:
            t=task_row(w["task_id"]); repo_row=repo(w["repository_id"])
            prompt=workspace_agent_prompt(w,t,repo_row,note_exited_without_report=note_exited_without_report)
            review=decision.latest_review(w["id"])
            source="REPAIR" if review and review["status"]=="FIX_REQUIRED" else "TASK"
            db.execute("INSERT INTO prompts(task_id,workspace_id,prompt_type,brief_version,content) VALUES(?,?,?,?,?)",
                       (w["task_id"],w["id"],"BUILDER",t["brief_version"],prompt))
            return agent_sessions.deliver_prompt(sid,prompt,source,t["brief_version"])
        except Exception:
            return False  # snapshot/delivery is best-effort audit, never raises into the caller
    def _start_builder_session(w,mode="INTERACTIVE",note_exited_without_report=False):
        """The one place 'Start Builder' actually happens: validates
        (Task exists, Builder Workspace exists, worktree path is the
        trusted server-resolved one, agent is in the trusted launcher
        registry), starts the AgentSession, then WAITS for the CLI to be
        ready and delivers the exact generated Builder Prompt into it
        (section 1) -- deliberately does NOT check implementation_prompt
        at all; Task Title fallback means intent is always resolvable.
        The user is never expected to paste the prompt manually as the
        normal flow; a failed delivery is recorded as prompt_status
        FAILED for [Retry Prompt Delivery], not silently retried."""
        if w["agent"] not in settings.agents: raise GitSafetyError("Agent is not allowed")
        mode="VIEW_ONLY" if mode=="VIEW_ONLY" else "INTERACTIVE"
        sid=agent_sessions.start(task_id=w["task_id"],workspace_id=w["id"],agent=w["agent"],worktree_path=w["worktree_path"],mode=mode)
        db.event("agent",w["id"],"SESSION_STARTED",f"session={sid} mode={mode}")
        delivered=_deliver_to_session(w,sid,note_exited_without_report=note_exited_without_report)
        db.event("agent",w["id"],"PROMPT_DELIVERED" if delivered else "PROMPT_DELIVERY_FAILED",f"session={sid}")
        return sid
    def _resume_builder_session(w):
        """[Resume Agent] (section 4): never blindly starts a second
        session on top of one already doing real work.
        A. no live session, or one that already exited -- start a fresh
           one (this doubles as the FIX_REQUIRED repair path, since
           _deliver_to_session already appends review findings then). If
           the prior session EXITED without ever producing a report at
           the current HEAD (section 6/8: EXITED is not itself failure),
           the fresh session's prompt gets a focused note about that --
           decided HERE, before the new session exists, never re-derived
           from "the latest session" downstream.
        B. a live session whose prompt was already DELIVERED -- the
           agent is presumably still working; no-op, never resend the
           original task a second time.
        C. a live session that never got its prompt delivered (PENDING/
           FAILED) -- deliver into that SAME session, no duplicate
           process."""
        session=decision.latest_session(w["id"])
        if session and session["status"] in ("STARTING","RUNNING","WAITING_FOR_INPUT"):
            if session["prompt_status"] in ("PENDING","FAILED"):
                delivered=_deliver_to_session(w,session["id"])
                db.event("agent",w["id"],"PROMPT_DELIVERED" if delivered else "PROMPT_DELIVERY_FAILED",f"session={session['id']} (retry)")
            return session["id"]
        note_exited=bool(session and session["status"] in ("EXITED","FAILED") and w["status"]!="READY")
        return _start_builder_session(w,note_exited_without_report=note_exited)
    @app.post("/api/workspaces/{wid}/sessions")
    def create_session(wid:int,mode:str=Form("INTERACTIVE")):
        """Command safety (section 14): the browser supplies only
        workspace_id + (fixed) mode -- agent name and cwd are both
        resolved server-side from the trusted workspace/launcher
        registry, never taken from the request. Duplicate-click
        protection (button-feedback section 4/8): a second 'Start
        Builder' click while a session is already STARTING/RUNNING/
        WAITING_FOR_INPUT reuses that live session (via
        _resume_builder_session's own guard) instead of forking a second
        real pty process for the same workspace."""
        w=agent_row(wid)
        try: sid=_resume_builder_session(w) if mode=="INTERACTIVE" else _start_builder_session(w,mode)
        except SessionError as exc: raise GitSafetyError(str(exc)) from exc
        return RedirectResponse(f"/workspaces/{wid}/sessions/{sid}",303)
    @app.post("/api/sessions/{sid}/deliver-prompt")
    def retry_prompt_delivery(sid:int):
        """[Retry Prompt Delivery] (section 3): the explicit, exceptional
        recovery action for a session whose automatic delivery failed --
        never triggered automatically on a timer/poll."""
        s=session_row(sid); w=agent_row(s["workspace_id"])
        delivered=_deliver_to_session(w,sid)
        db.event("agent",w["id"],"PROMPT_DELIVERED" if delivered else "PROMPT_DELIVERY_FAILED",f"session={sid} (manual retry)")
        return RedirectResponse(f"/workspaces/{w['id']}/sessions/{sid}",303)
    @app.post("/api/tasks/{tid}/start-all-builders")
    def start_all_builders(tid:int):
        """[Start All Builders]: for every valid Builder Workspace that
        isn't already RUNNING (agent_status not in the live-session set),
        start its AgentSession -- a missing Implementation Prompt never
        blocks any of them (Task Title fallback), and an already-running
        one is skipped, never restarted."""
        task_row(tid)
        d=decision.evaluate(tid)
        for b in d["builders"]:
            if b["agent_status"] in ("STARTING","RUNNING","WAITING_FOR_INPUT"): continue
            if b["agent"] not in settings.agents: continue
            w=agent_row(b["id"])
            if w["status"]=="CLOSED": continue
            try: _start_builder_session(w)
            except SessionError as exc: db.event("agent",b["id"],"SESSION_START_FAILED",str(exc))
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/sessions/{sid}/stop")
    def stop_session(sid:int):
        session_row(sid); agent_sessions.stop(sid); return RedirectResponse(f"/workspaces/{session_row(sid)['workspace_id']}",303)
    @app.post("/api/sessions/{sid}/mode")
    def set_session_mode(sid:int,mode:str=Form(...)):
        """The actual VIEW_ONLY/INTERACTIVE security boundary: persisted
        here and re-read by the WebSocket handler on every stdin message,
        never trusted from anything the client itself claims."""
        session_row(sid)
        if mode not in ("INTERACTIVE","VIEW_ONLY"): raise GitSafetyError("Invalid mode")
        agent_sessions.set_mode(sid,mode)
        return {"ok":True,"mode":mode}
    @app.get("/agents/live",response_class=HTMLResponse)
    def agents_live(request:Request):
        rows=db.all("SELECT s.*,w.agent workspace_agent,w.role,r.repo_name,w.task_id tid,t.title task_title FROM agent_sessions s "
                     "JOIN agent_workspaces w ON w.id=s.workspace_id JOIN repositories r ON r.id=w.repository_id LEFT JOIN tasks t ON t.id=w.task_id "
                     "WHERE s.status IN ('STARTING','RUNNING','WAITING_FOR_INPUT') ORDER BY s.last_activity_at DESC")
        for row in rows:
            sb=sandbox_for_workspace(row["workspace_id"]); row["sandbox_status"]=sb["status"] if sb else None; row["sandbox_health"]=sb["health_status"] if sb else None
            row["activity_hint"]=activity_summary(row["id"], 160)
        return render(request,"agents_live.html",sessions=rows)
    @app.get("/workspaces/{wid}/sessions/{sid}",response_class=HTMLResponse)
    def session_detail(request:Request,wid:int,sid:int):
        w=agent_row(wid); s=session_row(sid)
        if s["workspace_id"]!=wid: raise HTTPException(404)
        t=task_row(w["task_id"]) if w.get("task_id") else None
        return render(request,"session_detail.html",w=w,s=s,t=t)
    @app.websocket("/ws/sessions/{sid}")
    async def session_ws(websocket:WebSocket,sid:int):
        """Browser <-> WebSocket <-> real PTY. VIEW_ONLY connections never
        forward stdin, enforced here server-side regardless of what the
        client sends (section 15)."""
        await websocket.accept()
        row=db.one("SELECT * FROM agent_sessions WHERE id=?",(sid,))
        live=agent_sessions.get(sid)
        if not row or not live:
            await websocket.send_json({"type":"exit","exit_code":row["exit_code"] if row else None,"status":row["status"] if row else "NOT_FOUND"})
            await websocket.close(); return
        loop=asyncio.get_event_loop()
        def send_chunk(chunk:bytes):
            try: asyncio.run_coroutine_threadsafe(websocket.send_bytes(chunk),loop)
            except RuntimeError: pass
        tail=live.subscribe(send_chunk)
        try:
            if tail: await websocket.send_bytes(tail)
            while True:
                msg=await websocket.receive()
                if msg.get("type")=="websocket.disconnect": break
                if "text" in msg and msg["text"] is not None:
                    try: data=json.loads(msg["text"])
                    except ValueError: continue
                    if data.get("type")=="resize": live.resize(int(data.get("rows",24)),int(data.get("cols",80)))
                elif "bytes" in msg and msg["bytes"] is not None:
                    if live.mode=="INTERACTIVE" and not live.closed:
                        try: live.write(msg["bytes"])
                        except SessionError: pass
        except WebSocketDisconnect:
            pass
        finally:
            live.unsubscribe(send_chunk)
            try: agent_sessions.persist_tail(sid)
            except Exception: pass

    @app.post("/api/workspaces/{wid}/open-terminal")
    def open_terminal(wid:int):
        w=agent_row(wid)
        try:
            result=launcher.open_terminal(w["worktree_path"]); db.event("agent",wid,"TERMINAL_OPENED",f"agent={w['agent']} path={result['worktree']} terminal={result['terminal']} result=requested")
            return {"ok":True,"message":"Terminal đã được yêu cầu mở đúng worktree.",**result}
        except LauncherError as exc:
            db.event("agent",wid,"AGENT_LAUNCH_FAILED",f"agent={w['agent']} path={w['worktree_path']} result={exc.code}")
            return JSONResponse({"ok":False,"code":exc.code,"message":str(exc),"fallback":f"cd {w['worktree_path']}"},status_code=409)
    @app.post("/api/workspaces/{wid}/launch-agent")
    def launch_agent(wid:int):
        """This spawns a REAL, independent codex/claude process in a
        desktop terminal window on THIS HOST's own graphical session
        (TerminalLauncherService, entirely separate from AgentSession's
        browser/PTY mechanism) -- invisible to the app's own state
        tracking (agent_status, review readiness, ...) once running.
        Workspace Detail UX audit (section 3): no longer exposed as a
        page button -- the one real live-agent flow is AgentSession
        (Open Live Agent / Start Codex in the Current Action block).
        The route stays for API compatibility, but the SAME concurrency
        guard applies: never spawn a second, untracked agent process on
        top of one already doing real, tracked work."""
        w=agent_row(wid)
        live=latest_session_for_workspace(wid)
        if live and live["status"] in LIVE_SESSION_STATUSES:
            return JSONResponse({"ok":False,"code":"ACTIVE_SESSION_EXISTS","message":f"An AgentSession is already {live['status']} for this workspace -- open it instead of starting a second, untracked agent process.","session_id":live["id"]},status_code=409)
        try:
            result=launcher.launch_agent(w["worktree_path"],w["agent"]); db.event("agent",wid,"AGENT_LAUNCHED",f"agent={w['agent']} path={result['worktree']} terminal={result['terminal']} result=requested")
            return {"ok":True,"message":f"{result['agent']} terminal đã được yêu cầu mở.",**result}
        except LauncherError as exc:
            db.event("agent",wid,"AGENT_LAUNCH_FAILED",f"agent={w['agent']} path={w['worktree_path']} result={exc.code}")
            return JSONResponse({"ok":False,"code":exc.code,"message":str(exc),"fallback":f"cd {w['worktree_path']}"},status_code=409)

    @app.get("/integrations",response_class=HTMLResponse)
    def integrations(request:Request): return render(request,"integrations.html",integrations=db.all("SELECT i.*,r.repo_name FROM integration_workspaces i JOIN repositories r ON r.id=i.repository_id ORDER BY i.updated_at DESC"),repositories=db.all("SELECT * FROM repositories WHERE enabled=1"),agents=db.all("SELECT w.*,r.repo_name FROM agent_workspaces w JOIN repositories r ON r.id=w.repository_id WHERE w.status IN ('READY','CODING','CREATED')"))
    @app.post("/api/integrations")
    async def create_integration(request:Request):
        form=await request.form(); repository_id=int(form["repository_id"]); name=slugify(str(form["name"])); base=str(form.get("base_branch","main")); ids=[int(x) for x in form.getlist("workspace_ids")]
        if not ids: raise GitSafetyError("Select at least one agent workspace")
        r=repo(repository_id); sources=[agent_row(x) for x in ids]
        if any(x["repository_id"]!=repository_id for x in sources): raise GitSafetyError("All sources must belong to integration repository")
        branch,path,commit=git.create_integration(r["repo_path"],name,base)
        iid=db.execute("INSERT INTO integration_workspaces(repository_id,name,branch,worktree_path,base_branch,base_commit,status) VALUES(?,?,?,?,?,?,?)",(repository_id,name,branch,str(path),base,commit,"MERGING")); db.event("integration",iid,"INTEGRATION_CREATED",branch)
        for source in sources:
            result=git.merge(path,source["branch"]); current=git.head(r["repo_path"],source["branch"])
            db.execute("INSERT INTO integration_sources(integration_id,workspace_id,merged_commit,merged_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",(iid,source["id"],current))
            if result.returncode:
                db.execute("UPDATE integration_workspaces SET status='CONFLICT' WHERE id=?",(iid,)); db.event("integration",iid,"MERGE_CONFLICT",source["branch"]); break
            db.event("integration",iid,"BRANCH_MERGED",source["branch"])
        else: db.execute("UPDATE integration_workspaces SET status='TESTING' WHERE id=?",(iid,))
        return RedirectResponse(f"/integrations/{iid}",303)
    @app.get("/api/integrations")
    def api_integrations(): return db.all("SELECT * FROM integration_workspaces")
    @app.get("/integrations/{iid}",response_class=HTMLResponse)
    def integration_detail(request:Request,iid:int):
        i=integration_row(iid); sources=db.all("SELECT s.*,w.agent,w.task_name,w.branch,w.worktree_path FROM integration_sources s JOIN agent_workspaces w ON w.id=s.workspace_id WHERE s.integration_id=?",(iid,)); stale=[]
        for s in sources:
            current=git.head(i["repo_path"],s["branch"]); s["current_commit"]=current; s["stale"]=current!=s["merged_commit"]; stale.append(s["stale"])
        head=git.head(i["worktree_path"]); readiness_stale=bool(i["verified_commit"] and i["verified_commit"]!=head) or any(stale)
        if readiness_stale and i["ready_for_main"]: invalidate(iid); i=integration_row(iid)
        # Push state (section 5/6/11): local HEAD is always read live
        # above; "actually pushed" means the LAST successful push's HEAD
        # still matches it -- a stale push_status='PUSHED' from before a
        # later local commit is treated as not-pushed, never trusted.
        conflicts=git.conflict_files(i["worktree_path"]); dirty=bool(git.status(i["worktree_path"]).strip())
        pushed=bool(i["last_pushed_head"]) and i["last_pushed_head"]==head and i["push_status"]=="PUSHED"
        github_available=github_merge.available(i["repo_path"])
        push_disabled_reason=None
        if not github_available: push_disabled_reason="Repository has no GitHub remote."
        elif conflicts: push_disabled_reason="Integration is still in conflict."
        elif dirty: push_disabled_reason="Commit or resolve local changes first."
        elif pushed: push_disabled_reason="Already pushed at this exact HEAD."
        pr=None
        ti_row=db.one("SELECT task_id FROM task_integrations WHERE id=?",(i["task_integration_id"],)) if i["task_integration_id"] else None
        task_id=ti_row["task_id"] if ti_row else None
        if ti_row:
            pr=db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?",(task_id,i["repository_id"]))
        gate_status=decision.integration_gate_status(i,task_id)
        # Sections 17-20: exactly one dominant primary action, derived
        # from the SAME authoritative ladder the Task wizard uses
        # (TaskDecisionService.integration_next_action) -- never a
        # separately-hardcoded ordering here. A stale/unmerged source
        # branch is checked first since the decision-engine ladder itself
        # has no notion of `integration_sources` (that table is purely a
        # git-worktree/Integration-page concern, section 1's Merge Latest
        # Changes), and an in-progress conflict always wins outright.
        i["gate_status"]=gate_status
        if any(s["stale"] for s in sources) and i["status"]!="CONFLICT":
            primary_action={"action":"MERGE_LATEST"}
        else:
            primary_action=decision.integration_next_action(i,task_id,pr)
            # Section 19: never present PUSH_INTEGRATION as the dominant
            # action when it is not actually reachable (no GitHub remote
            # configured at all) -- ready-for-main itself never requires
            # push_status (only clean/no-conflicts/sources-current/tests-
            # pass), so once tests pass a GitHub-less repo's real next
            # step is Confirm Ready for Main, not a Push button that
            # would just 409 if clicked.
            if primary_action["action"]=="PUSH_INTEGRATION" and not github_available:
                primary_action=dict(primary_action,action="CONFIRM_INTEGRATION_READY")
        return render(request,"integration_detail.html",i=i,sources=sources,conflicts=conflicts,head=head,stale=readiness_stale,
                      runs=db.all("SELECT * FROM test_runs WHERE workspace_type='integration' AND workspace_id=? ORDER BY id DESC",(iid,)),
                      events=db.all("SELECT * FROM workspace_events WHERE entity_type='integration' AND entity_id=? ORDER BY id",(iid,)),
                      pushed=pushed,push_disabled_reason=push_disabled_reason,pr=pr,gate_status=gate_status,
                      primary_action=primary_action["action"],primary_action_label=primary_action.get("label"),
                      current_action=integration_current_action(iid),
                      merge_latest_op=ops.latest("integration",iid,"MERGE_LATEST"),
                      push_op=ops.latest("integration",iid,"PUSH_INTEGRATION"),
                      ready_op=ops.latest("integration",iid,"MARK_READY_FOR_MAIN"))
    @app.post("/api/integrations/{iid}/merge-latest")
    def merge_latest(iid:int):
        """[Merge Latest Changes]: tracked as one MERGE_LATEST Operation
        (button-feedback section 8) -- a second click while one is still
        RUNNING is reflected back, never launches a second real git merge
        (section 4)."""
        i=integration_row(iid)
        if git.conflict_files(i["worktree_path"]): raise GitSafetyError("Resolve current conflicts before merging latest")
        try: op_id=ops.begin("integration",iid,"MERGE_LATEST")
        except OperationInProgress: return RedirectResponse(f"/integrations/{iid}",303)
        try:
            sources=db.all("SELECT s.*,w.branch FROM integration_sources s JOIN agent_workspaces w ON w.id=s.workspace_id WHERE s.integration_id=?",(iid,)); invalidate(iid)
            merged_any=False; conflict_branch=None
            for s in sources:
                current=git.head(i["repo_path"],s["branch"])
                if current==s["merged_commit"]: continue
                result=git.merge(i["worktree_path"],s["branch"])
                if result.returncode:
                    db.execute("UPDATE integration_workspaces SET status='CONFLICT' WHERE id=?",(iid,)); db.event("integration",iid,"MERGE_CONFLICT",s["branch"])
                    conflict_branch=s["branch"]; break
                db.execute("UPDATE integration_sources SET merged_commit=?,merged_at=CURRENT_TIMESTAMP WHERE integration_id=? AND workspace_id=?",(current,iid,s["workspace_id"])); db.event("integration",iid,"BRANCH_MERGED",s["branch"])
                merged_any=True
            if conflict_branch: ops.fail(op_id,f"Conflict detected in {conflict_branch}")
            else: ops.succeed(op_id,"Merged latest changes" if merged_any else "Already up to date")
        except Exception as exc:
            ops.fail(op_id,exc); raise
        return RedirectResponse(f"/integrations/{iid}",303)
    @app.post("/api/integrations/{iid}/test")
    def test_integration(iid:int):
        """[Run Tests]: test_runs already tracks QUEUED/RUNNING/PASS/FAIL
        (TestRunner runs it in a background thread) -- reused as-is
        rather than a second `operations` row (db.py V12 comment). The
        only gap closed here is duplicate-click protection: a click while
        the current run for this Integration is still QUEUED/RUNNING is
        a no-op, never a second concurrent TestRun (section 4)."""
        i=integration_row(iid)
        if git.conflict_files(i["worktree_path"]): raise GitSafetyError("Cannot test unresolved conflicts")
        active=db.one("SELECT id FROM test_runs WHERE workspace_type='integration' AND workspace_id=? AND status IN ('QUEUED','RUNNING') ORDER BY id DESC LIMIT 1",(iid,))
        if active: return RedirectResponse(f"/integrations/{iid}",303)
        invalidate(iid); runner.start("integration",iid,Path(i["worktree_path"])); return RedirectResponse(f"/integrations/{iid}",303)
    @app.post("/api/integrations/{iid}/ready-for-main")
    def ready_main(iid:int):
        """[Mark Ready for Main] (section 15): tracked as one
        MARK_READY_FOR_MAIN Operation so the button never looks unchanged
        after a click -- 'Validating readiness...' while RUNNING (this
        check is local/fast, so in practice RUNNING is only ever visible
        to a concurrent second click or a very unlucky refresh), then
        'Ready for Main' or 'Blocked: <exact reason>'."""
        try: op_id=ops.begin("integration",iid,"MARK_READY_FOR_MAIN")
        except OperationInProgress: return RedirectResponse(f"/integrations/{iid}",303)
        try:
            r=integration_readiness(iid)
            if not r["clean"]: raise GitSafetyError("Integration worktree must be clean")
            if not r["no_conflicts"]: raise GitSafetyError("Merge conflict exists")
            if not r["sources_current"]: raise GitSafetyError("Source not current")
            if not r["tests_pass"]: raise GitSafetyError("All required tests must PASS at current HEAD")
            db.execute("UPDATE integration_workspaces SET status='READY_FOR_MAIN',ready_for_main=1,verified_commit=?,verified_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(r["head"],iid)); db.event("integration",iid,"READY_FOR_MAIN",r["head"])
            ops.succeed(op_id,f"Ready for Main at {r['head'][:8]}")
        except Exception as exc:
            ops.fail(op_id,exc); raise
        return RedirectResponse(f"/integrations/{iid}",303)
    INTEGRATION_PUSH_BRANCH_DENYLIST={"main","master","production"}
    @app.post("/api/integrations/{iid}/push")
    def push_integration(iid:int):
        """[Push Integration Branch] (section 1-3): the ONLY way an
        Integration branch's local HEAD reaches GitHub -- a real,
        non-force `git push origin <branch>:<branch>`, derived entirely
        from the registered Integration record (repo path, worktree,
        branch) -- never a branch/remote/cwd typed in the browser.
        Reuses GitHubMergeService.push_branch(), the same primitive
        Create PR already uses, rather than a second ad-hoc subprocess
        call (section 2). Refuses on a dirty worktree, an unresolved
        conflict, the worktree somehow not being on the integration
        branch, or the branch being main/master/production."""
        i=integration_row(iid)
        if i["branch"] in INTEGRATION_PUSH_BRANCH_DENYLIST:
            raise GitSafetyError("Refusing to push a main/master/production branch as an Integration branch")
        if git.conflict_files(i["worktree_path"]):
            raise GitSafetyError("INTEGRATION_WORKTREE_DIRTY: unresolved merge conflict -- resolve before pushing")
        if git.status(i["worktree_path"]).strip():
            raise GitSafetyError("INTEGRATION_WORKTREE_DIRTY: worktree has uncommitted changes")
        try: head=git.head(i["worktree_path"])
        except Exception as exc: raise GitSafetyError(f"HEAD does not resolve: {exc}") from exc
        current_branch=git.git(i["worktree_path"],"rev-parse","--abbrev-ref","HEAD",check=False).stdout.strip()
        if current_branch!=i["branch"]:
            raise GitSafetyError(f"INTEGRATION_BRANCH_MISMATCH: worktree is on '{current_branch}', expected '{i['branch']}'")
        if not github_merge.available(i["repo_path"]):
            raise GitSafetyError("REMOTE_NOT_CONFIGURED: repository has no GitHub remote")
        try: op_id=ops.begin("integration",iid,"PUSH_INTEGRATION")
        except OperationInProgress: return RedirectResponse(f"/integrations/{iid}",303)
        db.event("integration",iid,"INTEGRATION_PUSH_STARTED",f"branch={i['branch']} local_head={head}")
        try:
            github_merge.push_branch(i["repo_path"],i["branch"])
        except GitHubIntegrationError as exc:
            msg=str(exc).lower()
            blocked=any(s in msg for s in ("non-fast-forward","fetch first","rejected","stale info"))
            db.execute("UPDATE integration_workspaces SET push_status=?,push_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       ("PUSH_BLOCKED_REMOTE_CHANGED" if blocked else "PUSH_FAILED",str(exc)[:2000],iid))
            db.event("integration",iid,"INTEGRATION_PUSH_FAILED",f"branch={i['branch']} error={exc}")
            ops.fail(op_id,exc)
            raise GitSafetyError(f"{'PUSH_BLOCKED_REMOTE_CHANGED' if blocked else 'PUSH_FAILED'}: {exc}") from exc
        db.execute("UPDATE integration_workspaces SET last_pushed_head=?,push_status='PUSHED',pushed_at=CURRENT_TIMESTAMP,push_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",(head,iid))
        db.event("integration",iid,"INTEGRATION_PUSH_SUCCEEDED",f"branch={i['branch']} head={head}")
        pr_note=""
        # Section 9/10: refresh the SAME existing PR if one exists for
        # this repo on this Task -- NEVER create a new one from here.
        ti=db.one("SELECT task_id FROM task_integrations WHERE id=?",(i["task_integration_id"],)) if i["task_integration_id"] else None
        if ti:
            mr=db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?",(ti["task_id"],i["repository_id"]))
            if mr and mr["pr_number"]:
                try:
                    # Real, observed behavior: GitHub's PR headRefOid can
                    # very briefly lag right after a push completes --
                    # a small bounded retry (never indefinite) so "push
                    # refreshes the PR" is actually reliable, not racy.
                    status=github_merge.pr_status(i["repo_path"],mr["pr_number"])
                    for _ in range(4):
                        if status["head_sha"]==head: break
                        time.sleep(0.5)
                        status=github_merge.pr_status(i["repo_path"],mr["pr_number"])
                    db.execute(
                        "UPDATE merge_records SET pr_state=?,ci_status=?,mergeability=?,merge_state_status=?,head_sha=?,last_synced_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (status["pr_state"],status["ci_status"],status["mergeability"],status["merge_state_status"],status["head_sha"],mr["id"]))
                    pr_note=f" -- PR #{mr['pr_number']} updated"
                except GitHubIntegrationError:
                    pass  # the push itself already succeeded; PR-status refresh is best-effort
        ops.succeed(op_id,f"Pushed {head[:8]}{pr_note}")
        return RedirectResponse(f"/integrations/{iid}",303)
    @app.post("/api/integrations/{iid}/reproduce-baseline")
    def reproduce_baseline_failure(iid:int,gate:str=Form(...),test_identifier:str=Form(...)):
        """[View Baseline Evidence] step 1 (section 12/22): actually
        reproduces the failure against this Integration's own recorded
        base_commit in a disposable, detached worktree -- the only way a
        BaselineFailureEvidence row is ever written. Runs in the
        background (a real gate command can take minutes); the route
        returns immediately, the Integration step polls/shows the
        resulting evidence once it lands."""
        i=integration_row(iid)
        try: run_id=gate_waivers.start_reproduction(repository_id=i["repository_id"],repo_path=i["repo_path"],base_commit=i["base_commit"],gate=gate,test_identifier=test_identifier)
        except GateWaiverError as exc: raise GitSafetyError(str(exc)) from exc
        db.event("integration",iid,"BASELINE_REPRODUCTION_STARTED",f"gate={gate} test={test_identifier} run={run_id}")
        return RedirectResponse(f"/integrations/{iid}",303)
    @app.post("/api/integrations/{iid}/waive-baseline-failure")
    def waive_baseline_failure(iid:int,gate:str=Form(...),test_identifier:str=Form(...),reason:str=Form("")):
        """[Waive Baseline Failure] (section 14-17): the ONLY way past a
        currently-failing required gate other than actually fixing it --
        and only for a failure this route independently re-classifies
        (via the exact same TaskDecisionService.integration_gate_status()
        the Integration step itself displays) as BASELINE_FAILURE right
        now, at the current fingerprint. Never a blanket 'ignore tests':
        a mismatched or already-resolved failure is refused, not silently
        accepted. A waiver never marks anything PASS -- it only makes
        this one exact (gate, test, fingerprint) not block Ready for
        Main, via PASS_WITH_APPROVED_BASELINE_WAIVER."""
        i=integration_row(iid)
        ti=db.one("SELECT task_id FROM task_integrations WHERE id=?",(i["task_integration_id"],)) if i["task_integration_id"] else None
        if not ti: raise GitSafetyError("Integration has no parent Task")
        gs=decision.integration_gate_status(i,ti["task_id"])
        match=next((f for f in gs["failures"] if f["stage"]==gate and f["test_identifier"]==test_identifier),None)
        if not match: raise GitSafetyError("No matching current failure to waive at this exact commit")
        if match["classification"]!="BASELINE_FAILURE": raise GitSafetyError(f"Not eligible for a baseline waiver (classification={match['classification']})")
        evidence=db.one("SELECT * FROM baseline_failure_evidence WHERE id=?",(match["evidence_id"],))
        gate_waivers.approve_waiver(task_id=ti["task_id"],integration_id=iid,gate=gate,test_identifier=test_identifier,
                                     failure_fingerprint_value=match["fingerprint"],baseline_commit=evidence["base_commit"],
                                     baseline_run_id=evidence["baseline_run_id"],integration_run_id=None,
                                     reason=reason.strip() or "Verified pre-existing baseline failure, unrelated to this Task.",
                                     approved_by="local-operator")
        db.event("integration",iid,"BASELINE_WAIVER_APPROVED",f"gate={gate} test={test_identifier}")
        return RedirectResponse(f"/tasks/{ti['task_id']}",303)
    @app.post("/api/integrations/{iid}/close")
    def close_integration(iid:int): i=integration_row(iid); git.close(i["repo_path"],i["worktree_path"]); db.execute("UPDATE integration_workspaces SET status='CLOSED',ready_for_main=0,closed_at=CURRENT_TIMESTAMP WHERE id=?",(iid,)); db.event("integration",iid,"WORKSPACE_CLOSED"); return RedirectResponse("/integrations",303)

    @app.get("/test-runs",response_class=HTMLResponse)
    def runs(request:Request): return render(request,"test_runs.html",runs=db.all("SELECT * FROM test_runs ORDER BY id DESC LIMIT 200"))
    @app.get("/api/test-runs/{rid}")
    def api_run(rid:int):
        row=db.one("SELECT * FROM test_runs WHERE id=?",(rid,));
        if not row: raise HTTPException(404)
        return row
    @app.get("/api/operations/{op_id}")
    def api_operation(op_id:int):
        """Polling endpoint for the generic action-button feedback model
        (Merge Latest Changes / Push Integration Branch / Create PR /
        Merge PR / Mark Ready for Main). The button-feedback JS polls
        this while status is QUEUED/RUNNING and reloads the page once it
        reaches a terminal state, so the page always re-renders from the
        real persisted result -- never a client-side guess."""
        row=db.one("SELECT * FROM operations WHERE id=?",(op_id,))
        if not row: raise HTTPException(404)
        return row
    @app.get("/test-runs/{rid}/log",response_class=PlainTextResponse)
    def run_log(rid:int):
        row=db.one("SELECT * FROM test_runs WHERE id=?",(rid,));
        if not row: raise HTTPException(404)
        return f"STDOUT (tail)\n{row['stdout_tail']}\n\nSTDERR (tail)\n{row['stderr_tail']}"
    @app.get("/settings",response_class=HTMLResponse)
    def settings_page(request:Request): return render(request,"settings.html",launchers=launcher.status())

    # ---------------------------------------------------------------- Tasks
    def recompute_task_status(tid):
        """No-op (section 31/32): TaskDecisionService.evaluate() now
        derives ACTIVE/BLOCKED/READY_FOR_MAIN/DONE live from real
        workspace/review/QA/integration/merge state on every read. Only
        BACKLOG/ACTIVE/CANCELLED are ever persisted to tasks.status, and
        those change only through explicit user actions (select/cancel) --
        never something Submit for Review should silently overwrite. Kept
        as a no-op so its existing call sites (e.g. ready()) don't need to
        change."""
        return

    def auto_create_sandbox(task_id,repository_id,repo_path,owner_type,owner_id,role,branch,commit,worktree_path,explicit_profile,task_default_profile):
        try: contract=load_sandbox_contract(Path(repo_path))
        except SandboxContractError as exc:
            db.event(owner_type.lower(),owner_id,"SANDBOX_CONTRACT_INVALID",str(exc)); return None
        if contract is None: return None
        profile=resolve_profile(contract,explicit_profile,task_default_profile)
        if profile=="NONE": return None
        source=SourceSpec(repository_id=repository_id,role=role,branch=branch,commit_sha=commit,worktree_path=str(worktree_path),repo_path=str(repo_path),source_type=owner_type)
        try:
            sid=sandboxes.create(task_id=task_id,owner_type=owner_type,owner_id=owner_id,profile=profile,provider=source)
        except SandboxError as exc:
            db.event(owner_type.lower(),owner_id,"SANDBOX_CREATE_FAILED",str(exc)); return None
        if sid is None: return None
        try: sandboxes.provision(sid)
        except SandboxError: pass  # sandbox row already recorded FAILED; source worktree untouched
        return sid

    KANBAN_COLUMNS=["Backlog","Development","Review / QA","Integration","Ready for Main","Done","Blocked"]
    def workspace_status_label(status):
        """CREATED reads as CODING to a user -- the DB enum stays CREATED/
        READY/CLOSED (no new status column), this is presentation only."""
        return {"CREATED":"CODING"}.get(status,status)
    def kanban_column_for(d):
        """Kanban column, mapped from TaskDecisionService's own computed
        status/stage -- never a second calculation. Section 35's 7 columns
        are coarser than the 6 statuses x 7 stages TaskDecisionService can
        return, so this is a pure lookup, not new logic."""
        if d["status"]=="BLOCKED": return "Blocked"
        if d["status"]=="BACKLOG": return "Backlog"
        if d["status"]=="DONE": return "Done"
        if d["status"]=="READY_FOR_MAIN": return "Ready for Main"
        return {"PLANNING":"Development","DEVELOPMENT":"Development","REVIEW":"Review / QA","QA":"Review / QA",
                "INTEGRATION":"Integration","MERGING":"Integration","COMPLETE":"Done"}.get(d["stage"],"Development")
    def task_card_view(t):
        """Everything one Task card (Kanban or List) needs. status/stage/
        next_action/ready_for_main/blocking_reasons all come from
        TaskDecisionService.evaluate() -- this function only adds the
        lightweight display aggregates (sandbox/test tallies, repo list)
        that decision itself has no reason to compute."""
        d=decision.evaluate(t["id"])
        ws=d["builders"]
        sbxs=task_sandboxes(t["id"])
        # CLEANUP_ELIGIBLE is a retention countdown, not a runtime state --
        # counted as running here too so a just-completed Task's card
        # doesn't misreport its still-live sandbox as not running.
        sandbox={"total":len(sbxs),"running":sum(1 for s in sbxs if s["status"] in ("RUNNING","CLEANUP_ELIGIBLE")),
                 "unhealthy":sum(1 for s in sbxs if s["status"] in ("RUNNING","CLEANUP_ELIGIBLE") and s["health_status"]!="HEALTHY")}
        tests={"passed":sum(1 for w in ws if w["ready"]),"total":len(ws)}
        agents={"total":len(ws),"ready":sum(1 for w in ws if w["ready"]),
                "coding":sum(1 for w in ws if not w["ready"]),"failed":sum(1 for w in ws if w["fix_required"])}
        column=kanban_column_for(d)
        # CSS-safe class for the column badge -- "Review / QA" etc. can't
        # be lowercased straight into a class name.
        column_class={"Backlog":"backlog","Development":"development","Review / QA":"review",
                      "Integration":"integration","Ready for Main":"ready","Done":"done","Blocked":"blocked"}.get(column,"development")
        for w in ws: w["status_label"]=workspace_status_label(w["status"])
        blocking_workspace=next((w for w in ws if w["fix_required"]), next((w for w in ws if not w["ready"]), None))
        return {"task":t,"decision":d,"workspaces":ws,"agents":agents,"sandbox":sandbox,"tests":tests,
                "integration":("NONE" if not d["task_integration"] else d["task_integration"]["status"]),
                "column":column,"column_class":column_class,"blocking_workspace":blocking_workspace,"repos":sorted({w["repo_name"] for w in ws}),
                "next_action":d["next_action"],"needs_fix":bool(d["blocking_reasons"]),
                "status":d["status"],"stage":d["stage"],"ready_for_main":d["ready_for_main"]}

    def task_matches_filters(card,*,status,repository,agent,sandbox_status,test_status,integration_status,q):
        t=card["task"]
        if status and card["column"]!=status: return False
        if repository and repository not in card["repos"]: return False
        if agent and not any(w["agent"]==agent for w in card["workspaces"]): return False
        if sandbox_status=="running" and card["sandbox"]["running"]==0: return False
        if sandbox_status=="unhealthy" and card["sandbox"]["unhealthy"]==0: return False
        if sandbox_status=="none" and card["sandbox"]["total"]>0: return False
        if test_status=="pass" and not (card["tests"]["total"] and card["tests"]["passed"]==card["tests"]["total"]): return False
        if test_status=="fail" and card["agents"]["failed"]==0: return False
        if test_status=="not_run" and card["tests"]["passed"]>0: return False
        if integration_status and card["integration"]!=integration_status: return False
        if q and q.lower() not in t["title"].lower() and q.lower() not in t["slug"].lower(): return False
        return True

    def parse_filters(status,repository,agent,sandbox_status,test_status,integration_status,q):
        f={"status":status,"repository":repository,"agent":agent,"sandbox_status":sandbox_status,"test_status":test_status,"integration_status":integration_status,"q":q}
        return f,urlencode({k:v for k,v in f.items() if v})

    @app.get("/tasks",response_class=HTMLResponse)
    def tasks_page(request:Request,status:str="",repository:str="",agent:str="",sandbox_status:str="",test_status:str="",integration_status:str="",q:str=""):
        filters,qs=parse_filters(status,repository,agent,sandbox_status,test_status,integration_status,q)
        rows=db.all("SELECT * FROM tasks ORDER BY updated_at DESC")
        cards=[task_card_view(t) for t in rows]
        filtered=[c for c in cards if task_matches_filters(c,**filters)]
        return render(request,"tasks.html",cards=filtered,filters=filters,filters_qs=qs,columns=KANBAN_COLUMNS,
                      repositories=db.all("SELECT * FROM repositories WHERE enabled=1"),agents=settings.agents)
    @app.get("/kanban",response_class=HTMLResponse)
    def kanban_page(request:Request,status:str="",repository:str="",agent:str="",sandbox_status:str="",test_status:str="",integration_status:str="",q:str=""):
        filters,qs=parse_filters(status,repository,agent,sandbox_status,test_status,integration_status,q)
        rows=db.all("SELECT * FROM tasks WHERE status NOT IN ('CANCELLED') ORDER BY updated_at DESC")
        cards=[task_card_view(t) for t in rows]
        filtered=[c for c in cards if task_matches_filters(c,**filters)]
        board={col:[c for c in filtered if c["column"]==col] for col in KANBAN_COLUMNS}
        return render(request,"kanban.html",board=board,columns=KANBAN_COLUMNS,filters=filters,filters_qs=qs,
                      repositories=db.all("SELECT * FROM repositories WHERE enabled=1"),agents=settings.agents)
    @app.post("/api/tasks")
    def create_task(title:str=Form(...),description:str=Form(""),priority:str=Form("NORMAL"),tags:str=Form(""),repo_scope_id:str=Form(""),notes:str=Form(""),risk_profile:str=Form("NORMAL")):
        """The primary Task creation flow (section 1): a Task lands in
        BACKLOG with no branch/worktree/sandbox allocated at all -- those
        only appear once the Task is explicitly Selected (see /select) and
        an Agent Workspace is explicitly created (see add_task_workspace's
        BACKLOG gate below)."""
        slug=slugify(title); priority=priority.strip().upper() or "NORMAL"; risk_profile=risk_profile.strip().upper()
        if risk_profile not in RISK_PROFILES: risk_profile="NORMAL"
        scope=int(repo_scope_id) if repo_scope_id.strip().isdigit() else None
        tid=db.execute("INSERT INTO tasks(slug,title,description,status,priority,tags,repo_scope_id,notes,risk_profile) VALUES(?,?,?,?,?,?,?,?,?)",
                        (slug,title,description,"BACKLOG",priority,tags.strip(),scope,notes.strip(),risk_profile))
        db.event("task",tid,"TASK_CREATED_BACKLOG",slug); return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/select")
    def select_task(tid:int):
        """[Select for Development] (section 7): BACKLOG -> ACTIVE. Still
        allocates nothing -- Task Stage stays PLANNING (computed) until an
        Agent Workspace actually exists."""
        t=task_row(tid)
        if t["status"]!="BACKLOG": raise GitSafetyError(f"Task is not in BACKLOG (status={t['status']})")
        db.execute("UPDATE tasks SET status='ACTIVE',updated_at=CURRENT_TIMESTAMP WHERE id=?",(tid,)); db.event("task",tid,"TASK_SELECTED")
        return RedirectResponse(f"/tasks/{tid}",303)
    BRIEF_FIELDS=("brief_goal","brief_context","brief_requirements","brief_acceptance_criteria","brief_out_of_scope","brief_test_plan","brief_risks")
    @app.post("/api/tasks/{tid}/brief")
    def save_brief(tid:int,goal:str=Form(""),context:str=Form(""),requirements:str=Form(""),acceptance_criteria:str=Form(""),out_of_scope:str=Form(""),test_plan:str=Form(""),risks:str=Form(""),risk_profile:str=Form("")):
        """Implementation Brief (section 8): structured fields, saved in
        place (one current brief per Task, not a history log) -- but
        brief_version bumps whenever the content that actually drives a
        Builder/Reviewer prompt changes, which is what makes existing
        Review/QA evidence recompute STALE (TaskDecisionService.
        builder_view) instead of silently staying valid."""
        t=task_row(tid)
        new_values={"brief_goal":goal.strip(),"brief_context":context.strip(),"brief_requirements":requirements.strip(),
                    "brief_acceptance_criteria":acceptance_criteria.strip(),"brief_out_of_scope":out_of_scope.strip(),
                    "brief_test_plan":test_plan.strip(),"brief_risks":risks.strip()}
        changed=any((t.get(f) or "")!=new_values[f] for f in BRIEF_FIELDS)
        rp=risk_profile.strip().upper()
        sets=[f"{f}=?" for f in BRIEF_FIELDS]+["updated_at=CURRENT_TIMESTAMP"]
        params=[new_values[f] for f in BRIEF_FIELDS]
        if rp in RISK_PROFILES: sets.insert(-1,"risk_profile=?"); params.append(rp)
        if changed: sets.insert(-1,"brief_version=brief_version+1")
        db.execute(f"UPDATE tasks SET {','.join(sets)} WHERE id=?",(*params,tid))
        db.event("task",tid,"BRIEF_SAVED",f"brief_version+1={changed}")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/generate-prompt")
    def generate_prompt(tid:int):
        """Fill AGENT PROMPT from the Task's current prompt/brief
        (deterministic template, never a model call) -- the user still
        reviews/edits it before any agent is launched (section 3). Kept
        for the legacy structured-brief flow's own explicit button; the
        new prompt-first flow calls regenerate_agent_prompt() itself on
        create/edit instead of requiring this extra click."""
        ws=task_workspaces(tid)
        repo_row=repo(ws[0]["repository_id"]) if ws else None
        regenerate_agent_prompt(tid,repo_row)
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/prompt")
    def save_prompt(tid:int,implementation_prompt:str=Form("")):
        """Edit the Implementation Prompt (section: 'do not rewrite the
        user's prompt silently' -- only the user changes this text, ever).
        Bumps brief_version on an actual content change, the exact same
        mechanism save_brief already uses for the legacy structured
        fields, so TaskDecisionService.builder_view() flips any Review/QA
        pinned to the old version to STALE automatically -- no separate
        prompt_version bookkeeping needed. Regenerates the derived
        agent_prompt so it always reflects the latest saved intent."""
        t=task_row(tid)
        new_prompt=implementation_prompt.strip()
        changed=(t.get("implementation_prompt") or "")!=new_prompt
        sets=["implementation_prompt=?","updated_at=CURRENT_TIMESTAMP"]
        params=[new_prompt]
        if changed: sets.insert(-1,"brief_version=brief_version+1")
        db.execute(f"UPDATE tasks SET {','.join(sets)} WHERE id=?",(*params,tid))
        db.event("task",tid,"PROMPT_SAVED",f"brief_version+1={changed}")
        if new_prompt:
            ws=task_workspaces(tid)
            repo_row=repo(ws[0]["repository_id"]) if ws else None
            regenerate_agent_prompt(tid,repo_row)
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/agent-prompt")
    def save_agent_prompt(tid:int,agent_prompt:str=Form("")):
        t=task_row(tid); db.execute("UPDATE tasks SET agent_prompt=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(agent_prompt,tid))
        latest=db.one("SELECT id FROM prompts WHERE task_id=? AND prompt_type='BUILDER' ORDER BY id DESC LIMIT 1",(tid,))
        if latest: db.execute("UPDATE prompts SET content=? WHERE id=?",(agent_prompt,latest["id"]))
        else: db.execute("INSERT INTO prompts(task_id,prompt_type,brief_version,content) VALUES(?,?,?,?)",(tid,"BUILDER",t["brief_version"],agent_prompt))
        return RedirectResponse(f"/tasks/{tid}",303)
    def latest_prompt(tid,prompt_type="BUILDER"):
        return db.one("SELECT * FROM prompts WHERE task_id=? AND prompt_type=? ORDER BY id DESC LIMIT 1",(tid,prompt_type))
    @app.post("/api/tasks/new-with-workspace")
    async def create_task_with_workspace(request:Request):
        """Advanced/quick-start shortcut: Task + at least one Agent
        Workspace in one submit (optionally many, for a cross-repo Task
        defined immediately), skipping BACKLOG for when planning is
        unnecessary -- created directly ACTIVE since a Builder Workspace
        is being attached in the same request (section 5/7). A failed
        workspace never rolls back the Task or any already-created
        workspace -- it is recorded and shown, not hidden."""
        form=await request.form()
        title=str(form.get("title","")).strip()
        if not title: raise GitSafetyError("Task title is required")
        description=str(form.get("description",""))
        repo_ids=form.getlist("ws_repository_id"); agents=form.getlist("ws_agent"); roles=form.getlist("ws_role")
        bases=form.getlist("ws_base_branch"); profiles=form.getlist("ws_sandbox_profile")
        slug=slugify(title); tid=db.execute("INSERT INTO tasks(slug,title,description,status) VALUES(?,?,?,?)",(slug,title,description,"ACTIVE"))
        db.event("task",tid,"TASK_CREATED",slug)
        for i,rid_raw in enumerate(repo_ids):
            if not str(rid_raw).strip(): continue
            agent=agents[i] if i<len(agents) else ""
            if not agent: continue
            role=roles[i] if i<len(roles) else ""; base=(bases[i] if i<len(bases) else "main") or "main"
            profile=profiles[i] if i<len(profiles) else ""
            result=add_task_workspace(tid,int(rid_raw),agent,role,base,profile)
            if not result["ok"]: db.event("task",tid,"WORKSPACE_CREATE_FAILED",f"{agent} · repo #{rid_raw}: {result['error']}")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/create")
    async def create_task_unified(request:Request):
        """The primary, simplified Task Create flow: ONE Implementation
        Prompt instead of a structured Brief form (Task title / Prompt /
        Repository / Agent / Sandbox, everything else tucked under
        Advanced). Repository+Agent are optional: filled in, the Task goes
        straight to ACTIVE with its first Builder Workspace ("Create &
        Start"); left blank, it lands in BACKLOG with just the prompt
        saved as intent -- the same BACKLOG contract as /api/tasks (no
        branch/worktree/sandbox allocated at all). Advanced's "additional
        repositories" reuses the exact ws_repository_id/ws_agent/ws_role/
        ws_base_branch/ws_sandbox_profile array fields /new-with-workspace
        already established, so a cross-repo Task can still be defined in
        one submit. A failed workspace never rolls back the Task or any
        already-created workspace, same as /new-with-workspace."""
        form=await request.form()
        title=str(form.get("title","")).strip()
        if not title: raise GitSafetyError("Task title is required")
        prompt=str(form.get("implementation_prompt","")).strip()
        risk_profile=str(form.get("risk_profile","NORMAL")).strip().upper()
        if risk_profile not in RISK_PROFILES: risk_profile="NORMAL"

        rows=[]
        primary_repo=str(form.get("repository_id","")).strip()
        primary_agent=str(form.get("agent","")).strip()
        if primary_repo and primary_agent:
            rows.append((primary_repo,primary_agent,str(form.get("primary_role","")).strip(),
                         str(form.get("primary_base_branch","main")).strip() or "main",
                         str(form.get("sandbox_profile","")).strip()))
        extra_repo=form.getlist("ws_repository_id"); extra_agent=form.getlist("ws_agent")
        extra_role=form.getlist("ws_role"); extra_base=form.getlist("ws_base_branch"); extra_profile=form.getlist("ws_sandbox_profile")
        for i,rid_raw in enumerate(extra_repo):
            if not str(rid_raw).strip(): continue
            a=extra_agent[i] if i<len(extra_agent) else ""
            if not a: continue
            rows.append((rid_raw,a,extra_role[i] if i<len(extra_role) else "",
                         (extra_base[i] if i<len(extra_base) else "main") or "main",
                         extra_profile[i] if i<len(extra_profile) else ""))

        slug=slugify(title); status="ACTIVE" if rows else "BACKLOG"
        tid=db.execute("INSERT INTO tasks(slug,title,status,risk_profile,implementation_prompt) VALUES(?,?,?,?,?)",
                        (slug,title,status,risk_profile,prompt))
        db.event("task",tid,"TASK_CREATED",slug)

        primary_repo_row=None
        for rid_raw,agent,role,base,profile in rows:
            try: rid=int(rid_raw)
            except ValueError: db.event("task",tid,"WORKSPACE_CREATE_FAILED",f"{agent} · invalid repository id {rid_raw!r}"); continue
            result=add_task_workspace(tid,rid,agent,role,base,profile)
            if not result["ok"]: db.event("task",tid,"WORKSPACE_CREATE_FAILED",f"{agent} · repo #{rid_raw}: {result['error']}")
            elif primary_repo_row is None:
                try: primary_repo_row=repo(rid)
                except HTTPException: pass

        if prompt: regenerate_agent_prompt(tid,primary_repo_row)
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.get("/api/tasks")
    def api_tasks(): return db.all("SELECT * FROM tasks ORDER BY updated_at DESC")
    @app.get("/tasks/{tid}",response_class=HTMLResponse)
    def task_detail(request:Request,tid:int):
        """Every status/stage/gate/next-action value on this page comes
        from one call to decision.evaluate() (TaskDecisionService) --
        this route only attaches display-only detail (git worktree info,
        live session, per-workspace/integration sandbox view) onto that
        result. No independent readiness/stage computation lives here
        any more (section 32/37)."""
        t=task_row(tid)
        d=decision.evaluate(tid)
        workspaces=d["builders"]; ti=d["task_integration"]; ti_repos=d["integration_repos"]
        sbxs=task_sandboxes(tid)

        for w in workspaces:
            w["details"]=safe_details(w["worktree_path"]); w["status_label"]=workspace_status_label(w["status"])
            session=latest_session_for_workspace(w["id"]); w["session"]=session
            # Section 1/3 of the Live Terminal fix: a session only has an
            # actual terminal to attach to while it's STARTING/RUNNING/
            # WAITING_FOR_INPUT -- the latest session for a workspace is
            # very often already EXITED/FAILED (the normal end state after
            # a Builder finishes and submits for review), and a template
            # must never render "Open Live Terminal" pointing at a dead
            # session's route as if it were still attachable.
            w["live_session"]=session if session and session["status"] in LIVE_SESSION_STATUSES else None
            # Builder-completion-vs-live-session fix (section 3/4): the
            # agent's own completion report is only ever PRINTED to its
            # terminal -- there is no API callback wired up for it to
            # call. Detect a well-formed report in the session's own live
            # transcript (never auto-submitted -- section 4/13) so a
            # human gets one clear, one-click confirm action instead of
            # having to notice, copy and manually retype it into the
            # Edit Agent Report form themselves.
            w["detected_report"]=None
            if session and not w["ready"]:
                w["detected_report"]=parse_completion_report(agent_sessions.live_tail(session["id"]))
            # EXITED-without-report dead-end fix: AgentSession EXITED never
            # by itself means the Builder failed (section 8) -- it can just
            # as easily mean the source work finished but the process
            # exited before/without a report ever getting persisted. Only
            # compute the real-worktree validation (never a guess) when
            # there is genuinely no other evidence of completion yet.
            w["recovery_state"]="COMPLETION_REQUIRED" if w["agent_status"] in ("EXITED","FAILED") and not w["ready"] and not w["detected_report"] else None
            w["manual_ready_check"]=validate_manual_ready(w) if w["recovery_state"] else None
            w_repo=repo(w["repository_id"])
            try: w["sandbox_configured"]=load_sandbox_contract(Path(w_repo["repo_path"])) is not None
            except SandboxContractError: w["sandbox_configured"]=True  # misconfigured contract is not "absent"
            # Live per-Builder Workspace prompt (Task/Title-fallback + role
            # + Builder Instructions + sandbox + AGENTS.md) -- always
            # freshly computed for display, never a stored/stale copy.
            w["live_prompt"]=workspace_agent_prompt(w,t,w_repo)
            sb=next((s for s in sbxs if s["owner_type"]=="AGENT_WORKSPACE" and s["owner_id"]==w["id"]),None)
            if sb:
                v=sandbox_view(sb); v["stale"]=sandboxes.is_stale(sb["id"],sandbox_current_commits(sb["id"]))
                w["sandbox"]=v
            else:
                w["sandbox"]=None

        integration_sandboxes=[]
        for sb in sbxs:
            if sb["owner_type"]!="TASK_INTEGRATION": continue
            v=sandbox_view(sb); v["stale"]=sandboxes.is_stale(sb["id"],sandbox_current_commits(sb["id"]))
            v["sources"]=db.all("SELECT s.*,r.repo_name FROM sandbox_sources s JOIN repositories r ON r.id=s.repository_id WHERE s.sandbox_id=?",(sb["id"],))
            v["hardware"]=db.one("SELECT * FROM hardware_test_results WHERE sandbox_id=? ORDER BY id DESC LIMIT 1",(sb["id"],))
            integration_sandboxes.append(v)

        earliest_cleanup=min((sb["cleanup_eligible_at"] for sb in sbxs if sb["cleanup_eligible_at"]),default=None)
        blocking_workspace=next((w for w in workspaces if w["fix_required"]), next((w for w in workspaces if not w["ready"]),None))
        not_ready=[w for w in workspaces if not w["ready"]]

        # Real merge tracking (section 4/9): every MergeRecord's live
        # blocker list, computed fresh from the same `d` the rest of the
        # page already reads -- never a second, template-side gate.
        for m in d["merge_records"]:
            try: m_repo=repo(m["repository_id"])
            except HTTPException: m_repo=None
            m["github_available"]=bool(m_repo) and github_merge.available(m_repo["repo_path"])
            m["gate"]=decision.merge_gate_status(d,m["repository_id"],m)
            repo_ti=next((x for x in ti_repos if x["repository_id"]==m["repository_id"]),None)
            m["integration_id"]=repo_ti["id"] if repo_ti else None
            # Button-state-ux: Create PR / Merge PR feedback, keyed by
            # this exact MergeRecord row (never repository_id alone --
            # the same repo can have a MergeRecord per Task).
            m["create_pr_op"]=ops.latest("merge_record",m["id"],"CREATE_PR")
            m["merge_pr_op"]=ops.latest("merge_record",m["id"],"MERGE_PR")
            # Deployment (section 6/23): only ever meaningful once this
            # repo is actually MERGED -- computed for every repo anyway
            # (cheap, no real command execution) so a repo that merges
            # AFTER Task DONE also gets its own Deployment section
            # without a second code path.
            if m["merge_status"]=="MERGED":
                dep_target=deployer.target(m_repo["repo_path"],"DEV") if m_repo else None
                dep_row=latest_deployment(tid,m["repository_id"],"DEV")
                rb_target=deployer.rollback_target(dep_row) if dep_row else None
                m["deployment_dev"]=deployment_view(dep_row,bool(dep_target),rb_target)
            else:
                m["deployment_dev"]=None

        # Overview gate checklist (section 38): one row per gate this
        # Task's risk policy actually requires, ✓/○ from the same
        # decision the rest of the page reads -- explains exactly why
        # not ready, never a second ad hoc readiness calculation.
        gates=[{"label":"Task intent resolvable","ok":decision.brief_complete(t),"note":f"source: {d['prompt_source'].replace('_',' ').title()}"},
               {"label":"At least one Builder Workspace","ok":bool(workspaces)},
               {"label":"All Builder Workspaces submitted for review","ok":bool(workspaces) and all(b["ready"] for b in workspaces)},
               {"label":"All reviews PASS (exact commit, current Brief)","ok":bool(workspaces) and all(b["review_status"]=="PASS" for b in workspaces)}]
        if decision.requires_qa(d["risk_profile"]):
            gates.append({"label":"QA PASS (current Brief)","ok":decision.qa_current(d["qa"],t) and d["qa"]["status"]=="PASS"})
        else:
            gates.append({"label":"QA","ok":True,"note":"NOT_REQUIRED for this risk profile"})
        if decision.requires_integration(d["risk_profile"]):
            # Section 16: READY_FOR_MAIN must never look like a plain PASS
            # when it only got there via an approved baseline waiver --
            # count waived failures across every participating repo's
            # live gate_status so the checklist says PASS WITH N BASELINE
            # WAIVER, never a silent, indistinguishable-from-real PASS.
            waived_count=sum(1 for r in ti_repos for f in ((r.get("gate_status") or {}).get("failures") or []) if f["classification"]=="WAIVED")
            note=f"PASS WITH {waived_count} BASELINE WAIVER" if waived_count and decision.integration_healthy(ti,ti_repos) else None
            gates.append({"label":"Integration healthy, tests PASS, no conflicts","ok":decision.integration_healthy(ti,ti_repos),"note":note})
        else:
            gates.append({"label":"Integration","ok":True,"note":"NOT_REQUIRED for this risk profile"})
        gates.append({"label":"No blocking findings","ok":not d["blocking_reasons"]})
        gates.append({"label":"All required repos merged to main","ok":bool(d["merge_records"]) and all(m["merge_status"]=="MERGED" for m in d["merge_records"] if m["required"])})

        # Default reviewer differs from the builder's own agent when
        # another trusted agent is configured (section 10) -- computed
        # once per workspace, purely a UI default, never enforced.
        for w in workspaces:
            other=[a for a in settings.agents if a!=w["agent"]]
            w["default_reviewer"]=other[0] if other else w["agent"]
            w["last_activity_hint"]=activity_summary(w["session"]["id"], 200) if w["session"] else None

        qa_required=decision.requires_qa(d["risk_profile"]); integration_required=decision.requires_integration(d["risk_profile"])
        return render(request,"task_detail.html",t=t,decision=d,workspaces=workspaces,sandboxes=sbxs,task_integration=ti,ti_repos=ti_repos,
                      integration_sandboxes=integration_sandboxes,
                      status=d["status"],stage=d["stage"],risk_profile=d["risk_profile"],next_action=d["next_action"],
                      blocking_reasons=d["blocking_reasons"],test_readiness=d["test_readiness"],ready_for_main=d["ready_for_main"],
                      merge_records=d["merge_records"],qa=d["qa"],gates=gates,current_step=d["current_step"],
                      prompt_source=d["prompt_source"],effective_task_prompt=d["effective_task_prompt"],
                      qa_required=qa_required,
                      task_cleanup_countdown=format_countdown(earliest_cleanup),
                      blocking_workspace=blocking_workspace,not_ready_workspaces=not_ready,
                      repositories=db.all("SELECT * FROM repositories WHERE enabled=1"),agents=settings.agents,
                      user_state=user_task_state(d),progress=progress_summary(d["current_step"],qa_required,integration_required))
    @app.get("/api/tasks/{tid}")
    def api_task(tid:int):
        t=task_row(tid); return {**t,"workspaces":task_workspaces(tid),"sandboxes":task_sandboxes(tid),"task_integration":task_integration_row(tid)}
    @app.get("/api/tasks/{tid}/decision")
    def api_task_decision(tid:int):
        """The exact TaskDecisionService.evaluate() result -- the single
        source every page on this Task reads (section 32). Exposed
        directly so automation/tests never have to re-derive status/
        stage/gates from raw child rows themselves."""
        task_row(tid); return decision.evaluate(tid)
    @app.post("/api/workspaces/{wid}/builder-instructions")
    def save_builder_instructions(wid:int,builder_instructions:str=Form("")):
        """Optional, per-Builder-Workspace extra instructions layered on
        top of the Task's own effective prompt -- e.g. distinguishing what
        a Backend Builder should do from what a Firmware Builder on the
        same Task should do. Never required; empty means 'use the Task
        prompt alone'. Not separately versioned (see migration V8)."""
        w=agent_row(wid)
        db.execute("UPDATE agent_workspaces SET builder_instructions=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(builder_instructions.strip(),wid))
        db.event("agent",wid,"BUILDER_INSTRUCTIONS_SAVED")
        return RedirectResponse(f"/tasks/{w['task_id']}" if w.get("task_id") else f"/workspaces/{wid}",303)
    def add_task_workspace(tid,repository_id,agent,role,base_branch,sandbox_profile):
        """Single source for 'create one Agent Workspace inside a Task':
        used by the classic one-at-a-time /api/tasks/{tid}/workspaces route
        AND by New-Task's inline multi-workspace flow, so a failure in one
        workspace during Task creation is handled the exact same way (Task
        and any already-created workspace stay, nothing is silently rolled
        back) as adding a workspace later."""
        t=task_row(tid)
        if t["status"]=="BACKLOG": return {"ok":False,"error":"Task must be Selected for Development before creating an Agent Workspace"}
        try: r=repo(repository_id)
        except HTTPException as exc: return {"ok":False,"error":str(exc.detail)}
        agent_s=slugify(agent)
        if agent_s not in settings.agents: return {"ok":False,"error":f"Agent not allowed: {agent}"}
        # Branch/worktree name must include the repo, not just the task slug
        # (matches the same qualification already used for Integration
        # Workspaces below) -- agent_workspaces.branch is UNIQUE across the
        # WHOLE table, not scoped per repository_id, so a multi-repo Task
        # using the same agent for a second repo would otherwise compute
        # the identical branch string and fail with a raw
        # "UNIQUE constraint failed: agent_workspaces.branch" at INSERT
        # time even though each repo's own git history has no collision.
        try: branch,path,commit=git.create_agent(r["repo_path"],agent_s,f"{t['slug']}-{r['repo_name']}",base_branch)
        except (GitSafetyError,GitCommandError) as exc: return {"ok":False,"error":str(exc)}
        role_clean=role.strip()[:80]; profile_clean=sandbox_profile.strip().upper() or None
        try:
            wid=db.execute("INSERT INTO agent_workspaces(repository_id,agent,task_name,branch,worktree_path,base_branch,base_commit,last_commit,status,task_id,role,sandbox_profile) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (repository_id,agent_s,t["slug"],branch,str(path),base_branch,commit,commit,"CREATED",tid,role_clean,profile_clean))
        except Exception as exc:
            if not git.status(path).strip(): git.close(r["repo_path"],path)
            # Never surface a raw SQLite message (e.g. "UNIQUE constraint
            # failed: agent_workspaces.branch") to the browser -- the repo
            # qualification above already prevents the known cross-repo
            # collision; this is a last-resort, honest but readable
            # fallback for anything else that still hits the constraint.
            msg = "A workspace with this exact branch/worktree already exists for this task." if "UNIQUE constraint failed" in str(exc) else str(exc)
            return {"ok":False,"error":msg}
        db.event("agent",wid,"WORKSPACE_CREATED",branch)
        db.execute("UPDATE tasks SET updated_at=CURRENT_TIMESTAMP WHERE id=?",(tid,))
        sid=auto_create_sandbox(tid,repository_id,r["repo_path"],"AGENT_WORKSPACE",wid,role_clean or agent_s,branch,commit,path,profile_clean,t.get("default_sandbox_profile"))
        if sid: db.execute("UPDATE agent_workspaces SET sandbox_profile=(SELECT profile FROM sandboxes WHERE id=?) WHERE id=?",(sid,wid))
        return {"ok":True,"workspace_id":wid,"repo_name":r["repo_name"],"agent":agent_s}

    @app.post("/api/tasks/{tid}/workspaces")
    def create_task_workspace(tid:int,repository_id:int=Form(...),agent:str=Form(...),role:str=Form(""),base_branch:str=Form("main"),sandbox_profile:str=Form("")):
        result=add_task_workspace(tid,repository_id,agent,role,base_branch,sandbox_profile)
        if not result["ok"]: raise GitSafetyError(result["error"])
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/setup-and-start")
    def setup_and_start(tid:int,repository_id:str=Form(""),agent:str=Form(""),role:str=Form(""),base_branch:str=Form("main"),sandbox_profile:str=Form("")):
        """The Wizard's SETUP step single primary action ('Start Claude' /
        'Start Codex', section 4): Select for Development if still in
        BACKLOG, create the Builder Workspace if one doesn't already
        exist for this repo+agent pairing, then immediately start its
        AgentSession -- one click covers workspace allocation through a
        live running agent. The user never has to separately visit Agent
        Workspaces. With no repository/agent given, starts every
        NOT_STARTED existing Builder Workspace instead (covers 'workspace
        already exists, just press Start' and the multi-Builder case)."""
        t=task_row(tid)
        if t["status"]=="BACKLOG":
            db.execute("UPDATE tasks SET status='ACTIVE',updated_at=CURRENT_TIMESTAMP WHERE id=?",(tid,)); db.event("task",tid,"TASK_SELECTED")
        w=None
        if repository_id.strip() and agent.strip():
            rid=int(repository_id); agent_s=slugify(agent)
            existing=[x for x in task_workspaces(tid) if x["repository_id"]==rid and x["agent"]==agent_s]
            if existing:
                w=existing[0]
            else:
                result=add_task_workspace(tid,rid,agent,role,base_branch or "main",sandbox_profile)
                if not result["ok"]: raise GitSafetyError(result["error"])
                w=[x for x in task_workspaces(tid) if x["repository_id"]==rid and x["agent"]==agent_s][-1]
        if w is not None:
            try: _resume_builder_session(w)
            except SessionError as exc: raise GitSafetyError(str(exc)) from exc
        else:
            for b in decision.evaluate(tid)["builders"]:
                if b["agent_status"]=="NOT_STARTED":
                    try: _start_builder_session(agent_row(b["id"]))
                    except SessionError as exc: db.event("agent",b["id"],"SESSION_START_FAILED",str(exc))
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/integrations")
    def create_task_integration(tid:int):
        """Integration eligibility (section 22): only once every Builder
        Workspace is READY with a current, PASS review and nothing
        unresolved -- decision.evaluate() is the one gate, never a looser
        'has at least one READY workspace' check a route computes itself."""
        t=task_row(tid)
        d=decision.evaluate(tid)
        if not d["integration_eligibility"]["eligible"]:
            raise GitSafetyError("Not eligible for Integration yet: every Builder Workspace must be READY with a current, PASS review")
        ready=[w for w in task_workspaces(tid) if w["status"]=="READY"]
        tiid=db.execute("INSERT INTO task_integrations(task_id,status) VALUES(?,?)",(tid,"MERGING"))
        # tasks.status stays ACTIVE -- INTEGRATING/TESTING are Task Stage
        # values now, computed live by TaskDecisionService, never persisted
        # to this column (section 3/31).
        db.event("task",tid,"INTEGRATION_CREATED",str(tiid))
        by_repo={}
        for w in ready: by_repo.setdefault(w["repository_id"],[]).append(w)
        overall_conflict=False; repo_rows=[]
        for repository_id,sources in by_repo.items():
            r=repo(repository_id)
            # branch names are unique per-repo in git, but integration_workspaces.branch
            # is a single global-unique DB column across every managed repo --
            # qualify with the repo slug so two repos in the same Task never collide.
            branch,path,commit=git.create_integration(r["repo_path"],f"{t['slug']}-{r['repo_name']}","main")
            iid=db.execute("INSERT INTO integration_workspaces(repository_id,name,branch,worktree_path,base_branch,base_commit,status,task_integration_id) VALUES(?,?,?,?,?,?,?,?)",
                           (repository_id,t["slug"],branch,str(path),"main",commit,"MERGING",tiid))
            db.event("integration",iid,"INTEGRATION_CREATED",branch); conflict=False; pinned_commit=commit; pinned_branch=branch
            for source in sources:
                result=git.merge(path,source["branch"]); current=git.head(r["repo_path"],source["branch"])
                db.execute("INSERT INTO integration_sources(integration_id,workspace_id,merged_commit,merged_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",(iid,source["id"],current))
                if result.returncode:
                    db.execute("UPDATE integration_workspaces SET status='CONFLICT' WHERE id=?",(iid,)); db.event("integration",iid,"MERGE_CONFLICT",source["branch"]); conflict=True; break
                db.event("integration",iid,"BRANCH_MERGED",source["branch"])
                # Sandbox pinning must track the ORIGINAL source branch's own
                # commit (what "the branch changed" means for staleness, docs
                # section 29), never the integration branch's own --no-ff
                # merge commit hash -- those never match even when nothing
                # about the source has changed. With >1 source per repo this
                # is the last one merged; combining multiple same-repo
                # sources into a single pinned identity is a known V1
                # simplification (see docs/CI_CD note in report).
                pinned_commit=current; pinned_branch=source["branch"]
            db.execute("UPDATE integration_workspaces SET status=? WHERE id=?",("CONFLICT" if conflict else "TESTING",iid))
            overall_conflict=overall_conflict or conflict
            repo_rows.append({"repository_id":repository_id,"integration_id":iid,"repo_path":r["repo_path"],"worktree_path":str(path),"branch":pinned_branch,"pinned_commit":pinned_commit})
        if overall_conflict:
            db.execute("UPDATE task_integrations SET status='CONFLICT',updated_at=CURRENT_TIMESTAMP WHERE id=?",(tiid,))
            return RedirectResponse(f"/tasks/{tid}",303)
        db.execute("UPDATE task_integrations SET status='TESTING',updated_at=CURRENT_TIMESTAMP WHERE id=?",(tiid,))
        provider=None; extra=[]
        for row_ in repo_rows:
            try: contract=load_sandbox_contract(Path(row_["repo_path"]))
            except SandboxContractError: contract=None
            if contract and provider is None: provider={**row_,"contract":contract}
            else: extra.append(row_)
        if provider:
            role_for=lambda repository_id: next((w["role"] or w["agent"] for w in ready if w["repository_id"]==repository_id),str(repository_id))
            wanted=provider["contract"].get("integration_profile") or resolve_profile(provider["contract"],None,t.get("default_sandbox_profile"))
            provider_source=SourceSpec(repository_id=provider["repository_id"],role=role_for(provider["repository_id"]),branch=provider["branch"],commit_sha=provider["pinned_commit"],worktree_path=provider["worktree_path"],repo_path=provider["repo_path"],source_type="TASK_INTEGRATION")
            extra_sources=[SourceSpec(repository_id=x["repository_id"],role=role_for(x["repository_id"]),branch=x["branch"],commit_sha=x["pinned_commit"],worktree_path=x["worktree_path"],repo_path=x["repo_path"],source_type="TASK_INTEGRATION") for x in extra]
            try:
                sid=sandboxes.create(task_id=tid,owner_type="TASK_INTEGRATION",owner_id=tiid,profile=wanted,provider=provider_source,extra_sources=extra_sources)
                if sid:
                    try: sandboxes.provision(sid)
                    except SandboxError: pass
            except SandboxContractError:
                db.event("task",tid,"SANDBOX_CONTRACT_REQUIRED","integration profile not runnable")
        else:
            db.event("task",tid,"SANDBOX_CONTRACT_REQUIRED","no participating repo declares sandbox: contract")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/verification-sandbox")
    def create_verification_sandbox(tid:int):
        """[Create Verification Sandbox] (section 11-13): post-completion
        'view the running app' fallback for when Integration creation
        never provisioned one (contract added later, earlier provision
        attempt failed, etc). Reuses the exact registered Integration
        Workspace worktree(s) already on disk -- never checks out a
        fresh commit or accepts one from the browser -- pinned to
        whatever that worktree's real current HEAD is right now (the
        same 'pin to the registered worktree's live HEAD' pattern every
        other sandbox in this app already uses). A sandbox already
        existing for this task_integration is left alone (idempotent,
        never a duplicate)."""
        t=task_row(tid); ti=task_integration_row(tid)
        if not ti: raise GitSafetyError("No Integration exists for this Task yet")
        if db.one("SELECT id FROM sandboxes WHERE owner_type='TASK_INTEGRATION' AND owner_id=?",(ti["id"],)):
            return RedirectResponse(f"/tasks/{tid}",303)
        ti_repos=decision.integration_repos(ti["id"])
        if not ti_repos: raise GitSafetyError("No Integration Workspace to build a sandbox from")
        rows=[]
        for r_ in ti_repos:
            try: head=git.head(r_["worktree_path"])
            except Exception: continue
            rows.append({"repository_id":r_["repository_id"],"repo_path":repo(r_["repository_id"])["repo_path"],
                         "worktree_path":r_["worktree_path"],"branch":r_["branch"],"pinned_commit":head})
        provider=None; extra=[]
        for row_ in rows:
            try: contract=load_sandbox_contract(Path(row_["repo_path"]))
            except SandboxContractError: contract=None
            if contract and provider is None: provider={**row_,"contract":contract}
            else: extra.append(row_)
        if not provider:
            raise GitSafetyError("No participating repository declares a sandbox: contract")
        role_for=lambda repository_id: repo(repository_id)["repo_name"]
        wanted=provider["contract"].get("integration_profile") or resolve_profile(provider["contract"],None,t.get("default_sandbox_profile"))
        provider_source=SourceSpec(repository_id=provider["repository_id"],role=role_for(provider["repository_id"]),branch=provider["branch"],commit_sha=provider["pinned_commit"],worktree_path=provider["worktree_path"],repo_path=provider["repo_path"],source_type="TASK_INTEGRATION")
        extra_sources=[SourceSpec(repository_id=x["repository_id"],role=role_for(x["repository_id"]),branch=x["branch"],commit_sha=x["pinned_commit"],worktree_path=x["worktree_path"],repo_path=x["repo_path"],source_type="TASK_INTEGRATION") for x in extra]
        sid=sandboxes.create(task_id=tid,owner_type="TASK_INTEGRATION",owner_id=ti["id"],profile=wanted,provider=provider_source,extra_sources=extra_sources)
        if sid: sandboxes.provision(sid)
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/merges/{repository_id}/mark-merged")
    def mark_merge_record(tid:int,repository_id:int,pr_ref:str=Form(""),merged_commit:str=Form("")):
        """Per-repository merge tracking (section 25): a cross-repo Task
        does NOT become DONE just because one repo's PR landed --
        MergeRecord is the fact TaskDecisionService actually checks. If no
        commit is given, pin whatever the repo's Task Integration branch
        (or, absent Integration, the Builder's own branch) is at right
        now, never a guess."""
        task_row(tid); decision.merge_records(tid)
        row=db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?",(tid,repository_id))
        if not row: raise GitSafetyError("No MergeRecord for this repository on this Task")
        commit=merged_commit.strip()
        if not commit:
            ti=task_integration_row(tid)
            ir=db.one("SELECT worktree_path FROM integration_workspaces WHERE task_integration_id=? AND repository_id=?",(ti["id"],repository_id)) if ti else None
            w=db.one("SELECT worktree_path FROM agent_workspaces WHERE task_id=? AND repository_id=? ORDER BY id DESC LIMIT 1",(tid,repository_id))
            path=(ir or w or {}).get("worktree_path")
            commit=git.head(path) if path else None
        db.execute("UPDATE merge_records SET merge_status='MERGED',merged_commit=?,pr_ref=?,merged_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(commit,pr_ref.strip() or row["pr_ref"],row["id"]))
        db.event("task",tid,"MERGE_RECORDED",f"repo={repository_id} commit={commit}")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/merges/{repository_id}/mark-pr-open")
    def mark_merge_pr_open(tid:int,repository_id:int,pr_ref:str=Form("")):
        task_row(tid); decision.merge_records(tid)
        row=db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?",(tid,repository_id))
        if not row: raise GitSafetyError("No MergeRecord for this repository on this Task")
        db.execute("UPDATE merge_records SET merge_status='PR_OPEN',pr_ref=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(pr_ref.strip(),row["id"]))
        db.event("task",tid,"MERGE_PR_OPEN",f"repo={repository_id}")
        return RedirectResponse(f"/tasks/{tid}",303)
    def _schedule_cleanup_if_done(tid):
        """Section 14: Task DONE only ever falls out of every required
        MergeRecord being MERGED (TaskDecisionService computes it, never
        set directly here) -- but the moment that becomes true, sandbox
        retention starts counting down. Never deletes a worktree/branch
        itself (section 14/15). Also the one place TASK_COMPLETED gets
        emitted (section 15/16): idempotent -- a Task that is already
        DONE and already has a TASK_COMPLETED event never gets a second
        one on a later Refresh/merge/reconcile, and re-entering DONE
        never resets cleanup_eligible_at (mark_cleanup_eligible is
        itself idempotent-ish per sandbox, guarded by status)."""
        d=decision.evaluate(tid)
        if d["status"]=="DONE":
            if not db.one("SELECT id FROM workspace_events WHERE entity_type='task' AND entity_id=? AND action='TASK_COMPLETED'",(tid,)):
                required=[f"{m['repo_name']}(pr={m.get('pr_number')},merge_sha={(m.get('merged_commit') or '')[:12]})" for m in d["merge_records"] if m["required"]]
                db.event("task",tid,"TASK_COMPLETED",f"all required repos merged: {', '.join(required)}")
            for sb in task_sandboxes(tid):
                if sb["status"] not in ("CLOSED","CLEANING"): sandboxes.mark_cleanup_eligible(sb["id"])
    def _reconcile_merge_record(tid,row,status,source):
        """Section 1/5/6/7/15/16: the ONE place a freshly-fetched GitHub
        PR status ever gets written into a MergeRecord -- GitHub's own
        reported state is authoritative and always wins over whatever
        was persisted before (a PR gh reports MERGED is never left
        stuck at PR_OPEN/CONFLICT). Exact merge commit SHA and GitHub's
        own merged_at are persisted, never guessed or set to "now".
        Idempotent: re-reconciling an already-MERGED record just writes
        the same GitHub-sourced facts again and never emits a second
        PR_MERGED_DETECTED (source is WORKSPACE_MANAGER_MERGE for the
        real /merge action, GITHUB_REFRESH for a passive Refresh/
        Create-PR-reuse discovering it externally)."""
        was_merged=row["merge_status"]=="MERGED"
        new_status="MERGED" if status["pr_state"] in MERGED_STATES else ("CONFLICT" if status["mergeability"]=="CONFLICTING" else "PR_OPEN")
        if new_status=="MERGED":
            # State-consistency invariant: merge_status=='MERGED' must
            # never persist with merged_commit NULL -- GitHub always
            # returns a real merge commit for an actually-merged PR, but
            # fall back to the PR's own last-known head_sha rather than
            # ever writing MERGED with no commit to point to at all.
            merged_commit=status.get("merged_commit") or status.get("head_sha") or row.get("merged_commit")
            db.execute(
                "UPDATE merge_records SET merge_status='MERGED',pr_state=?,ci_status=?,mergeability=?,merge_state_status=?,head_sha=?,merged_commit=?,merged_at=?,last_synced_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (status["pr_state"],status["ci_status"],status["mergeability"],status["merge_state_status"],status["head_sha"],
                 merged_commit,status.get("merged_at") or row.get("merged_at"),row["id"]))
        else:
            db.execute(
                "UPDATE merge_records SET merge_status=?,pr_state=?,ci_status=?,mergeability=?,merge_state_status=?,head_sha=?,last_synced_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (new_status,status["pr_state"],status["ci_status"],status["mergeability"],status["merge_state_status"],status["head_sha"],row["id"]))
        db.event("task",tid,"PR_REFRESHED",f"repo={row['repository_id']} pr={row['pr_number']} head={status.get('head_sha')} source={source}")
        if new_status=="MERGED" and not was_merged:
            db.event("task",tid,"PR_MERGED_DETECTED",f"repo={row['repository_id']} pr={row['pr_number']} merge_sha={status.get('merged_commit')} source={source}")
        _schedule_cleanup_if_done(tid)
    @app.post("/api/tasks/{tid}/merges/{repository_id}/create-pr")
    def create_merge_pr(tid:int,repository_id:int):
        """[Create PR] (section 3): only from the exact verified source
        (the repo's Task Integration branch when Integration is required,
        else the Builder's own branch) -- never a branch name accepted
        from the browser. Reuses an existing OPEN/MERGED PR for the same
        head/base instead of creating a duplicate (section 3)."""
        t=task_row(tid); r=repo(repository_id); d=decision.evaluate(tid)
        row=db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?",(tid,repository_id))
        if not row: raise GitSafetyError("No MergeRecord for this repository on this Task")
        if not d["ready_for_main"]: raise GitSafetyError("Task is not READY_FOR_MAIN yet")
        branch,commit=decision.effective_source_for_repo(d,repository_id)
        if not branch or not commit: raise GitSafetyError("No verified source branch/commit for this repository")
        if not github_merge.available(r["repo_path"]): raise GitSafetyError("Repository has no GitHub remote -- use Confirm External Merge instead")
        base_branch=r["default_branch"] or "main"
        try: op_id=ops.begin("merge_record",row["id"],"CREATE_PR")
        except OperationInProgress: return RedirectResponse(f"/tasks/{tid}",303)
        try:
            # Neither a Builder Workspace branch nor a Task Integration
            # branch is ever pushed anywhere automatically before this
            # point (both only exist as local worktree branches) --
            # push the exact verified branch to origin first, every
            # time, so GitHub actually has something to open a PR
            # against (and so a later commit landing on the same branch
            # gets synced too, not just the first push).
            github_merge.push_branch(r["repo_path"],branch)
            existing=github_merge.find_existing_pr(r["repo_path"],branch,base_branch)
            reused=bool(existing and existing["state"]!="CLOSED")
            if reused:
                status=github_merge.pr_status(r["repo_path"],existing["number"])
            else:
                title,body=t["title"],f"Automated PR for Task #{t['id']}: {t['title']}\n\nGenerated by ProjectFlow Workspace Manager."
                status=github_merge.create_pr(r["repo_path"],branch,base_branch,title,body)
                db.event("task",tid,"PR_CREATED",f"repo={repository_id} pr={status['pr_number']} head={status['head_sha']}")
        except GitHubIntegrationError as exc:
            ops.fail(op_id,f"{exc.code}: {exc}")
            raise GitSafetyError(f"{exc.code}: {exc}") from exc
        # pr_number/pr_url/base_branch/source_branch/verified_commit are
        # pinned here (this is the one place they're first established);
        # everything else GitHub-authoritative (merge_status/merged_commit/
        # merged_at included) goes through the same reconciliation helper
        # Refresh/Merge use, so re-discovering an ALREADY-merged PR while
        # reusing it (section 6/7) is handled identically, never a second
        # ad hoc "is it merged" check.
        db.execute(
            "UPDATE merge_records SET pr_number=?,pr_url=?,base_branch=?,source_branch=?,verified_commit=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status["pr_number"],status["pr_url"],base_branch,branch,commit,row["id"]))
        row=db.one("SELECT * FROM merge_records WHERE id=?",(row["id"],))
        _reconcile_merge_record(tid,row,status,source="GITHUB_REFRESH")
        ops.succeed(op_id,f"PR #{status['pr_number']} already open" if reused else f"PR #{status['pr_number']} created")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/merges/{repository_id}/refresh")
    def refresh_merge_pr(tid:int,repository_id:int):
        """[Refresh] (section 2/5/7): re-reads live PR/CI/mergeability
        from GitHub -- never trusts whatever was last rendered. If
        GitHub reports the PR as MERGED (whether Workspace Manager
        merged it or it was merged directly on GitHub), reconciles the
        MergeRecord to MERGED right here, in the same request -- no
        second button press, no stale READY_FOR_MAIN left standing
        (section 7/19)."""
        task_row(tid); r=repo(repository_id)
        row=db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?",(tid,repository_id))
        if not row or not row["pr_number"]: raise GitSafetyError("No PR to refresh yet")
        try: status=github_merge.pr_status(r["repo_path"],row["pr_number"])
        except GitHubIntegrationError as exc: raise GitSafetyError(f"{exc.code}: {exc}") from exc
        _reconcile_merge_record(tid,row,status,source="GITHUB_REFRESH")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/merges/{repository_id}/merge")
    def real_merge_pr(tid:int,repository_id:int):
        """[Merge] (section 5): re-fetches PR status, revalidates head
        SHA/CI/mergeability against the Task's live decision one more
        time right here (never trusts whatever the page last rendered),
        then -- only if every gate still actually passes -- calls the
        real GitHub merge API. Persists the exact merge commit GitHub
        reports back, never a guess."""
        t=task_row(tid); r=repo(repository_id)
        row=db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?",(tid,repository_id))
        if not row or not row["pr_number"]: raise GitSafetyError("No open PR for this repository")
        try: op_id=ops.begin("merge_record",row["id"],"MERGE_PR")
        except OperationInProgress: return RedirectResponse(f"/tasks/{tid}",303)
        try: status=github_merge.pr_status(r["repo_path"],row["pr_number"])
        except GitHubIntegrationError as exc:
            ops.fail(op_id,f"{exc.code}: {exc}")
            raise GitSafetyError(f"{exc.code}: {exc}") from exc
        db.execute(
            "UPDATE merge_records SET pr_state=?,ci_status=?,mergeability=?,merge_state_status=?,head_sha=?,last_synced_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status["pr_state"],status["ci_status"],status["mergeability"],status["merge_state_status"],status["head_sha"],row["id"]))
        row=db.one("SELECT * FROM merge_records WHERE id=?",(row["id"],))
        d=decision.evaluate(tid)
        gate=decision.merge_gate_status(d,repository_id,row)
        if not gate["eligible"]:
            db.event("task",tid,"MERGE_BLOCKED",f"repo={repository_id} blockers={','.join(gate['blockers'])}")
            ops.fail(op_id,f"Merge blocked: {', '.join(gate['blockers'])}")
            raise GitSafetyError(f"Merge blocked: {', '.join(gate['blockers'])}")
        db.event("task",tid,"MERGE_REQUESTED",f"repo={repository_id} pr={row['pr_number']} head={row['head_sha']}")
        try: merged=github_merge.merge_pr(r["repo_path"],row["pr_number"],row["merge_strategy"] or "MERGE_COMMIT")
        except GitHubIntegrationError as exc:
            db.event("task",tid,"MERGE_BLOCKED",f"repo={repository_id} error={exc.code}")
            ops.fail(op_id,f"{exc.code}: {exc}")
            raise GitSafetyError(f"{exc.code}: {exc}") from exc
        # Persisted through the same reconciliation helper Refresh/Create-PR
        # use (section 6): exact merge commit SHA + GitHub's own merged_at,
        # MERGE_SUCCEEDED recorded here for the WM-initiated action
        # specifically, PR_MERGED_DETECTED/TASK_COMPLETED handled by the
        # helper itself -- Task DONE is immediate, no later Refresh needed.
        db.event("task",tid,"MERGE_SUCCEEDED",f"repo={repository_id} pr={row['pr_number']} merge_sha={merged['merged_commit']}")
        _reconcile_merge_record(tid,row,merged,source="WORKSPACE_MANAGER_MERGE")
        db.execute("UPDATE merge_records SET pr_ref=? WHERE id=?",(merged["pr_url"],row["id"]))
        ops.succeed(op_id,f"Merged {(merged['merged_commit'] or '')[:8]}")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/merges/{repository_id}/confirm-external-merge")
    def confirm_external_merge(tid:int,repository_id:int,merged_commit:str=Form(""),reason:str=Form(...)):
        """Manual fallback (section 12), Advanced-only: real ancestry
        verification -- refuses unless the verified source commit is
        actually an ancestor of the target branch's real, freshly-fetched
        HEAD. Never a bare 'trust the click' (unlike the legacy
        mark-merged route this supersedes for GitHub-less repos)."""
        t=task_row(tid); r=repo(repository_id); d=decision.evaluate(tid)
        row=db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?",(tid,repository_id))
        if not row: raise GitSafetyError("No MergeRecord for this repository on this Task")
        branch,commit=decision.effective_source_for_repo(d,repository_id)
        if not commit: raise GitSafetyError("No verified source commit for this repository")
        base_branch=row["base_branch"] or r["default_branch"] or "main"
        target_head=github_merge.target_head(r["repo_path"],base_branch)
        if not target_head or not github_merge.is_ancestor(r["repo_path"],commit,f"origin/{base_branch}"):
            raise GitSafetyError(f"Verified commit {(commit or '?')[:12]} is not an ancestor of origin/{base_branch} -- cannot confirm external merge")
        final_commit=merged_commit.strip() or target_head
        db.execute(
            "UPDATE merge_records SET merge_status='MERGED',merged_commit=?,verified_commit=?,base_branch=?,external_merge_reason=?,merged_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (final_commit,commit,base_branch,reason.strip(),row["id"]))
        db.event("task",tid,"EXTERNAL_MERGE_CONFIRMED",f"repo={repository_id} commit={final_commit} reason={reason.strip()[:200]}")
        _schedule_cleanup_if_done(tid)
        return RedirectResponse(f"/tasks/{tid}",303)

    # ------------------------------------------------------------ Deployment
    # Section 23: a genuinely separate lifecycle from Task/MergeRecord --
    # never folded into TaskDecisionService. A Task stays DONE regardless
    # of what happens here; a Deployment row is never the thing that
    # decides Task status.
    DEPLOY_ENVIRONMENTS=("DEV",)
    def latest_deployment(task_id,repository_id,environment):
        return db.one("SELECT * FROM deployments WHERE task_id=? AND repository_id=? AND environment=? ORDER BY id DESC LIMIT 1",(task_id,repository_id,environment))
    def deployment_source(tid,repository_id):
        """Section 3/24: the ONLY authoritative source for a post-merge
        deployment is the exact GitHub merge commit already recorded on
        this repo's own MergeRecord -- never the integration/agent
        branch (those may have moved on, or been cleaned up, since
        merge), never a guess. Refuses if the repo isn't actually
        MERGED yet."""
        mr=db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?",(tid,repository_id))
        if not mr or mr["merge_status"]!="MERGED" or not mr.get("merged_commit"):
            raise GitSafetyError("This repository is not MERGED yet -- nothing to deploy")
        return mr["merged_commit"], mr.get("base_branch") or "main"
    @app.post("/api/tasks/{tid}/deployments")
    def create_deployment(tid:int,repository_id:int=Form(...),environment:str=Form("DEV")):
        """[Deploy to DEV]: only reachable once the repo is actually
        MERGED (section 1/25) -- never the old workflow's Merge/Create
        PR/Push/Mark Ready actions, and never re-merges or re-touches
        PR state. Duplicate-click protection (section 22): a second
        click while a deployment for this exact task/repo/environment
        is still in flight just redirects back to it, never starts a
        second real build/deploy."""
        t=task_row(tid); r=repo(repository_id)
        environment=environment.strip().upper()
        if environment not in DEPLOY_ENVIRONMENTS: raise GitSafetyError(f"Unknown environment: {environment}")
        d=decision.evaluate(tid)
        if d["status"]!="DONE": raise GitSafetyError("Task is not DONE yet -- nothing to deploy")
        target=deployer.target(r["repo_path"],environment)
        if not target: raise GitSafetyError(f"{environment} target not configured for this repository")
        existing=latest_deployment(tid,repository_id,environment)
        if existing and existing["status"] in ("PENDING","PREPARING","BUILDING","DEPLOYING","VERIFYING"):
            return RedirectResponse(f"/tasks/{tid}",303)
        source_commit,source_branch=deployment_source(tid,repository_id)
        did=db.execute(
            "INSERT INTO deployments(task_id,repository_id,environment,target_name,source_branch,source_commit,status) VALUES(?,?,?,?,?,?,'PENDING')",
            (tid,repository_id,environment,target.get("target"),source_branch,source_commit))
        db.event("task",tid,"DEPLOYMENT_REQUESTED",f"repo={repository_id} env={environment} commit={source_commit[:12]} deployment={did}")
        deployer.deploy(did)
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/deployments/{did}/redeploy")
    def redeploy(did:int):
        """[Redeploy] (section 19): always the SAME exact source_commit
        as the deployment being redeployed -- never silently deploys
        whatever main/HEAD currently is. A genuinely newer merged
        source requires going back through create_deployment (a fresh
        Task/repo lookup), never this shortcut."""
        prev=db.one("SELECT * FROM deployments WHERE id=?",(did,))
        if not prev: raise HTTPException(404)
        existing=latest_deployment(prev["task_id"],prev["repository_id"],prev["environment"])
        if existing and existing["status"] in ("PENDING","PREPARING","BUILDING","DEPLOYING","VERIFYING"):
            return RedirectResponse(f"/tasks/{prev['task_id']}",303)
        did2=db.execute(
            "INSERT INTO deployments(task_id,repository_id,environment,target_name,source_branch,source_commit,status) VALUES(?,?,?,?,?,?,'PENDING')",
            (prev["task_id"],prev["repository_id"],prev["environment"],prev["target_name"],prev["source_branch"],prev["source_commit"]))
        db.event("task",prev["task_id"],"DEPLOYMENT_REQUESTED",f"repo={prev['repository_id']} env={prev['environment']} commit={prev['source_commit'][:12]} deployment={did2} redeploy_of={did}")
        deployer.deploy(did2)
        return RedirectResponse(f"/tasks/{prev['task_id']}",303)
    @app.post("/api/deployments/{did}/rollback")
    def rollback_deployment(did:int):
        """[Rollback] (section 5/6/7): only ever reachable from a FAILED
        or ROLLBACK_FAILED deployment with a still-eligible prior
        VERIFIED deployment -- DeploymentService.rollback() re-checks
        this itself (never trusts the button having been rendered) and
        refuses honestly rather than faking success."""
        prev=db.one("SELECT * FROM deployments WHERE id=?",(did,))
        if not prev: raise HTTPException(404)
        existing=latest_deployment(prev["task_id"],prev["repository_id"],prev["environment"])
        if existing and existing["status"] in ("PENDING","PREPARING","BUILDING","DEPLOYING","VERIFYING"):
            return RedirectResponse(f"/tasks/{prev['task_id']}",303)
        ok,error,new_id=deployer.rollback(did)
        if not ok: raise GitSafetyError(error)
        db.event("task",prev["task_id"],"ROLLBACK_REQUESTED",f"repo={prev['repository_id']} env={prev['environment']} rollback_of={did} deployment={new_id}")
        return RedirectResponse(f"/tasks/{prev['task_id']}",303)
    @app.get("/api/deployments/{did}")
    def api_deployment(did:int):
        row=db.one("SELECT * FROM deployments WHERE id=?",(did,))
        if not row: raise HTTPException(404)
        return row
    @app.get("/deployments/{did}",response_class=HTMLResponse)
    def deployment_detail(request:Request,did:int):
        row=db.one("SELECT d.*,r.repo_name FROM deployments d JOIN repositories r ON r.id=d.repository_id WHERE d.id=?",(did,))
        if not row: raise HTTPException(404)
        phases=db.all("SELECT * FROM deployment_phases WHERE deployment_id=? ORDER BY id",(did,))
        history=db.all("SELECT * FROM deployments WHERE task_id=? AND repository_id=? AND environment=? ORDER BY id DESC",(row["task_id"],row["repository_id"],row["environment"]))
        view=deployment_view(row,True,deployer.rollback_target(row))
        return render(request,"deployment_detail.html",d=row,phases=phases,history=history,view=view)

    @app.post("/api/tasks/{tid}/mark-merged")
    def mark_task_merged(tid:int):
        """Bulk convenience for the common single-repo Task (or "I already
        confirmed everything is merged manually") -- marks every required
        MergeRecord MERGED at once. Task DONE still only ever falls out of
        that same MergeRecord state (section 41/42), never a status value
        this route sets directly."""
        task_row(tid)
        for m in decision.merge_records(tid):
            if m["required"] and m["merge_status"]!="MERGED":
                db.execute("UPDATE merge_records SET merge_status='MERGED',merged_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(m["id"],))
        db.event("task",tid,"ALL_MERGES_RECORDED")
        for sb in task_sandboxes(tid):
            if sb["status"] not in ("CLOSED","CLEANING"): sandboxes.mark_cleanup_eligible(sb["id"])
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/close")
    def close_task(tid:int):
        """Close (section 41/42): available once computed status is DONE.
        closed_at is only ever a timestamp annotation on an already-DONE
        Task, never its own status -- and forcing sandbox cleanup here is
        independent of that: a Task can be DONE for a long time with its
        sandboxes still sitting in their normal retention window."""
        t=task_row(tid); d=decision.evaluate(tid)
        if d["status"]!="DONE": raise GitSafetyError(f"Task is not DONE yet (status={d['status']})")
        db.execute("UPDATE tasks SET closed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(tid,)); db.event("task",tid,"TASK_CLOSED")
        for sb in task_sandboxes(tid):
            if sb["status"] not in ("CLOSED","CLEANING"): sandboxes.cleanup(sb["id"],force=True)
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/cancel")
    def cancel_task(tid:int):
        task_row(tid); db.execute("UPDATE tasks SET status='CANCELLED',closed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(tid,)); db.event("task",tid,"TASK_CANCELLED")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/workspaces/{wid}/create-sandbox")
    def create_workspace_sandbox(tid:int,wid:int,profile:str=Form("")):
        t=task_row(tid); w=agent_row(wid)
        if w["task_id"]!=tid: raise HTTPException(404,"Workspace does not belong to this task")
        sid=auto_create_sandbox(tid,w["repository_id"],w["repo_path"],"AGENT_WORKSPACE",wid,w["role"] or w["agent"],w["branch"],w["last_commit"],w["worktree_path"],profile.strip().upper() or None,t.get("default_sandbox_profile"))
        if sid: db.execute("UPDATE agent_workspaces SET sandbox_profile=(SELECT profile FROM sandboxes WHERE id=?) WHERE id=?",(sid,wid))
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/extend-retention")
    def extend_task_retention(tid:int,hours:int=Form(24)):
        task_row(tid)
        for sb in task_sandboxes(tid):
            if sb["status"]!="CLOSED": sandboxes.mark_cleanup_eligible(sb["id"],hours)
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/cleanup-now")
    def cleanup_task_now(tid:int):
        task_row(tid)
        for sb in task_sandboxes(tid):
            if sb["status"]!="CLOSED": sandboxes.cleanup(sb["id"],force=True)
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/verification-report")
    def submit_task_report(tid:int,work_status:str=Form("READY"),what_changed:str=Form(""),automated_tests:str=Form(""),how_to_verify:str=Form(""),expected_result:str=Form(""),test_data:str=Form(""),runtime_requirements:str=Form("NONE"),risks:str=Form("")):
        task_row(tid)
        db.execute("INSERT INTO verification_reports(task_id,workspace_id,work_status,what_changed,automated_tests,how_to_verify,expected_result,test_data,runtime_requirements,risks) VALUES(?,NULL,?,?,?,?,?,?,?,?)",
                   (tid,work_status.strip().upper() or "READY",what_changed.strip(),automated_tests.strip(),how_to_verify.strip(),expected_result.strip(),test_data.strip(),runtime_requirements.strip().upper() or "NONE",risks.strip()))
        db.event("task",tid,"VERIFICATION_REPORT_ADDED",work_status)
        return RedirectResponse(f"/tasks/{tid}",303)

    # ------------------------------------------------------------ Sandboxes
    @app.get("/sandboxes",response_class=HTMLResponse)
    def sandboxes_page(request:Request,status:str="",task_id:str="",repository_id:str="",owner_type:str="",profile:str=""):
        clauses=[]; params=[]
        if status=="running": clauses.append("s.status='RUNNING'")
        elif status=="unhealthy": clauses.append("s.status='RUNNING' AND s.health_status!='HEALTHY'")
        elif status=="stopped": clauses.append("s.status='STOPPED'")
        elif status=="cleanup_pending": clauses.append("s.status='CLEANUP_ELIGIBLE'")
        if task_id: clauses.append("s.task_id=?"); params.append(int(task_id))
        if repository_id: clauses.append("s.repository_id=?"); params.append(int(repository_id))
        if owner_type: clauses.append("s.owner_type=?"); params.append(owner_type)
        if profile: clauses.append("s.profile=?"); params.append(profile)
        where=(" WHERE "+" AND ".join(clauses)) if clauses else ""
        rows=db.all(f"SELECT s.*,r.repo_name,t.title task_title FROM sandboxes s LEFT JOIN repositories r ON r.id=s.repository_id LEFT JOIN tasks t ON t.id=s.task_id{where} ORDER BY s.updated_at DESC",tuple(params))
        return render(request,"sandboxes.html",sandboxes=[sandbox_view(r) for r in rows],running=sandboxes.running_count(),max_running=settings.max_running_sandboxes,
                      filters={"status":status,"task_id":task_id,"repository_id":repository_id,"owner_type":owner_type,"profile":profile},
                      tasks=db.all("SELECT id,title FROM tasks ORDER BY title"),repositories=db.all("SELECT * FROM repositories WHERE enabled=1"))
    @app.get("/api/sandboxes")
    def api_sandboxes(): return db.all("SELECT * FROM sandboxes ORDER BY id DESC")
    @app.get("/sandboxes/{sid}",response_class=HTMLResponse)
    def sandbox_detail(request:Request,sid:int):
        sb=sandbox_row(sid); sources=db.all("SELECT s.*,r.repo_name FROM sandbox_sources s JOIN repositories r ON r.id=s.repository_id WHERE s.sandbox_id=?",(sid,))
        ports_=ports.ports_for(sid); ops=db.all("SELECT * FROM sandbox_operations WHERE sandbox_id=? ORDER BY id DESC LIMIT 20",(sid,)); hw=db.all("SELECT * FROM hardware_test_results WHERE sandbox_id=? ORDER BY id DESC",(sid,))
        outputs_=sandboxes.outputs(sid); lan_ip=sandbox_runtime.local_ip() if sb["profile"]=="HARDWARE" else None
        task=db.one("SELECT id,title FROM tasks WHERE id=?",(sb["task_id"],)) if sb["task_id"] else None
        stale=sandboxes.is_stale(sid,sandbox_current_commits(sid))
        primary=sources[0] if sources else None
        manual=manual_verification_status(sid,primary["worktree_path"]) if primary else {"status":"NOT_RUN","row":None}
        manual_history=db.all("SELECT * FROM manual_verifications WHERE sandbox_id=? ORDER BY id DESC",(sid,))
        return render(request,"sandbox_detail.html",sb=sb,sources=sources,ports=ports_,ops=ops,hw=hw,outputs=outputs_,lan_ip=lan_ip,
                      task=task,stale=stale,view=sandbox_view(sb),manual=manual,manual_history=manual_history)
    @app.get("/api/sandboxes/{sid}")
    def api_sandbox(sid:int): return sandbox_row(sid)
    @app.get("/api/sandboxes/{sid}/status")
    def api_sandbox_status(sid:int):
        """Small polling endpoint for the Provision/Reset Data/Cleanup
        button-feedback JS: sandbox.status + its own most recent
        sandbox_operations row (already the real source of truth --
        never a second ledger, per db.py V12's comment)."""
        sb=sandbox_row(sid)
        op=db.one("SELECT * FROM sandbox_operations WHERE sandbox_id=? ORDER BY id DESC LIMIT 1",(sid,))
        return {"status":sb["status"],"health_status":sb["health_status"],"error_code":sb["error_code"],"error_message":sb["error_message"],"latest_operation":op}
    SANDBOX_BUSY_STATUSES=("PROVISIONING","STARTING","RESETTING","CLEANING")
    @app.post("/api/sandboxes/{sid}/start")
    def start_sandbox(sid:int):
        sb=sandbox_row(sid)
        if sb["status"] in SANDBOX_BUSY_STATUSES: return RedirectResponse(f"/sandboxes/{sid}",303)
        sandboxes.provision(sid); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/stop")
    def stop_sandbox(sid:int): sandbox_row(sid); sandboxes.stop(sid); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/restart")
    def restart_sandbox(sid:int):
        sb=sandbox_row(sid)
        if sb["status"] in SANDBOX_BUSY_STATUSES: return RedirectResponse(f"/sandboxes/{sid}",303)
        sandboxes.stop(sid); sandboxes.provision(sid); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/rebuild")
    def rebuild_sandbox(sid:int):
        sb=sandbox_row(sid)
        if sb["status"] in SANDBOX_BUSY_STATUSES: return RedirectResponse(f"/sandboxes/{sid}",303)
        for src in db.all("SELECT * FROM sandbox_sources WHERE sandbox_id=?",(sid,)):
            db.execute("UPDATE sandbox_sources SET commit_sha=? WHERE id=?",(git.head(src["worktree_path"]),src["id"]))
        sandboxes._write_manifest(sid); db.execute("UPDATE sandboxes SET status='CREATED',health_status='UNKNOWN' WHERE id=?",(sid,)); db.event("sandbox",sid,"SANDBOX_REBUILD_REQUESTED")
        sandboxes.provision(sid); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/reset-data")
    def reset_sandbox_data(sid:int):
        """[Reset Sandbox Data] (sections 6-9): the first-class, audited
        way to test default-credential/first-run/seed/migration behavior
        from a genuinely clean state -- never a raw docker compose down
        -v exposed to the browser. Real ownership check + volume removal
        + re-provision from the exact current source, all through
        SandboxManager, all recorded as a RESET_DATA SandboxOperation.
        Runs in a background thread (SandboxManager.reset_data) so the
        button-feedback state (RESETTING -> PROVISIONING -> RUNNING)
        survives a refresh instead of freezing the tab; a click while
        already busy is a no-op, never a second concurrent reset."""
        sb=sandbox_row(sid)
        if sb["status"] in SANDBOX_BUSY_STATUSES: return RedirectResponse(f"/sandboxes/{sid}",303)
        sandboxes.reset_data(sid); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/health")
    def health_sandbox(sid:int): sandbox_row(sid); sandboxes.health_check(sid); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/cleanup")
    def cleanup_sandbox_now(sid:int):
        sb=sandbox_row(sid)
        if sb["status"] in SANDBOX_BUSY_STATUSES: return RedirectResponse(f"/sandboxes/{sid}",303)
        sandboxes.cleanup(sid,force=True); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/extend-retention")
    def extend_retention(sid:int,hours:int=Form(24)): sandbox_row(sid); sandboxes.mark_cleanup_eligible(sid,hours); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/hardware-test")
    def record_hardware_test(sid:int,result:str=Form(...),notes:str=Form("")):
        sandbox_row(sid)
        if result not in ("PASS","FAIL","NOT_RUN"): raise GitSafetyError("Invalid hardware test result")
        db.execute("INSERT INTO hardware_test_results(sandbox_id,result,notes) VALUES(?,?,?)",(sid,result,notes)); db.event("sandbox",sid,"HARDWARE_TEST_RECORDED",result)
        return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/manual-verification")
    def record_manual_verification(sid:int,result:str=Form(...),note:str=Form("")):
        sb=sandbox_row(sid)
        if result not in ("PASS","FAIL"): raise GitSafetyError("Invalid manual verification result")
        primary=db.one("SELECT * FROM sandbox_sources WHERE sandbox_id=? ORDER BY id LIMIT 1",(sid,))
        worktree_path=primary["worktree_path"] if primary else sb["worktree_path"]
        try: source_commit=git.head(worktree_path)
        except Exception: source_commit=primary["commit_sha"] if primary else ""
        workspace_id=sb["owner_id"] if sb["owner_type"]=="AGENT_WORKSPACE" else None
        db.execute("INSERT INTO manual_verifications(task_id,workspace_id,sandbox_id,result,note,source_commit) VALUES(?,?,?,?,?,?)",
                   (sb["task_id"],workspace_id,sid,result,note.strip()[:2000],source_commit))
        db.event("sandbox",sid,"MANUAL_VERIFICATION_RECORDED",result)
        return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/build-firmware")
    def build_firmware(sid:int):
        import os as _os, subprocess as _sp
        sandbox_row(sid); provider=db.one("SELECT * FROM sandbox_sources WHERE sandbox_id=? ORDER BY id LIMIT 1",(sid,))
        contract=load_sandbox_contract(Path(provider["worktree_path"])); cmd=hardware_build_command(contract or {})
        if not cmd: raise SandboxError("HARDWARE_BUILD_NOT_DECLARED","sandbox.hardware.build_command not declared")
        backend_url=next(iter(sandboxes.outputs(sid).values()),"")
        env={**_os.environ,"SANDBOX_BACKEND_URL":backend_url,"SANDBOX_ID":str(sid)}
        result=_sp.run(cmd,shell=True,cwd=provider["worktree_path"],env=env,text=True,capture_output=True,timeout=900)
        op_id=sandboxes._op_start(sid,"FIRMWARE_BUILD"); sandboxes._op_finish(op_id,"SUCCESS" if result.returncode==0 else "FAILED",result.returncode,result.stdout,result.stderr)
        return RedirectResponse(f"/sandboxes/{sid}",303)

    return app

app=create_app()
