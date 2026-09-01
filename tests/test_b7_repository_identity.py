"""B7.1 -- Repository Identity Durability
(docs/B7_WORKSPACE_REPOSITORY_IDENTITY.md).

Real git repos, real filesystem moves, real re-registration calls,
real restart-simulating create_app() calls against the same DB file --
the realistic identity test matrix this phase's own doc names,
scoped to what B7.1 actually changed: repository identity (workspace/
task identity via agent_workspaces.id/tasks.id was already durable and
is re-confirmed unaffected, not re-built)."""
from __future__ import annotations
import subprocess

from app.config import Settings
from app.services.git_workspace import GitWorkspaceService
from tests.conftest import build_client
from tests.test_b03_authz import auth_client, captured_logs, _bootstrap, _csrf, _create_org, _invite, _accept, two_org_fixture


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _make_repo(root, name):
    # Content, not the directory name, is what git hashes -- two
    # otherwise-identical repos (same file content, same commit
    # message, same committer identity/timestamp) legitimately produce
    # the SAME root commit SHA, correctly per this fingerprint's own
    # design. Each repo's own name goes into its content so genuinely
    # unrelated repos in this test file are never accidentally
    # content-identical.
    repo = root / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text(f"base: {name}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"base: {name}")
    return repo


def _register(client, repo, name):
    return client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"},
                        follow_redirects=False)


