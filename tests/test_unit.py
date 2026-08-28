from pathlib import Path
import pytest
from app.services.git_workspace import GitWorkspaceService, GitSafetyError, slugify
from app.services.project_contract import load_contract, ContractError

def test_slug_and_names(tmp_path):
    assert slugify("Session Filter UI") == "session-filter-ui"
    svc=GitWorkspaceService(tmp_path)
    assert svc.validate_branch("agent/codex/session-filter")
    with pytest.raises(GitSafetyError): svc.validate_branch("agent/codex/../../main")
    with pytest.raises(GitSafetyError): slugify("../")

def test_path_validation(tmp_path):
    svc=GitWorkspaceService(tmp_path)
    assert svc.validate_worktree(tmp_path/".worktrees"/"codex-task")
    with pytest.raises(GitSafetyError): svc.validate_worktree(tmp_path/"elsewhere")

def test_external_repo_uses_shared_collision_safe_worktree_root(tmp_path):
    root=tmp_path/"allowed"; repo=root/"example-repo"; repo.mkdir(parents=True)
    import subprocess
    subprocess.run(["git","init","-b","main"],cwd=repo,check=True,capture_output=True)
    subprocess.run(["git","config","user.email","test@example.invalid"],cwd=repo,check=True)
    subprocess.run(["git","config","user.name","Test"],cwd=repo,check=True)
    (repo/"README.md").write_text("base\n")
    subprocess.run(["git","add","."],cwd=repo,check=True); subprocess.run(["git","commit","-m","base"],cwd=repo,check=True,capture_output=True)
    svc=GitWorkspaceService(root,worktree_root=root/".worktrees")
    _,path,_=svc.create_agent(repo,"codex","task")
    assert path == root/".worktrees"/"example-repo-codex-task"
    svc.close(repo,path)

def test_worktree_root_outside_allowed_root_rejected(tmp_path):
    with pytest.raises(GitSafetyError): GitWorkspaceService(tmp_path/"allowed",worktree_root=tmp_path/"outside")

def test_contract_parsing(tmp_path):
    (tmp_path/"PROJECT.yaml").write_text("commands:\n  test:\n    command: ./test.sh\nci:\n  required: [test]\n")
    assert load_contract(tmp_path)[0][:2] == ("test","./test.sh")
    (tmp_path/"PROJECT.yaml").write_text("commands: {}\nci: {required: []}\n")
    with pytest.raises(ContractError): load_contract(tmp_path)
