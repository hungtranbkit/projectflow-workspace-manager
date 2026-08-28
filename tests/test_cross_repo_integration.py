"""The mandatory cross-repo fixture proof (docs section 62): two
independent, disposable git repos (backend + client). Both source
branches stay UNMERGED to their own `main` for the entire test. A Task
Integration builds ONE fresh Integration Sandbox from the backend's own
unmerged integration branch at its exact commit; the "client" consumes
that sandbox's generated backend_url directly -- never `main`, never a
guessed URL. This is the single most load-bearing behavior this whole
phase exists to prove."""
from __future__ import annotations
import shutil
import subprocess
import urllib.request

import pytest

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not on PATH")


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


if not _docker_available():
    pytestmark = pytest.mark.skip(reason="docker daemon not reachable in this environment")


def run(path, *args):
    return subprocess.run(list(args), cwd=path, text=True, capture_output=True, check=True)


def head(path, ref="HEAD"):
    return run(path, "git", "rev-parse", ref).stdout.strip()


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


def test_backend_and_client_test_together_without_either_merging_to_main(client, git_repo, sandboxable_repo_factory, tmp_path):
    root, _ = git_repo

    # --- backend repo: real sandbox: contract, a feature on its own unmerged branch
    backend = sandboxable_repo_factory(root, "backend-repo", port_range=(21300, 21349))
    backend_main_before = head(backend)

    # --- client repo: no sandbox: contract of its own -- it only ever
    # CONSUMES the backend's sandbox output. Independent, disposable repo.
    client_repo = root / "client-repo"
    client_repo.mkdir()
    run(client_repo, "git", "init", "-b", "main")
    run(client_repo, "git", "config", "user.email", "t@t"); run(client_repo, "git", "config", "user.name", "t")
    (client_repo / "PROJECT.yaml").write_text(
        "schema_version: 1\nproject: {code: client-repo}\nsource: {root: .}\n"
        "commands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n"
    )
    (client_repo / "README.md").write_text("client base\n")
    run(client_repo, "git", "add", "."); run(client_repo, "git", "commit", "-m", "base")
    client_main_before = head(client_repo)

    register(client, backend, "backend-repo")
    register(client, client_repo, "client-repo")
    repos = {r["repo_name"]: r["id"] for r in client.get("/api/repositories").json()}

    client.post("/api/tasks", data={"title": "Cross-repo kiosk fixture"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]

    # backend agent workspace: real feature commit on an UNMERGED branch
    r = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": repos["backend-repo"], "agent": "codex", "role": "Backend", "base_branch": "main", "sandbox_profile": "backend"}, follow_redirects=False)
    assert r.status_code == 303
    backend_ws = client.get("/api/workspaces").json()[-1]
    (client.app.state.git.validate_worktree(backend_ws["worktree_path"]) / "feature.txt").write_text("backend feature\n")
    run(backend_ws["worktree_path"], "git", "add", ".")
    run(backend_ws["worktree_path"], "git", "commit", "-m", "backend feature")
    backend_feature_commit = head(backend_ws["worktree_path"])

    # client agent workspace: also a real commit on an UNMERGED branch
    r = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": repos["client-repo"], "agent": "claude", "role": "Client", "base_branch": "main", "sandbox_profile": ""}, follow_redirects=False)
    assert r.status_code == 303
    client_ws = [w for w in client.get("/api/workspaces").json() if w["repository_id"] == repos["client-repo"]][0]
    (client.app.state.git.validate_worktree(client_ws["worktree_path"]) / "client_feature.txt").write_text("client feature\n")
    run(client_ws["worktree_path"], "git", "add", ".")
    run(client_ws["worktree_path"], "git", "commit", "-m", "client feature")
    client_feature_commit = head(client_ws["worktree_path"])

    # neither source branch has been merged anywhere yet -- confirm main is untouched
    assert head(backend, "main") == backend_main_before
    assert head(client_repo, "main") == client_main_before

    for w in (backend_ws, client_ws):
        assert client.post(f"/api/workspaces/{w['id']}/ready", follow_redirects=False).status_code == 303

    sandbox_id = None
    try:
        r = client.post(f"/api/tasks/{tid}/integrations", follow_redirects=False)
        assert r.status_code == 303

        # main in BOTH repos is still completely untouched by the integration
        assert head(backend, "main") == backend_main_before
        assert head(client_repo, "main") == client_main_before

        task_integration = client.app.state.db.one("SELECT * FROM task_integrations WHERE task_id=?", (tid,))
        assert task_integration["status"] in ("TESTING", "CONFLICT")
        assert task_integration["status"] == "TESTING", "unexpected conflict merging two independent single-source branches"

        sandboxes = client.get("/api/sandboxes").json()
        integration_sandboxes = [s for s in sandboxes if s["owner_type"] == "TASK_INTEGRATION"]
        assert len(integration_sandboxes) == 1, "Task Integration must create exactly ONE fresh sandbox, not reuse an agent sandbox"
        sb = integration_sandboxes[0]
        sandbox_id = sb["id"]
        assert sb["status"] == "RUNNING", sb
        assert sb["health_status"] == "HEALTHY"

        # SandboxSource pins the EXACT commits -- both the backend feature
        # commit and the client feature commit, not "latest" or a branch name.
        sources = client.app.state.db.all("SELECT * FROM sandbox_sources WHERE sandbox_id=?", (sandbox_id,))
        commits = {s["repository_id"]: s["commit_sha"] for s in sources}
        assert commits[repos["backend-repo"]] == backend_feature_commit
        assert commits[repos["client-repo"]] == client_feature_commit

        # --- THE proof: the client "tests" directly against the backend's
        # unmerged-branch sandbox runtime -- no merge to main was needed.
        outputs = client.app.state.sandboxes.outputs(sandbox_id)
        assert "backend_url" in outputs
        with urllib.request.urlopen(outputs["backend_url"], timeout=5) as resp:
            assert resp.status == 200

        # --- staleness: the backend source branch moves AFTER the sandbox
        # was built -> the sandbox must be recognized stale.
        (client.app.state.git.validate_worktree(backend_ws["worktree_path"]) / "feature2.txt").write_text("more\n")
        run(backend_ws["worktree_path"], "git", "add", ".")
        run(backend_ws["worktree_path"], "git", "commit", "-m", "backend feature v2")
        new_backend_commit = head(backend_ws["worktree_path"])
        assert new_backend_commit != backend_feature_commit
        assert client.app.state.sandboxes.is_stale(sandbox_id, {repos["backend-repo"]: new_backend_commit}) is True
        assert client.app.state.sandboxes.is_stale(sandbox_id, {repos["backend-repo"]: backend_feature_commit}) is False

        # main in both repos STILL untouched, even after the extra commit
        assert head(backend, "main") == backend_main_before
        assert head(client_repo, "main") == client_main_before
    finally:
        if sandbox_id:
            client.post(f"/api/sandboxes/{sandbox_id}/cleanup")
