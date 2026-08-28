import time

def register(client,repo): return client.post("/api/repositories",data={"repo_path":str(repo),"repo_name":"demo","default_branch":"main"},follow_redirects=False)

def test_pages_render(client,git_repo):
    _,repo=git_repo; register(client,repo)
    for path in ["/","/repositories","/workspaces","/integrations","/test-runs","/help","/settings"]:
        response=client.get(path); assert response.status_code==200, path

def test_help_page_navigation_and_operator_content(client):
    response=client.get("/help")
    assert response.status_code==200
    text=response.text
    assert "Hướng dẫn sử dụng Workspace Manager" in text
    assert "READY_FOR_MAIN không có nghĩa là đã merge main" in text
    assert "GitHub Pull Request" in text and "không bypass GitHub CI" in text
    assert 'href="/help">Hướng dẫn</a>' in text
    assert "visual sample" in text and "không đụng repository thật" in text

def test_first_run_and_empty_states_link_to_guide(client):
    dashboard=client.get("/").text
    assert "Bắt đầu với Workspace Manager" in dashboard
    assert "/help#quick-start" in dashboard
    assert "Chưa có Agent Workspace" in dashboard
    assert "/help#agent-workspaces" in dashboard
    assert "Chưa có Integration" in dashboard
    assert "/help#integration" in dashboard

def test_contextual_help_links(client):
    assert "/help#agent-workspaces" in client.get("/workspaces").text
    assert "/help#integration" in client.get("/integrations").text

def test_launcher_ui_help_and_settings(client):
    help_text=client.get("/help").text
    assert 'id="open-agent"' in help_text
    assert "codex --yolo" in help_text
    assert "claude --dangerously-skip-permissions" in help_text
    settings=client.get("/settings").text
    assert "Agent Launchers" in settings and "--dangerously-skip-permissions" in settings

def test_launch_api_known_workspace_uses_owner_service(client,git_repo,monkeypatch):
    _,repo=git_repo; register(client,repo); rid=client.get("/api/repositories").json()[0]["id"]
    client.post("/api/workspaces",data={"repository_id":rid,"agent":"codex","task_name":"launcher-api","base_branch":"main"})
    workspace=client.get("/api/workspaces").json()[0]
    called={}
    def fake_launch(path,agent): called.update(path=path,agent=agent); return {"terminal":"ptyxis","worktree":path,"agent":"Codex","result":"requested"}
    monkeypatch.setattr(client.app.state.launcher,"launch_agent",fake_launch)
    response=client.post(f"/api/workspaces/{workspace['id']}/launch-agent")
    assert response.status_code==200 and response.json()["ok"] is True
    assert called=={"path":workspace["worktree_path"],"agent":"codex"}
    assert client.app.state.db.one("SELECT action FROM workspace_events WHERE action='AGENT_LAUNCHED'")

def test_launch_api_rejects_invalid_workspace_and_outside_path(client,tmp_path):
    assert client.post("/api/workspaces/99999/launch-agent").status_code==404
    db=client.app.state.db
    rid=db.execute("INSERT INTO repositories(repo_name,repo_path,default_branch) VALUES(?,?,?)",("unsafe",str(tmp_path),"main"))
    wid=db.execute("INSERT INTO agent_workspaces(repository_id,agent,task_name,branch,worktree_path,base_branch,base_commit) VALUES(?,?,?,?,?,?,?)",(rid,"codex","unsafe","agent/codex/unsafe",str(tmp_path/"outside"),"main","deadbeef"))
    response=client.post(f"/api/workspaces/{wid}/open-terminal")
    assert response.status_code==409
    assert response.json()["code"] in ("WORKTREE_NOT_FOUND","INVALID_WORKTREE")

def test_legacy_external_worktree_record_renders_but_launcher_stays_blocked(client,git_repo,tmp_path):
    _,repo=git_repo; register(client,repo); rid=client.get("/api/repositories").json()[0]["id"]
    legacy=tmp_path.parent/"legacy-worktree"; legacy.mkdir(exist_ok=True)
    wid=client.app.state.db.execute("INSERT INTO agent_workspaces(repository_id,agent,task_name,branch,worktree_path,base_branch,base_commit) VALUES(?,?,?,?,?,?,?)",(rid,"codex","legacy","agent/codex/legacy",str(legacy),"main","deadbeef"))
    assert client.get(f"/workspaces/{wid}").status_code==200
    assert client.post(f"/api/workspaces/{wid}/open-terminal").status_code==409

def test_create_agent_and_integration_pages(client,git_repo):
    _,repo=git_repo; register(client,repo); rid=client.get("/api/repositories").json()[0]["id"]
    response=client.post("/api/workspaces",data={"repository_id":rid,"agent":"codex","task_name":"Web Feature","base_branch":"main"},follow_redirects=False)
    assert response.status_code==303
    wid=client.get("/api/workspaces").json()[0]["id"]
    assert "agent/codex/web-feature" in client.get(f"/workspaces/{wid}").text
    response=client.post("/api/integrations",data={"repository_id":str(rid),"name":"web-integration","base_branch":"main","workspace_ids":str(wid)},follow_redirects=False)
    assert response.status_code==303
    iid=client.get("/api/integrations").json()[0]["id"]
    assert "Integration · web-integration" in client.get(f"/integrations/{iid}").text

def test_invalid_outside_path_rejected(client,tmp_path):
    outside=tmp_path/"outside"; outside.mkdir()
    response=client.post("/api/repositories",data={"repo_path":str(outside),"repo_name":"bad","default_branch":"main"})
    assert response.status_code==409
