from __future__ import annotations
import subprocess
import warnings
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app

_created_test_sandboxes = {}

def run(path, *args): return subprocess.run(list(args),cwd=path,text=True,capture_output=True,check=True)

@pytest.fixture
def git_repo(tmp_path):
    root=tmp_path/"root"; repo=root/"demo"; repo.mkdir(parents=True)
    run(repo,"git","init","-b","main"); run(repo,"git","config","user.email","test@example.invalid"); run(repo,"git","config","user.name","Test")
    (repo/"README.md").write_text("base\n"); (repo/"PROJECT.yaml").write_text("schema_version: 1\nproject: {code: demo}\nsource: {root: .}\ncommands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n")
    run(repo,"git","add","."); run(repo,"git","commit","-m","base")
    return root,repo

def build_client(settings):
    """Shared TestClient factory -- ALWAYS use this (never a bare
    `TestClient(create_app(settings))`) so every test, including ones
    that build a second/third app instance to simulate a process
    restart, gets the same synchronous-spawn override below.
    Button-state-ux: SandboxManager.provision/reset_data/cleanup run
    their real docker work via self.spawn (a real background thread in
    production, so a browser request never blocks on `docker compose
    up/down`). Tests override it to run synchronously inline -- every
    existing assertion made right after calling one of these methods
    still sees the already-finished real result; docker itself is
    still 100% real either way, only the thread scheduling differs."""
    c=TestClient(create_app(settings))
    c.app.state.sandboxes.spawn=lambda fn,args=():fn(*args)
    c.app.state.deployer.spawn=lambda fn,args=():fn(*args)
    runtime = c.app.state.sandbox_runtime
    compose_up = runtime.compose_up

    def tracked_compose_up(compose_project, compose_file, env_file, cwd, services, sandbox_id, project_directory=None):
        # Record before starting: failed compose-up can still create resources.
        # Only actual starts from this test process are eligible for cleanup.
        _created_test_sandboxes[compose_project] = (c.app.state.sandboxes, sandbox_id)
        return compose_up(compose_project, compose_file, env_file, cwd, services, sandbox_id,
                          project_directory=project_directory)

    runtime.compose_up = tracked_compose_up
    return c

@pytest.fixture
def client(git_repo,tmp_path):
    root,_=git_repo
    # configured_state_dir is test-isolated (tmp_path), not the real host
    # ~/.local/state/projectflow-workspace-manager/ -- sandbox env files
    # must never leak into (or be polluted by) a real user's state dir.
    settings=Settings(root,"127.0.0.1",8765,tmp_path/"test.db",30,configured_state_dir=tmp_path/"state")
    return build_client(settings)

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
    # Most fixtures test lifecycle/HTTP behavior, not network allocation.
    # Reuse Docker's bridge instead of consuming a host-wide default subnet.
    network_mode: bridge
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

@pytest.fixture(autouse=True)
def _docker_sandbox_safety_net():
    """Clean only compose starts recorded by this test after its fixtures finish.

    A prefix sweep is unsafe: another task or ProjectFlow session may own
    another wm-* namespace. Use the runtime's existing ownership guard and
    exact compose project; never enumerate or prune host-wide resources.
    Releasing after each test also prevents this process from retaining
    networks until the end of a large suite and exhausting the host pool.
    """
    yield
    for project, (manager, sandbox_id) in list(_created_test_sandboxes.items()):
        try:
            sandbox = manager.db.one("SELECT * FROM sandboxes WHERE id=?", (sandbox_id,))
            if sandbox and sandbox["compose_project"] == project:
                manager.cleanup(sandbox_id, force=True)
        except Exception as exc:
            warnings.warn(f"Test sandbox cleanup failed for {project}: {type(exc).__name__}")
    _created_test_sandboxes.clear()
