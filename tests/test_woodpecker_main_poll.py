"""Unit tests: ProjectFlow Woodpecker poller never fabricates PASS or builds PRs."""
import json
import time

import pytest

from scripts import woodpecker_main_poll as poll


SHA = "a" * 40
OTHER = "b" * 40


def test_new_sha_runs():
    assert poll.should_run(SHA, {"sha": OTHER, "status": "PASS", "attempts": 9}, 1000) == (True, "NEW_SHA", 1)


def test_passed_sha_skips():
    assert poll.should_run(SHA, {"sha": SHA, "status": "PASS", "attempts": 1}, 1000) == (False, "ALREADY_PASSED", 1)


def test_failed_sha_enforces_cooldown_and_limit():
    state = {"sha": SHA, "status": "FAIL", "attempts": 2, "last_attempt_epoch": 900}
    assert poll.should_run(SHA, state, 1000)[1] == "RETRY_COOLDOWN"
    assert poll.should_run(SHA, state, 900 + poll.RETRY_SECONDS + 1) == (True, "RETRY", 3)
    state["attempts"] = 3
    assert poll.should_run(SHA, state, 999999)[1] == "RETRY_LIMIT_REACHED"


def test_remote_sha_requires_exact_branch(monkeypatch):
    monkeypatch.setattr(poll, "run_simple", lambda *a, **kw: SHA + "\trefs/heads/main")
    assert poll.remote_sha() == SHA
    monkeypatch.setattr(poll, "run_simple", lambda *a, **kw: SHA + "\trefs/heads/evil")
    with pytest.raises(ValueError, match="INVALID_REMOTE"):
        poll.remote_sha()


def test_remote_sha_invalid_response(monkeypatch):
    monkeypatch.setattr(poll, "run_simple", lambda *a, **kw: "")
    with pytest.raises(ValueError, match="UNKNOWN_REMOTE"):
        poll.remote_sha()


def test_private_record_file(tmp_path):
    record = {"sha": SHA, "status": "FAIL", "reason": "UNIT_TEST"}
    target = tmp_path / "status.json"
    poll.write_json(target, record)
    assert poll.read_json(target) == record
    assert (target.stat().st_mode & 0o777) == 0o600


def test_child_environment_excludes_provider_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(poll, "ROOT", tmp_path / "state")
    monkeypatch.setattr(poll, "APP", tmp_path / "app")
    monkeypatch.setenv("GITHUB_TOKEN", "DO_NOT_LEAK")
    monkeypatch.setenv("WOODPECKER_AGENT_SECRET", "DO_NOT_LEAK")
    env = poll.child_environment()
    assert "GITHUB_TOKEN" not in env
    assert "WOODPECKER_AGENT_SECRET" not in env
    assert "DO_NOT_LEAK" not in json.dumps(env)


def test_execute_rejects_invalid_sha():
    with pytest.raises(ValueError, match="INVALID_SHA"):
        poll.execute_sha("main", None)


def test_fail_status_does_not_convert_to_pass():
    assert poll.should_run(SHA, {"sha": SHA, "status": "FAIL", "attempts": 1, "last_attempt_epoch": time.time()}, time.time())[0] is False
