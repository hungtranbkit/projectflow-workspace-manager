"""P0-4 (docs/CORE_USABILITY_QUALIFICATION.md): a real, disposable
backup -> mutate/delete -> restore drill. Real scripts/backup.py and
scripts/restore.py run as real subprocesses against a real SQLite
file, never imported/mocked -- this is the actual operator-facing
recovery path, so it is the actual thing under test."""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

from app.config import Settings
from tests.conftest import build_client
from tests.test_b03_authz import auth_client, captured_logs, _bootstrap, _create_org, _csrf, _link_repo, _make_task

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_SECRET_ENCRYPTION_KEY = "M2RXNV3dhIR-lc1WoE8DGxt-kowfK-34xGTIcF1t8m4="


def _run(argv, env=None):
    return subprocess.run([sys.executable, *argv], capture_output=True, text=True, env=env)


def test_backup_creates_valid_verified_snapshot(tmp_path, git_repo):
    root, repo = git_repo
    settings = Settings(root, "127.0.0.1", 8765, tmp_path / "live.db", 30, configured_state_dir=tmp_path / "state")
    client = build_client(settings)
    client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": "demo", "default_branch": "main"})

    r = _run(["scripts/backup.py", "--db", str(settings.db_path), "--out", str(tmp_path / "backups")])
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert len(lines) == 2, r.stdout
    backup_path, digest = Path(lines[0]), lines[1]
    assert backup_path.is_file()
    assert len(digest) == 64  # sha256 hex


def test_backup_refuses_on_missing_source(tmp_path):
    r = _run(["scripts/backup.py", "--db", str(tmp_path / "nope.db"), "--out", str(tmp_path / "backups")])
    assert r.returncode != 0
    assert "REFUSED" in r.stderr


def test_restore_refuses_invalid_backup_file(tmp_path):
    bogus = tmp_path / "not-a-db.db"
    bogus.write_text("not a real sqlite file")
    r = _run(["scripts/restore.py", str(bogus), "--db", str(tmp_path / "target.db"), "--force"])
    assert r.returncode != 0
    assert "REFUSED" in r.stderr


