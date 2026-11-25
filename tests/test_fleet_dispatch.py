import json
from pathlib import Path

from app.services.fleet_permissions import FleetProfile, Probe
from app.services.git_workspace import GitWorkspaceService


def test_local_only_preflight_does_not_require_github_or_sudo(git_repo, tmp_path, monkeypatch):
    root, repo = git_repo
    _, wt, _ = GitWorkspaceService(root).create_agent(repo, 'codex', 'local-ready')
    profile = FleetProfile(wt, tmp_path/'state')
    profile.apply(True)
    checks = {key: {'code': 'AUTH_REQUIRED'} for key in ('repository','gh_auth','gh_identity','github_push','git_helper','sudo')}
    checks.update({key: {'code': 'PASS'} for key in ('workspace','disk','codex_auth','codex_version')})
    monkeypatch.setattr(profile, 'audit', lambda: {'checks': checks})
    monkeypatch.setattr('app.services.fleet_permissions.probe', lambda *a, **k: Probe('PASS', 0, 1, '--sandbox --config --cd'))
    assert profile.preflight('codex', scope='local')['code'] == 'PASS'
    assert profile.preflight('codex', scope='delivery')['code'] == 'BLOCKED'


def test_actual_builder_api_consumes_enrolled_profile(client, git_repo, monkeypatch):
    root, repo = git_repo
    client.post('/api/repositories', data={'repo_path':str(repo),'repo_name':'demo','default_branch':'main'})
    rid = client.get('/api/repositories').json()[0]['id']
    response = client.post('/api/tasks/create', data={'title':'Write one local file','repository_id':rid,'agent':'codex','sandbox_profile':'NONE'}, follow_redirects=False)
    tid = int(response.headers['location'].split('/')[-1])
    w = client.get(f'/api/tasks/{tid}').json()['workspaces'][0]
    wid = w['id']
    enrollment = client.post(f'/api/workspaces/{wid}/fleet', json={'trust_owned_workspace':True,'repo':'hungtranbkit/projectflow-workspace-manager','scope':'local','runtime':'native'})
    assert enrollment.status_code == 200, enrollment.text
    assert client.get(f'/api/workspaces/{wid}/fleet').json()['enrolled'] is True
    monkeypatch.setattr(FleetProfile,'preflight',lambda *a,**k: {'code':'BLOCKED','failures':{'codex_auth':'AUTH_REQUIRED'}})
    before = client.app.state.db.all('SELECT * FROM agent_sessions')
    blocked = client.post(f'/api/workspaces/{wid}/sessions',data={'mode':'INTERACTIVE'},follow_redirects=False)
    assert blocked.status_code == 409
    assert 'AUTH_REQUIRED' in blocked.text
    assert client.app.state.db.all('SELECT * FROM agent_sessions') == before
    assert client.delete(f'/api/workspaces/{wid}/fleet').status_code == 200
    assert client.get(f'/api/workspaces/{wid}/fleet').json()['enrolled'] is False


def test_launch_request_replay_preserves_finished_receipt(git_repo,tmp_path,monkeypatch):
    root,repo=git_repo
    _,wt,_=GitWorkspaceService(root).create_agent(repo,'codex','dedupe')
    profile=FleetProfile(wt,tmp_path/'state'); profile.apply(True)
    monkeypatch.setattr(profile,'preflight',lambda *a,**k:{'code':'PASS'})
    calls=[]
    def execute(*args,**kwargs):
        calls.append(args)
        return Probe('PASS',0,1,'finished')
    monkeypatch.setattr('app.services.fleet_permissions.probe',execute)
    first=profile.launch('codex','task',request_key='task-83-canary')
    second=profile.launch('codex','task',request_key='task-83-canary')
    assert first['code']==second['code']=='PASS'
    assert second['replayed'] and len(calls)==1


def test_container_preserves_image_build_tools():
    from app.services.sandbox_runtime import fleet_container_argv
    argv=fleet_container_argv(Path('/owned'), 'task', Path('/installed/codex'), Path('/auth.json'), 'sha256:'+'a'*64,1000,1000,'wm-projectflow-fleet1000-test')
    assert 'type=bind,src=/installed,dst=/opt/codex-bin,readonly' in argv
    assert not any('dst=/usr/local/bin' in value for value in argv)
    assert 'PATH=/opt/codex-bin:/usr/local/bin:/usr/bin:/bin' in argv


def test_dispatch_request_stable_after_commit_and_prompt_change(git_repo,tmp_path,monkeypatch):
    from app.services.fleet_dispatch import FleetDispatch
    from types import SimpleNamespace
    root,repo=git_repo
    _,wt,_=GitWorkspaceService(root).create_agent(repo,'codex','stable-request')
    workspace={'id':7,'task_id':83,'worktree_path':str(wt),'agent':'codex'}
    dispatch=FleetDispatch(SimpleNamespace(state_dir=tmp_path/'state'))
    dispatch.enroll(workspace,{'trust_owned_workspace':True,'scope':'local'})
    monkeypatch.setattr(FleetProfile,'preflight',lambda *a,**k:{'code':'PASS'})
    first=dispatch.prepare(workspace,'original task')
    git=GitWorkspaceService(root)
    git.git(wt,'commit','--allow-empty','-m','agent checkpoint')
    second=dispatch.prepare(workspace,'original task')
    assert first['request_key']==second['request_key']
    import pytest
    from app.services.fleet_permissions import FleetError
    with pytest.raises(FleetError,match='request prompt changed'):
        dispatch.prepare(workspace,'regenerated task after exit')
    dispatch.profile(workspace).rollback()
    dispatch.enroll(workspace,{'trust_owned_workspace':True,'scope':'local','request_key':'owner-version-2'})
    third=dispatch.prepare(workspace,'authorized new work')
    assert first['request_key']!=third['request_key']


def test_fast_trusted_wrapper_cannot_leave_false_running_or_delivered(client,git_repo,tmp_path):
    import sys,time
    root,repo=git_repo
    client.post('/api/repositories',data={'repo_path':str(repo),'repo_name':'demo','default_branch':'main'})
    rid=client.get('/api/repositories').json()[0]['id']
    response=client.post('/api/tasks/create',data={'title':'blocked wrapper','repository_id':rid,'agent':'codex','sandbox_profile':'NONE'},follow_redirects=False)
    tid=int(response.headers['location'].split('/')[-1]);workspace=client.get(f'/api/tasks/{tid}').json()['workspaces'][0]
    manager=client.app.state.agent_sessions
    for _ in range(3):
        sid=manager.start(task_id=tid,workspace_id=workspace['id'],agent='codex',worktree_path=workspace['worktree_path'],trusted_launch={'argv':[sys.executable,'-c','print("BLOCKED")'],'profile':'trusted-project-unattended-v1','prompt':'task','receipt':str(tmp_path/'absent.json')})
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            row=client.app.state.db.one('SELECT * FROM agent_sessions WHERE id=?',(sid,))
            if row['status']=='EXITED':break
            time.sleep(.02)
        assert row['status']=='EXITED'
        assert row['prompt_status']=='FAILED'
        assert sid not in manager._live
