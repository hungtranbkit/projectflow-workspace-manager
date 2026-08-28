from __future__ import annotations
import asyncio
import json
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
from app.services.project_contract import ContractError, load_contract
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

def create_app(settings=None):
    settings = settings or load_settings(); db = Database(settings.db_path); db.init()
    git = GitWorkspaceService(settings.root, worktree_root=settings.worktree_root); runner = TestRunner(db, git); launcher = TerminalLauncherService(settings, git)
    sandbox_runtime = SandboxRuntimeService(); ports = PortAllocatorService(db)
    sandboxes = SandboxManager(db, sandbox_runtime, ports, settings.state_dir, settings.max_running_sandboxes, settings.sandbox_retention_hours)
    cleanup_worker = CleanupWorker(db, sandboxes, settings.cleanup_poll_seconds)
    agent_sessions = AgentSessionManager(db)
    app = FastAPI(title="ProjectFlow Workspace Manager", docs_url=None, redoc_url=None)
    base = Path(__file__).parent; templates = Jinja2Templates(directory=base / "templates")
    app.mount("/static", StaticFiles(directory=base / "static"), name="static")
    app.state.settings, app.state.db, app.state.git, app.state.runner, app.state.launcher = settings, db, git, runner, launcher
    app.state.sandboxes, app.state.ports, app.state.sandbox_runtime, app.state.cleanup_worker = sandboxes, ports, sandbox_runtime, cleanup_worker
    app.state.agent_sessions = agent_sessions
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
        second, independently-calculated status."""
        i=integration_row(iid); head=git.head(i["worktree_path"])
        clean=not git.status(i["worktree_path"]).strip(); no_conflicts=not git.conflict_files(i["worktree_path"])
        sources_current=True
        for s in db.all("SELECT s.*,w.branch FROM integration_sources s JOIN agent_workspaces w ON w.id=s.workspace_id WHERE s.integration_id=?",(iid,)):
            current=git.head(i["repo_path"],s["branch"])
            if current!=s["merged_commit"] or not git.is_ancestor(i["worktree_path"],current): sources_current=False
        try: required=len(load_contract(Path(i["worktree_path"])))
        except ContractError: required=0
        latest={}
        for x in db.all("SELECT stage,status FROM test_runs WHERE workspace_type='integration' AND workspace_id=? AND tested_commit=? ORDER BY id DESC",(iid,head)):
            latest.setdefault(x["stage"],x["status"])
        tests_pass=required>0 and len(latest)>=required and all(v=="PASS" for v in latest.values())
        return {"integration":i,"head":head,"clean":clean,"no_conflicts":no_conflicts,"sources_current":sources_current,
                "tests_pass":tests_pass,"tests_passed":sum(1 for v in latest.values() if v=="PASS"),"tests_required":required,
                "ready":clean and no_conflicts and sources_current and tests_pass}
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
        if st!="RUNNING": return "SANDBOX_NOT_RUNNING"
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
    LEGACY_TASK_STATUS={"OPEN":"BACKLOG","IN_PROGRESS":"DEVELOPMENT","READY_FOR_INTEGRATION":"DEVELOPMENT","INTEGRATING":"INTEGRATION","TESTING":"INTEGRATION"}
    def normalize_task_status(status):
        """Display-only mapping for Task rows created before this control
        plane existed (OPEN/IN_PROGRESS/...) -- their stored string is
        never rewritten, only how it is shown/columned is normalized."""
        return LEGACY_TASK_STATUS.get(status,status)
    def latest_session_for_workspace(wid):
        return db.one("SELECT * FROM agent_sessions WHERE workspace_id=? ORDER BY id DESC LIMIT 1",(wid,))
    def workspace_builder_status(w,session,report):
        """Section 5's Agent states, derived -- never a stored column that
        could drift from the AgentSession that's actually live or the
        report the builder actually submitted."""
        if w["review_status"]=="REVIEW_PASS": return "DONE"
        if report and report["work_status"]=="FIX_REQUIRED": return "FIX_REQUIRED"
        if session and session["status"]=="RUNNING": return "RUNNING"
        if session and session["status"]=="WAITING_FOR_INPUT": return "WAITING_FOR_INPUT"
        if session and session["status"]=="STARTING": return "RUNNING"
        if w["status"]=="READY": return "READY_FOR_REVIEW"
        if report or (session and session["status"] in ("EXITED","FAILED")): return "TESTING"
        return "NOT_STARTED"
    def workspace_pipeline_stage(w,builder,risk_profile):
        """One workspace's own position in BACKLOG..READY_FOR_MAIN -- the
        Task's overall stage is the earliest (weakest-link) stage among
        its workspaces, exactly like the previous phase's blocking-
        workspace logic, now extended with Review/QA."""
        if w["review_status"]=="FIX_REQUIRED" or w["qa_status"] in ("QA_FAIL","BLOCKED"): return "DEVELOPMENT"
        if w["review_status"]=="BLOCKED": return "REVIEW"
        if requires_qa(risk_profile) and w["review_status"]=="REVIEW_PASS" and w["qa_status"]!="QA_PASS": return "QA"
        if w["review_status"]=="REVIEW_PASS": return "READY"
        if builder=="READY_FOR_REVIEW" or w["reviewer_agent"]: return "REVIEW"
        return "DEVELOPMENT"
    STAGE_ORDER=["DEVELOPMENT","REVIEW","QA","READY"]
    def task_stage(t,workspaces,ti,ready_for_main):
        """The Task's overall pipeline stage. BACKLOG/PREPARE/MERGED/
        CLOSED/CANCELLED are explicit, persisted decisions (nothing to
        derive them from -- a BACKLOG task has zero workspaces by
        definition). Everything from DEVELOPMENT onward is derived live
        from workspace/review/QA/integration state, never a second
        independently-advanced status a route could forget to bump."""
        status=t["status"]
        if status in ("MERGED","CLOSED","CANCELLED"): return status
        if status=="BACKLOG": return "BACKLOG"
        if not workspaces: return "PREPARE"
        if ready_for_main: return "READY_FOR_MAIN"
        if ti: return "INTEGRATION"
        stages=[workspace_pipeline_stage(w,w.get("builder_status") or workspace_builder_status(w,latest_session_for_workspace(w["id"]),workspace_verification(w["id"])),t["risk_profile"]) for w in workspaces]
        idx=min(STAGE_ORDER.index(s) for s in stages)
        return STAGE_ORDER[idx] if STAGE_ORDER[idx]!="READY" else "INTEGRATION"
    def render_agent_prompt(t):
        """Deterministic template fill from the structured brief -- never
        an actual model call. The user reviews/edits the result before any
        agent is launched (section 3: never auto-start an agent)."""
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
    def render_review_prompt(t,w,report):
        """Deterministic template (section 6): brief, acceptance criteria,
        source branch/commit, and the Builder's own completion report --
        never a diff computed by an LLM, only real recorded facts."""
        head=git.head(w["worktree_path"])
        parts=[f"# Review: {t['title']}",f"Branch: {w['branch']} @ {head[:12]}",""]
        if t["brief_acceptance_criteria"]: parts+=["## ACCEPTANCE_CRITERIA",t["brief_acceptance_criteria"],""]
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
        # Dashboard is Task-centric (section 22): every count below comes
        # from task_card_view's derived Kanban column, the same function
        # /tasks and /kanban use -- never a second, dashboard-only tally.
        active_task_rows=db.all("SELECT * FROM tasks WHERE status NOT IN ('MERGED','CANCELLED','CLOSED')")
        active_cards=[task_card_view(t) for t in active_task_rows]
        by_col={c:sum(1 for card in active_cards if card["column"]==c) for c in KANBAN_COLUMNS}
        summary={"active":len(agents),"ready":sum(x["status"]=="READY" for x in agents),"testing":sum(x["status"]=="TESTING" for x in ints),"main":sum(bool(x["ready_for_main"]) for x in ints),
                 "tasks":len(active_task_rows),"sandboxes":running_sandboxes,"cleanup":cleanup_pending,
                 "tasks_development":by_col["DEVELOPMENT"],"tasks_review":by_col["REVIEW"],"tasks_fix_required":sum(1 for c in active_cards if c["needs_fix"]),
                 "integrations_running":by_col["INTEGRATION"],"tasks_ready_for_main":by_col["READY_FOR_MAIN"]}
        return render(request,"dashboard.html",agents=agents,integrations=ints,summary=summary,first_run=not db.one("SELECT id FROM repositories LIMIT 1"))

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
        w=agent_row(wid); details=safe_details(w["worktree_path"])
        runs=db.all("SELECT * FROM test_runs WHERE workspace_type='agent' AND workspace_id=? ORDER BY id DESC",(wid,))
        readiness=workspace_readiness(w)
        report=workspace_verification(wid) or (task_verification(w["task_id"]) if w["task_id"] else None)
        manual_history=db.all("SELECT * FROM manual_verifications WHERE workspace_id=? ORDER BY id DESC",(wid,))
        ti=task_integration_row(w["task_id"]) if w["task_id"] else None
        integration_exists=None if not w["task_id"] else bool(ti)
        ready_for_main=bool(ti and ti["ready_for_main"])
        code=next_action_code(readiness,integration_exists,ready_for_main)
        action=resolve_next_action(code,wid=wid,tid=w["task_id"],sandbox_id=readiness["sandbox"]["id"] if readiness["sandbox"] else None)
        sessions=db.all("SELECT * FROM agent_sessions WHERE workspace_id=? ORDER BY id DESC LIMIT 10",(wid,))
        return render(request,"workspace_detail.html",w=w,details=details,runs=runs,readiness=readiness,report=report,
                      manual_history=manual_history,next_action=action,sessions=sessions)
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
    @app.post("/api/workspaces/{wid}/verification-report")
    def submit_workspace_report(wid:int,work_status:str=Form("READY"),what_changed:str=Form(""),files_changed:str=Form(""),tests_run:str=Form(""),automated_tests:str=Form(""),how_to_verify:str=Form(""),expected_result:str=Form(""),test_data:str=Form(""),runtime_requirements:str=Form("NONE"),risks:str=Form("")):
        """Builder completion report (section 5): WHAT_CHANGED/
        FILES_CHANGED/TESTS_RUN/HOW_TO_VERIFY/EXPECTED_RESULT/RISKS. This
        submission IS the builder's own "I'm done" signal -- it's what
        workspace_builder_status() reads to show READY_FOR_REVIEW."""
        w=agent_row(wid)
        db.execute("INSERT INTO verification_reports(task_id,workspace_id,work_status,what_changed,files_changed,tests_run,automated_tests,how_to_verify,expected_result,test_data,runtime_requirements,risks) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (w["task_id"],wid,work_status.strip().upper() or "READY",what_changed.strip(),files_changed.strip(),tests_run.strip(),automated_tests.strip(),how_to_verify.strip(),expected_result.strip(),test_data.strip(),runtime_requirements.strip().upper() or "NONE",risks.strip()))
        db.event("agent",wid,"VERIFICATION_REPORT_ADDED",work_status)
        if work_status.strip().upper()=="READY" and w["status"]!="READY":
            # WORK_STATUS: READY *is* Mark Ready -- a builder that already
            # filed a structured completion report shouldn't also need a
            # separate manual click to reach the same state.
            head=git.head(w["worktree_path"])
            db.execute("UPDATE agent_workspaces SET status='READY',ready_for_integration=1,last_commit=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(head,wid))
            db.event("agent",wid,"READY_MARKED",head); recompute_task_status(w.get("task_id"))
        return RedirectResponse(f"/workspaces/{wid}",303)
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
        tid=db.execute("INSERT INTO tasks(slug,title,description,status) VALUES(?,?,?,?)",(slug,title,f"Created from existing Agent Workspace #{wid} ({w['agent']}/{w['repo_name']}).","IN_PROGRESS" if w["status"]!="CREATED" else "OPEN"))
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
        """[Start Review] (section 6): only after Builder is
        READY_FOR_REVIEW. Generates the review prompt deterministically
        (real branch/commit/brief/builder-report, never an LLM call) and
        records who is reviewing; does not touch source."""
        w=agent_row(wid)
        if workspace_builder_status(w,latest_session_for_workspace(wid),workspace_verification(wid))!="READY_FOR_REVIEW":
            raise GitSafetyError("Builder is not READY_FOR_REVIEW yet")
        t=task_row(w["task_id"]) if w["task_id"] else None
        report=workspace_verification(wid)
        prompt=render_review_prompt(t,w,report) if t else render_review_prompt({"title":w["task_name"],"brief_acceptance_criteria":""},w,report)
        db.execute("UPDATE agent_workspaces SET reviewer_agent=?,review_status=NULL,review_notes=? WHERE id=?",(slugify(reviewer_agent),prompt,wid))
        db.event("agent",wid,"REVIEW_STARTED",reviewer_agent)
        return RedirectResponse(f"/workspaces/{wid}",303)
    @app.post("/api/workspaces/{wid}/submit-review")
    def submit_review(wid:int,result:str=Form(...),notes:str=Form("")):
        """Reviewer result (section 6/7): REVIEW_PASS/FIX_REQUIRED/
        BLOCKED. review_commit pins the exact commit reviewed -- a new
        commit after this is what invalidates a PASS (checked at render
        time, never a second stale flag). FIX_REQUIRED returns the Task
        to the Builder; findings are persisted in review_notes."""
        w=agent_row(wid)
        if result not in ("REVIEW_PASS","FIX_REQUIRED","BLOCKED"): raise GitSafetyError("Invalid review result")
        head=git.head(w["worktree_path"])
        db.execute("UPDATE agent_workspaces SET review_status=?,review_notes=?,review_commit=? WHERE id=?",(result,notes.strip(),head,wid))
        db.event("agent",wid,"REVIEW_SUBMITTED",result)
        return RedirectResponse(f"/workspaces/{wid}",303)
    @app.post("/api/workspaces/{wid}/start-tester")
    def start_tester(wid:int,tester_agent:str=Form("")):
        """[Start Tester] (section 8): only offered after REVIEW_PASS.
        QA verifies sandbox/automated tests/manual acceptance criteria --
        all of which already exist (readiness/manual_verifications); QA
        itself only records the human/agent's PASS/FAIL/BLOCKED decision,
        never a second copy of the sandbox or test data."""
        w=agent_row(wid)
        if w["review_status"]!="REVIEW_PASS": raise GitSafetyError("QA requires REVIEW_PASS first")
        db.execute("UPDATE agent_workspaces SET qa_status=NULL,tester_agent=? WHERE id=?",(slugify(tester_agent) if tester_agent else "qa",wid)); db.event("agent",wid,"QA_STARTED",tester_agent)
        return RedirectResponse(f"/workspaces/{wid}",303)
    @app.post("/api/workspaces/{wid}/submit-qa")
    def submit_qa(wid:int,result:str=Form(...),notes:str=Form("")):
        w=agent_row(wid)
        if result not in ("QA_PASS","QA_FAIL","BLOCKED"): raise GitSafetyError("Invalid QA result")
        db.execute("UPDATE agent_workspaces SET qa_status=?,qa_notes=? WHERE id=?",(result,notes.strip(),wid)); db.event("agent",wid,"QA_SUBMITTED",result)
        return RedirectResponse(f"/workspaces/{wid}",303)

    # ------------------------------------------------- Agent Sessions (PTY)
    def session_row(sid):
        row=db.one("SELECT s.*,w.worktree_path,w.agent workspace_agent FROM agent_sessions s JOIN agent_workspaces w ON w.id=s.workspace_id WHERE s.id=?",(sid,))
        if not row: raise HTTPException(404,"Session not found")
        return row
    @app.post("/api/workspaces/{wid}/sessions")
    def create_session(wid:int,mode:str=Form("INTERACTIVE")):
        """Command safety (section 14): the browser supplies only
        workspace_id + (fixed) mode -- agent name and cwd are both
        resolved server-side from the trusted workspace/launcher
        registry, never taken from the request."""
        w=agent_row(wid)
        if w["agent"] not in settings.agents: raise GitSafetyError("Agent is not allowed")
        mode="VIEW_ONLY" if mode=="VIEW_ONLY" else "INTERACTIVE"
        try: sid=agent_sessions.start(task_id=w["task_id"],workspace_id=wid,agent=w["agent"],worktree_path=w["worktree_path"],mode=mode)
        except SessionError as exc: raise GitSafetyError(str(exc)) from exc
        db.event("agent",wid,"SESSION_STARTED",f"session={sid} mode={mode}")
        return RedirectResponse(f"/workspaces/{wid}/sessions/{sid}",303)
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
        return render(request,"agents_live.html",sessions=rows)
    @app.get("/workspaces/{wid}/sessions/{sid}",response_class=HTMLResponse)
    def session_detail(request:Request,wid:int,sid:int):
        w=agent_row(wid); s=session_row(sid)
        if s["workspace_id"]!=wid: raise HTTPException(404)
        return render(request,"session_detail.html",w=w,s=s)
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
        w=agent_row(wid)
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
        return render(request,"integration_detail.html",i=i,sources=sources,conflicts=git.conflict_files(i["worktree_path"]),head=head,stale=readiness_stale,runs=db.all("SELECT * FROM test_runs WHERE workspace_type='integration' AND workspace_id=? ORDER BY id DESC",(iid,)),events=db.all("SELECT * FROM workspace_events WHERE entity_type='integration' AND entity_id=? ORDER BY id",(iid,)))
    @app.post("/api/integrations/{iid}/merge-latest")
    def merge_latest(iid:int):
        i=integration_row(iid)
        if git.conflict_files(i["worktree_path"]): raise GitSafetyError("Resolve current conflicts before merging latest")
        sources=db.all("SELECT s.*,w.branch FROM integration_sources s JOIN agent_workspaces w ON w.id=s.workspace_id WHERE s.integration_id=?",(iid,)); invalidate(iid)
        for s in sources:
            current=git.head(i["repo_path"],s["branch"])
            if current==s["merged_commit"]: continue
            result=git.merge(i["worktree_path"],s["branch"])
            if result.returncode:
                db.execute("UPDATE integration_workspaces SET status='CONFLICT' WHERE id=?",(iid,)); db.event("integration",iid,"MERGE_CONFLICT",s["branch"]); break
            db.execute("UPDATE integration_sources SET merged_commit=?,merged_at=CURRENT_TIMESTAMP WHERE integration_id=? AND workspace_id=?",(current,iid,s["workspace_id"])); db.event("integration",iid,"BRANCH_MERGED",s["branch"])
        return RedirectResponse(f"/integrations/{iid}",303)
    @app.post("/api/integrations/{iid}/test")
    def test_integration(iid:int):
        i=integration_row(iid)
        if git.conflict_files(i["worktree_path"]): raise GitSafetyError("Cannot test unresolved conflicts")
        invalidate(iid); runner.start("integration",iid,Path(i["worktree_path"])); return RedirectResponse(f"/integrations/{iid}",303)
    @app.post("/api/integrations/{iid}/ready-for-main")
    def ready_main(iid:int):
        r=integration_readiness(iid)
        if not r["clean"]: raise GitSafetyError("Integration worktree must be clean")
        if not r["no_conflicts"]: raise GitSafetyError("Merge conflict exists")
        if not r["sources_current"]: raise GitSafetyError("Source not current")
        if not r["tests_pass"]: raise GitSafetyError("All required tests must PASS at current HEAD")
        db.execute("UPDATE integration_workspaces SET status='READY_FOR_MAIN',ready_for_main=1,verified_commit=?,verified_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(r["head"],iid)); db.event("integration",iid,"READY_FOR_MAIN",r["head"]); return RedirectResponse(f"/integrations/{iid}",303)
    @app.post("/api/integrations/{iid}/close")
    def close_integration(iid:int): i=integration_row(iid); git.close(i["repo_path"],i["worktree_path"]); db.execute("UPDATE integration_workspaces SET status='CLOSED',ready_for_main=0,closed_at=CURRENT_TIMESTAMP WHERE id=?",(iid,)); db.event("integration",iid,"WORKSPACE_CLOSED"); return RedirectResponse("/integrations",303)

    @app.get("/test-runs",response_class=HTMLResponse)
    def runs(request:Request): return render(request,"test_runs.html",runs=db.all("SELECT * FROM test_runs ORDER BY id DESC LIMIT 200"))
    @app.get("/api/test-runs/{rid}")
    def api_run(rid:int):
        row=db.one("SELECT * FROM test_runs WHERE id=?",(rid,));
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
        """No-op since the control-plane phase: task_stage() now derives
        DEVELOPMENT/REVIEW/QA/INTEGRATION/READY_FOR_MAIN live from
        workspace/review/QA/integration state on every read. Only
        BACKLOG/PREPARE/MERGED/CLOSED/CANCELLED are ever persisted, and
        those are explicit user actions (select/mark-merged/close/cancel),
        never something Mark Ready should silently overwrite. Kept as a
        no-op so its existing call sites (e.g. ready()) don't need to
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

    KANBAN_COLUMNS=["BACKLOG","PREPARE","DEVELOPMENT","REVIEW","QA","INTEGRATION","READY_FOR_MAIN","DONE"]
    TIMELINE_STAGES=["Backlog","Prepare","Development","Review","QA","Integration","Ready for Main","Done"]
    def workspace_status_label(status):
        """CREATED reads as CODING to a user -- the DB enum stays CREATED/
        READY/CLOSED (no new status column), this is presentation only."""
        return {"CREATED":"CODING"}.get(status,status)
    def kanban_column_for(t,workspaces,ti,ready_for_main):
        """Kanban column = task_stage(), with the three terminal
        dispositions folded into one DONE column (their real distinction
        stays visible as the Task Status badge on the card/detail page)."""
        stage=task_stage(t,workspaces,ti,ready_for_main)
        return "DONE" if stage in ("MERGED","CLOSED","CANCELLED") else stage
    def timeline_stage_for(t,workspaces,ti,ready_for_main):
        stage=kanban_column_for(t,workspaces,ti,ready_for_main)
        return TIMELINE_STAGES[KANBAN_COLUMNS.index(stage)]
    def compute_task_next_action(tid,ws,ti,ready_for_main):
        """Bước tiếp theo (section 13): deterministic, shared by Task
        Detail, the Kanban card and the List row so they can never
        disagree. `ws` items must already carry w['readiness']."""
        blocking=None
        for w in ws:
            if next_action_code(w["readiness"],None,False) not in ("VERIFIED_STANDALONE","NO_SANDBOX_CONTRACT_WAIT"): blocking=w; break
        if blocking:
            code=next_action_code(blocking["readiness"],None,False)
            return resolve_next_action(code,wid=blocking["id"],tid=tid,sandbox_id=blocking["readiness"]["sandbox"]["id"] if blocking["readiness"]["sandbox"] else None)
        if ws:
            code=next_action_code(ws[-1]["readiness"],bool(ti),ready_for_main)
            return resolve_next_action(code,tid=tid)
        return {"code":"NO_WORKSPACE","text":"Task chưa có Agent Workspace nào.","label":"Add Agent Workspace","href":None,"method":"GET"}
    def task_ready_for_main(tid,ti):
        """READY_FOR_MAIN, computed live the same way Task Detail's own
        page does (per-repo integration_readiness()) -- task_integrations.
        ready_for_main is never actually written by any route, so Kanban/
        List must never read that stale column as the source of truth."""
        if not ti: return False
        ti_repos=db.all("SELECT id FROM integration_workspaces WHERE task_integration_id=?",(ti["id"],))
        return bool(ti_repos) and all(integration_readiness(r["id"])["ready"] for r in ti_repos)
    def task_card_view(t):
        """Everything one Task card (Kanban or List) needs, derived from
        child state -- never a second, independently-tracked task field.
        Kanban column is computed, not stored: BACKLOG (no workspace) ->
        DEVELOPMENT (coding) -> TEST (all workspaces READY, not yet
        integrated) -> INTEGRATION (Task Integration exists) ->
        READY_FOR_MAIN (gate passed) -> DONE (merged/cancelled), with
        FIX_REQUIRED entered from anywhere a signal is actually failing."""
        ws=task_workspaces(t["id"])
        for w in ws: w["readiness"]=workspace_readiness(w); w["status_label"]=workspace_status_label(w["status"])
        readiness=[w["readiness"] for w in ws]
        agents={"total":len(ws),"ready":sum(1 for w in ws if w["status"]=="READY"),
                "coding":sum(1 for w in ws if w["status"]=="CREATED"),
                "failed":sum(1 for r in readiness if r["automated_status"]=="FAIL" or r["manual"]["status"] in ("FAIL",))}
        sbxs=task_sandboxes(t["id"])
        sandbox={"total":len(sbxs),"running":sum(1 for s in sbxs if s["status"]=="RUNNING"),
                 "unhealthy":sum(1 for s in sbxs if s["status"]=="RUNNING" and s["health_status"]!="HEALTHY")}
        tests={"passed":sum(1 for r in readiness if r["automated_status"]=="PASS"),"total":len(ws)}
        ti=task_integration_row(t["id"])
        ready_for_main=task_ready_for_main(t["id"],ti)
        integration=("NONE" if not ti else "CONFLICT" if ti["status"]=="CONFLICT" else "READY" if ready_for_main else ti["status"])
        column=kanban_column_for(t,ws,ti,ready_for_main)
        blocking_workspace=next((w for w,r in zip(ws,readiness) if r["automated_status"]=="FAIL" or r["manual"]["status"]=="FAIL"),
                                 next((w for w in ws if w["status"]!="READY"),None))
        next_action=compute_task_next_action(t["id"],ws,ti,ready_for_main)
        needs_fix=any(w["review_status"]=="FIX_REQUIRED" or w["qa_status"] in ("QA_FAIL","BLOCKED") for w in ws)
        return {"task":t,"workspaces":ws,"agents":agents,"sandbox":sandbox,"tests":tests,"integration":integration,
                "column":column,"blocking_workspace":blocking_workspace,"repos":sorted({w["repo_name"] for w in ws}),
                "next_action":next_action,"needs_fix":needs_fix}

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
        """[Select for Development] (section 2): BACKLOG -> PREPARE. Still
        allocates nothing -- PREPARE is exactly "no Agent Workspace yet"
        until Create Agent Workspace is used."""
        t=task_row(tid)
        if t["status"]!="BACKLOG": raise GitSafetyError(f"Task is not in BACKLOG (status={t['status']})")
        db.execute("UPDATE tasks SET status='PREPARE',updated_at=CURRENT_TIMESTAMP WHERE id=?",(tid,)); db.event("task",tid,"TASK_SELECTED")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/brief")
    def save_brief(tid:int,goal:str=Form(""),context:str=Form(""),requirements:str=Form(""),acceptance_criteria:str=Form(""),out_of_scope:str=Form(""),test_plan:str=Form(""),risks:str=Form(""),risk_profile:str=Form("")):
        """Implementation Brief (section 3): structured fields, saved
        in place (one current brief per Task, not a history log)."""
        task_row(tid)
        rp=risk_profile.strip().upper()
        sets=["brief_goal=?","brief_context=?","brief_requirements=?","brief_acceptance_criteria=?","brief_out_of_scope=?","brief_test_plan=?","brief_risks=?","updated_at=CURRENT_TIMESTAMP"]
        params=[goal.strip(),context.strip(),requirements.strip(),acceptance_criteria.strip(),out_of_scope.strip(),test_plan.strip(),risks.strip()]
        if rp in RISK_PROFILES: sets.insert(-1,"risk_profile=?"); params.append(rp)
        db.execute(f"UPDATE tasks SET {','.join(sets)} WHERE id=?",(*params,tid)); db.event("task",tid,"BRIEF_SAVED")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/generate-prompt")
    def generate_prompt(tid:int):
        """Fill AGENT PROMPT from the structured brief (deterministic
        template, never a model call) -- the user still reviews/edits it
        before any agent is launched (section 3)."""
        t=task_row(tid); prompt=render_agent_prompt(t)
        db.execute("UPDATE tasks SET agent_prompt=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(prompt,tid)); db.event("task",tid,"PROMPT_GENERATED")
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/agent-prompt")
    def save_agent_prompt(tid:int,agent_prompt:str=Form("")):
        task_row(tid); db.execute("UPDATE tasks SET agent_prompt=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(agent_prompt,tid))
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/new-with-workspace")
    async def create_task_with_workspace(request:Request):
        """Advanced/quick-start shortcut: Task + at least one Agent
        Workspace in one submit (optionally many, for a cross-repo Task
        defined immediately), skipping BACKLOG/PREPARE for when planning
        is unnecessary. A failed workspace never rolls back the Task or
        any already-created workspace -- it is recorded and shown, not
        hidden."""
        form=await request.form()
        title=str(form.get("title","")).strip()
        if not title: raise GitSafetyError("Task title is required")
        description=str(form.get("description",""))
        repo_ids=form.getlist("ws_repository_id"); agents=form.getlist("ws_agent"); roles=form.getlist("ws_role")
        bases=form.getlist("ws_base_branch"); profiles=form.getlist("ws_sandbox_profile")
        slug=slugify(title); tid=db.execute("INSERT INTO tasks(slug,title,description,status) VALUES(?,?,?,?)",(slug,title,description,"PREPARE"))
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
    @app.get("/api/tasks")
    def api_tasks(): return db.all("SELECT * FROM tasks ORDER BY updated_at DESC")
    @app.get("/tasks/{tid}",response_class=HTMLResponse)
    def task_detail(request:Request,tid:int):
        t=task_row(tid); workspaces=task_workspaces(tid); sbxs=task_sandboxes(tid); ti=task_integration_row(tid)
        ti_repos=db.all("SELECT i.*,r.repo_name FROM integration_workspaces i JOIN repositories r ON r.id=i.repository_id WHERE i.task_integration_id=?",(ti["id"],)) if ti else []
        all_ready=bool(workspaces) and all(w["status"]=="READY" for w in workspaces)

        # Nest each Agent Workspace's own sandbox (Sandbox remains the
        # source of truth -- this only joins by owner_id, it stores no
        # sandbox field on agent_workspaces itself) and, for a workspace
        # with none, tell "not yet created" apart from "repo has no
        # sandbox: contract" so the empty state never lies. workspace_readiness()
        # is the single place that reads sandbox/tests/manual-verification for
        # a workspace -- reused here so this section can never disagree with
        # the "Bước tiếp theo" box below or workspace_detail's own page.
        for w in workspaces:
            r=workspace_readiness(w); w["readiness"]=r; w["report"]=workspace_verification(w["id"])
            w["details"]=safe_details(w["worktree_path"]); w["status_label"]=workspace_status_label(w["status"])
            w["session"]=latest_session_for_workspace(w["id"])
            w["builder"]=workspace_builder_status(w,w["session"],w["report"])
            w["qa_required"]=requires_qa(t["risk_profile"])
            if r["sandbox_view"]:
                w["sandbox"]=r["sandbox_view"]; w["sandbox"]["stale"]=sandboxes.is_stale(r["sandbox"]["id"],sandbox_current_commits(r["sandbox"]["id"]))
            else:
                w["sandbox"]=None; w["sandbox_configured"]=r["sandbox_configured"]
        blocking_workspace=next((w for w in workspaces if w["readiness"]["automated_status"]=="FAIL" or w["readiness"]["manual"]["status"]=="FAIL"),
                                 next((w for w in workspaces if w["status"]!="READY"),None))

        integration_sandboxes=[]
        for sb in sbxs:
            if sb["owner_type"]!="TASK_INTEGRATION": continue
            v=sandbox_view(sb); v["stale"]=sandboxes.is_stale(sb["id"],sandbox_current_commits(sb["id"]))
            v["sources"]=db.all("SELECT s.*,r.repo_name FROM sandbox_sources s JOIN repositories r ON r.id=s.repository_id WHERE s.sandbox_id=?",(sb["id"],))
            v["hardware"]=db.one("SELECT * FROM hardware_test_results WHERE sandbox_id=? ORDER BY id DESC LIMIT 1",(sb["id"],))
            integration_sandboxes.append(v)

        # Integration Readiness: one real check per participating repo's
        # Integration Workspace, reusing the exact gate /ready-for-main
        # enforces -- never a second, template-only status calculation.
        readiness=[{"repo":r,**integration_readiness(r["id"])} for r in ti_repos]
        tests_passed=sum(r["tests_passed"] for r in readiness); tests_required=sum(r["tests_required"] for r in readiness)
        ready_for_main=bool(ti_repos) and all(r["ready"] for r in readiness)
        hw=[v["hardware"] for v in integration_sandboxes if v["hardware"]]
        hardware_status=hw[0]["result"] if hw else ("PENDING" if any(v["row"]["profile"]=="HARDWARE" for v in integration_sandboxes) else None)
        summary={
            "agents_ready":sum(1 for w in workspaces if w["status"]=="READY"),"agents_total":len(workspaces),
            "sandboxes_running":sum(1 for sb in sbxs if sb["status"]=="RUNNING"),"sandboxes_total":len(sbxs),
            "integration_health":integration_sandboxes[0]["row"]["health_status"] if integration_sandboxes else None,
            "tests_passed":tests_passed,"tests_required":tests_required,"hardware_status":hardware_status,"ready_for_main":ready_for_main,
        }
        earliest_cleanup=min((sb["cleanup_eligible_at"] for sb in sbxs if sb["cleanup_eligible_at"]),default=None)

        report=effective_verification(tid,workspaces)
        manual_status=[w["readiness"]["manual"]["status"] for w in workspaces if w["readiness"]["sandbox"]]
        summary["manual_verification"]=("PASS" if manual_status and all(s=="PASS" for s in manual_status) else
                                         "FAIL" if "FAIL" in manual_status else
                                         "STALE" if "STALE" in manual_status else
                                         "NOT_RUN" if manual_status else None)
        summary["automated_tests"]=("PASS" if workspaces and all(w["readiness"]["automated_status"]=="PASS" for w in workspaces) else
                                     "FAIL" if any(w["readiness"]["automated_status"]=="FAIL" for w in workspaces) else "NOT_RUN")

        # Bước tiếp theo: the exact same deterministic function the Kanban
        # card and List row use (compute_task_next_action) -- never a
        # second, page-only computation.
        next_action=compute_task_next_action(tid,workspaces,ti,ready_for_main)

        # Test Readiness (section 9): can this task be tested NOW? Derived,
        # never a stored field. NO if any required workspace still CODING,
        # a needed sandbox is absent/unhealthy/stale, or there's an
        # unresolved conflict; YES only once every workspace is READY with
        # a healthy (or not-applicable) sandbox and current source; PARTIAL
        # for everything in between (some but not all workspaces testable).
        checks=[]; per_ws_ok=[]
        for w in workspaces:
            r=w["readiness"]
            ok=r["agent_ready"] and (not r["sandbox_required"] or (r["sandbox_status"]=="RUNNING" and not (w["sandbox"] and w["sandbox"].get("stale"))))
            per_ws_ok.append(ok)
            checks.append({"label":f"{w['agent'].capitalize()} · {w['role'] or w['repo_name']} workspace READY","ok":ok})
        conflict=any(not r["no_conflicts"] for r in readiness)  # per participating repo's Integration Workspace
        if ti_repos: checks.append({"label":"Không còn merge conflict","ok":not conflict})
        if integration_sandboxes:
            all_healthy=all(v["row"]["health_status"]=="HEALTHY" for v in integration_sandboxes)
            checks.append({"label":"Integration Sandbox HEALTHY","ok":all_healthy})
        if not workspaces or conflict or not any(per_ws_ok): test_readiness="NO"
        elif all(per_ws_ok): test_readiness="YES"
        else: test_readiness="PARTIAL"
        full_integration_ok = test_readiness=="YES" and bool(ti) and not conflict

        stage=timeline_stage_for(t,workspaces,ti,ready_for_main)
        column=kanban_column_for(t,workspaces,ti,ready_for_main)
        not_ready=[w for w in workspaces if w["status"]!="READY"]

        return render(request,"task_detail.html",t=t,workspaces=workspaces,sandboxes=sbxs,task_integration=ti,ti_repos=ti_repos,
                      integration_sandboxes=integration_sandboxes,readiness=readiness,summary=summary,all_ready=all_ready,
                      task_cleanup_countdown=format_countdown(earliest_cleanup),report=report,next_action=next_action,
                      test_readiness=test_readiness,test_readiness_checks=checks,full_integration_ok=full_integration_ok,
                      blocking_workspace=blocking_workspace,timeline_stages=TIMELINE_STAGES,current_stage=stage,column=column,
                      not_ready_workspaces=not_ready,integration_conflict=conflict,
                      repositories=db.all("SELECT * FROM repositories WHERE enabled=1"),agents=settings.agents)
    @app.get("/api/tasks/{tid}")
    def api_task(tid:int):
        t=task_row(tid); return {**t,"workspaces":task_workspaces(tid),"sandboxes":task_sandboxes(tid),"task_integration":task_integration_row(tid)}
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
        try: branch,path,commit=git.create_agent(r["repo_path"],agent_s,t["slug"],base_branch)
        except (GitSafetyError,GitCommandError) as exc: return {"ok":False,"error":str(exc)}
        role_clean=role.strip()[:80]; profile_clean=sandbox_profile.strip().upper() or None
        try:
            wid=db.execute("INSERT INTO agent_workspaces(repository_id,agent,task_name,branch,worktree_path,base_branch,base_commit,last_commit,status,task_id,role,sandbox_profile) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (repository_id,agent_s,t["slug"],branch,str(path),base_branch,commit,commit,"CREATED",tid,role_clean,profile_clean))
        except Exception as exc:
            if not git.status(path).strip(): git.close(r["repo_path"],path)
            return {"ok":False,"error":str(exc)}
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
    @app.post("/api/tasks/{tid}/integrations")
    def create_task_integration(tid:int):
        t=task_row(tid); ready=[w for w in task_workspaces(tid) if w["status"]=="READY"]
        if not ready: raise GitSafetyError("No READY agent workspaces to integrate")
        tiid=db.execute("INSERT INTO task_integrations(task_id,status) VALUES(?,?)",(tid,"MERGING"))
        db.execute("UPDATE tasks SET status='INTEGRATING',updated_at=CURRENT_TIMESTAMP WHERE id=?",(tid,)); db.event("task",tid,"INTEGRATION_CREATED",str(tiid))
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
            db.execute("UPDATE tasks SET status='INTEGRATING',updated_at=CURRENT_TIMESTAMP WHERE id=?",(tid,))
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
        db.execute("UPDATE tasks SET status='TESTING',updated_at=CURRENT_TIMESTAMP WHERE id=?",(tid,))
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/mark-merged")
    def mark_task_merged(tid:int):
        task_row(tid); db.execute("UPDATE tasks SET status='MERGED',merged_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(tid,)); db.event("task",tid,"TASK_MERGED")
        for sb in task_sandboxes(tid):
            if sb["status"] not in ("CLOSED","CLEANING"): sandboxes.mark_cleanup_eligible(sb["id"])
        return RedirectResponse(f"/tasks/{tid}",303)
    @app.post("/api/tasks/{tid}/close")
    def close_task(tid:int):
        """READY_FOR_MAIN -> PR (external/GitHub) -> MERGED -> CLOSED
        (section 22): only reachable once already MERGED. Closing forces
        every remaining task sandbox onto the cleanup path immediately
        rather than waiting out its retention window."""
        t=task_row(tid)
        if t["status"]!="MERGED": raise GitSafetyError("Task must be MERGED before it can be Closed")
        db.execute("UPDATE tasks SET status='CLOSED',closed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(tid,)); db.event("task",tid,"TASK_CLOSED")
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
    @app.post("/api/sandboxes/{sid}/start")
    def start_sandbox(sid:int): sandbox_row(sid); sandboxes.provision(sid); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/stop")
    def stop_sandbox(sid:int): sandbox_row(sid); sandboxes.stop(sid); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/restart")
    def restart_sandbox(sid:int): sandbox_row(sid); sandboxes.stop(sid); sandboxes.provision(sid); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/rebuild")
    def rebuild_sandbox(sid:int):
        sandbox_row(sid)
        for src in db.all("SELECT * FROM sandbox_sources WHERE sandbox_id=?",(sid,)):
            db.execute("UPDATE sandbox_sources SET commit_sha=? WHERE id=?",(git.head(src["worktree_path"]),src["id"]))
        sandboxes._write_manifest(sid); db.execute("UPDATE sandboxes SET status='CREATED',health_status='UNKNOWN' WHERE id=?",(sid,)); db.event("sandbox",sid,"SANDBOX_REBUILD_REQUESTED")
        sandboxes.provision(sid); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/health")
    def health_sandbox(sid:int): sandbox_row(sid); sandboxes.health_check(sid); return RedirectResponse(f"/sandboxes/{sid}",303)
    @app.post("/api/sandboxes/{sid}/cleanup")
    def cleanup_sandbox_now(sid:int): sandbox_row(sid); sandboxes.cleanup(sid,force=True); return RedirectResponse(f"/sandboxes/{sid}",303)
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
