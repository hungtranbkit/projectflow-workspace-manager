"""Agent observability P0 FINAL correctness repair (PR #56 Codex audit A-D).

A. Multi-session: execution is derived PER session, then aggregated
   conservatively; an old session's exit never masks a new session's
   fresh progress, failed siblings stay visible as blockers.
B. Timeline cursor: stable (occurred_at, dedupe_key) order + opaque
   cursor; no skipped/duplicated events across pages; malformed cursors
   rejected; GET never writes.
C. Raw ProjectFlow evidence rows are HISTORIC/UNKNOWN currency and never
   set milestones.* by themselves; current gate truth comes only from
   TaskDecisionService (commit/brief pinned). Workspace ownership kept.
D. Dedupe/security: event_id vs dedupe_key disagreement or a reused
   event_id with different data is quarantined, never overwritten or
   reassigned; tenant/task binding enforced for in-memory GET views.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import base64
import json
import sqlite3

import pytest

from app.db import Database
from app.services import agent_observability as obs

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


def ev(eid, kind, minutes_ago, **kw):
    base = {"schema_version": 1, "event_id": eid, "source": "cli_hook",
            "occurred_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
            "node_id": "nec1", "project_id": "projectflow", "task_id": "pf-task-7",
            "session_id": "sess-new", "agent_id": "claude-new", "kind": kind,
            "provenance_level": "LOCAL_OBSERVED"}
    base.update(kw)
    return base


OLD = {"session_id": "sess-old", "agent_id": "claude-old"}


@pytest.fixture
def store(tmp_path):
    return obs.ObservabilityEventStore(tmp_path / "h.db")


# ---------------------------------------------------------------- A

def test_old_session_exit_does_not_mask_new_session_progress(store):
    store.ingest_many([
        ev("o-start", "session_started", 2, **OLD),
        ev("o-exit", "process_exited", 1, result="PASS", **OLD),
        ev("n-start", "session_started", 0.5),
        ev("n-out", "output_growth", 0),
    ], observed_at=NOW)
    v = obs.derive_task_view(store.all_task_events("pf-task-7"), at=NOW)
    assert v["execution"] == "RUNNING_VERIFIED"
    per = {s["session_id"]: s["execution"] for s in v["sessions"]}
    assert per == {"sess-old": "EXITED", "sess-new": "RUNNING_VERIFIED"}
    assert v["milestones"]["accepted"] is False


def test_failed_sibling_is_preserved_as_blocker(store):
    store.ingest_many([
        ev("o-start", "session_started", 3, **OLD),
        ev("o-fail", "process_failed", 1, result="FAIL", **OLD),
        ev("n-start", "session_started", 0.5),
        ev("n-tool", "tool_call", 0),
    ], observed_at=NOW)
    v = obs.derive_task_view(store.all_task_events("pf-task-7"), at=NOW)
    assert v["execution"] == "RUNNING_VERIFIED"
    assert "SESSION_FAILED:sess-old" in v["blockers"]


def test_old_session_late_event_never_revives_or_overrides(store):
    # new session already exited; old session's late (stale-timestamped)
    # progress must not revive anything.
    store.ingest_many([
        ev("o-start", "session_started", 40, **OLD),
        ev("o-exit", "process_exited", 30, result="PASS", **OLD),
        ev("n-start", "session_started", 5),
        ev("n-exit", "process_exited", 1, result="PASS"),
    ], observed_at=NOW)
    store.ingest(ev("o-late", "output_growth", 31, **OLD), observed_at=NOW)
    store.ingest(ev("o-late-hb", "heartbeat", 0.2, **OLD), observed_at=NOW)
    v = obs.derive_task_view(store.all_task_events("pf-task-7"), at=NOW)
    assert v["execution"] == "EXITED"
    assert {s["execution"] for s in v["sessions"]} == {"EXITED"}


def test_latest_ending_session_decides_terminal_aggregate(store):
    store.ingest_many([
        ev("o-start", "session_started", 20, **OLD),
        ev("o-exit", "process_exited", 10, result="PASS", **OLD),
        ev("n-start", "session_started", 5),
        ev("n-fail", "process_failed", 2, result="FAIL"),
    ], observed_at=NOW)
    v = obs.derive_task_view(store.all_task_events("pf-task-7"), at=NOW)
    assert v["execution"] == "FAILED"
    assert "SESSION_FAILED:sess-new" in v["blockers"]


def test_waiting_sibling_is_more_conservative_than_running(store):
    store.ingest_many([
        ev("o-start", "session_started", 3, **OLD),
        ev("o-wait", "waiting_input", 1, **OLD),
        ev("n-start", "session_started", 2),
        ev("n-out", "output_growth", 0),
    ], observed_at=NOW)
    assert obs.derive_task_view(store.all_task_events("pf-task-7"), at=NOW)["execution"] == "WAITING_FOR_INPUT"


def test_evidence_only_lane_does_not_mask_running_session():
    raws = [ev("n-start", "session_started", 2), ev("n-out", "output_growth", 0),
            ev("qa", "qa_result", 1, source="projectflow_evidence", provenance_level="LOCAL_VERIFIED",
               result="PASS", session_id="pf-task-7-evidence", agent_id="projectflow-evidence")]
    v, _, _ = obs.view_from_raw(raws, at=NOW)
    assert v["execution"] == "RUNNING_VERIFIED"


# ---------------------------------------------------------------- B

def _walk(events, limit):
    seen, cursor, pages = [], None, 0
    while True:
        page = obs.page_events(events, cursor=cursor, limit=limit)
        seen.extend(e["event_id"] for e in page["events"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            return seen, pages
        assert pages < 50


def test_out_of_order_input_is_not_skipped_across_pages():
    # input order: seq1 is NEWER, seq2 is OLDER (the reported repro)
    raws = [ev("newer", "tool_call", 1), ev("older", "tool_call", 5)]
    _, events, _ = obs.view_from_raw(raws, at=NOW)
    p1 = obs.page_events(events, cursor=None, limit=1)
    assert [e["event_id"] for e in p1["events"]] == ["older"]
    p2 = obs.page_events(events, cursor=p1["next_cursor"], limit=1)
    assert [e["event_id"] for e in p2["events"]] == ["newer"]
    p3 = obs.page_events(events, cursor=p2["next_cursor"], limit=1)
    assert p3["events"] == [] and p3["next_cursor"] is None


def test_same_timestamps_are_totally_ordered_and_complete():
    raws = [ev(f"e{i}", "tool_call", 1) for i in (5, 1, 4, 2, 3, 0)]
    raws += [ev(f"x{i}", "tool_call", 3 - i) for i in range(3)]
    _, events, _ = obs.view_from_raw(raws, at=NOW)
    for limit in (1, 2, 4, 100):
        seen, _ = _walk(events, limit)
        assert sorted(seen) == sorted(r["event_id"] for r in raws)
        assert len(seen) == len(set(seen))
    # reversed input -> identical order
    _, events_rev, _ = obs.view_from_raw(list(reversed(raws)), at=NOW)
    assert [e["event_id"] for e in events] == [e["event_id"] for e in events_rev]


def test_repeated_get_same_cursor_is_identical():
    raws = [ev(f"e{i}", "tool_call", (i * 7) % 5) for i in range(9)]
    _, events, _ = obs.view_from_raw(raws, at=NOW)
    c = obs.page_events(events, cursor=None, limit=3)["next_cursor"]
    a = obs.page_events(events, cursor=c, limit=3)
    b = obs.page_events(events, cursor=c, limit=3)
    assert a == b and len(a["events"]) == 3


def test_late_arrivals_after_cursor_appear_and_before_cursor_are_detectable():
    raws = [ev(f"e{i}", "tool_call", 10 - i) for i in range(4)]
    _, events, _ = obs.view_from_raw(raws, at=NOW)
    p1 = obs.page_events(events, cursor=None, limit=2)
    raws2 = raws + [ev("late-after", "tool_call", 0.5), ev("late-before", "tool_call", 30)]
    _, events2, _ = obs.view_from_raw(raws2, at=NOW)
    p2 = obs.page_events(events2, cursor=p1["next_cursor"], limit=10)
    assert [e["event_id"] for e in p2["events"]] == ["e2", "e3", "late-after"]
    # an event that arrived later with an older timestamp is not silently
    # lost: the before-cursor count grows so the client knows to resync.
    p2_old = obs.page_events(events, cursor=p1["next_cursor"], limit=10)
    assert p2["before_cursor_count"] == p2_old["before_cursor_count"] + 1


@pytest.mark.parametrize("bad", [
    "garbage", "v1.", "v1.!!!", "v2." + base64.urlsafe_b64encode(b'["x","y"]').decode(),
    "v1." + base64.urlsafe_b64encode(b'{"a":1}').decode(),
    "v1." + base64.urlsafe_b64encode(b'["not-a-time","k"]').decode(),
    "v1." + base64.urlsafe_b64encode(b'["2026-11-20T10:00:00","k"]').decode(),  # naive
    "v1." + base64.urlsafe_b64encode(json.dumps(["2026-11-20T10:00:00+00:00", "a b"]).encode()).decode(),
    "x" * 600,
])
def test_malformed_cursor_is_rejected(bad):
    with pytest.raises(obs.ObservabilityError):
        obs.decode_cursor(bad)


def _seed_api(tmp_path, tid=601):
    db = sqlite3.connect(tmp_path / "test.db")
    db.execute("INSERT INTO tasks(id,slug,title) VALUES(?,?,?)", (tid, f"obs-{tid}", "Obs"))
    now = datetime.now(timezone.utc)
    rows = []
    # session ids ascending but timestamps deliberately out of order
    for i, mins in enumerate((1, 9, 3, 9, 6, 2)):
        rows.append((tid * 10 + i, tid, 70 + i, "EXITED", (now - timedelta(minutes=mins + 1)).isoformat(),
                     (now - timedelta(minutes=mins)).isoformat()))
    db.executemany("""INSERT INTO agent_sessions(id,task_id,workspace_id,agent,command_profile,cwd,status,
                      started_at,exited_at,exit_code,transcript_tail)
                      VALUES(?,?,?,'claude','claude','/x',?,?,?,0,'')""", rows)
    db.commit()
    db.close()


def _counts(path):
    db = sqlite3.connect(path)
    try:
        return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if not t.startswith("sqlite_")}
    finally:
        db.close()


def test_api_paging_page1_page2_page3_complete_and_read_only(client, tmp_path):
    _seed_api(tmp_path)
    before = _counts(tmp_path / "test.db")
    full = client.get("/api/tasks/601/observability/events?limit=200").json()["events"]
    assert len(full) == 12
    seen, cursor = [], None
    for _ in range(3):
        url = "/api/tasks/601/observability/events?limit=4" + (f"&cursor={cursor}" if cursor else "")
        body = client.get(url).json()
        again = client.get(url).json()

        def ident(b):  # observed_at is the read time, so compare identity/order/cursor
            return ([(e["event_id"], e["occurred_at"], e["dedupe_key"]) for e in b["events"]],
                    b["next_cursor"], b["before_cursor_count"])
        assert ident(body) == ident(again)
        seen += [e["event_id"] for e in body["events"]]
        cursor = body["next_cursor"]
    assert seen == [e["event_id"] for e in full] and len(set(seen)) == 12
    last = client.get(f"/api/tasks/601/observability/events?limit=4&cursor={cursor}").json() if cursor else None
    assert last is None or last["events"] == []
    times = [e["occurred_at"] for e in full]
    assert times == sorted(times)
    assert client.get("/api/tasks/601/observability/events?cursor=garbage").status_code == 400
    assert client.get("/api/tasks/601/observability/events?cursor=5").status_code == 400
    assert _counts(tmp_path / "test.db") == before


# ---------------------------------------------------------------- C

def _pf_db(tmp_path):
    path = tmp_path / "pf.db"
    Database(path).init()
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("INSERT INTO tasks(id,slug,title) VALUES(9,'t9','t9')")
    db.executemany("""INSERT INTO agent_workspaces(id,repository_id,agent,task_name,branch,worktree_path,
                      base_branch,base_commit,task_id) VALUES(?,1,?,?,?,?,'main','abc',9)""",
                   [(31, "claude", "a", "agent/claude/a", "/w/a"), (32, "codex", "b", "agent/codex/b", "/w/b")])
    db.execute("""INSERT INTO agent_sessions(id,task_id,workspace_id,agent,command_profile,cwd,status,
                  started_at,exited_at,exit_code,transcript_tail)
                  VALUES(81,9,31,'claude','claude','/w/a','EXITED',?,?,0,'')""",
               ((NOW - timedelta(hours=3)).isoformat(), (NOW - timedelta(hours=2)).isoformat()))
    db.execute("""INSERT INTO agent_sessions(id,task_id,workspace_id,agent,command_profile,cwd,status,
                  started_at,last_activity_at,transcript_tail)
                  VALUES(82,9,32,'codex','codex','/w/b','RUNNING',?,?,'')""",
               ((NOW - timedelta(minutes=5)).isoformat(), (NOW - timedelta(seconds=20)).isoformat()))
    db.execute("""INSERT INTO review_runs(id,task_id,workspace_id,status,reviewed_commit,completed_at)
                  VALUES(1,9,31,'PASS','oldcommit',?)""", ((NOW - timedelta(hours=1)).isoformat(),))
    db.execute("""INSERT INTO test_runs(id,workspace_type,workspace_id,command,stage,status,finished_at,tested_commit)
                  VALUES(1,'agent',31,'pytest','test','PASS',?,'oldcommit')""", ((NOW - timedelta(hours=1)).isoformat(),))
    db.execute("INSERT INTO qa_runs(id,task_id,status,completed_at) VALUES(1,9,'PASS',?)",
               ((NOW - timedelta(minutes=50)).isoformat(),))
    db.commit()
    db.close()
    return path


def test_raw_evidence_is_not_relabeled_to_latest_session_and_keeps_workspace(tmp_path):
    raws, _ = obs.projectflow_task_events(_pf_db(tmp_path), task_id=9, node_id="n", project_id="pf")
    evid = {r["sanitized_evidence_ref"]: r for r in raws if r["source"] == "projectflow_evidence"}
    latest_session = "pf-session-82"
    assert all(r["session_id"] != latest_session for r in evid.values())
    assert evid["review_runs:1"]["session_id"] == "pf-workspace-31-evidence"
    assert evid["review_runs:1"]["worktree_ref"] == "pf-workspace-31"
    assert evid["test_runs:1"]["session_id"] == "pf-workspace-31-evidence"
    assert evid["qa_runs:1"]["session_id"] == "pf-task-9-evidence"
    assert {r["evidence_currency"] for r in evid.values()} == {"UNKNOWN"}


def test_raw_pass_rows_never_set_current_milestones(tmp_path):
    raws, _ = obs.projectflow_task_events(_pf_db(tmp_path), task_id=9, node_id="n", project_id="pf")
    v, _, _ = obs.view_from_raw(raws, at=NOW)
    assert not any(v["milestones"][m] for m in ("tests_passed", "review_passed", "qa_passed",
                                                "merged", "deployed", "accepted"))
    assert v["delivery_stage"] == "CODING"
    refs = {h["ref"]: h for h in v["historic_evidence"]}
    assert refs["review_runs:1"]["result"] == "PASS" and refs["review_runs:1"]["currency"] == "UNKNOWN"
    assert "evidence_currency_unverified" in v["reasons"]
    # without gate truth everything stays unverified, never PASS
    g = obs.apply_gate_truth(v, None)
    assert g["gates"]["status"] == "UNKNOWN" and not g["milestones"]["review_passed"]
    assert g["completion_percent"] is None


def _decision(review="done", tests="done", qa="done", merge="future", builders=None):
    return {"status": "ACTIVE", "checklist": [
        {"key": "TASK", "state": "done"}, {"key": "BUILDER", "state": "done"},
        {"key": "AUTOMATED_TESTS", "state": tests}, {"key": "REVIEW", "state": review},
        {"key": "RUNTIME_VERIFICATION", "state": qa}, {"key": "INTEGRATION", "state": "skipped"},
        {"key": "MERGE", "state": merge}],
        "builders": builders or [{"id": 31, "worktree_path": "/home/rag/secret-wt", "head": "newcommit123456",
                                  "review_status": "STALE"}]}


def test_old_commit_pass_new_commit_unverified_via_gate_truth(tmp_path):
    raws, _ = obs.projectflow_task_events(_pf_db(tmp_path), task_id=9, node_id="n", project_id="pf")
    v, _, _ = obs.view_from_raw(raws, at=NOW)
    # TaskDecisionService: review row pinned to oldcommit, HEAD is newcommit -> STALE
    truth = obs.gate_truth_from_decision(_decision(review="future", tests="future", qa="future"))
    g = obs.apply_gate_truth(v, truth)
    assert g["milestones"]["review_passed"] is False and g["milestones"]["tests_passed"] is False
    assert g["gates"]["review"] == "NOT_CURRENT" and g["gates"]["tests"] == "NOT_CURRENT"
    assert g["gates"]["workspaces"] == [{"workspace_ref": "pf-workspace-31", "review_status": "STALE",
                                         "head": "newcommit123"}]
    assert "/home/rag" not in json.dumps(g)
    assert g["delivery_stage"] == "CODING"
    ok = obs.apply_gate_truth(v, obs.gate_truth_from_decision(_decision()))
    assert ok["milestones"]["review_passed"] and ok["milestones"]["tests_passed"] and ok["milestones"]["qa_passed"]
    assert ok["delivery_stage"] == "QA" and ok["milestones"]["accepted"] is False
    assert ok["completion_percent"] is None


def test_not_required_gate_is_not_a_pass():
    v = obs.derive_task_view([], at=NOW)
    g = obs.apply_gate_truth(v, obs.gate_truth_from_decision(_decision(qa="skipped")))
    assert g["gates"]["qa"] == "NOT_REQUIRED" and g["milestones"]["qa_passed"] is False


def test_api_uses_task_decision_gate_truth(client, tmp_path):
    db = sqlite3.connect(tmp_path / "test.db")
    db.execute("INSERT INTO tasks(id,slug,title,status) VALUES(701,'t701','t','ACTIVE')")
    db.execute("INSERT INTO qa_runs(id,task_id,status,completed_at) VALUES(1,701,'PASS',?)",
               (datetime.now(timezone.utc).isoformat(),))
    db.commit()
    db.close()
    body = client.get("/api/tasks/701/observability").json()
    assert body["gates"]["source"] == "TaskDecisionService.evaluate"
    assert body["milestones"]["review_passed"] is False  # no builder -> never PASS
    assert any(h["ref"] == "qa_runs:1" for h in body["historic_evidence"])


# ---------------------------------------------------------------- D

def test_event_id_reused_with_different_dedupe_key_is_quarantined(store):
    assert store.ingest(ev("e1", "tool_call", 1, dedupe_key="k-one"), observed_at=NOW).status == "ACCEPTED"
    r = store.ingest(ev("e1", "tool_call", 1, dedupe_key="k-two"), observed_at=NOW)
    assert (r.status, r.reason) == ("QUARANTINED", "event_id_conflict")
    assert [e["dedupe_key"] for e in store.all_task_events("pf-task-7")] == ["k-one"]


def test_event_id_reused_with_different_data_is_quarantined_not_reassigned(store):
    store.ingest(ev("e1", "tool_call", 1), observed_at=NOW)
    r = store.ingest(ev("e1", "process_exited", 0.5, dedupe_key="fresh-key"), observed_at=NOW)
    assert r.reason == "event_id_conflict"
    r2 = store.ingest(ev("e1", "tool_call", 1, session_id="sess-other", agent_id="x"), observed_at=NOW)
    assert r2.status == "QUARANTINED"
    evs = store.all_task_events("pf-task-7")
    assert [(e["event_id"], e["kind"], e["session_id"]) for e in evs] == [("e1", "tool_call", "sess-new")]
    # exact replay is still a harmless duplicate
    assert store.ingest(ev("e1", "tool_call", 1), observed_at=NOW).status == "DUPLICATE"


def test_dedupe_key_reused_by_different_event_id_is_quarantined(store):
    store.ingest(ev("e1", "tool_call", 1, dedupe_key="shared"), observed_at=NOW)
    r = store.ingest(ev("e2", "tool_call", 1, dedupe_key="shared"), observed_at=NOW)
    assert r.status == "QUARANTINED"
    assert [e["event_id"] for e in store.all_task_events("pf-task-7")] == ["e1"]


def test_view_from_raw_applies_same_conflict_rules():
    raws = [ev("e1", "tool_call", 1, dedupe_key="k1"), ev("e1", "tool_call", 1, dedupe_key="k2"),
            ev("e2", "tool_call", 1, dedupe_key="k1"), ev("e1", "tool_call", 1, dedupe_key="k1")]
    v, events, q = obs.view_from_raw(raws, at=NOW)
    assert [e["event_id"] for e in events] == ["e1"] and q == 2


def test_view_from_raw_enforces_tenant_task_and_session_binding():
    expected = {"node_id": "nec1", "project_id": "projectflow", "task_id": "pf-task-7"}
    raws = [ev("ok", "tool_call", 1),
            ev("other-task", "tool_call", 1, task_id="pf-task-8"),
            ev("other-tenant", "tool_call", 1, project_id="otherorg"),
            ev("other-node", "tool_call", 1, node_id="nec2"),
            ev("relabel", "tool_call", 1, agent_id="impostor")]  # same session, other agent
    v, events, q = obs.view_from_raw(raws, at=NOW, expected=expected)
    assert [e["event_id"] for e in events] == ["ok"] and q == 4


def test_redaction_still_applies_to_new_fields():
    secret = "ghp_" + "Q1w2E3r4T5y6U7i8O9p0A1s2"
    raw = ev("e1", "review_result", 1, source="projectflow_evidence", provenance_level="LOCAL_VERIFIED",
             result="PASS", evidence_currency=secret)
    item = obs.normalize(raw, observed_at=NOW)
    assert isinstance(item, obs.NormalizedEvent)
    assert item.evidence_currency == "UNKNOWN" and secret not in json.dumps(item.to_public())
