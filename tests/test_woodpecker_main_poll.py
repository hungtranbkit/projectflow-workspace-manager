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


def test_operator_retry_bypasses_cooldown_not_attempt_limit():
    now = 10000
    previous = {"sha": SHA, "status": "FAIL", "attempts": 1, "last_attempt_epoch": now - 1}
    assert poll.should_run(SHA, previous, now)[1] == "RETRY_COOLDOWN"
    assert poll.should_run(SHA, previous, now, force_retry=True) == (True, "RETRY", 2)
    previous["attempts"] = 3
    assert poll.should_run(SHA, previous, now, force_retry=True)[1] == "RETRY_LIMIT_REACHED"


def test_fast_pass_skips_without_claiming_full():
    out = poll.should_run(SHA, {"sha": SHA, "status": "FAST_PASS", "attempts": 1}, 12345)
    assert out == (False, "ALREADY_PASSED", 1)


def test_child_environment_pins_runtime_and_fail_fast(tmp_path, monkeypatch):
    monkeypatch.setattr(poll, "ROOT", tmp_path / "state")
    monkeypatch.setattr(poll, "APP", tmp_path / "app")
    monkeypatch.setattr(poll, "VENV", tmp_path / "app" / "venv-py311")
    env = poll.child_environment()
    assert env["VIRTUAL_ENV"].endswith("/venv-py311")
    assert env["PATH"].startswith(env["VIRTUAL_ENV"] + "/bin:")
    assert "--maxfail=1" in env["PYTEST_ADDOPTS"]
    assert "--tb=short" in env["PYTEST_ADDOPTS"]


def test_output_is_capped_but_subprocess_drained(tmp_path, monkeypatch):
    import os
    import sys
    monkeypatch.setattr(poll, "MAX_LOG_BYTES", 1024)
    path = tmp_path / "log.txt"
    with path.open("w", encoding="utf-8") as log:
        poll.logged_command(
            [sys.executable, "-c", "import sys;sys.stdout.write('X'*50000)"],
            log, cwd=tmp_path, env=os.environ.copy(), timeout=5,
        )
    data = path.read_text()
    assert "CI_LOG_TRUNCATED" in data
    assert len(data) <= 1500


def test_timeout_kills_subprocess(tmp_path):
    import os
    import sys
    path = tmp_path / "slow.log"
    with path.open("w", encoding="utf-8") as log:
        with pytest.raises(RuntimeError, match="COMMAND_TIMEOUT"):
            poll.logged_command(
                [sys.executable, "-c", "import time;time.sleep(9)"],
                log, cwd=tmp_path, env=os.environ.copy(), timeout=1,
            )


def test_profile_upgrade_preserves_prior_logs_and_does_not_fake_full_pass(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(poll, "ROOT", tmp_path / "ci")
    monkeypatch.setattr(poll, "remote_sha", lambda: SHA)
    monkeypatch.setattr(poll, "execute_sha", lambda sha, log: log.write("FAST_GATES_OK\\n"))
    monkeypatch.setattr(poll, "policy_identity", lambda: poll.PROFILE_VERSION + "-test")
    monkeypatch.setattr(poll.sys, "argv", ["woodpecker_main_poll.py"])
    poll.private_directory(poll.ROOT)
    poll.write_json(poll.ROOT / "status.json",
                    {"sha": SHA, "status": "FAIL", "attempts": 3, "reason": "LEGACY",
                     "profile": "py314-old"})
    old_log = poll.ROOT / "logs" / (SHA[:12] + "-attempt1.log")
    poll.private_directory(old_log.parent)
    old_log.write_text("OLD_EVIDENCE")
    assert poll.main() == 0
    d = poll.read_json(poll.ROOT / "status.json")
    assert d["status"] == "FAST_PASS"
    assert d["full_regression"] == "NOT_RUN_NO_ISOLATED_DOCKER_RUNNER"
    assert d["github_pr_checks"] == "NOT_CONNECTED"
    assert "attempt1" in d["log_path"]
    assert poll.PROFILE_VERSION in d["log_path"]
    assert Path(d["log_path"]).is_file()
    assert old_log.read_text() == "OLD_EVIDENCE"
    assert list((poll.ROOT / "history").glob(SHA[:12] + "-*.json"))
    assert poll.main() == 0
    after = poll.read_json(poll.ROOT / "status.json")
    assert after["status"] == "FAST_PASS"
    assert after["poll_count"] == 1
    assert after["last_skip_reason"] == "ALREADY_PASSED"
    assert "last_polled_at" in after



def test_policy_fingerprint_changes_with_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(poll, "APP", tmp_path / "app")
    poll.APP.mkdir()
    path = poll.APP / "trusted-main.yaml"
    path.write_text("steps: one", encoding="utf-8")
    first = poll.policy_identity()
    path.write_text("steps: two", encoding="utf-8")
    second = poll.policy_identity()
    assert first != second
    assert first.startswith(poll.PROFILE_VERSION)


def test_stale_running_guard():
    old = {"status": "RUNNING", "last_attempt_epoch": 100}
    assert poll.stale_running(old, 100 + poll.JOB_TIMEOUT_SECONDS + 121)
    assert not poll.stale_running(old, 100 + poll.JOB_TIMEOUT_SECONDS - 1)
    assert not poll.stale_running({"status": "FAST_PASS"}, 99999)


def test_network_outage_preserves_latest_fast_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(poll, "ROOT", tmp_path / "state")
    monkeypatch.setattr(poll, "remote_sha", lambda: (_ for _ in ()).throw(ValueError("offline")))
    monkeypatch.setattr(poll.sys, "argv", ["woodpecker_main_poll.py"])
    poll.private_directory(poll.ROOT)
    poll.write_json(poll.ROOT / "status.json",
                    {"sha": SHA, "status": "FAST_PASS", "attempts": 1})
    assert poll.main() == 3
    record = poll.read_json(poll.ROOT / "status.json")
    assert record["status"] == "FAST_PASS"
    assert record["last_poll_error"].startswith("REMOTE_CHECK_ERROR:")
    assert record["last_polled_at"]


def test_timer_uses_calendar_not_monotonic_elapsed_schedule():
    from pathlib import Path
    installer = Path(__file__).resolve().parents[1] / "infra/woodpecker/local-main/install-user-timer.sh"
    content = installer.read_text(encoding="utf-8")
    assert "OnCalendar=*:0/15" in content
    assert "OnUnitInactiveSec=15min" not in content
    assert "Persistent=true" in content
