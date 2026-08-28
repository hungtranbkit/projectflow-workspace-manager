from pathlib import Path
import subprocess,time

def git(path,*args): return subprocess.run(["git",*args],cwd=path,text=True,capture_output=True,check=True).stdout.strip()
def add_commit(path,name,text):
    (Path(path)/name).write_text(text); git(path,"add",name); git(path,"commit","-m",f"add {name}")
def wait_tests(client,iid):
    for _ in range(100):
        runs=client.app.state.db.all("SELECT status FROM test_runs WHERE workspace_type='integration' AND workspace_id=?",(iid,))
        if runs and all(x["status"] not in ("QUEUED","RUNNING") for x in runs): return
        time.sleep(.05)
    raise AssertionError("tests did not finish")

def test_full_real_git_golden_flow_and_staleness(client,git_repo):
    _,repo=git_repo; client.post("/api/repositories",data={"repo_path":repo,"repo_name":"demo","default_branch":"main"}); rid=client.get("/api/repositories").json()[0]["id"]
    for agent,task in [("codex","change-a"),("claude","change-b")]: client.post("/api/workspaces",data={"repository_id":rid,"agent":agent,"task_name":task,"base_branch":"main"})
    ws=client.get("/api/workspaces").json(); codex=next(x for x in ws if x["agent"]=="codex"); claude=next(x for x in ws if x["agent"]=="claude")
    add_commit(codex["worktree_path"],"a.txt","A"); add_commit(claude["worktree_path"],"b.txt","B")
    client.post("/api/integrations",data={"repository_id":str(rid),"name":"golden","base_branch":"main","workspace_ids":[str(codex["id"]),str(claude["id"])]})
    iid=client.get("/api/integrations").json()[0]["id"]
    client.post(f"/api/integrations/{iid}/test"); wait_tests(client,iid); assert client.post(f"/api/integrations/{iid}/ready-for-main",follow_redirects=False).status_code==303
    assert client.get("/api/integrations").json()[0]["ready_for_main"]==1
    add_commit(codex["worktree_path"],"a2.txt","A2")
    client.get(f"/integrations/{iid}"); assert client.get("/api/integrations").json()[0]["ready_for_main"]==0
    client.post(f"/api/integrations/{iid}/merge-latest"); client.post(f"/api/integrations/{iid}/test"); wait_tests(client,iid)
    assert client.post(f"/api/integrations/{iid}/ready-for-main",follow_redirects=False).status_code==303
    integration=client.get("/api/integrations").json()[0]
    assert integration["ready_for_main"]==1 and integration["verified_commit"]==git(integration["worktree_path"],"rev-parse","HEAD")
    assert client.post(f"/api/integrations/{iid}/close",follow_redirects=False).status_code==303
    for workspace in (codex,claude):
        assert client.post(f"/api/workspaces/{workspace['id']}/close",follow_redirects=False).status_code==303
        assert not Path(workspace["worktree_path"]).exists()
    assert not Path(integration["worktree_path"]).exists()
