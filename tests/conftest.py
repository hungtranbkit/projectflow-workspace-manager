from __future__ import annotations
import subprocess
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app

def run(path, *args): return subprocess.run(list(args),cwd=path,text=True,capture_output=True,check=True)

@pytest.fixture
def git_repo(tmp_path):
    root=tmp_path/"root"; repo=root/"demo"; repo.mkdir(parents=True)
    run(repo,"git","init","-b","main"); run(repo,"git","config","user.email","test@example.invalid"); run(repo,"git","config","user.name","Test")
    (repo/"README.md").write_text("base\n"); (repo/"PROJECT.yaml").write_text("schema_version: 1\nproject: {code: demo}\nsource: {root: .}\ncommands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n")
    run(repo,"git","add","."); run(repo,"git","commit","-m","base")
    return root,repo

@pytest.fixture
def client(git_repo,tmp_path):
    root,_=git_repo; settings=Settings(root,"127.0.0.1",8765,tmp_path/"test.db",30)
    return TestClient(create_app(settings))
