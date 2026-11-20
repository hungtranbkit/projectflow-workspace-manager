"""HTTP merge acceptance with fake external GitHub: no real merge, deploy or API writes."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.services.quality_attestation_pilot import payload_from_machine_report, sign_receipt
from tests.test_real_merge import FakeGh, ready_low_risk_task

KEY = "test-only-signer-key-with-at-least-32-characters"
DIGEST = "f" * 64


@pytest.fixture
def gated_pr(client, git_repo, tmp_path, monkeypatch):
    fake = FakeGh()
    client.app.state.github_merge.runner = fake
    tid, rid, _ = ready_low_risk_task(client, git_repo, "QA-gated local-only PR")
    created = client.post(
        f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    assert created.status_code == 303, created.text
    fake.prs[1]["statusCheckRollup"] = [
        {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "local-test-ci"}]
    row = client.app.state.db.one(
        "SELECT verified_commit FROM merge_records WHERE task_id=? AND repository_id=?",
        (tid, rid))
    commit = row["verified_commit"]
    assert commit and len(commit) == 40
    monkeypatch.setenv("PROJECTFLOW_QUALITY_PILOT_TASK_IDS", str(tid))
    monkeypatch.setenv("PROJECTFLOW_QUALITY_PILOT_REPO", "demo")
    monkeypatch.setenv("PROJECTFLOW_QUALITY_ATTESTATION_KEY", KEY)
    monkeypatch.setenv("PROJECTFLOW_QUALITY_ATTESTATION_DIR", str(tmp_path))
    return client, fake, tid, rid, commit, tmp_path


def _write_receipt(tid, rid, commit, folder):
    report = {
        "status": "PASS", "test_exit_code": 0, "scope_dirty": False,
        "lint_violations": [], "git_head": commit,
        "test_command": ["python", "-m", "pytest", "-q", "tests/test_harness_monitor_report.py"],
        "code_sha256": {"scripts/harness_monitor_report.py": DIGEST},
        "test_sha256": {"tests/test_harness_monitor_report.py": DIGEST},
        "policy_sha256": DIGEST,
    }
    payload = payload_from_machine_report(
        report, task_id=tid, repository_id=rid,
        repository_name="demo", now=datetime.now(timezone.utc))
    signed = sign_receipt(payload, KEY)
    (folder / f"task-{tid}-repo-{rid}.json").write_text(json.dumps(signed))


def test_http_merge_requires_receipt_and_does_not_mutate_merge_status(gated_pr):
    client, fake, tid, rid, sha, _ = gated_pr
    result = client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)
    assert result.status_code == 409
    assert fake.prs[1]["state"] == "OPEN"
    row = client.app.state.db.one(
        "SELECT merge_status FROM merge_records WHERE task_id=? AND repository_id=?",
        (tid, rid))
    assert row["merge_status"] != "MERGED"
    assert "QUALITY_ATTESTATION_MISSING" in result.text


def test_http_merge_accepts_recent_signed_receipt(gated_pr):
    client, fake, tid, rid, sha, folder = gated_pr
    _write_receipt(tid, rid, sha, folder)
    result = client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)
    assert result.status_code == 303, result.text
    assert fake.prs[1]["state"] == "MERGED"
    row = client.app.state.db.one(
        "SELECT merge_status FROM merge_records WHERE task_id=? AND repository_id=?",
        (tid, rid))
    assert row["merge_status"] == "MERGED"


def test_http_merge_rejects_receipt_for_other_commit(gated_pr):
    client, fake, tid, rid, sha, folder = gated_pr
    _write_receipt(tid, rid, "a" * 40 if sha != "a" * 40 else "b" * 40, folder)
    result = client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)
    assert result.status_code == 409, result.text
    assert fake.prs[1]["state"] == "OPEN"
    assert "QUALITY_ATTESTATION_STALE" in result.text
