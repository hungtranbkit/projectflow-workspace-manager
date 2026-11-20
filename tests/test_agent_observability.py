"""Agent observability P0 (specs/001-agent-observability-recovery T005-T009).

Covers: schema/provenance (valid/missing/spoofed), secret redaction, replay
and out-of-order dedupe, identity conflict quarantine, bounded retention,
stale -> UNKNOWN, separated dispatch/execution/delivery dimensions, no fake
completion from activity volume, Harness TTL/progress reuse, and read-only
adapters that never mutate their source DB.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3

import pytest

from app.services import agent_observability as obs
from app.services.agent_harness_mvp import HarnessStore

NOW = datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)
SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2"


def ev(eid: str, kind: str, minutes_ago: float, **kw) -> dict:
    base = {
        "schema_version": 1, "event_id": eid, "source": "cli_hook",
        "occurred_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "node_id": "nec1", "project_id": "projectflow", "task_id": "pf-task-7",
        "session_id": "sess-7", "agent_id": "claude-7", "kind": kind,
        "provenance_level": "LOCAL_OBSERVED",
    }
    base.update(kw)
    return base


@pytest.fixture
def store(tmp_path):
    return obs.ObservabilityEventStore(tmp_path / "harness.db")


def view(store, task="pf-task-7", at=NOW, **kw):
    return obs.derive_task_view(store.all_task_events(task), at=at, **kw)


# ---------------------------------------------------------------- T005

def test_valid_event_normalizes_with_provenance_and_dedupe_key():
    out = obs.normalize(ev("e1", "tool_call", 1, tool_kind="edit",
                           sanitized_evidence_ref="test_runs:42"), observed_at=NOW)
    assert isinstance(out, obs.NormalizedEvent)
    assert out.provenance_level == "LOCAL_OBSERVED"
    assert out.tool_kind == "edit" and out.sanitized_evidence_ref == "test_runs:42"
    assert out.dedupe_key and out.redaction_flags == ()


@pytest.mark.parametrize("missing", ["node_id", "project_id", "task_id", "session_id", "agent_id"])
def test_missing_identity_is_quarantined_not_guessed(missing):
    raw = ev("e1", "tool_call", 1)
    raw.pop(missing)
    raw["terminal_title"] = "pf-task-7 claude"  # must not be used to infer identity
    out = obs.normalize(raw, observed_at=NOW)
    assert isinstance(out, obs.Quarantine) and out.reason == "missing_identity"


def test_spoofed_provenance_is_capped_by_source_ceiling():
    raw = ev("e1", "acceptance_recorded", 1, source="agent_self_report",
             provenance_level="INDEPENDENT_ACCEPTANCE", result="PASS")
    out = obs.normalize(raw, observed_at=NOW)
    assert out.provenance_level == "SELF_REPORTED"
    assert "provenance_downgraded" in out.redaction_flags


def test_central_attested_cannot_claim_local_verification():
    out = obs.normalize(ev("e1", "tool_call", 1, source="terminal_mcp_central",
                           provenance_level="LOCAL_VERIFIED"), observed_at=NOW)
    assert out.provenance_level == "CENTRAL_ATTESTED"


@pytest.mark.parametrize("raw_change,reason", [
    ({"schema_version": 2}, "schema_version"),
    ({"source": "random_daemon"}, "unknown_source"),
    ({"kind": "teleport"}, "unknown_kind"),
    ({"occurred_at": "2026-11-20T08:00:00"}, "invalid_timestamp"),
    ({"occurred_at": (NOW + timedelta(hours=1)).isoformat()}, "future_timestamp"),
    ({"provenance_level": "TRUST_ME"}, "invalid_provenance"),
])
def test_invalid_events_are_quarantined(raw_change, reason):
    out = obs.normalize({**ev("e1", "tool_call", 1), **raw_change}, observed_at=NOW)
    assert isinstance(out, obs.Quarantine) and out.reason == reason


def test_no_secret_prompt_path_or_terminal_output_is_stored(store, tmp_path):
    raw = ev("e-secret", "tool_call", 1,
             prompt="please deploy with " + SECRET,
             terminal_output="password=hunter2",
             command="curl -H 'Authorization: Bearer abcdefghijkl' http://10.0.0.5",
             branch_ref="feat/x-" + SECRET,
             worktree_ref="/home/rag/workspace/secret-wt",
             parent_agent_id="sk-" + "z" * 24,
             sanitized_evidence_ref="test_runs:/home/rag/x password=1")
    assert store.ingest(raw, observed_at=NOW).status == "ACCEPTED"
    # Secret in an identity field quarantines the event entirely.
    bad = ev("e-secret-2", "tool_call", 1, session_id=SECRET)
    assert store.ingest(bad, observed_at=NOW).reason == "missing_identity"
    store.ingest({**ev("e-secret-3", "tool_call", 1), "event_id": SECRET}, observed_at=NOW)

    dump = "\n".join(sqlite3.connect(tmp_path / "harness.db").iterdump())
    for leaked in (SECRET, "hunter2", "please deploy", "Bearer abcdef", "10.0.0.5",
                   "/home/rag", "sk-zzzz"):
        assert leaked not in dump
    stored = store.all_task_events("pf-task-7")[0]
    for k in ("prompt", "terminal_output", "command"):
        assert k not in stored
    flags = set(stored["redaction_flags"])
    assert {"dropped_field:prompt", "dropped_field:terminal_output", "dropped_field:command",
            "secret_redacted:branch_ref", "private_location_redacted:worktree_ref",
            "secret_redacted:parent_agent_id"} <= flags
    assert stored["branch_ref"] is None and stored["worktree_ref"] is None
    assert stored["sanitized_evidence_ref"] is None


# ---------------------------------------------------------------- T006/T007

def test_replay_twice_out_of_order_yields_one_event_per_id(store):
    fixture = [ev(f"e{i}", "tool_call", 30 - i, tool_kind="read") for i in range(10)]
    shuffled = fixture[::2] + fixture[1::2]
    first = store.ingest_many(shuffled, observed_at=NOW)
    second = store.ingest_many(list(reversed(fixture)), observed_at=NOW)
    assert first == {"ACCEPTED": 10, "DUPLICATE": 0, "QUARANTINED": 0}
    assert second == {"ACCEPTED": 0, "DUPLICATE": 10, "QUARANTINED": 0}
    events = store.all_task_events("pf-task-7")
    assert sorted(e["event_id"] for e in events) == sorted(f"e{i}" for i in range(10))
    # ingest order is monotonic; late arrivals are flagged, not reordered away
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
    assert sum(e["late_arrival"] for e in events) == 4
    assert store.counters()["duplicates"] == 10


def test_same_dedupe_key_with_different_content_never_rewrites_history(store):
    assert store.ingest(ev("e1", "tool_call", 2, result="PASS"), observed_at=NOW).status == "ACCEPTED"
    r = store.ingest(ev("e1", "tool_call", 2, result="FAIL"), observed_at=NOW)
    assert (r.status, r.reason) == ("QUARANTINED", "replay_conflict")
    assert [e["result"] for e in store.all_task_events("pf-task-7")] == ["PASS"]


def test_session_is_never_relabeled_to_another_task(store):
    store.ingest(ev("e1", "session_started", 5), observed_at=NOW)
    r = store.ingest(ev("e2", "tool_call", 1, task_id="pf-task-99"), observed_at=NOW)
    assert r.reason == "identity_conflict"
    assert store.all_task_events("pf-task-99") == []
    assert store.quarantine()[0]["reason"] == "identity_conflict"


def test_bounded_retention_and_replay_of_pruned_event_is_still_duplicate(tmp_path):
    s = obs.ObservabilityEventStore(tmp_path / "h.db", max_events=5, max_dedupe=50)
    for i in range(12):
        s.ingest(ev(f"e{i}", "output_growth", 20 - i), observed_at=NOW)
    assert s.counters()["stored_events"] == 5
    assert s.ingest(ev("e0", "output_growth", 20), observed_at=NOW).status == "DUPLICATE"
    # restart: a new instance over the same file keeps the dedupe ledger
    s2 = obs.ObservabilityEventStore(tmp_path / "h.db", max_events=5, max_dedupe=50)
    assert s2.ingest(ev("e3", "output_growth", 17), observed_at=NOW).status == "DUPLICATE"


def test_paging_is_bounded(store):
    store.ingest_many([ev(f"e{i}", "tool_call", 1) for i in range(30)], observed_at=NOW)
    page = store.events("pf-task-7", limit=10_000)
    assert len(page) == 30
    assert len(store.events("pf-task-7", limit=7)) == 7
    assert store.events("pf-task-7", after_seq=page[-1]["seq"]) == []


# ---------------------------------------------------------------- T008

def test_prompt_submitted_alone_is_not_running(store):
    store.ingest(ev("p1", "prompt_submitted", 1, source="terminal_mcp_central",
                    provenance_level="CENTRAL_ATTESTED"), observed_at=NOW)
    v = view(store)
    assert v["dispatch"] == "DISPATCHED_UNVERIFIED"
    assert v["execution"] == "UNKNOWN"
    assert v["milestones"]["running_verified"] is False
    assert v["delivery_stage"] == "NOT_STARTED"


def test_live_tool_event_is_running_verified(store):
    store.ingest_many([ev("s", "session_started", 3), ev("t", "tool_call", 1)], observed_at=NOW)
    v = view(store)
    assert (v["dispatch"], v["execution"], v["delivery_stage"]) == ("ACKNOWLEDGED", "RUNNING_VERIFIED", "CODING")


def test_heartbeat_only_is_stalled_not_progress(store):
    store.ingest(ev("s", "session_started", 30), observed_at=NOW)
    store.ingest_many([ev(f"hb{i}", "heartbeat", i) for i in range(5)], observed_at=NOW)
    v = view(store)
    assert v["execution"] == "STALLED"
    assert "heartbeat_only_no_progress_ever" in v["reasons"]
    assert v["milestones"]["running_verified"] is False


def test_stale_source_becomes_unknown_never_stays_green(store):
    store.ingest_many([ev("s", "session_started", 5), ev("t", "tool_call", 1)], observed_at=NOW)
    assert view(store)["execution"] == "RUNNING_VERIFIED"
    later = view(store, at=NOW + timedelta(hours=1))
    assert later["execution"] == "UNKNOWN" and "stale_source" in later["reasons"]
    assert later["freshness"]["stale"] is True


def test_waiting_input_disconnect_exit_failed_are_distinct(store):
    store.ingest_many([ev("s", "session_started", 5), ev("w", "waiting_input", 1)], observed_at=NOW)
    assert view(store)["execution"] == "WAITING_FOR_INPUT"
    store.ingest(ev("d", "node_disconnected", 0.5), observed_at=NOW)
    v = view(store)
    assert v["execution"] == "UNKNOWN" and "node_disconnected" in v["reasons"]
    store.ingest(ev("x", "process_exited", 0.2, result="FAIL"), observed_at=NOW)
    assert view(store)["execution"] == "FAILED"


def test_late_heartbeat_does_not_revive_exited_session(store):
    store.ingest_many([ev("s", "session_started", 10), ev("x", "process_exited", 2, result="PASS")],
                      observed_at=NOW)
    store.ingest(ev("hb-late", "heartbeat", 1), observed_at=NOW)
    store.ingest(ev("tool-late", "tool_call", 3), observed_at=NOW)  # out-of-order, before exit
    assert view(store)["execution"] == "EXITED"


def test_missing_central_receipt_only_attested_negative_is_unknown(store):
    store.ingest(ev("a", "process_exited", 1, source="terminal_mcp_central",
                    provenance_level="CENTRAL_ATTESTED"), observed_at=NOW)
    v = view(store)
    assert v["execution"] == "UNKNOWN"
    assert "attested_process_exited" in v["reasons"]


def test_submitted_running_qa_merged_deployed_are_separate(store):
    store.ingest_many([ev("s", "session_started", 9), ev("t", "tool_call", 1)], observed_at=NOW)
    # agent self-report of merge/deploy/accept is ignored
    for k in ("merge_recorded", "deploy_recorded", "acceptance_recorded", "qa_result"):
        store.ingest(ev(f"self-{k}", k, 1, source="agent_self_report",
                        provenance_level="LOCAL_VERIFIED", result="PASS"), observed_at=NOW)
    v = view(store)
    assert v["milestones"] == {"submitted": True, "running_verified": True, "tests_passed": False,
                               "review_passed": False, "qa_passed": False, "merged": False,
                               "deployed": False, "accepted": False}
    assert "merged_claim_below_required_provenance" in v["reasons"]
    ver = {"source": "projectflow_evidence", "provenance_level": "LOCAL_VERIFIED"}
    store.ingest(ev("qa1", "qa_result", 1, result="PASS", **ver), observed_at=NOW)
    v = view(store)
    assert v["milestones"]["qa_passed"] and not v["milestones"]["merged"]
    assert v["delivery_stage"] == "QA"
    store.ingest(ev("m1", "merge_recorded", 0.5, result="PASS", **ver), observed_at=NOW)
    v = view(store)
    assert v["milestones"]["merged"] and not v["milestones"]["deployed"]
    assert not v["milestones"]["accepted"]  # LOCAL_VERIFIED can never accept
    # a newer failed QA supersedes the older pass
    store.ingest(ev("qa2", "qa_result", 0.1, result="FAIL", **ver), observed_at=NOW)
    assert view(store)["milestones"]["qa_passed"] is False


# ---------------------------------------------------------------- T009

def harness_snapshot(observed_at=NOW, status="HEALTHY", clock=None):
    return {
        "observed_at": observed_at.isoformat(),
        "health": {"status": status},
        "monitors": [{"id": "AUTO_1", "completion_percent": 10, "gates_passed": 1,
                      "gates_total": 6, "active_tasks": [clock] if clock else [],
                      "recent_tasks": []}],
    }


def test_activity_volume_never_changes_completion(store):
    snap = harness_snapshot()
    store.ingest(ev("s", "session_started", 5), observed_at=NOW)
    base = obs.attach_harness(view(store), snapshot=snap, automation_id="AUTO_1", at=NOW)
    burst = [ev(f"burst{i}", "tool_call", 0.5, tool_kind="edit") for i in range(2000)]
    burst += [ev(f"tok{i}", "heartbeat", 0.5) for i in range(2000)]
    store.ingest_many(burst, observed_at=NOW)
    after = obs.attach_harness(view(store), snapshot=snap, automation_id="AUTO_1", at=NOW)
    assert base["completion_percent"] == after["completion_percent"] == 10
    assert obs.derive_task_view(store.all_task_events("pf-task-7"), at=NOW)["completion_percent"] is None


def test_completion_unknown_without_reviewed_or_fresh_harness(store):
    v = view(store)
    assert obs.attach_harness(v, snapshot=None, automation_id=None)["completion_percent"] is None
    stale = obs.attach_harness(v, snapshot=harness_snapshot(observed_at=NOW - timedelta(hours=2)),
                               automation_id="AUTO_1", at=NOW)
    assert stale["completion_percent"] is None and "HARNESS_EVIDENCE_STALE" in stale["blockers"]
    unhealthy = obs.attach_harness(v, snapshot=harness_snapshot(status="FAILED_OR_STALE"),
                                   automation_id="AUTO_1", at=NOW)
    assert unhealthy["completion_percent"] is None


def test_expired_child_stays_blocked_cleanup_pending(store):
    store.ingest_many([ev("s", "session_started", 5), ev("t", "tool_call", 1)], observed_at=NOW)
    clock = {"id": 3, "state": "EXPIRED", "cleanup": "CLEANUP_PENDING", "time_used_percent": 100,
             "overdue": False, "finished": True, "remaining_label": "đã kết thúc"}
    v = obs.attach_harness(view(store), snapshot=harness_snapshot(clock=clock),
                           automation_id="AUTO_1", harness_task_id=3, at=NOW)
    assert "CLEANUP_PENDING" in v["blockers"]
    assert v["execution"] == "UNKNOWN"  # never RUNNING past an expired TTL
    assert v["time_budget"]["time_used_percent"] == 100
    assert v["completion_percent"] == 10  # TTL % and gate % stay separate


def test_reuses_real_harness_progress_snapshot(tmp_path):
    from scripts.harness_monitor_bridge import bootstrap, load_registry
    from scripts import harness_progress as progress
    db = tmp_path / "harness.db"
    HarnessStore(db)
    bootstrap(load_registry(), db)
    snap = progress.summarize(db, reference=NOW)
    zgh = "Automation_eec08a5752d08191b3de371065c27320"
    snap["health"] = {"status": "HEALTHY"}
    v = obs.attach_harness(obs.derive_task_view([], at=NOW), snapshot=snap,
                           automation_id=zgh, at=NOW)
    expected = next(m for m in snap["monitors"] if m["id"] == zgh)["completion_percent"]
    assert v["completion_percent"] == expected
    # the observability tables coexist in the same Harness DB without touching its ledger
    obs.ObservabilityEventStore(db)
    assert HarnessStore(db).list_tasks() == []


# ---------------------------------------------------------------- adapters

def _pf_db(path):
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE agent_sessions(id INTEGER PRIMARY KEY, task_id INTEGER, workspace_id INTEGER,
        agent TEXT, command_profile TEXT, cwd TEXT, pid INTEGER, status TEXT, mode TEXT,
        started_at TEXT, last_activity_at TEXT, exited_at TEXT, exit_code INTEGER,
        transcript_tail TEXT);
      CREATE TABLE qa_runs(id INTEGER PRIMARY KEY, task_id INTEGER, status TEXT, notes TEXT,
        completed_at TEXT);
      CREATE TABLE test_runs(id INTEGER PRIMARY KEY, workspace_type TEXT, workspace_id INTEGER,
        command TEXT, status TEXT, finished_at TEXT, stdout_tail TEXT);
    """)
    t = lambda m: (NOW - timedelta(minutes=m)).isoformat()
    db.execute("INSERT INTO agent_sessions VALUES(1,7,3,'claude','claude','/home/rag/wt',99,'RUNNING',"
               "'INTERACTIVE',?,?,NULL,NULL,?)", (t(20), t(1), "export TOKEN=" + SECRET))
    db.execute("INSERT INTO qa_runs VALUES(1,7,'PASS','notes with password=x',?)", (t(2),))
    db.execute("INSERT INTO test_runs VALUES(1,'agent',3,'pytest',"
               "'PASS',?, 'secret stdout')", (t(3),))
    db.commit()
    db.close()


