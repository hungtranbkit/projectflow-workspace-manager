from __future__ import annotations
import itertools
import ipaddress
import json
import os
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
    runtime = c.app.state.sandboxes.runtime
    compose_up = runtime.compose_up

    def compose_up_with_test_ipam(compose_project, compose_file, env_file, cwd, services, sandbox_id, project_directory=None):
        # Compose network definitions live in the repository fixture, but a
        # task-integration sandbox can reuse that file while another sandbox
        # from the same repo is still running. Choose an address per sandbox
        # in the test-owned env file so each real bridge network stays unique.
        subnet = next_test_docker_subnet()
        env_path = Path(env_file)
        with env_path.open("a", encoding="utf-8") as stream:
            stream.write(f"WM_TEST_DOCKER_SUBNET={subnet}\n")
        return compose_up(
            compose_project, compose_file, env_path, cwd, services, sandbox_id,
            project_directory=project_directory,
        )

    runtime.compose_up = compose_up_with_test_ipam
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
    ports:
      - "${WM_PORT_APP}:80"
networks:
  default:
    ipam:
      config:
        - subnet: ${WM_TEST_DOCKER_SUBNET}
"""

_test_docker_subnet_ids = itertools.count()
_test_docker_pool: ipaddress.IPv4Network | None = None


def _non_overlapping_test_docker_pool() -> ipaddress.IPv4Network:
    """Return a private test-only pool verified against current routes/networks.

    Do not edit the daemon's address pools: each pytest fixture gives its own
    Compose project a /28 from this non-overlapping range. Override the default
    candidate on hosts whose route inventory uses it.
    """
    global _test_docker_pool
    if _test_docker_pool is not None:
        return _test_docker_pool

    pool = ipaddress.ip_network(
        os.environ.get("PROJECTFLOW_TEST_DOCKER_IPAM_POOL", "10.252.0.0/16"),
        strict=True,
    )
    if not isinstance(pool, ipaddress.IPv4Network) or pool.prefixlen > 28:
        raise RuntimeError("PROJECTFLOW_TEST_DOCKER_IPAM_POOL must be an IPv4 pool no smaller than /28")

    routes_result = subprocess.run(
        ["ip", "-j", "route", "show"], capture_output=True, text=True, check=True,
    )
    routes = json.loads(routes_result.stdout)
    occupied: list[ipaddress.IPv4Network] = []
    for route in routes:
        destination = route.get("dst")
        if not destination or destination == "default":
            continue
        try:
            network = ipaddress.ip_network(destination, strict=False)
        except ValueError:
            continue
        if isinstance(network, ipaddress.IPv4Network):
            occupied.append(network)

    listed = subprocess.run(
        ["docker", "network", "ls", "-q"], capture_output=True, text=True, check=True,
    )
    network_ids = listed.stdout.split()
    if network_ids:
        inspected = subprocess.run(
            ["docker", "network", "inspect", *network_ids],
            capture_output=True, text=True, check=True,
        )
        for docker_network in json.loads(inspected.stdout):
            for config in (docker_network.get("IPAM") or {}).get("Config") or []:
                subnet = config.get("Subnet")
                if not subnet:
                    continue
                network = ipaddress.ip_network(subnet, strict=False)
                if isinstance(network, ipaddress.IPv4Network):
                    occupied.append(network)

    conflicts = [str(network) for network in occupied if pool.overlaps(network)]
    if conflicts:
        raise RuntimeError(
            f"test Docker pool {pool} overlaps host routes/networks {', '.join(conflicts)}; "
            "set PROJECTFLOW_TEST_DOCKER_IPAM_POOL to an unused private IPv4 range"
        )
    _test_docker_pool = pool
    return pool


def nginx_compose_for_test() -> str:
    """Compose fixture using a per-sandbox, route-checked test subnet."""
    return NGINX_COMPOSE


def next_test_docker_subnet() -> str:
    pool = _non_overlapping_test_docker_pool()
    subnet_index = next(_test_docker_subnet_ids)
    subnet_count = 1 << (28 - pool.prefixlen)
    if subnet_index >= subnet_count:
        raise RuntimeError(f"test Docker pool {pool} exhausted its {subnet_count} isolated /28 networks")
    address = int(pool.network_address) + subnet_index * 16
    subnet = ipaddress.ip_network((address, 28))
    return str(subnet)

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
        (repo/"compose.yml").write_text(nginx_compose_for_test())
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
