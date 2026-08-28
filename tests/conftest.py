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
    root,_=git_repo
    # configured_state_dir is test-isolated (tmp_path), not the real host
    # ~/.local/state/projectflow-workspace-manager/ -- sandbox env files
    # must never leak into (or be polluted by) a real user's state dir.
    settings=Settings(root,"127.0.0.1",8765,tmp_path/"test.db",30,configured_state_dir=tmp_path/"state")
    return TestClient(create_app(settings))

def make_repo(root, name, project_yaml_extra=""):
    """A disposable git repo with a minimal but real PROJECT.yaml -- shared
    helper for sandbox/cross-repo tests that need more than one repo."""
    repo=root/name; repo.mkdir(parents=True)
    run(repo,"git","init","-b","main"); run(repo,"git","config","user.email","test@example.invalid"); run(repo,"git","config","user.name","Test")
    (repo/"README.md").write_text("base\n")
    (repo/"PROJECT.yaml").write_text(
        "schema_version: 1\nproject: {code: "+name+"}\nsource: {root: .}\n"
        "commands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n"
        +project_yaml_extra
    )
    run(repo,"git","add","."); run(repo,"git","commit","-m","base")
    return repo

NGINX_SANDBOX_CONTRACT = """
sandbox:
  compose_file: compose.yml
  default_profile: backend
  profiles:
    backend:
      services: [app]
  ports:
    app:
      container: 80
      range: {lo}-{hi}
  health:
    app:
      path: /
  outputs:
    backend_url:
      service: app
"""

NGINX_COMPOSE = """
services:
  app:
    image: nginx:1.28-alpine
    ports:
      - "${WM_PORT_APP}:80"
"""

@pytest.fixture
def sandboxable_repo_factory(tmp_path):
    """Creates a repo with a real, runnable sandbox: contract (nginx:alpine,
    already present locally in this environment -- no network pull) so
    sandbox tests exercise the actual docker compose pipeline, not a mock."""
    counter = {"n": 0}
    def make(root, name, port_range=(21000, 21099)):
        counter["n"] += 1
        lo, hi = port_range
        repo = make_repo(root, name, NGINX_SANDBOX_CONTRACT.format(lo=lo, hi=hi))
        (repo/"compose.yml").write_text(NGINX_COMPOSE)
        run(repo,"git","add","."); run(repo,"git","commit","-m","sandbox contract")
        return repo
    return make

@pytest.fixture(scope="session", autouse=True)
def _docker_sandbox_safety_net():
    """Session-end safety net for the real-Docker sandbox tests: even if an
    individual test's own cleanup call fails to run (an assertion raised
    before reaching its `finally`, etc.), never leave a `wm-*`-namespaced
    container/network behind on this shared host. Narrowly scoped to the
    exact `wm-` prefix this app always uses (docs section 15/66) -- never
    touches any other container on the host, real or otherwise."""
    yield
    try:
        import subprocess as _sp
        ids = _sp.run(
            ["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project"],
            capture_output=True, text=True, timeout=15,
        ).stdout.split()
        for cid in ids:
            name = _sp.run(["docker", "inspect", "-f", "{{.Name}}", cid], capture_output=True, text=True, timeout=5).stdout.strip().lstrip("/")
            if name.startswith("wm-"):
                _sp.run(["docker", "rm", "-f", cid], capture_output=True, timeout=15)
        nets = _sp.run(["docker", "network", "ls", "--format", "{{.Name}}"], capture_output=True, text=True, timeout=15).stdout.split()
        for net in nets:
            if net.startswith("wm-"):
                _sp.run(["docker", "network", "rm", net], capture_output=True, timeout=15)
    except Exception:
        pass  # best-effort safety net only -- never fail the test session over cleanup itself