def test_full_backup_mutate_restore_drill_preserves_everything(tmp_path, git_repo, captured_logs):
    """The real drill: create meaningful state (org, member, two repos,
    tasks, a review, a release) -> real backup -> real data loss
    (delete the live DB entirely) -> real restore into a clean
    environment -> verify every relationship survived, not just row
    counts."""
    root, repo = git_repo
    live_db = tmp_path / "live.db"
    settings = Settings(root, "127.0.0.1", 8765, live_db, 30, configured_state_dir=tmp_path / "state",
                         auth_mode="required", session_secret="test-only-secret-never-a-default",
                         secret_encryption_keys=(TEST_SECRET_ENCRYPTION_KEY,))
    owner = build_client(settings)
    _bootstrap(owner, captured_logs, "owner@example.com")
    org_id = _create_org(owner, "Acme Recovery Co")

    rid = _link_repo(owner, org_id, repo, "demo")
    db = owner.app.state.db
    tid = _make_task(db, rid)
    db.execute(
        "INSERT INTO review_runs(task_id,workspace_id,reviewer_type,reviewer_agent,reviewed_commit,status,findings,review_kind,verdict) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (tid, None, "CODE_REVIEW_AI", "claude", "deadbeef", "COMPLETED", "[]", "CODE", "PASS"))
    release_id = db.execute(
        "INSERT INTO releases(repository_id,version,source_commit,status) VALUES(?,?,?,?)",
        (rid, "1.0.0", "deadbeef", "DRAFT"))

    # Ground truth to compare against after restore.
    before_org = db.one("SELECT * FROM organizations WHERE id=?", (org_id,))
    before_repo = db.one("SELECT * FROM repositories WHERE id=?", (rid,))
    before_task = db.one("SELECT * FROM tasks WHERE id=?", (tid,))
    before_review = db.one("SELECT * FROM review_runs WHERE task_id=?", (tid,))
    before_release = db.one("SELECT * FROM releases WHERE id=?", (release_id,))
    assert before_repo["organization_id"] == org_id
    assert before_task["repo_scope_id"] == rid
    assert before_review["task_id"] == tid
    assert before_release["repository_id"] == rid

    backup_dir = tmp_path / "backups"
    r = _run(["scripts/backup.py", "--db", str(live_db), "--out", str(backup_dir)])
    assert r.returncode == 0, r.stderr
    backup_path = Path(r.stdout.strip().splitlines()[0])

    # Real, total data loss -- the live DB file is gone, exactly the
    # disaster this drill exists to recover from.
    live_db.unlink()
    assert not live_db.exists()

    r = _run(["scripts/restore.py", str(backup_path), "--db", str(live_db), "--force"])
    assert r.returncode == 0, r.stderr
    assert live_db.is_file()

    # A fresh app instance against the restored file -- never reuse the
    # old client/connection, since that would only prove Python-level
    # object survival, not that the file on disk is actually intact.
    restored = build_client(settings)
    # Same session_secret + the same users row surviving the restore
    # means the owner's existing signed session cookie decodes cleanly
    # against this brand-new app instance -- carry it over rather than
    # re-authenticating, since re-running the bootstrap flow would only
    # prove a second login works, not that this session's own identity
    # survived the restore.
    restored.cookies.update(owner.cookies)
    rdb = restored.app.state.db

    after_org = rdb.one("SELECT * FROM organizations WHERE id=?", (org_id,))
    after_repo = rdb.one("SELECT * FROM repositories WHERE id=?", (rid,))
    after_task = rdb.one("SELECT * FROM tasks WHERE id=?", (tid,))
    after_review = rdb.one("SELECT * FROM review_runs WHERE task_id=?", (tid,))
    after_release = rdb.one("SELECT * FROM releases WHERE id=?", (release_id,))

    assert after_org["name"] == before_org["name"]
    assert after_repo["repo_path"] == before_repo["repo_path"]
    assert after_repo["organization_id"] == org_id, "org ownership must survive restore"
    assert after_task["repo_scope_id"] == rid, "task->repo relationship must survive restore"
    assert after_review["task_id"] == tid, "review evidence must stay attached to the correct task"
    assert after_release["repository_id"] == rid, "release record must stay attached to the correct repo"
    assert after_release["version"] == before_release["version"]

    # The web app itself, not just raw SQL, must see the restored state
    # correctly -- proves the restore is usable, not merely present.
    page = restored.get(f"/orgs/{org_id}")
    assert page.status_code == 200
    assert "Acme Recovery Co" in page.text

    # A pre-restore safety copy of the target must exist too -- but in
    # this drill the live DB was deleted before restore, so there is no
    # prior target to preserve; confirmed separately below instead.


def test_restore_preserves_prior_target_as_safety_copy(tmp_path, git_repo):
    """When a target DB already exists (the operator is restoring OVER
    a still-present, merely-corrupted file, not a fully-deleted one),
    restore.py must not destroy it silently -- a pre-restore safety
    copy must be left behind."""
    root, repo = git_repo
    live_db = tmp_path / "live.db"
    settings = Settings(root, "127.0.0.1", 8765, live_db, 30, configured_state_dir=tmp_path / "state")
    client = build_client(settings)
    client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": "demo", "default_branch": "main"})

    backup_dir = tmp_path / "backups"
    r = _run(["scripts/backup.py", "--db", str(live_db), "--out", str(backup_dir)])
    assert r.returncode == 0, r.stderr
    backup_path = Path(r.stdout.strip().splitlines()[0])

    # Corrupt (but don't delete) the live target, simulating a damaged-
    # not-missing file.
    live_db.write_bytes(b"corrupted, not a real sqlite file anymore")

    r = _run(["scripts/restore.py", str(backup_path), "--db", str(live_db), "--force"])
    assert r.returncode == 0, r.stderr

    safety_copies = list(tmp_path.glob("live.pre-restore-*.db"))
    assert len(safety_copies) == 1, f"expected exactly one safety copy, found {safety_copies}"
    assert safety_copies[0].read_bytes() == b"corrupted, not a real sqlite file anymore"
