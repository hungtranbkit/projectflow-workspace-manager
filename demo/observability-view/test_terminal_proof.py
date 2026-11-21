"""Fail-closed tests for the opt-in Terminal MCP public runtime probe."""
import json
from datetime import datetime, timedelta, timezone
import sqlite3

from terminal_proof import TerminalProofMonitor, read_explicit_task_label


def test_live_output_must_change_to_verify_running():
    clock = [100.0]
    output = ["reading"]
    def read(node, name):
        return {"state": "RUNNING", "exists": True, "allowed": True,
                "last_output": output[0]}
    monitor = TerminalProofMonitor(read, interval=1, clock=lambda: clock[0],
                                   task_reader=lambda *args: None)
    rows = [{"node_id": "hp-linux", "session_name": "projectflow-observability-01"}]
    now = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)
    assert monitor.snapshot(rows, now=now) == {}
    clock[0] += 2
    output[0] = "reading\nwriting"
    current = monitor.snapshot(rows, now=now + timedelta(seconds=2))
    info = current[("hp-linux", "projectflow-observability-01")]
    assert info["state"] == "RUNNING_VERIFIED"
    assert info["evidence"] == "terminal_output_delta"
    assert "last_output" not in json.dumps(list(current.values()))
    clock[0] += 50
    assert monitor.snapshot(rows, now=now + timedelta(seconds=60)) == {}


def test_stale_output_and_idle_are_not_running():
    value = {"state": "RUNNING", "exists": True, "allowed": True, "last_output": "unchanged"}
    clock = [0.]
    def read(*args):
        return value.copy()
    monitor = TerminalProofMonitor(read, interval=1, clock=lambda: clock[0],
                                   task_reader=lambda *args: None)
    row = [{"node_id": "hp-linux", "session_name": "projectflow-agent-1"}]
    now = datetime(2026,11,21,10,tzinfo=timezone.utc)
    assert monitor.snapshot(row, now=now) == {}
    clock[0] += 2
    assert monitor.snapshot(row, now=now + timedelta(seconds=2)) == {}
    value["state"] = "IDLE"
    clock[0] += 2
    result = monitor.snapshot(row, now=now + timedelta(seconds=4))
    assert result[("hp-linux","projectflow-agent-1")]["state"] == "IDLE"
    assert result[("hp-linux","projectflow-agent-1")]["evidence"] == "node_observed"


def test_waiting_input_and_failed_authorization_fail_closed():
    statuses = {"one": {"state":"RUNNING","input_required":True,"exists":True,"allowed":True,
                        "last_output":"password=secret"},
                "two": {"state":"RUNNING","exists":True,"allowed":False,"last_output":"secret"}}
    def read(node, name):
        return statuses["one" if name.endswith("one") else "two"]
    monitor=TerminalProofMonitor(read, interval=0, task_reader=lambda *args: None)
    now=datetime(2026,11,21,10,tzinfo=timezone.utc)
    rows=[{"node_id":"hp-linux","session_name":"projectflow-one"},
          {"node_id":"hp-linux","session_name":"projectflow-two"}]
    result=monitor.snapshot(rows,now=now)
    assert result[("hp-linux","projectflow-one")]["state"]=="WAITING_INPUT"
    assert ("hp-linux","projectflow-two") not in result
    assert "secret" not in json.dumps(list(result.values()))


def test_task_titles_are_curated_and_prompt_fallback_is_never_public(tmp_path):
    db_path=tmp_path/"audit.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE session_task_labels "
                   "(node_id TEXT, session TEXT, summary TEXT, source TEXT, updated_at TEXT)")
        db.execute("INSERT INTO session_task_labels VALUES(?,?,?,?,?)",
                   ("hp-linux","projectflow-proof","CrommaCode 123 / private token: abc","title",
                    "2026-11-21T10:00:00+00:00"))
        db.execute("INSERT INTO session_task_labels VALUES(?,?,?,?,?)",
                   ("hp-linux","zgh-private","Private customer prompt and credentials","prompt_fallback",
                    "2026-11-21T10:00:00+00:00"))
    a=read_explicit_task_label("hp-linux","projectflow-proof",db_path=db_path)
    assert a["title"]=="Xây dựng nguồn bằng chứng hoạt động agent"
    assert "private" not in json.dumps(a)
    assert read_explicit_task_label("hp-linux","zgh-private",db_path=db_path) is None
    assert read_explicit_task_label("hp-linux","../invalid",db_path=db_path) is None


