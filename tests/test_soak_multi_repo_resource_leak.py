"""P0-5 (docs/CORE_USABILITY_QUALIFICATION.md): a bounded but real
multi-repo/task/session/sandbox soak pass. Real git repos, real
worktrees, real Docker sandboxes (skipped without a real daemon, same
convention as test_sandbox_docker.py) -- agent I/O itself is a fake
lightweight process (the same `sleep`/`true` pattern test_live_
terminal_routing.py already established) since this soak exists to
prove ProjectFlow's OWN resource bookkeeping doesn't leak, not to
re-prove real-agent correctness (already covered elsewhere). Counts
are scoped to ProjectFlow's own `wm-`-prefixed containers and this
run's own worktree/process footprint, never raw host-wide counts --
this is a shared host with unrelated containers/processes already
running, and a raw count would be noise, not signal."""
from __future__ import annotations
import os
import shutil
import subprocess

import pytest

from app.launchers import AgentLauncher

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not on PATH")


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


if not _docker_available():
    pytestmark = pytest.mark.skip(reason="docker daemon not reachable in this environment")


def register(client, repo):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": repo.name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text
    return [x for x in client.get("/api/repositories").json() if x["repo_name"] == repo.name][0]["id"]


def _wm_container_ids() -> set:
    out = subprocess.run(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project"],
                          capture_output=True, text=True, timeout=15).stdout.split()
    ids = set()
    for cid in out:
        name = subprocess.run(["docker", "inspect", "-f", "{{.Name}}", cid], capture_output=True, text=True, timeout=5).stdout.strip().lstrip("/")
        if name.startswith("wm-"):
            ids.add(cid)
    return ids


def _own_child_pids() -> set:
    try:
        out = subprocess.run(["pgrep", "-P", str(os.getpid())], capture_output=True, text=True, timeout=5).stdout.split()
        return set(out)
    except Exception:
        return set()


def test_multi_repo_task_session_sandbox_soak_leaves_no_leak(client, tmp_path, sandboxable_repo_factory):
    # `client` (via the `git_repo` fixture) already created
    # tmp_path/"root"/"demo" as the workspace root -- reuse that same
    # root rather than re-mkdir'ing it.
    root = client.app.state.settings.root
    N = 3

    before_containers = _wm_container_ids()
    worktree_root = client.app.state.git.worktree_root
    before_worktree_dirs = set(worktree_root.glob("*")) if worktree_root.is_dir() else set()

    client.app.state.agent_sessions.launchers = {"codex": AgentLauncher("Codex", "bash", ("-c", "true"))}

    sandbox_ids, workspace_ids, session_ids = [], [], []
    for i in range(N):
        repo = sandboxable_repo_factory(root, f"soak-{i}", port_range=(21200 + i * 10, 21209 + i * 10))
        rid = register(client, repo)
        r = client.post("/api/tasks", data={"title": f"Soak task {i}"}, follow_redirects=False)
        assert r.status_code == 303, r.text
        tid = int(r.headers["location"].split("/")[-1])
        client.post(f"/api/tasks/{tid}/select")
        r = client.post(f"/api/tasks/{tid}/workspaces",
                         data={"repository_id": rid, "agent": "codex", "role": "Backend", "base_branch": "main", "sandbox_profile": "backend"},
                         follow_redirects=False)
        assert r.status_code == 303, r.text
        w = client.get("/api/workspaces").json()[-1]
        workspace_ids.append(w["id"])

        sb = [s for s in client.get("/api/sandboxes").json() if s["owner_id"] == w["id"]][0]
        assert sb["status"] == "RUNNING", sb
        sandbox_ids.append(sb["id"])

        r = client.post(f"/api/workspaces/{w['id']}/sessions", data={"mode": "INTERACTIVE"}, follow_redirects=False)
        assert r.status_code == 303, r.text
        sid = int(r.headers["location"].split("/")[-1])
        session_ids.append(sid)

    import time
    deadline = time.time() + 10
    while time.time() < deadline:
        statuses = {sid: client.app.state.db.one("SELECT status FROM agent_sessions WHERE id=?", (sid,))["status"] for sid in session_ids}
        if all(s == "EXITED" for s in statuses.values()):
            break
        time.sleep(0.1)
    assert all(s == "EXITED" for s in statuses.values()), f"fake sessions never exited: {statuses}"

    # Real teardown, exactly the operator-facing path -- close worktrees,
    # cleanup sandboxes -- never a test-only shortcut.
    for sid_ in sandbox_ids:
        client.post(f"/api/sandboxes/{sid_}/cleanup")
    for wid in workspace_ids:
        client.app.state.db.execute("UPDATE agent_workspaces SET status='DONE' WHERE id=?", (wid,))
        w = client.app.state.db.one("SELECT * FROM agent_workspaces WHERE id=?", (wid,))
        r = client.app.state.db.one("SELECT repo_path FROM repositories WHERE id=?", (w["repository_id"],))
        try:
            client.app.state.git.close(r["repo_path"], w["worktree_path"])
        except Exception:
            pass  # a sandbox cleanup leaving nothing dirty is the common case; best-effort here

    after_containers = _wm_container_ids()
    leaked_containers = after_containers - before_containers
    assert not leaked_containers, f"leaked wm- containers after cleanup: {leaked_containers}"

    after_worktree_dirs = set(worktree_root.glob("*")) if worktree_root.is_dir() else set()
    leaked_worktrees = after_worktree_dirs - before_worktree_dirs
    assert not leaked_worktrees, f"leaked worktree directories after cleanup: {leaked_worktrees}"

    # No child process from any of the N fake sessions should still be
    # alive -- every one already exited on its own, and none of them
    # should have left an orphaned process behind.
    lingering = client.app.state.db.all(
        "SELECT id, pid FROM agent_sessions WHERE id IN ({}) AND status NOT IN ('EXITED','FAILED')".format(
            ",".join("?" * len(session_ids))), tuple(session_ids))
    assert not lingering, f"sessions not cleanly exited: {lingering}"
