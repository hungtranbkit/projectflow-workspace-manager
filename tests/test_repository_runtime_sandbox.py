"""Repository Runtime & Sandbox Settings: PROJECT.yaml's `sandbox:` block
stays the single source of truth (section 4/22) -- the UI only ever
reads/writes that same file (via RepositoryContractEditor, ruamel.yaml
round-trip so unrelated keys/comments survive) and the DB never stores a
second, potentially-conflicting copy of sandbox semantics. Real docker
(nginx:alpine, no network pull) for the Test Configuration provisioning
tests, same convention as sandboxable_repo_factory."""
from __future__ import annotations
import subprocess

import pytest

from tests.conftest import make_repo, run, NGINX_COMPOSE


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


def repo_id(client, name):
    return [r for r in client.get("/api/repositories").json() if r["repo_name"] == name][0]["id"]


def enable_form(profile="QA_CENTER", services="app", deps=None, seed="QA_DEMO", port="80", health="/", auto=False):
    # httpx's TestClient needs repeated form keys as a dict-of-lists, not
    # a flat list of tuples -- the latter is silently mishandled (empty
    # body) by the installed httpx version rather than raising.
    deps = deps or []
    data = {
        "enabled": "on", "profile_name": profile, "services": services,
        "seed_default": seed, "own_port": port, "health_path": health,
    }
    if auto: data["auto_provision"] = "on"
    if deps:
        data["dep_repo"] = [d["repo"] for d in deps]
        data["dep_profile"] = [d.get("profile", "BACKEND") for d in deps]
        data["dep_mode"] = [d.get("mode", "KNOWN_GOOD_MAIN") for d in deps]
    return data


def make_sandboxable_no_contract(root, name):
    """A repo with a real nginx compose file present on disk but NO
    sandbox: block in PROJECT.yaml yet -- exactly the "NOT CONFIGURED"
    starting state this whole feature exists to fix. Named
    compose.sandbox.yml to match RepositoryContractEditor's own default
    compose_file convention."""
    repo = make_repo(root, name)
    (repo / "compose.sandbox.yml").write_text(NGINX_COMPOSE)
    run(repo, "git", "add", ".")
    run(repo, "git", "commit", "-m", "add compose (no sandbox contract yet)")
    return repo


