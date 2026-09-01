"""B4 -- GitHub PR/CI Webhook Event Ingestion
(docs/B4_GITHUB_WEBHOOK_STATUS_INGESTION.md, ADR-001's own "phase 2").

B4.1: github_owner_repo() parses real git remote URLs (both SSH and
HTTPS GitHub forms), a real local git operation, not a hand-typed
string.

B4.3: real HMAC-signed pull_request/check_run/status payloads (matching
GitHub's own documented shape) update merge_records' webhook snapshot
columns via B3's existing verified /webhooks/github route -- proven
against real merge_records rows, with the existing pr_number/head_sha
columns (E10's own migration 10) used for matching, never written by
B4. Idempotent redelivery, unresolvable-repo/PR safe no-ops, and the
existing 5 live pr_status() call sites / B3's installation tests
provably unaffected are all covered."""
from __future__ import annotations
import hashlib
import hmac
import json
import subprocess

import pytest

from tests.conftest import build_client
from app.config import Settings
from app.services.github_merge_service import GitHubMergeService


def _webhook_client(root, tmp_path, **overrides):
    """AUTH_MODE=none (the default) + a webhook secret configured --
    B4's own design point: the webhook route's own gate is `if not
    settings.github_webhook_secret`, independent of auth_mode entirely,
    so this is the right fixture shape for these tests (also lets
    /tasks/{tid} render without a login, unlike test_b03's own
    auth_client() which always forces AUTH_MODE=required)."""
    settings = Settings(root, "127.0.0.1", 8765, tmp_path / "test.db", 30, configured_state_dir=tmp_path / "state",
                         **overrides)
    return build_client(settings)


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _post(client, secret, event, payload):
    body = json.dumps(payload).encode("utf-8")
    return client.post("/webhooks/github", content=body, headers={
        "X-Hub-Signature-256": _sign(secret, body), "X-GitHub-Event": event, "Content-Type": "application/json"})


# ================================================================ B4.1: github_owner_repo() -- real git remote parsing
def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_github_owner_repo_parses_https_remote(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "remote", "add", "origin", "https://github.com/octocat/hello-world.git")
    svc = GitHubMergeService()
    assert svc.github_owner_repo(repo) == "octocat/hello-world"


def test_github_owner_repo_parses_ssh_remote(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "remote", "add", "origin", "git@github.com:octocat/hello-world.git")
    svc = GitHubMergeService()
    assert svc.github_owner_repo(repo) == "octocat/hello-world"


def test_github_owner_repo_none_for_non_github_remote(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "remote", "add", "origin", "https://gitlab.com/octocat/hello-world.git")
    svc = GitHubMergeService()
    assert svc.github_owner_repo(repo) is None


