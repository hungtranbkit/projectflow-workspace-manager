from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, Form, HTTPException, Request
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

def create_app(settings=None):
    settings = settings or load_settings(); db = Database(settings.db_path); db.init()
    git = GitWorkspaceService(settings.root, worktree_root=settings.worktree_root); runner = TestRunner(db, git); launcher = TerminalLauncherService(settings, git)
    sandbox_runtime = SandboxRuntimeService(); ports = PortAllocatorService(db)
    sandboxes = SandboxManager(db, sandbox_runtime, ports, settings.state_dir, settings.max_running_sandboxes, settings.sandbox_retention_hours)
    cleanup_worker = CleanupWorker(db, sandboxes, settings.cleanup_poll_seconds)
    app = FastAPI(title="ProjectFlow Workspace Manager", docs_url=None, redoc_url=None)
    base = Path(__file__).parent; templates = Jinja2Templates(directory=base / "templates")
    app.mount("/static", StaticFiles(directory=base / "static"), name="static")
    app.state.settings, app.state.db, app.state.git, app.state.runner, app.state.launcher = settings, db, git, runner, launcher
    app.state.sandboxes, app.state.ports, app.state.sandbox_runtime, app.state.cleanup_worker = sandboxes, ports, sandbox_runtime, cleanup_worker
    cleanup_worker.start()
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
        active_tasks=db.one("SELECT COUNT(*) n FROM tasks WHERE status NOT IN ('MERGED','CANCELLED','CLOSED')")["n"]
        running_sandboxes=sandboxes.running_count()
        cleanup_pending=db.one("SELECT COUNT(*) n FROM sandboxes WHERE status='CLEANUP_ELIGIBLE'")["n"]
        summary={"active":len(agents),"ready":sum(x["status"]=="READY" for x in agents),"testing":sum(x["status"]=="TESTING" for x in ints),"main":sum(bool(x["ready_for_main"]) for x in ints),"tasks":active_tasks,"sandboxes":running_sandboxes,"cleanup":cleanup_pending}
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
    def workspaces(request: Request): return render(request,"workspaces.html",workspaces=db.all("SELECT w.*,r.repo_name FROM agent_workspaces w JOIN repositories r ON r.id=w.repository_id ORDER BY w.updated_at DESC"),repositories=db.all("SELECT * FROM repositories WHERE enabled=1"))
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
        runs=db.all("SELECT * FROM test_runs WHERE workspace_type='agent' AND workspace_id=? ORDER BY id DESC",(wid,)); return render(request,"workspace_detail.html",w=w,details=details,runs=runs)
    @app.get("/api/workspaces/{wid}")
    def api_workspace(wid:int): return agent_row(wid)
    @app.post("/api/workspaces/{wid}/ready")
    def ready(wid:int):
        w=agent_row(wid); head=git.head(w["worktree_path"]); db.execute("UPDATE agent_workspaces SET status='READY',ready_for_integration=1,last_commit=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(head,wid)); db.event("agent",wid,"READY_MARKED",head)
        recompute_task_status(w.get("task_id"))
        return RedirectResponse(f"/workspaces/{wid}",303)
    @app.post("/api/workspaces/{wid}/test")
    def test_agent(wid:int): w=agent_row(wid); runner.start("agent",wid,Path(w["worktree_path"])); return RedirectResponse(f"/workspaces/{wid}",303)
    @app.post("/api/workspaces/{wid}/close")
    def close_agent(wid:int): w=agent_row(wid); git.close(w["repo_path"],w["worktree_path"]); db.execute("UPDATE agent_workspaces SET status='CLOSED',closed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(wid,)); db.event("agent",wid,"WORKSPACE_CLOSED"); return RedirectResponse("/workspaces",303)
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
        if not tid: return
        t=db.one("SELECT status FROM tasks WHERE id=?",(tid,))
        if not t or t["status"] in ("MERGED","CANCELLED","CLOSED","INTEGRATING","TESTING","READY_FOR_MAIN","PR_OPEN"): return
        ws=task_workspaces(tid)
        new_status="READY_FOR_INTEGRATION" if ws and all(w["status"]=="READY" for w in ws) else ("IN_PROGRESS" if ws else "OPEN")
        db.execute("UPDATE tasks SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(new_status,tid))

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

    @app.get("/tasks",response_class=HTMLResponse)
    def tasks_page(request:Request):
        rows=db.all("SELECT * FROM tasks ORDER BY updated_at DESC")
        for t in rows: t["workspace_count"]=db.one("SELECT COUNT(*) n FROM agent_workspaces WHERE task_id=?",(t["id"],))["n"]
        return render(request,"tasks.html",tasks=rows)
    @app.post("/api/tasks")
    def create_task(title:str=Form(...),description:str=Form("")):
        slug=slugify(title); tid=db.execute("INSERT INTO tasks(slug,title,description,status) VALUES(?,?,?,?)",(slug,title,description,"OPEN"))
        db.event("task",tid,"TASK_CREATED",slug); return RedirectResponse(f"/tasks/{tid}",303)
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
        # sandbox: contract" so the empty state never lies.
        by_owner={(sb["owner_type"],sb["owner_id"]):sb for sb in sbxs}
        for w in workspaces:
            sb=by_owner.get(("AGENT_WORKSPACE",w["id"]))
            if sb:
                w["sandbox"]=sandbox_view(sb); w["sandbox"]["stale"]=sandboxes.is_stale(sb["id"],sandbox_current_commits(sb["id"]))
            else:
                w["sandbox"]=None
                try: w["sandbox_configured"]=load_sandbox_contract(Path(w["repo_path"])) is not None
                except SandboxContractError: w["sandbox_configured"]=True

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
        return render(request,"task_detail.html",t=t,workspaces=workspaces,sandboxes=sbxs,task_integration=ti,ti_repos=ti_repos,
                      integration_sandboxes=integration_sandboxes,readiness=readiness,summary=summary,all_ready=all_ready,
                      task_cleanup_countdown=format_countdown(earliest_cleanup),
                      repositories=db.all("SELECT * FROM repositories WHERE enabled=1"),agents=settings.agents)
    @app.get("/api/tasks/{tid}")
    def api_task(tid:int):
        t=task_row(tid); return {**t,"workspaces":task_workspaces(tid),"sandboxes":task_sandboxes(tid),"task_integration":task_integration_row(tid)}
    @app.post("/api/tasks/{tid}/workspaces")
    def create_task_workspace(tid:int,repository_id:int=Form(...),agent:str=Form(...),role:str=Form(""),base_branch:str=Form("main"),sandbox_profile:str=Form("")):
        t=task_row(tid); r=repo(repository_id); agent_s=slugify(agent)
        if agent_s not in settings.agents: raise GitSafetyError("Agent is not allowed")
        branch,path,commit=git.create_agent(r["repo_path"],agent_s,t["slug"],base_branch)
        role_clean=role.strip()[:80]; profile_clean=sandbox_profile.strip().upper() or None
        try:
            wid=db.execute("INSERT INTO agent_workspaces(repository_id,agent,task_name,branch,worktree_path,base_branch,base_commit,last_commit,status,task_id,role,sandbox_profile) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (repository_id,agent_s,t["slug"],branch,str(path),base_branch,commit,commit,"CREATED",tid,role_clean,profile_clean))
        except Exception:
            if not git.status(path).strip(): git.close(r["repo_path"],path)
            raise
        db.event("agent",wid,"WORKSPACE_CREATED",branch)
        db.execute("UPDATE tasks SET status=CASE WHEN status='OPEN' THEN 'IN_PROGRESS' ELSE status END,updated_at=CURRENT_TIMESTAMP WHERE id=?",(tid,))
        sid=auto_create_sandbox(tid,repository_id,r["repo_path"],"AGENT_WORKSPACE",wid,role_clean or agent_s,branch,commit,path,profile_clean,t.get("default_sandbox_profile"))
        if sid: db.execute("UPDATE agent_workspaces SET sandbox_profile=(SELECT profile FROM sandboxes WHERE id=?) WHERE id=?",(sid,wid))
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
        return render(request,"sandbox_detail.html",sb=sb,sources=sources,ports=ports_,ops=ops,hw=hw,outputs=outputs_,lan_ip=lan_ip,
                      task=task,stale=stale,view=sandbox_view(sb))
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