def test_unknown_foreground_with_real_output_delta_is_verified():
    state = {"state": "UNKNOWN", "reason": "current command is 'python3'; activity age is old",
             "exists": True, "allowed": True, "last_output": "one"}
    clock = [0.]
    monitor = TerminalProofMonitor(lambda *args: state.copy(), interval=1,
                                   clock=lambda: clock[0], task_reader=lambda *args: None)
    row = [{"node_id": "hp-linux", "session_name": "projectflow-active-foreground"}]
    now = datetime(2026, 11, 21, 10, tzinfo=timezone.utc)
    assert monitor.snapshot(row, now=now) == {}
    clock[0] += 2
    state["last_output"] = "one\ntwo"
    result = monitor.snapshot(row, now=now + timedelta(seconds=2))
    assert result[("hp-linux","projectflow-active-foreground")]["state"] == "RUNNING_VERIFIED"
    # The exact same output pattern does NOT prove a foreground execution
    # when the trusted node reports a shell prompt/unknown.
    state["reason"] = "current command is 'bash'; activity age is old"
    state["last_output"] = "one\ntwo\nthree"
    clock[0] += 2
    assert monitor.snapshot(row, now=now + timedelta(seconds=4)) == {}


def test_session_recreated_with_same_name_cannot_reuse_execution_proof():
    step = {"state": "RUNNING", "exists": True, "allowed": True,
            "last_output": "first"}
    clock = [0.]
    monitor = TerminalProofMonitor(lambda *args: step.copy(), interval=1,
                                   clock=lambda: clock[0], task_reader=lambda *args: None)
    row = [{"node_id": "hp-linux", "session_name": "projectflow-proof",
            "stable_session_id": "instance-one", "generation": 1}]
    at = datetime(2026, 11, 21, 10, tzinfo=timezone.utc)
    assert monitor.snapshot(row, now=at) == {}
    clock[0] += 2
    step["last_output"] = "first-second"
    assert monitor.snapshot(row, now=at + timedelta(seconds=2))[
        ("hp-linux", "projectflow-proof")]["state"] == "RUNNING_VERIFIED"
    # Restart with the SAME output and name but a different instance.
    row[0]["stable_session_id"] = "instance-two"
    clock[0] += 2
    assert monitor.snapshot(row, now=at + timedelta(seconds=4)) == {}
    # A new output event in instance two is now fresh evidence.
    step["last_output"] = "first-second-third"
    clock[0] += 2
    assert monitor.snapshot(row, now=at + timedelta(seconds=6))[
        ("hp-linux", "projectflow-proof")]["state"] == "RUNNING_VERIFIED"
    # A KILLED/reopened registry row can retain a stable ID while
    # generation changes; old proof must not cross that boundary.
    row[0]["generation"] = 2
    clock[0] += 2
    assert monitor.snapshot(row, now=at + timedelta(seconds=8)) == {}


def test_idle_clears_old_output_baseline():
    step = {"state": "RUNNING", "exists": True, "allowed": True, "last_output": "A"}
    clock = [0.]
    monitor = TerminalProofMonitor(lambda *args: step.copy(), interval=1,
                                   clock=lambda: clock[0], task_reader=lambda *args: None)
    row = [{"node_id": "hp-linux", "session_name": "projectflow-proof",
            "stable_session_id": "instance-one", "generation": 1}]
    at = datetime(2026, 11, 21, 10, tzinfo=timezone.utc)
    monitor.snapshot(row, now=at)
    clock[0] += 2
    step["last_output"] = "AB"
    assert monitor.snapshot(row, now=at + timedelta(seconds=2))[
        ("hp-linux", "projectflow-proof")]["state"] == "RUNNING_VERIFIED"
    step["state"] = "IDLE"
    clock[0] += 2
    assert monitor.snapshot(row, now=at + timedelta(seconds=4))[
        ("hp-linux", "projectflow-proof")]["state"] == "IDLE"
    step["state"] = "RUNNING"
    clock[0] += 2
    assert monitor.snapshot(row, now=at + timedelta(seconds=6)) == {}


def test_remote_node_never_inherits_hp_local_task_label(tmp_path):
    db_path = tmp_path / "audit.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE session_task_labels "
                   "(node_id TEXT, session TEXT, summary TEXT, source TEXT, updated_at TEXT)")
        db.execute("INSERT INTO session_task_labels VALUES(?,?,?,?,?)",
                   ("local", "projectflow-session", "CrommaCode private client alpha",
                    "title", "2026-11-21T10:00:00+00:00"))
    # The node-local mirror belongs to HP; a similarly named remote
    # session cannot claim that title, even if the task text is curated.
    assert read_explicit_task_label("m910", "projectflow-session", db_path=db_path) is None
    owned = read_explicit_task_label("hp-linux", "projectflow-session", db_path=db_path)
    assert owned["title"] == "Xây dựng nguồn bằng chứng hoạt động agent"