def test_projectflow_adapter_is_read_only_replay_safe_and_leak_free(tmp_path, store):
    src = tmp_path / "pf.db"
    _pf_db(src)
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    raws, meta = obs.projectflow_task_events(src, task_id=7, node_id="nec1", project_id="projectflow")
    assert meta["truncated"] == [] and "review_runs" in meta["skipped_sources"]
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before
    blob = json.dumps(raws)
    for leaked in (SECRET, "/home/rag/wt", "password=x", "secret stdout", "pytest"):
        assert leaked not in blob
    first = store.ingest_many(raws, observed_at=NOW)
    again = store.ingest_many(obs.projectflow_task_events(src, task_id=7, node_id="nec1",
                                                          project_id="projectflow")[0],
                              observed_at=NOW)
    assert first["QUARANTINED"] == 0 and again["ACCEPTED"] == 0
    v = view(store)
    assert v["execution"] == "RUNNING_VERIFIED"
    # PR #56 fix C: raw rows cannot prove the current commit/brief, so they
    # are history only; current milestones come from TaskDecisionService.
    assert not any(v["milestones"][m] for m in ("qa_passed", "tests_passed", "merged", "accepted"))
    assert {h["ref"] for h in v["historic_evidence"]} == {"qa_runs:1", "test_runs:1"}


