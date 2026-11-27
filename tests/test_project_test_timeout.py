import subprocess
import threading
from pathlib import Path

import pytest

from app.services.test_runner import TestRunner as _TestRunner


class _DB:
    def __init__(self):
        self.updates = []
        self.events = []
        self.finished = threading.Event()
        self._next_id = 1

    def one(self, _sql, _params):
        return None

    def execute(self, sql, params):
        self.updates.append((sql, params))
        if sql.startswith("INSERT INTO test_runs"):
            result = self._next_id
            self._next_id += 1
            return result
        if sql.startswith("UPDATE test_runs") and (
            ("status=?" in sql and params[0] in {"PASS", "TIMEOUT"} and params[-1] == 2)
            or ("status='TIMEOUT'" in sql and params[-1] == 2)
        ):
            self.finished.set()
        return None

    def event(self, *args):
        self.events.append(args)


class _Git:
    def head(self, _path):
        return "fixture-sha"

    def source_state(self, _path):
        return {"head": "fixture-sha", "tracked_diff_sha256": "", "untracked_paths": (), "clean": True}


class _Runner:
    def __init__(self, *, timeout_test=False):
        self.calls = []
        self.timeout_test = timeout_test

    def run(self, command, root, working_dir, timeout):
        self.calls.append((command, root, working_dir, timeout))
        if self.timeout_test and command == "./scripts/test.sh":
            raise subprocess.TimeoutExpired(command, timeout, output="partial", stderr="bounded timeout")
        return subprocess.CompletedProcess(command, 0, "ok", "")


@pytest.mark.parametrize("timeout_test, expected_status, expected_event", [
    (False, "PASS", "TEST_PASS"),
    (True, "TIMEOUT", "TEST_FAIL"),
])
def test_project_test_timeout_reaches_test_runner_and_remains_bounded(
    timeout_test, expected_status, expected_event
):
    root = Path(__file__).resolve().parents[1]
    db = _DB()
    runner = _Runner(timeout_test=timeout_test)
    test_runner = _TestRunner(db, _Git(), runner=runner)

    run_ids = test_runner.start("project", 1, root)

    assert db.finished.wait(2), "TestRunner did not finish both required stages"
    assert run_ids == [1, 2]
    assert [(call[0], call[3]) for call in runner.calls] == [
        ("./scripts/preflight.sh", 120),
        ("./scripts/test.sh", 4500),
    ]
    final_rows = [
        entry for entry in db.updates
        if entry[0].startswith("UPDATE test_runs") and (
            ("status=?" in entry[0] and entry[1][0] in {"PASS", "TIMEOUT"} and entry[1][-1] == 2)
            or ("status='TIMEOUT'" in entry[0] and entry[1][-1] == 2)
        )
    ]
    assert (final_rows[-1][1][0] if "status=?" in final_rows[-1][0] else "TIMEOUT") == expected_status
    if timeout_test:
        assert "exit_code=124" in final_rows[-1][0]
        assert "bounded timeout" in final_rows[-1][1][2]
    assert db.events[-1][2] == expected_event