def test_github_owner_repo_none_when_no_remote(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    svc = GitHubMergeService()
    assert svc.github_owner_repo(repo) is None


# ================================================================ Fixture: a repo + task + merge_records row with a known PR
@pytest.fixture
def wired_merge_record(git_repo, tmp_path):
    root, repo = git_repo
    _git(repo, "remote", "add", "origin", "https://github.com/acme/widgets.git")
    client = _webhook_client(root, tmp_path, github_webhook_secret="whsec_b4test")
    db = client.app.state.db
    rid = db.execute("INSERT INTO repositories(repo_name,repo_path) VALUES(?,?)", ("widgets", str(repo)))
    tid = db.execute("INSERT INTO tasks(slug,title,status,repo_scope_id) VALUES(?,?,?,?)", ("t1", "T", "BACKLOG", rid))
    mrid = db.execute(
        "INSERT INTO merge_records(task_id,repository_id,required,merge_status,pr_number,head_sha) "
        "VALUES(?,?,1,'PR_OPEN',?,?)", (tid, rid, 42, "abc123def456"))
    return {"client": client, "db": db, "rid": rid, "tid": tid, "mrid": mrid}


# ================================================================ B4.3: pull_request event
def test_pull_request_webhook_updates_mergeability(wired_merge_record):
    f = wired_merge_record
    payload = {
        "action": "synchronize",
        "repository": {"full_name": "acme/widgets"},
        "pull_request": {"number": 42, "merged": False, "mergeable_state": "clean",
                          "head": {"sha": "abc123def456"}},
    }
    r = _post(f["client"], "whsec_b4test", "pull_request", payload)
    assert r.status_code == 200, r.text
    row = f["db"].one("SELECT * FROM merge_records WHERE id=?", (f["mrid"],))
    assert row["webhook_mergeability"] == "CLEAN"
    assert row["webhook_updated_at"] is not None
    # B4's own non-goal: the live-poll-owned columns are untouched.
    assert row["merge_status"] == "PR_OPEN"


def test_pull_request_webhook_merged_sets_mergeability_merged(wired_merge_record):
    f = wired_merge_record
    payload = {
        "action": "closed",
        "repository": {"full_name": "acme/widgets"},
        "pull_request": {"number": 42, "merged": True, "mergeable_state": "unknown",
                          "head": {"sha": "abc123def456"}},
    }
    r = _post(f["client"], "whsec_b4test", "pull_request", payload)
    assert r.status_code == 200, r.text
    row = f["db"].one("SELECT webhook_mergeability FROM merge_records WHERE id=?", (f["mrid"],))
    assert row["webhook_mergeability"] == "MERGED"


# ================================================================ B4.3: check_run event
def test_check_run_webhook_updates_ci_status_via_pr_number(wired_merge_record):
    f = wired_merge_record
    payload = {
        "action": "completed",
        "repository": {"full_name": "acme/widgets"},
        "check_run": {"conclusion": "success", "head_sha": "abc123def456", "pull_requests": [{"number": 42}]},
    }
    r = _post(f["client"], "whsec_b4test", "check_run", payload)
    assert r.status_code == 200, r.text
    row = f["db"].one("SELECT webhook_ci_status FROM merge_records WHERE id=?", (f["mrid"],))
    assert row["webhook_ci_status"] == "PASS"


def test_check_run_webhook_failure_conclusion(wired_merge_record):
    f = wired_merge_record
    payload = {
        "action": "completed",
        "repository": {"full_name": "acme/widgets"},
        "check_run": {"conclusion": "failure", "head_sha": "abc123def456", "pull_requests": [{"number": 42}]},
    }
    r = _post(f["client"], "whsec_b4test", "check_run", payload)
    assert r.status_code == 200, r.text
    row = f["db"].one("SELECT webhook_ci_status FROM merge_records WHERE id=?", (f["mrid"],))
    assert row["webhook_ci_status"] == "FAIL"


def test_check_run_webhook_falls_back_to_head_sha_when_no_pr_list(wired_merge_record):
    """A real check_run payload sometimes carries an empty
    pull_requests[] (GitHub doesn't always populate it, e.g. for a
    fork) -- head_sha matching is the real fallback."""
    f = wired_merge_record
    payload = {
        "action": "completed",
        "repository": {"full_name": "acme/widgets"},
        "check_run": {"conclusion": "success", "head_sha": "abc123def456", "pull_requests": []},
    }
    r = _post(f["client"], "whsec_b4test", "check_run", payload)
    assert r.status_code == 200, r.text
    row = f["db"].one("SELECT webhook_ci_status FROM merge_records WHERE id=?", (f["mrid"],))
    assert row["webhook_ci_status"] == "PASS"


# ================================================================ B4.3: status event
def test_status_webhook_updates_ci_status_via_head_sha(wired_merge_record):
    f = wired_merge_record
    payload = {"state": "success", "sha": "abc123def456", "repository": {"full_name": "acme/widgets"}}
    r = _post(f["client"], "whsec_b4test", "status", payload)
    assert r.status_code == 200, r.text
    row = f["db"].one("SELECT webhook_ci_status FROM merge_records WHERE id=?", (f["mrid"],))
    assert row["webhook_ci_status"] == "PASS"


def test_status_webhook_failure_state(wired_merge_record):
    f = wired_merge_record
    payload = {"state": "failure", "sha": "abc123def456", "repository": {"full_name": "acme/widgets"}}
    r = _post(f["client"], "whsec_b4test", "status", payload)
    assert r.status_code == 200, r.text
    row = f["db"].one("SELECT webhook_ci_status FROM merge_records WHERE id=?", (f["mrid"],))
    assert row["webhook_ci_status"] == "FAIL"


# ================================================================ Safe no-ops (acceptance criterion 2)
def test_unresolvable_repository_is_safe_noop(wired_merge_record):
    f = wired_merge_record
    payload = {"state": "success", "sha": "abc123def456", "repository": {"full_name": "someone-else/unrelated"}}
    r = _post(f["client"], "whsec_b4test", "status", payload)
    assert r.status_code == 200, r.text
    row = f["db"].one("SELECT webhook_ci_status FROM merge_records WHERE id=?", (f["mrid"],))
    assert row["webhook_ci_status"] is None


def test_unresolvable_pr_number_is_safe_noop(wired_merge_record):
    f = wired_merge_record
    payload = {
        "action": "synchronize",
        "repository": {"full_name": "acme/widgets"},
        "pull_request": {"number": 9999, "merged": False, "mergeable_state": "clean", "head": {"sha": "zzz"}},
    }
    r = _post(f["client"], "whsec_b4test", "pull_request", payload)
    assert r.status_code == 200, r.text
    row = f["db"].one("SELECT webhook_mergeability FROM merge_records WHERE id=?", (f["mrid"],))
    assert row["webhook_mergeability"] is None


# ================================================================ Idempotent redelivery (acceptance criterion 2b)
def test_redelivery_is_idempotent(wired_merge_record):
    f = wired_merge_record
    payload = {
        "action": "completed",
        "repository": {"full_name": "acme/widgets"},
        "check_run": {"conclusion": "success", "head_sha": "abc123def456", "pull_requests": [{"number": 42}]},
    }
    r1 = _post(f["client"], "whsec_b4test", "check_run", payload)
    assert r1.status_code == 200
    row1 = f["db"].one("SELECT webhook_ci_status FROM merge_records WHERE id=?", (f["mrid"],))
    r2 = _post(f["client"], "whsec_b4test", "check_run", payload)
    assert r2.status_code == 200
    row2 = f["db"].one("SELECT webhook_ci_status FROM merge_records WHERE id=?", (f["mrid"],))
    assert row1["webhook_ci_status"] == row2["webhook_ci_status"] == "PASS"
    # No duplicate row was ever created -- still exactly one merge_records
    # row for this (task_id, repository_id) pair.
    count = f["db"].one("SELECT COUNT(*) c FROM merge_records WHERE task_id=? AND repository_id=?",
                         (f["tid"], f["rid"]))["c"]
    assert count == 1


# ================================================================ B4.4: read-only surface
def test_task_detail_shows_webhook_snapshot_when_present(wired_merge_record):
    f = wired_merge_record
    f["db"].execute(
        "UPDATE merge_records SET webhook_ci_status='PASS',webhook_mergeability='CLEAN',"
        "webhook_updated_at=CURRENT_TIMESTAMP WHERE id=?", (f["mrid"],))
    r = f["client"].get(f"/tasks/{f['tid']}")
    assert r.status_code == 200, r.text
    assert "GitHub webhook last reported" in r.text
    assert "CLEAN" in r.text


def test_task_detail_shows_nothing_extra_when_no_webhook_traffic(wired_merge_record):
    f = wired_merge_record
    r = f["client"].get(f"/tasks/{f['tid']}")
    assert r.status_code == 200, r.text
    assert "GitHub webhook last reported" not in r.text