def test_adapter_refuses_to_write(tmp_path):
    src = tmp_path / "pf.db"
    _pf_db(src)
    db = obs._ro_connect(src)
    with pytest.raises(sqlite3.OperationalError):
        db.execute("UPDATE agent_sessions SET status='EXITED'")
    db.close()


def test_harness_adapter_dispatch_is_unverified_and_done_is_not_acceptance(tmp_path, store):
    db = tmp_path / "harness.db"
    hs = HarnessStore(db)
    t = hs.create(key="obs-k1", main_goal="g", child_goal="c", dod="d", agent="codex",
                  node="nec1", created_at=NOW - timedelta(minutes=10), ttl=timedelta(hours=1))
    hs.dispatch(t.id, external_task_id="dt_abcdefgh", at=NOW - timedelta(minutes=9))
    raws = obs.harness_task_events(db, project_id="projectflow")
    store.ingest_many(raws, observed_at=NOW)
    v = obs.derive_task_view(store.all_task_events(f"harness-{t.id}"), at=NOW)
    assert v["dispatch"] == "DISPATCHED_UNVERIFIED"
    assert v["execution"] == "UNKNOWN"
    assert v["milestones"]["accepted"] is False


# ---------------------------------------------------------------- read-only API (T010/T011 slice)

def _seed_task(tmp_path):
    db = sqlite3.connect(tmp_path / "test.db")  # FK checks off by default on a fresh connection
    db.execute("INSERT INTO tasks(id,slug,title) VALUES(501,'obs-501','Obs task')")
    now = datetime.now(timezone.utc)
    db.execute("""INSERT INTO agent_sessions(id,task_id,workspace_id,agent,command_profile,cwd,status,
                  started_at,last_activity_at,transcript_tail) VALUES(901,501,77,'claude','claude',
                  '/home/rag/private-wt','RUNNING',?,?,?)""",
               ((now - timedelta(minutes=5)).isoformat(), (now - timedelta(seconds=30)).isoformat(),
                "TOKEN=" + SECRET))
    db.commit()
    db.close()


def _table_counts(path):
    db = sqlite3.connect(path)
    try:
        return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if not t.startswith("sqlite_")}
    finally:
        db.close()


def test_observability_api_is_read_only_and_leak_free(client, tmp_path):
    _seed_task(tmp_path)
    before = _table_counts(tmp_path / "test.db")
    r = client.get("/api/tasks/501/observability")
    assert r.status_code == 200
    body = r.json()
    assert body["dispatch"] == "ACKNOWLEDGED"
    assert body["execution"] == "RUNNING_VERIFIED"
    assert body["completion_percent"] is None  # never from activity
    assert body["milestones"]["merged"] is False and body["milestones"]["deployed"] is False
    page = client.get("/api/tasks/501/observability/events?limit=1").json()
    assert len(page["events"]) == 1 and page["next_cursor"] is not None
    html = client.get("/tasks/501/observability")
    assert html.status_code == 200 and 'name="viewport"' in html.text
    for text in (r.text, html.text, json.dumps(page)):
        assert SECRET not in text and "/home/rag" not in text
    assert _table_counts(tmp_path / "test.db") == before
    assert not any(t.startswith("agent_obs_") for t in before)
    assert client.get("/api/tasks/99999/observability").status_code == 404
