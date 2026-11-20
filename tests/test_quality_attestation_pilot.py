"""Opt-in QA proof is mandatory at real merge boundaries, but never for unrelated tasks."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from app.services.quality_attestation_pilot import (
    PILOT_POLICY_TASK, PILOT_TEST, payload_from_machine_report,
    quality_merge_blocker, sign_receipt, verified_receipt,
)
from app.services.task_decision_service import TaskDecisionService
from app.services.integration_service import IntegrationService

KEY = "test-secret-of-at-least-32-bytes-for-qa-merge-check"
SHA = "a" * 40
NEXT_SHA = "b" * 40
DIGEST = "f" * 64
NOW = datetime(2026, 11, 20, 13, 0, tzinfo=timezone.utc)
TASK, REPO = 17, 22
NAME = "projectflow-workspace-manager"


def machine_report():
    return {
        "status": "PASS", "test_exit_code": 0, "scope_dirty": False,
        "lint_violations": [], "git_head": SHA,
        "test_command": ["python", "-m", "pytest", "-q", PILOT_TEST],
        "code_sha256": {"scripts/harness_monitor_report.py": DIGEST},
        "test_sha256": {PILOT_TEST: DIGEST},
        "policy_sha256": DIGEST,
    }


def receipt():
    return sign_receipt(
        payload_from_machine_report(machine_report(), task_id=TASK,
                                    repository_id=REPO, repository_name=NAME, now=NOW), KEY)


def env(tmp_path):
    return {
        "PROJECTFLOW_QUALITY_PILOT_TASK_IDS": "17",
        "PROJECTFLOW_QUALITY_PILOT_REPO": NAME,
        "PROJECTFLOW_QUALITY_ATTESTATION_KEY": KEY,
        "PROJECTFLOW_QUALITY_ATTESTATION_DIR": str(tmp_path),
    }


def persist(tmp_path, signed):
    (tmp_path / f"task-{TASK}-repo-{REPO}.json").write_text(json.dumps(signed), encoding="utf-8")


def test_disabled_by_default_does_not_require_receipt(tmp_path):
    assert quality_merge_blocker(TASK, REPO, NAME, SHA, environ={}, now=NOW) is None
    assert quality_merge_blocker(TASK, REPO, NAME, SHA,
                                 environ={"PROJECTFLOW_QUALITY_PILOT_TASK_IDS": "12",
                                          "PROJECTFLOW_QUALITY_PILOT_REPO": NAME}, now=NOW) is None


def test_valid_machine_signed_receipt_allows_exact_sha(tmp_path):
    persist(tmp_path, receipt())
    assert quality_merge_blocker(TASK, REPO, NAME, SHA, environ=env(tmp_path), now=NOW) is None


def test_missing_and_invalid_receipts_block_merge(tmp_path):
    settings = env(tmp_path)
    assert quality_merge_blocker(TASK, REPO, NAME, SHA, environ=settings, now=NOW) == "QUALITY_ATTESTATION_MISSING"
    signed = receipt()
    signed["payload"]["status"] = "PASS_WITHOUT_TESTS"
    persist(tmp_path, signed)
    assert quality_merge_blocker(TASK, REPO, NAME, SHA, environ=settings, now=NOW) == "QUALITY_ATTESTATION_INVALID"


def test_wrong_task_repo_sha_and_old_receipt_fail(tmp_path):
    signed = receipt()
    assert verified_receipt(signed, KEY, task_id=TASK+1, repository_id=REPO,
                            repository_name=NAME, expected_sha=SHA, now=NOW) == "QUALITY_ATTESTATION_STALE"
    assert verified_receipt(signed, KEY, task_id=TASK, repository_id=REPO+1,
                            repository_name=NAME, expected_sha=SHA, now=NOW) == "QUALITY_ATTESTATION_STALE"
    assert verified_receipt(signed, KEY, task_id=TASK, repository_id=REPO,
                            repository_name=NAME, expected_sha=NEXT_SHA, now=NOW) == "QUALITY_ATTESTATION_STALE"
    assert verified_receipt(signed, KEY, task_id=TASK, repository_id=REPO,
                            repository_name=NAME, expected_sha=SHA,
                            now=NOW+timedelta(hours=25)) == "QUALITY_ATTESTATION_STALE"


def test_signed_failing_report_cannot_be_accepted_or_signed():
    report = machine_report()
    report["test_exit_code"] = 1
    with pytest.raises(ValueError, match="Cannot sign"):
        payload_from_machine_report(report, task_id=TASK, repository_id=REPO, repository_name=NAME)
    signed = receipt()
    signed["payload"]["status"] = "FAIL"
    signed = sign_receipt(signed["payload"], KEY)
    assert verified_receipt(signed, KEY, task_id=TASK, repository_id=REPO,
                            repository_name=NAME, expected_sha=SHA, now=NOW) == "QUALITY_ATTESTATION_INVALID"


def test_missing_secret_and_invalid_configuration_fail_closed(tmp_path):
    e = env(tmp_path)
    del e["PROJECTFLOW_QUALITY_ATTESTATION_KEY"]
    assert quality_merge_blocker(TASK, REPO, NAME, SHA, environ=e, now=NOW) == "QUALITY_ATTESTATION_CONFIG"
    e = env(tmp_path)
    e["PROJECTFLOW_QUALITY_PILOT_TASK_IDS"] = "17,invalid"
    assert quality_merge_blocker(TASK, REPO, NAME, SHA, environ=e, now=NOW) == "QUALITY_ATTESTATION_CONFIG"


def test_symlink_or_large_receipt_not_accepted(tmp_path):
    dest = tmp_path / f"task-{TASK}-repo-{REPO}.json"
    target = tmp_path / "untrusted.json"
    target.write_text(json.dumps(receipt()))
    dest.symlink_to(target)
    assert quality_merge_blocker(TASK, REPO, NAME, SHA, environ=env(tmp_path), now=NOW) == "QUALITY_ATTESTATION_MISSING"
    dest.unlink()
    dest.write_text("x" * 70000)
    assert quality_merge_blocker(TASK, REPO, NAME, SHA, environ=env(tmp_path), now=NOW) == "QUALITY_ATTESTATION_MISSING"


class FakeDb:
    def one(self, sql, params):
        assert "FROM repositories" in sql
        return {"repo_name": NAME}


def decision_input():
    return {
        "task": {"id": TASK}, "risk_profile": "LOW", "ready_for_main": True,
        "builders": [{"repository_id": REPO, "branch": "feature", "head": SHA,
                      "review_status": "PASS"}],
        "integration_repos": [], "qa": None,
    }


def merge_record():
    return {
        "merge_status": "PR_OPEN", "pr_number": 12, "pr_state": "OPEN",
        "ci_status": "PASS", "mergeability": "MERGEABLE", "merge_state_status": "CLEAN",
        "verified_commit": SHA, "head_sha": SHA,
    }


def test_central_merge_gate_blocks_until_signed_receipt(tmp_path, monkeypatch):
    for k, v in env(tmp_path).items():
        monkeypatch.setenv(k, v)
    decision = TaskDecisionService(FakeDb(), None)
    result = decision.merge_gate_status(decision_input(), REPO, merge_record())
    assert result["eligible"] is False
    assert "QUALITY_ATTESTATION_MISSING" in result["blockers"]


def test_central_merge_gate_accepts_recent_proof(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    payload = payload_from_machine_report(machine_report(), task_id=TASK,
                                          repository_id=REPO, repository_name=NAME, now=now)
    persist(tmp_path, sign_receipt(payload, KEY))
    for k, v in env(tmp_path).items():
        monkeypatch.setenv(k, v)
    decision = TaskDecisionService(FakeDb(), None)
    result = decision.merge_gate_status(decision_input(), REPO, merge_record())
    assert result["eligible"] is True, result["blockers"]


def test_local_integration_is_blocked_too(tmp_path, monkeypatch):
    for k, v in env(tmp_path).items():
        monkeypatch.setenv(k, v)
    class FakeOrchestrator:
        def integration_readiness(self, tid):
            return {"ready": True, "governing_task_id": tid}
    class FakeWorktree:
        def get_task_worktree(self, tid):
            return {"repository_id": REPO, "worktree_path": "/safe/test/worktree"}
    class FakeGit:
        def head(self, path):
            return SHA
    service = IntegrationService(FakeDb(), None, FakeWorktree(),
                                 FakeOrchestrator(), FakeGit())
    result = service.integrate_task(TASK)
    assert result["outcome"] == "BLOCKED"
    assert result["reasons"] == ["QUALITY_ATTESTATION_MISSING"]