# ================================================================ 1/2/15: restart / reboot / repeated-migration idempotency
def test_repo_identity_survives_projectflow_restart(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    repo = _make_repo(root, "svc")
    settings = Settings(root, "127.0.0.1", 8765, tmp_path / "t.db", 30, configured_state_dir=tmp_path / "state")

    client1 = build_client(settings)
    _register(client1, repo, "svc")
    row1 = client1.app.state.db.one("SELECT * FROM repositories WHERE repo_path=?", (str(repo),))
    assert row1["git_fingerprint"], "expected a real fingerprint after registration"

    # Simulate a genuine ProjectFlow restart: a fresh create_app() call
    # against the SAME db file -- the startup backfill must be a no-op
    # (already fingerprinted) and the id/fingerprint must be identical.
    client2 = build_client(settings)
    row2 = client2.app.state.db.one("SELECT * FROM repositories WHERE repo_path=?", (str(repo),))
    assert row2["id"] == row1["id"]
    assert row2["git_fingerprint"] == row1["git_fingerprint"]

    # And again, a third time -- the backfill loop must be fully
    # idempotent under repeated restarts, not just "runs twice ok".
    client3 = build_client(settings)
    row3 = client3.app.state.db.one("SELECT * FROM repositories WHERE repo_path=?", (str(repo),))
    assert row3["git_fingerprint"] == row1["git_fingerprint"]


def test_startup_backfill_fingerprints_pre_b7_rows(tmp_path):
    """A repository row registered before B7 existed has git_fingerprint
    NULL -- the startup backfill must compute it on the next restart
    without any manual action, closing the 'migration of existing
    production-like records' requirement."""
    root = tmp_path / "root"
    root.mkdir()
    repo = _make_repo(root, "legacy")
    settings = Settings(root, "127.0.0.1", 8765, tmp_path / "t.db", 30, configured_state_dir=tmp_path / "state")
    client = build_client(settings)
    db = client.app.state.db
    # Simulate a pre-B7 row: inserted directly, fingerprint never computed.
    db.execute("INSERT INTO repositories(repo_name,repo_path,default_branch) VALUES(?,?,?)",
               ("legacy", str(repo), "main"))
    row_before = db.one("SELECT * FROM repositories WHERE repo_path=?", (str(repo),))
    assert row_before["git_fingerprint"] is None

    client2 = build_client(settings)  # restart -- backfill runs
    row_after = client2.app.state.db.one("SELECT * FROM repositories WHERE repo_path=?", (str(repo),))
    assert row_after["id"] == row_before["id"]
    assert row_after["git_fingerprint"], "backfill should have computed a real fingerprint"


# ================================================================ 3/4/16: rename/move rebinds, history stays attached
def test_renamed_repo_directory_rebinds_not_duplicates(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    repo = _make_repo(root, "svc")
    client = build_client(Settings(root, "127.0.0.1", 8765, tmp_path / "t.db", 30, configured_state_dir=tmp_path / "state"))
    db = client.app.state.db
    _register(client, repo, "svc")
    original = db.one("SELECT * FROM repositories WHERE repo_path=?", (str(repo),))

    # A real, orphaned dependent record -- must remain attached to the
    # SAME repositories.id after the rebind below.
    tid = db.execute("INSERT INTO tasks(slug,title,status,repo_scope_id) VALUES(?,?,?,?)",
                      ("t1", "Task before move", "BACKLOG", original["id"]))

    moved = root / "svc-renamed"
    repo.rename(moved)
    r = _register(client, moved, "svc")
    assert r.status_code == 303, r.text

    rows = db.all("SELECT * FROM repositories")
    assert len(rows) == 1, f"expected a rebind, not a new row: {rows}"
    rebound = rows[0]
    assert rebound["id"] == original["id"]
    assert rebound["repo_path"] == str(moved)
    assert rebound["git_fingerprint"] == original["git_fingerprint"]

    task = db.one("SELECT * FROM tasks WHERE id=?", (tid,))
    assert task["repo_scope_id"] == original["id"], "Task must still reference the SAME repository id after rebind"


def test_rebind_preserves_org_ownership(two_org_fixture):
    """Tenant/data ownership preservation: rebinding a repo whose
    organization_id was already set must not lose or change it."""
    f = two_org_fixture
    db = f["db"]
    row = db.one("SELECT * FROM repositories WHERE id=?", (f["rid_a"],))
    assert row["organization_id"] == f["org_a"]
    old_path = row["repo_path"]

    # two_org_fixture links repositories via a direct SQL insert
    # (_link_repo), predating any fingerprint -- simulate what the
    # startup backfill would compute on the next restart, so the
    # rebind below has the deterministic evidence it needs.
    fp = f["owner"].app.state.git.repo_fingerprint(old_path)
    db.execute("UPDATE repositories SET git_fingerprint=? WHERE id=?", (fp, f["rid_a"]))

    from pathlib import Path
    moved = Path(old_path).parent / (Path(old_path).name + "-moved")
    Path(old_path).rename(moved)

    csrf = _csrf(f["owner"], "/account")
    r = f["owner"].post("/api/repositories",
                         data={"repo_path": str(moved), "repo_name": "repo-a", "default_branch": "main", "csrf_token": csrf},
                         follow_redirects=False)
    assert r.status_code == 303, r.text
    rebound = db.one("SELECT * FROM repositories WHERE id=?", (f["rid_a"],))
    assert rebound["organization_id"] == f["org_a"], "org ownership must survive a rebind"
    assert rebound["repo_path"] == str(moved)


def test_rebind_clears_stale_github_owner_repo(tmp_path):
    """Git remote changes: github_owner_repo (B4.1) must not silently
    keep routing webhooks to a stale owner/repo after a rebind."""
    root = tmp_path / "root"
    root.mkdir()
    repo = _make_repo(root, "svc")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/svc.git")
    client = build_client(Settings(root, "127.0.0.1", 8765, tmp_path / "t.db", 30, configured_state_dir=tmp_path / "state"))
    db = client.app.state.db
    _register(client, repo, "svc")
    rid = db.one("SELECT id FROM repositories WHERE repo_path=?", (str(repo),))["id"]
    db.execute("UPDATE repositories SET github_owner_repo=? WHERE id=?", ("stale/owner-repo", rid))

    moved = root / "svc-moved"
    repo.rename(moved)
    _register(client, moved, "svc")
    row = db.one("SELECT github_owner_repo FROM repositories WHERE id=?", (rid,))
    assert row["github_owner_repo"] is None, "must be cleared so it recomputes fresh, not keep a stale value"


# ================================================================ 9/12: missing path detection + recovery
def test_missing_path_is_flagged_live_not_stored(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    repo = _make_repo(root, "svc")
    client = build_client(Settings(root, "127.0.0.1", 8765, tmp_path / "t.db", 30, configured_state_dir=tmp_path / "state"))
    _register(client, repo, "svc")

    r = client.get("/repositories")
    assert "Path not found" not in r.text

    import shutil
    shutil.rmtree(repo)
    r2 = client.get("/repositories")
    assert "Path not found" in r2.text

    # Recovery: the directory reappears (e.g. a restored backup at the
    # exact same path) -- the flag must clear on the very next read,
    # nothing to invalidate since it was never stored.
    repo2 = _make_repo(root, "svc")  # same path, real repo restored
    r3 = client.get("/repositories")
    assert "Path not found" not in r3.text


# ================================================================ 2/10: distinct repos never collide; a live duplicate is flagged, never merged
def test_two_unrelated_repos_never_fingerprint_collide(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    repo_a = _make_repo(root, "a")
    repo_b = _make_repo(root, "b")
    client = build_client(Settings(root, "127.0.0.1", 8765, tmp_path / "t.db", 30, configured_state_dir=tmp_path / "state"))
    db = client.app.state.db
    _register(client, repo_a, "a")
    _register(client, repo_b, "b")
    rows = db.all("SELECT git_fingerprint FROM repositories")
    assert len(rows) == 2
    assert rows[0]["git_fingerprint"] != rows[1]["git_fingerprint"]


def test_second_live_clone_registers_separately_and_is_flagged_not_merged(tmp_path):
    """A deliberate second clone of the SAME repo at a path that still
    exists must remain independently usable -- never silently merged --
    while still being surfaced as a possible duplicate."""
    root = tmp_path / "root"
    root.mkdir()
    original = _make_repo(root, "svc")
    clone = root / "svc-clone"
    _git(root, "clone", str(original), str(clone))
    client = build_client(Settings(root, "127.0.0.1", 8765, tmp_path / "t.db", 30, configured_state_dir=tmp_path / "state"))
    db = client.app.state.db
    _register(client, original, "svc")
    r = _register(client, clone, "svc-clone")
    assert r.status_code == 303, r.text

    rows = db.all("SELECT * FROM repositories ORDER BY id")
    assert len(rows) == 2, "both clones must remain independent rows, never merged"
    assert rows[0]["git_fingerprint"] == rows[1]["git_fingerprint"]

    page = client.get("/repositories")
    assert "Possible duplicate" in page.text


def test_duplicate_flag_respects_tenant_isolation(two_org_fixture):
    """Two tenants referencing similar repositories: org A must never
    learn that org B's repository shares its fingerprint."""
    f = two_org_fixture
    db = f["db"]
    row_a = db.one("SELECT * FROM repositories WHERE id=?", (f["rid_a"],))
    # Force a fingerprint collision directly (simulating two orgs each
    # registering their own clone of the same public template repo).
    db.execute("UPDATE repositories SET git_fingerprint=? WHERE id=?", ("shared-fp-123", f["rid_b"]))
    db.execute("UPDATE repositories SET git_fingerprint=? WHERE id=?", ("shared-fp-123", f["rid_a"]))

    page = f["member"].get("/repositories")  # member is Org A only
    assert page.status_code == 200, page.text
    assert "Possible duplicate" not in page.text, \
        "Org A must not see a duplicate flag pointing at Org B's own repository"


# ================================================================ 13/14: duplicate enrollment + ambiguous collision
def test_duplicate_enrollment_same_path_updates_in_place(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    repo = _make_repo(root, "svc")
    client = build_client(Settings(root, "127.0.0.1", 8765, tmp_path / "t.db", 30, configured_state_dir=tmp_path / "state"))
    db = client.app.state.db
    _register(client, repo, "svc")
    row1 = db.one("SELECT * FROM repositories WHERE repo_path=?", (str(repo),))
    _register(client, repo, "svc-renamed-display-name")
    row2 = db.one("SELECT * FROM repositories WHERE repo_path=?", (str(repo),))
    assert row2["id"] == row1["id"]
    assert db.one("SELECT COUNT(*) c FROM repositories")["c"] == 1


def test_ambiguous_multiple_missing_candidates_never_guesses(tmp_path):
    """Two DIFFERENT rows both share a fingerprint AND both their paths
    are currently missing (e.g. an old pre-B7 duplicate that then also
    moved) -- deterministic evidence is gone; must not guess which one
    to rebind. Falls through to a plain new registration instead."""
    root = tmp_path / "root"
    root.mkdir()
    repo = _make_repo(root, "svc")
    client = build_client(Settings(root, "127.0.0.1", 8765, tmp_path / "t.db", 30, configured_state_dir=tmp_path / "state"))
    db = client.app.state.db
    fp = client.app.state.git.repo_fingerprint(repo)
    # Two pre-existing rows, same fingerprint, both paths already gone.
    db.execute("INSERT INTO repositories(repo_name,repo_path,default_branch,git_fingerprint) VALUES(?,?,?,?)",
               ("ghost1", str(root / "ghost1"), "main", fp))
    db.execute("INSERT INTO repositories(repo_name,repo_path,default_branch,git_fingerprint) VALUES(?,?,?,?)",
               ("ghost2", str(root / "ghost2"), "main", fp))

    r = _register(client, repo, "svc")
    assert r.status_code == 303, r.text
    rows = db.all("SELECT * FROM repositories WHERE git_fingerprint=?", (fp,))
    assert len(rows) == 3, "ambiguous case must insert a new row, never silently pick one of the two ghosts"
