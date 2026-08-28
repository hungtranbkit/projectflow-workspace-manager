from pathlib import Path
import subprocess
from app.services.git_workspace import GitWorkspaceService

def commit(path:Path,name:str,text:str):
    (path/name).write_text(text); subprocess.run(["git","add",name],cwd=path,check=True); subprocess.run(["git","commit","-m",name],cwd=path,check=True,capture_output=True)

def test_real_agent_integration_update_cleanup(git_repo):
    root,repo=git_repo; svc=GitWorkspaceService(root)
    b1,w1,_=svc.create_agent(repo,"codex","feature-a"); commit(w1,"a.txt","a")
    b2,w2,_=svc.create_agent(repo,"claude","feature-b"); commit(w2,"b.txt","b")
    ib,iw,_=svc.create_integration(repo,"combined")
    assert svc.merge(iw,b1).returncode == 0; assert svc.merge(iw,b2).returncode == 0
    assert (iw/"a.txt").exists() and (iw/"b.txt").exists()
    old=svc.head(repo,b1); commit(w1,"a2.txt","new"); new=svc.head(repo,b1)
    assert old != new and not svc.is_ancestor(iw,new)
    assert svc.merge(iw,b1).returncode == 0 and svc.is_ancestor(iw,new)
    svc.close(repo,iw); svc.close(repo,w1); svc.close(repo,w2)
    assert w1.name.startswith("demo-") and iw.name.startswith("demo-integration-")

def test_real_merge_conflict(git_repo):
    root,repo=git_repo; svc=GitWorkspaceService(root)
    b1,w1,_=svc.create_agent(repo,"codex","conflict-a"); commit(w1,"same.txt","A")
    b2,w2,_=svc.create_agent(repo,"claude","conflict-b"); commit(w2,"same.txt","B")
    _,iw,_=svc.create_integration(repo,"conflict")
    assert svc.merge(iw,b1).returncode == 0
    assert svc.merge(iw,b2).returncode != 0
    assert svc.conflict_files(iw)==["same.txt"]
