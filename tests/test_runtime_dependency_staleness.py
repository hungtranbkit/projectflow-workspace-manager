"""Follow-up to the QA Center sandbox contract fix (found while verifying
the real regression live): a RUNTIME_DEPENDENCY source is deliberately
pinned to a known-good snapshot (mode: KNOWN_GOOD_MAIN), not meant to
track its own repo's live branch tip -- it must never make an otherwise
fresh, healthy sandbox show SOURCE STALE just because someone pushed an
unrelated commit to the dependency's main branch."""
from __future__ import annotations


def test_runtime_dependency_commit_move_does_not_mark_sandbox_stale(client, git_repo):
    root, repo = git_repo
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": "demo", "default_branch": "main"})
    assert r.status_code in (200, 303)
    rid = client.get("/api/repositories").json()[0]["id"]
    db = client.app.state.db
    sandboxes = client.app.state.sandboxes

    sid = db.execute(
        "INSERT INTO sandboxes(repository_id,owner_type,owner_id,sandbox_slug,profile,compose_project,status) VALUES(?,?,?,?,?,?,?)",
        (rid, "REPOSITORY_TEST", rid, "staleness-test-slug", "BACKEND", "wm-staleness-test-proj", "RUNNING"),
    )
    db.execute(
        "INSERT INTO sandbox_sources(sandbox_id,repository_id,role,branch,commit_sha,worktree_path,source_type) VALUES(?,?,?,?,?,?,?)",
        (sid, rid, "demo", "main", "a" * 40, str(repo), "AGENT_WORKSPACE"),
    )
    db.execute(
        "INSERT INTO sandbox_sources(sandbox_id,repository_id,role,branch,commit_sha,worktree_path,source_type) VALUES(?,?,?,?,?,?,?)",
        (sid, rid, "dep-repo", "main", "b" * 40, str(repo), "RUNTIME_DEPENDENCY"),
    )

    # Both sources happen to key off the same repository_id here (a repo
    # can legitimately be both a Task's own source and, in a different
    # sandbox, someone else's dependency) -- the primary source is still
    # pinned at "a"*40, so this repo's current HEAD moving to "a"*40
    # itself (no real movement for the PRIMARY source) must stay fresh;
    # is_stale() only ever sees the AGENT_WORKSPACE row's own pin here,
    # never the RUNTIME_DEPENDENCY row's "b"*40.
    assert sandboxes.is_stale(sid, {rid: "a" * 40}) is False

    # The dependency repo's "current HEAD" moving away from what the
    # RUNTIME_DEPENDENCY row pinned must NOT count as stale (isolate by
    # removing the AGENT_WORKSPACE row so only the dependency remains).
    db.execute("DELETE FROM sandbox_sources WHERE sandbox_id=? AND source_type='AGENT_WORKSPACE'", (sid,))
    assert sandboxes.is_stale(sid, {rid: "c" * 40}) is False

    # But a real AGENT_WORKSPACE/primary source moving must still be
    # caught -- this fix must not blind staleness entirely.
    db.execute(
        "INSERT INTO sandbox_sources(sandbox_id,repository_id,role,branch,commit_sha,worktree_path,source_type) VALUES(?,?,?,?,?,?,?)",
        (sid, rid, "demo", "main", "a" * 40, str(repo), "AGENT_WORKSPACE"),
    )
    db.execute("DELETE FROM sandbox_sources WHERE sandbox_id=? AND source_type='RUNTIME_DEPENDENCY'", (sid,))
    assert sandboxes.is_stale(sid, {rid: "c" * 40}) is True