# ---------------------------------------------------------------- loading
def test_not_configured_repo_shows_configure_sandbox(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = repo_id(client, "demo")
    html = client.get(f"/repositories/{rid}/runtime").text
    assert "Runtime sandbox is not configured" in html
    assert "Configure Sandbox" in html or "Save Configuration" in html


def test_existing_project_yaml_loads_into_ui(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = repo_id(client, "demo")
    client.post(f"/api/repositories/{rid}/runtime-sandbox", data=enable_form(), follow_redirects=False)
    html = client.get(f"/repositories/{rid}/runtime").text
    assert "QA_CENTER" in html
    assert "QA_DEMO" in html


# -------------------------------------------------------------------- save
def test_save_modifies_supported_contract_fields(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = repo_id(client, "demo")
    r = client.post(f"/api/repositories/{rid}/runtime-sandbox", data=enable_form(), follow_redirects=False)
    assert r.status_code == 303, r.text
    text = (repo / "PROJECT.yaml").read_text()
    assert "sandbox:" in text
    assert "QA_CENTER" in text
    assert "QA_DEMO" in text


def test_unrelated_yaml_fields_and_comments_preserved(client, git_repo):
    root, repo = git_repo
    (repo / "PROJECT.yaml").write_text(
        (repo / "PROJECT.yaml").read_text() + "\ncustom_note: keep me  # a real comment\n"
    )
    run(repo, "git", "commit", "-am", "add custom field")
    register(client, repo, "demo")
    rid = repo_id(client, "demo")
    client.post(f"/api/repositories/{rid}/runtime-sandbox", data=enable_form(), follow_redirects=False)
    text = (repo / "PROJECT.yaml").read_text()
    assert "custom_note: keep me" in text
    assert "# a real comment" in text
    assert "commands:" in text and "preflight:" in text


def test_invalid_config_does_not_modify_file(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = repo_id(client, "demo")
    before = (repo / "PROJECT.yaml").read_text()
    r = client.post(f"/api/repositories/{rid}/runtime-sandbox", data=enable_form(profile=""), follow_redirects=False)
    assert r.status_code == 409
    assert (repo / "PROJECT.yaml").read_text() == before


def test_dependency_repo_must_be_registered(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = repo_id(client, "demo")
    before = (repo / "PROJECT.yaml").read_text()
    r = client.post(f"/api/repositories/{rid}/runtime-sandbox", data=enable_form(deps=[{"repo": "not-registered"}]), follow_redirects=False)
    assert r.status_code == 409
    assert "not-registered" in r.text
    assert (repo / "PROJECT.yaml").read_text() == before


def test_dependency_cycle_rejected(client, git_repo):
    root, repo_a = git_repo
    repo_b = root / "repo-b"
    repo_b.mkdir()
    run(repo_b, "git", "init", "-b", "main")
    run(repo_b, "git", "config", "user.email", "t@t.invalid")
    run(repo_b, "git", "config", "user.name", "T")
    (repo_b / "PROJECT.yaml").write_text("schema_version: 1\nproject: {code: repo-b}\nsource: {root: .}\n")
    run(repo_b, "git", "add", ".")
    run(repo_b, "git", "commit", "-m", "base")
    register(client, repo_a, "demo")
    register(client, repo_b, "repo-b")
    rid_a = repo_id(client, "demo")
    rid_b = repo_id(client, "repo-b")
    r = client.post(f"/api/repositories/{rid_b}/runtime-sandbox", data=enable_form(profile="B", deps=[{"repo": "demo"}]), follow_redirects=False)
    assert r.status_code == 303, r.text
    r2 = client.post(f"/api/repositories/{rid_a}/runtime-sandbox", data=enable_form(deps=[{"repo": "repo-b"}]), follow_redirects=False)
    assert r2.status_code == 409
    assert "cycle" in r2.text.lower()


def test_production_profile_name_rejected(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = repo_id(client, "demo")
    r = client.post(f"/api/repositories/{rid}/runtime-sandbox", data=enable_form(profile="PRODUCTION"), follow_redirects=False)
    assert r.status_code == 409


# --------------------------------------------------------- no dual source
def test_runtime_dependency_does_not_create_a_builder_workspace(client, git_repo):
    root, repo = git_repo
    repo_b = root / "repo-b"
    repo_b.mkdir()
    run(repo_b, "git", "init", "-b", "main")
    run(repo_b, "git", "config", "user.email", "t@t.invalid")
    run(repo_b, "git", "config", "user.name", "T")
    (repo_b / "PROJECT.yaml").write_text("schema_version: 1\nproject: {code: repo-b}\nsource: {root: .}\n")
    run(repo_b, "git", "add", ".")
    run(repo_b, "git", "commit", "-m", "base")
    register(client, repo, "demo")
    register(client, repo_b, "repo-b")
    rid = repo_id(client, "demo")
    before_count = len(client.get("/api/workspaces").json())
    client.post(f"/api/repositories/{rid}/runtime-sandbox", data=enable_form(deps=[{"repo": "repo-b"}]), follow_redirects=False)
    after_count = len(client.get("/api/workspaces").json())
    assert before_count == after_count


def test_no_duplicate_db_source_of_truth_for_sandbox_profile(client, git_repo):
    """The `repositories` table itself must never gain a column
    duplicating sandbox_profile/contract semantics -- PROJECT.yaml stays
    the only place that value lives."""
    root, repo = git_repo
    register(client, repo, "demo")
    rid = repo_id(client, "demo")
    client.post(f"/api/repositories/{rid}/runtime-sandbox", data=enable_form(), follow_redirects=False)
    cols = [c["name"] for c in client.app.state.db.all("PRAGMA table_info(repositories)")]
    assert not any("sandbox" in c.lower() or "profile" in c.lower() for c in cols)


def test_active_agent_worktree_is_not_silently_modified(client, git_repo):
    """Section 20: the contract edit must operate against the shared
    repo checkout, never touch an already-created Builder Workspace's
    own isolated worktree."""
    root, repo = git_repo
    register(client, repo, "demo")
    rid = repo_id(client, "demo")
    r = client.post("/api/tasks/create", data={"title": "T", "repository_id": rid, "agent": "codex", "sandbox_profile": "NONE", "risk_profile": "LOW"}, follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    worktree_project_yaml = subprocess.run(["cat", f"{w['worktree_path']}/PROJECT.yaml"], capture_output=True, text=True).stdout
    client.post(f"/api/repositories/{rid}/runtime-sandbox", data=enable_form(), follow_redirects=False)
    worktree_project_yaml_after = subprocess.run(["cat", f"{w['worktree_path']}/PROJECT.yaml"], capture_output=True, text=True).stdout
    assert "sandbox:" not in worktree_project_yaml_after
    assert worktree_project_yaml == worktree_project_yaml_after


# ---------------------------------------------------------------- audit
def test_save_generates_audit_event(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = repo_id(client, "demo")
    client.post(f"/api/repositories/{rid}/runtime-sandbox", data=enable_form(), follow_redirects=False)
    events = client.app.state.db.all("SELECT * FROM workspace_events WHERE entity_type='repository' AND entity_id=? AND action='REPOSITORY_CONTRACT_UPDATED'", (rid,))
    assert len(events) == 1
    assert "before_sha=" in events[0]["details"] and "after_sha=" in events[0]["details"]


def test_page_refresh_reflects_project_yaml_state(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = repo_id(client, "demo")
    client.post(f"/api/repositories/{rid}/runtime-sandbox", data=enable_form(), follow_redirects=False)
    html1 = client.get(f"/repositories/{rid}/runtime").text
    html2 = client.get(f"/repositories/{rid}/runtime").text
    assert "QA_CENTER" in html1 and "QA_CENTER" in html2


# ------------------------------------------------------- task integration
def test_configured_repo_shows_create_sandbox_on_workspace_page(client, git_repo):
    root, repo = git_repo
    (repo / "compose.yml").write_text(NGINX_COMPOSE)
    run(repo, "git", "add", "compose.yml")
    run(repo, "git", "commit", "-m", "add compose")
    register(client, repo, "demo")
    rid = repo_id(client, "demo")
    r = client.post("/api/tasks/create", data={"title": "T", "repository_id": rid, "agent": "codex", "sandbox_profile": "NONE", "risk_profile": "LOW"}, follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    before_html = client.get(f"/workspaces/{w['id']}").text
    assert "Configure Sandbox" in before_html

    client.post(f"/api/repositories/{rid}/runtime-sandbox", data=enable_form(profile="DEMO_PROFILE"), follow_redirects=False)
    after_html = client.get(f"/workspaces/{w['id']}").text
    assert "NOT CONFIGURED" not in after_html or "Create Sandbox" in after_html


# --------------------------------------------------------------- real test config
@pytest.mark.skipif(subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode != 0, reason="docker daemon not reachable")
def test_test_configuration_pass_provisions_real_isolated_sandbox(client, git_repo):
    root, _ = git_repo
    repo = make_sandboxable_no_contract(root, "sbtest")
    register(client, repo, "demo")
    rid = repo_id(client, "demo")
    r = client.post(f"/api/repositories/{rid}/runtime-sandbox", data=enable_form(profile="DEMOSB", port="80", health="/"), follow_redirects=False)
    assert r.status_code == 303, r.text
    r2 = client.post(f"/api/repositories/{rid}/runtime-sandbox/test", follow_redirects=False)
    assert r2.status_code == 303
    sid = int(r2.headers["location"].rstrip("/").split("/")[-1])
    row = client.app.state.db.one("SELECT * FROM sandboxes WHERE id=?", (sid,))
    assert row["owner_type"] == "REPOSITORY_TEST"
    assert row["status"] in ("RUNNING", "STARTING", "PROVISIONING")
    client.post(f"/api/sandboxes/{sid}/cleanup")


def test_duplicate_test_configuration_reuses_existing(client, git_repo):
    root, _ = git_repo
    repo = make_sandboxable_no_contract(root, "sbtest")
    register(client, repo, "demo")
    rid = repo_id(client, "demo")
    client.post(f"/api/repositories/{rid}/runtime-sandbox", data=enable_form(profile="DEMOSB", port="80", health="/"), follow_redirects=False)
    r1 = client.post(f"/api/repositories/{rid}/runtime-sandbox/test", follow_redirects=False)
    sid1 = r1.headers["location"].rstrip("/").split("/")[-1]
    r2 = client.post(f"/api/repositories/{rid}/runtime-sandbox/test", follow_redirects=False)
    sid2 = r2.headers["location"].rstrip("/").split("/")[-1]
    assert sid1 == sid2
    client.post(f"/api/sandboxes/{sid1}/cleanup")
