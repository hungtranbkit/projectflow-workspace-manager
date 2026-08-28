from __future__ import annotations
from pathlib import Path
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.config import load_settings
from app.db import Database
from app.repositories import discover_repositories
from app.services.git_workspace import GitWorkspaceService, GitSafetyError, GitCommandError, slugify
from app.services.test_runner import TestRunner
from app.services.terminal_launcher import TerminalLauncherService, LauncherError

def create_app(settings=None):
    settings = settings or load_settings(); db = Database(settings.db_path); db.init()
    git = GitWorkspaceService(settings.root, worktree_root=settings.worktree_root); runner = TestRunner(db, git); launcher = TerminalLauncherService(settings, git)
    app = FastAPI(title="ProjectFlow Workspace Manager", docs_url=None, redoc_url=None)
    base = Path(__file__).parent; templates = Jinja2Templates(directory=base / "templates")
    app.mount("/static", StaticFiles(directory=base / "static"), name="static")
    app.state.settings, app.state.db, app.state.git, app.state.runner, app.state.launcher = settings, db, git, runner, launcher
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

    @app.exception_handler(GitSafetyError)
    @app.exception_handler(GitCommandError)
    async def git_error(request, exc): return HTMLResponse(f"<h1>Action blocked</h1><pre>{str(exc)}</pre><a href='javascript:history.back()'>Back</a>", status_code=409)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        agents=db.all("SELECT w.*,r.repo_name FROM agent_workspaces w JOIN repositories r ON r.id=w.repository_id WHERE w.status NOT IN ('CLOSED','DONE') ORDER BY w.updated_at DESC")
        ints=db.all("SELECT i.*,r.repo_name FROM integration_workspaces i JOIN repositories r ON r.id=i.repository_id WHERE i.status!='CLOSED' ORDER BY i.updated_at DESC")
        summary={"active":len(agents),"ready":sum(x["status"]=="READY" for x in agents),"testing":sum(x["status"]=="TESTING" for x in ints),"main":sum(bool(x["ready_for_main"]) for x in ints)}
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
        w=agent_row(wid); head=git.head(w["worktree_path"]); db.execute("UPDATE agent_workspaces SET status='READY',ready_for_integration=1,last_commit=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(head,wid)); db.event("agent",wid,"READY_MARKED",head); return RedirectResponse(f"/workspaces/{wid}",303)
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
        i=integration_row(iid); head=git.head(i["worktree_path"])
        if git.status(i["worktree_path"]).strip(): raise GitSafetyError("Integration worktree must be clean")
        if git.conflict_files(i["worktree_path"]): raise GitSafetyError("Merge conflict exists")
        for s in db.all("SELECT s.*,w.branch FROM integration_sources s JOIN agent_workspaces w ON w.id=s.workspace_id WHERE s.integration_id=?",(iid,)):
            current=git.head(i["repo_path"],s["branch"])
            if current!=s["merged_commit"] or not git.is_ancestor(i["worktree_path"],current): raise GitSafetyError(f"Source not current: {s['branch']}")
        required=len(__import__('app.services.project_contract',fromlist=['load_contract']).load_contract(Path(i["worktree_path"])))
        passed=db.all("SELECT stage,tested_commit,status FROM test_runs WHERE workspace_type='integration' AND workspace_id=? AND tested_commit=? ORDER BY id DESC",(iid,head))
        latest={}
        for x in passed: latest.setdefault(x["stage"],x["status"])
        if len(latest)<required or any(x!="PASS" for x in latest.values()): raise GitSafetyError("All required tests must PASS at current HEAD")
        db.execute("UPDATE integration_workspaces SET status='READY_FOR_MAIN',ready_for_main=1,verified_commit=?,verified_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(head,iid)); db.event("integration",iid,"READY_FOR_MAIN",head); return RedirectResponse(f"/integrations/{iid}",303)
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
    return app

app=create_app()
