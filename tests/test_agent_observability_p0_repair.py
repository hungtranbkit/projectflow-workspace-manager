"""Agent observability P0 repair (PR #56 review follow-up).

1. Task-bounded SQL evidence reads (large mixed-task data, index use,
   truncation, empty task, older schema).
2. Harness gate/TTL bridge only through an explicit reviewed link + the
   existing progress snapshot file; otherwise UNKNOWN (never 0%).
3. Operator page: two independent meters / UNKNOWN text, no leaks.
4. GET never writes (app DB row counts, Harness files untouched).
5. Cross-tenant 404 under AUTH_MODE=required.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import sqlite3

import pytest

from app.db import Database
from app.services import agent_observability as obs
from app.services.agent_harness_mvp import HarnessStore
from test_b03_authz import (  # noqa: F401  (pytest fixtures)
    auth_client, captured_logs, two_org_fixture,
)

NOW = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)
SECRET = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8"
ZGH = "Automation_eec08a5752d08191b3de371065c27320"  # enabled in config/harness_monitor_registry.json
PAUSED = None  # resolved from registry in fixture


def _iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


# ---------------------------------------------------------------- 1. bounded reads

@pytest.fixture
def big_db(tmp_path):
    """Real app schema (all migrations, incl. idx_agent_sessions_task),
    60 tasks with interleaved sessions/evidence; task 7 is the target."""
    path = tmp_path / "pf.db"
    Database(path).init()
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys=OFF")
    db.executemany("INSERT INTO tasks(id,slug,title) VALUES(?,?,?)",
                   [(t, f"t{t}", f"Task {t}") for t in range(1, 61)])
    db.executemany(
        """INSERT INTO agent_workspaces(id,repository_id,agent,task_name,branch,worktree_path,
           base_branch,base_commit,task_id) VALUES(?,?,?,?,?,?,?,?,?)""",
        [(t, 1, "claude", f"t{t}", f"agent/claude/t{t}", f"/home/rag/wt/{t}", "main", "abc", t)
         for t in range(1, 61)])
    sessions, reviews, qas, tests = [], [], [], []
    for i in range(3600):  # 60 rows per task: above the 50-session bound
        t = (i % 60) + 1
        sessions.append((i + 1, t, t, "claude", "claude", f"/home/rag/wt/{t}", "EXITED",
                         _iso(600 - i * 0.1), _iso(500), _iso(400), 0, "TOKEN=" + SECRET))
        reviews.append((i + 1, t, t, "PASS" if t != 7 else ("FIX_REQUIRED" if i % 2 else "PASS"),
                        "findings " + SECRET, _iso(300 - i * 0.05)))
        qas.append((i + 1, t, t, "PASS", "notes password=x", _iso(200 - i * 0.05)))
        tests.append((i + 1, "agent", t, "pytest -k secret", "test", "PASS", _iso(100 - i * 0.01), "stdout"))
    db.executemany("""INSERT INTO agent_sessions(id,task_id,workspace_id,agent,command_profile,cwd,status,
                      started_at,last_activity_at,exited_at,exit_code,transcript_tail)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", sessions)
    db.executemany("""INSERT INTO review_runs(id,task_id,workspace_id,status,findings,completed_at)
                      VALUES(?,?,?,?,?,?)""", reviews)
    db.executemany("""INSERT INTO qa_runs(id,task_id,workspace_id,status,notes,completed_at)
                      VALUES(?,?,?,?,?,?)""", qas)
    db.executemany("""INSERT INTO test_runs(id,workspace_type,workspace_id,command,stage,status,finished_at,stdout_tail)
                      VALUES(?,?,?,?,?,?,?,?)""", tests)
    db.execute("""INSERT INTO merge_records(id,task_id,repository_id,merge_status,merged_at)
                  VALUES(1,8,1,'MERGED',?)""", (_iso(5),))  # other task merged
    db.commit()
    db.close()
    return path


def _expected_ids(path, sql, args):
    db = sqlite3.connect(path)
    try:
        return {r[0] for r in db.execute(sql, args)}
    finally:
        db.close()


def test_bounded_read_never_leaks_other_tasks(big_db):
    raws, meta = obs.projectflow_task_events(big_db, task_id=7, node_id="nec1", project_id="pf")
    assert {r["task_id"] for r in raws} == {"pf-task-7"}
    own = {
        "review_runs": _expected_ids(big_db, "SELECT id FROM review_runs WHERE task_id=7", ()),
        "qa_runs": _expected_ids(big_db, "SELECT id FROM qa_runs WHERE task_id=7", ()),
        "test_runs": _expected_ids(big_db, "SELECT id FROM test_runs WHERE workspace_id=7", ()),
        "agent_sessions": _expected_ids(big_db, "SELECT id FROM agent_sessions WHERE task_id=7", ()),
    }
    for r in raws:
        ref = r.get("sanitized_evidence_ref")
        if ref:
            table, rid = ref.split(":")
            assert int(rid) in own[table], ref
        else:
            assert int(r["session_id"].split("-")[-1]) in own["agent_sessions"]
    assert not any(r["kind"] == "merge_recorded" for r in raws)  # task 8's merge never appears
    blob = json.dumps(raws)
    for leaked in (SECRET, "/home/rag", "password=x", "pytest -k", "findings"):
        assert leaked not in blob
    assert meta["session_count"] == 50 and "agent_sessions" in meta["truncated"]
    assert meta["workspace_count"] == 1


def test_bounded_read_truncates_and_uses_indices(big_db, monkeypatch):
    seen: list[str] = []
    real = obs._ro_connect

    def traced(path):
        db = real(path)
        db.set_trace_callback(seen.append)
        return db
    monkeypatch.setattr(obs, "_ro_connect", traced)
    raws, meta = obs.projectflow_task_events(big_db, task_id=7, node_id="nec1", project_id="pf",
                                             evidence_limit=20)
    assert {"review_runs", "qa_runs", "test_runs"} <= set(meta["truncated"])
    assert sum(r["kind"] == "review_result" for r in raws) == 20
    selects = [s for s in seen if s.lstrip().upper().startswith("SELECT")
               and "sqlite_master" not in s]
    assert selects
    db = sqlite3.connect(big_db)
    try:
        for sql in selects:
            assert "LIMIT" in sql.upper(), sql
            plan = " ".join(r[3] for r in db.execute("EXPLAIN QUERY PLAN " + sql))
            assert "SCAN " not in plan, (sql, plan)  # no full-table scan anywhere
            assert plan.startswith("SEARCH ") and "INDEX" in plan, (sql, plan)
    finally:
        db.close()
    # test_runs (no task_id column) is bounded by this task's own workspace ids
    assert any("FROM test_runs" in s and "workspace_id IN" in s for s in selects)


def test_empty_task_is_queued_unknown_not_error(big_db):
    raws, meta = obs.projectflow_task_events(big_db, task_id=9999, node_id="nec1", project_id="pf")
    assert raws == [] and meta["session_count"] == 0
    view, events, q = obs.view_from_raw(raws, at=NOW)
    assert (view["dispatch"], view["execution"], view["completion_percent"]) == ("QUEUED", "UNKNOWN", None)


def test_evidence_without_session_stays_on_its_own_task(tmp_path):
    path = tmp_path / "pf.db"
    Database(path).init()
    db = sqlite3.connect(path)
    db.execute("INSERT INTO tasks(id,slug,title) VALUES(3,'t3','t3')")
    db.execute("INSERT INTO qa_runs(id,task_id,status,completed_at) VALUES(1,3,'PASS',?)", (_iso(1),))
    db.commit()
    db.close()
    raws, _ = obs.projectflow_task_events(path, task_id=3, node_id="n", project_id="pf")
    assert [r["session_id"] for r in raws] == ["pf-task-3-evidence"]
    view, _, _ = obs.view_from_raw(raws, at=NOW)
    # PR #56 fix C: a raw PASS row is history, never a current milestone.
    assert not view["milestones"]["qa_passed"] and view["execution"] == "UNKNOWN"
    assert [h["ref"] for h in view["historic_evidence"]] == ["qa_runs:1"]


def test_older_schema_is_skipped_not_guessed(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE agent_sessions(id INTEGER PRIMARY KEY, task_id INTEGER, workspace_id INTEGER,"
               " agent TEXT, status TEXT, started_at TEXT, last_activity_at TEXT, exited_at TEXT, exit_code INTEGER)")
    db.execute("CREATE TABLE qa_runs(id INTEGER PRIMARY KEY, task_id INTEGER, status TEXT)")  # no completed_at
    db.commit()
    db.close()
    raws, meta = obs.projectflow_task_events(path, task_id=1, node_id="n", project_id="pf")
    assert raws == []
    assert {"qa_runs", "review_runs", "merge_records", "deployments", "test_runs"} <= set(meta["skipped_sources"])


@pytest.mark.parametrize("bad", [0, -1, "7", None, 7.0, True])
def test_task_id_must_be_positive_int(big_db, bad):
    with pytest.raises(obs.ObservabilityError):
        obs.projectflow_task_events(big_db, task_id=bad, node_id="n", project_id="pf")


# ---------------------------------------------------------------- 2. Harness bridge

def _registry():
    path = obs.DEFAULT_HARNESS_REGISTRY
    data = json.loads(path.read_text())
    paused = next(a["automation_id"] for a in data["automations"] if not a.get("configured_enabled"))
    return path, paused


@pytest.fixture
def harness(tmp_path):
    """A real Harness DB + real summarize() snapshot written to a file, as
    the existing timer's --write would."""
    from scripts.harness_monitor_bridge import bootstrap, load_registry
    from scripts import harness_progress as progress
    hdb = tmp_path / "harness.db"
    store = HarnessStore(hdb)
    registry = load_registry()
    bootstrap(registry, hdb)
    task = store.create(key="obs-ttl-1", main_goal=registry[ZGH]["main_goal"],
                        child_goal="Atomic observability child", dod="independent pass",
                        agent="claude", node="nec1", created_at=NOW - timedelta(hours=6),
                        ttl=timedelta(hours=24))
    snap = progress.summarize(hdb, reference=NOW)
    snap["health"] = {"status": "HEALTHY", "observed_at": NOW.isoformat()}
    snap_path = tmp_path / "monitor-progress.json"
    snap_path.write_text(json.dumps(snap))
    expected = next(m for m in snap["monitors"] if m["id"] == ZGH)
    return {"db": hdb, "snap": snap, "snap_path": snap_path, "task": task, "expected": expected}


def _links(tmp_path, entries, name="links.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"schema_version": 1, "links": entries}))
    return p


def _link(**kw):
    base = {"pf_task_id": 7, "automation_id": ZGH, "harness_task_id": None,
            "proof_ref": "pr:56", "reviewed_on": "2026-11-20"}
    base.update(kw)
    return base


def _view():
    return obs.derive_task_view([], at=NOW)


def test_no_link_file_means_unknown_not_zero(harness):
    v = obs.harness_view_for_task(_view(), task_id=7, at=NOW, snapshot_path=harness["snap_path"],
                                  links_path=None)
    assert v["completion_percent"] is None
    assert v["completion_source"].startswith("UNKNOWN") and "link" in v["completion_source"]
    assert v["time_budget"]["status"] == "UNKNOWN"


def test_valid_link_copies_harness_percent_and_ttl(harness, tmp_path):
    links = _links(tmp_path, [_link(harness_task_id=harness["task"].id)])
    v = obs.harness_view_for_task(_view(), task_id=7, at=NOW, snapshot_path=harness["snap_path"],
                                  links_path=links)
    assert v["completion_percent"] == harness["expected"]["completion_percent"]
    tb = v["time_budget"]
    assert tb["status"] == "KNOWN" and tb["time_used_percent"] == 25  # 6h of 24h
    assert v["harness_link"] == {"automation_id": ZGH, "harness_task_id": harness["task"].id,
                                 "proof_ref": "pr:56"}
    # another task with no link stays UNKNOWN even with the same snapshot
    other = obs.harness_view_for_task(_view(), task_id=8, at=NOW, snapshot_path=harness["snap_path"],
                                      links_path=links)
    assert other["completion_percent"] is None


def test_stale_snapshot_is_unknown_with_blocker(harness, tmp_path):
    links = _links(tmp_path, [_link(harness_task_id=harness["task"].id)])
    v = obs.harness_view_for_task(_view(), task_id=7, at=NOW + timedelta(hours=1),
                                  snapshot_path=harness["snap_path"], links_path=links)
    assert v["completion_percent"] is None and "HARNESS_EVIDENCE_STALE" in v["blockers"]
    assert v["time_budget"]["status"] == "UNKNOWN"


def test_unhealthy_snapshot_is_unknown(harness, tmp_path):
    snap = dict(harness["snap"], health={"status": "FAILED_OR_STALE"})
    harness["snap_path"].write_text(json.dumps(snap))
    links = _links(tmp_path, [_link()])
    v = obs.harness_view_for_task(_view(), task_id=7, at=NOW, snapshot_path=harness["snap_path"],
                                  links_path=links)
    assert v["completion_percent"] is None


@pytest.mark.parametrize("entries,needle", [
    ([_link(automation_id="Automation_doesnotexist0000")], "automation_id"),
    ([_link(), _link()], "duplicate"),
    ([_link(proof_ref="")], "proof_ref"),
    ([_link(proof_ref="pr:" + SECRET)], "proof_ref"),
    ([_link(reviewed_on="yesterday")], "reviewed_on"),
    ([_link(pf_task_id="7")], "pf_task_id"),
    ([_link(harness_task_id=-3)], "harness_task_id"),
])
def test_invalid_link_file_fails_closed(harness, tmp_path, entries, needle):
    links = _links(tmp_path, entries)
    result, why = obs.load_task_links(links)
    assert result == {} and needle in why
    v = obs.harness_view_for_task(_view(), task_id=7, at=NOW, snapshot_path=harness["snap_path"],
                                  links_path=links)
    assert v["completion_percent"] is None and SECRET not in json.dumps(v)


def test_link_to_paused_monitor_is_refused(harness, tmp_path):
    _, paused = _registry()
    links = _links(tmp_path, [_link(automation_id=paused)])
    assert obs.load_task_links(links)[0] == {}


def test_harness_child_not_in_linked_monitor_is_ttl_unknown(harness, tmp_path):
    links = _links(tmp_path, [_link(harness_task_id=987654)])
    v = obs.harness_view_for_task(_view(), task_id=7, at=NOW, snapshot_path=harness["snap_path"],
                                  links_path=links)
    assert v["completion_percent"] == harness["expected"]["completion_percent"]
    assert v["time_budget"]["status"] == "UNKNOWN" and "not in this monitor" in v["time_budget"]["reason"]


@pytest.mark.parametrize("content", ["{not json", json.dumps({"schema": "other", "monitors": []}),
                                     json.dumps([1, 2])])
def test_malformed_snapshot_is_unknown(harness, tmp_path, content):
    bad = tmp_path / "bad.json"
    bad.write_text(content)
    links = _links(tmp_path, [_link()])
    v = obs.harness_view_for_task(_view(), task_id=7, at=NOW, snapshot_path=bad, links_path=links)
    assert v["completion_percent"] is None and v["time_budget"]["status"] == "UNKNOWN"


def test_symlinked_snapshot_is_refused(harness, tmp_path):
    link = tmp_path / "snap-link.json"
    os.symlink(harness["snap_path"], link)
    assert obs.load_harness_snapshot(link)[0] is None


def test_snapshot_read_does_not_touch_harness_db(harness, tmp_path):
    before = hashlib.sha256(harness["db"].read_bytes()).hexdigest()
    links = _links(tmp_path, [_link(harness_task_id=harness["task"].id)])
    obs.harness_view_for_task(_view(), task_id=7, at=NOW, snapshot_path=harness["snap_path"],
                              links_path=links)
    assert hashlib.sha256(harness["db"].read_bytes()).hexdigest() == before


def test_maintenance_monitor_reports_na_not_zero(harness, tmp_path):
    snap = harness["snap"]
    maint = next(m for m in snap["monitors"] if m["kind"] == "maintenance")
    links = _links(tmp_path, [_link(automation_id=maint["id"])])
    v = obs.harness_view_for_task(_view(), task_id=7, at=NOW, snapshot_path=harness["snap_path"],
                                  links_path=links)
    assert v["completion_percent"] is None and v["completion_source"].startswith("N/A")


# ---------------------------------------------------------------- 3/4. API + page

def _seed(tmp_path, tid=501):
    db = sqlite3.connect(tmp_path / "test.db")
    db.execute("INSERT INTO tasks(id,slug,title) VALUES(?,?,?)", (tid, f"obs-{tid}", "Obs task"))
    now = datetime.now(timezone.utc)
    db.execute("""INSERT INTO agent_sessions(id,task_id,workspace_id,agent,command_profile,cwd,status,
                  started_at,last_activity_at,transcript_tail) VALUES(?,?,77,'claude','claude',
                  '/home/rag/private-wt','RUNNING',?,?,?)""",
               (tid + 400, tid, (now - timedelta(minutes=5)).isoformat(),
                (now - timedelta(seconds=30)).isoformat(), "TOKEN=" + SECRET))
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


def test_api_without_link_shows_unknown_and_never_zero_percent(client, tmp_path, monkeypatch):
    monkeypatch.delenv("PROJECTFLOW_OBS_HARNESS_LINKS", raising=False)
    monkeypatch.setenv("PROJECTFLOW_HARNESS_PROGRESS_JSON", str(tmp_path / "absent.json"))
    _seed(tmp_path)
    before = _counts(tmp_path / "test.db")
    body = client.get("/api/tasks/501/observability").json()
    assert body["completion_percent"] is None
    assert body["completion_source"].startswith("UNKNOWN")
    assert body["time_budget"]["status"] == "UNKNOWN"
    assert body["read_bounds"]["task_id"] == 501
    page = client.get("/tasks/501/observability").text
    assert "UNKNOWN" in page and "0% gate" not in page
    assert 'class="meter m-done"' not in page and 'class="meter m-ttl"' not in page
    assert _counts(tmp_path / "test.db") == before
    assert SECRET not in page and "/home/rag" not in page


def test_api_with_reviewed_link_shows_both_meters(client, tmp_path, monkeypatch, harness):
    _seed(tmp_path)
    links = _links(tmp_path, [_link(pf_task_id=501, harness_task_id=harness["task"].id)])
    fresh = dict(harness["snap"], observed_at=datetime.now(timezone.utc).isoformat())
    harness["snap_path"].write_text(json.dumps(fresh))
    monkeypatch.setenv("PROJECTFLOW_OBS_HARNESS_LINKS", str(links))
    monkeypatch.setenv("PROJECTFLOW_HARNESS_PROGRESS_JSON", str(harness["snap_path"]))
    snap_before = hashlib.sha256(harness["snap_path"].read_bytes()).hexdigest()
    hdb_before = hashlib.sha256(harness["db"].read_bytes()).hexdigest()
    counts = _counts(tmp_path / "test.db")
    body = client.get("/api/tasks/501/observability").json()
    assert body["completion_percent"] == harness["expected"]["completion_percent"]
    assert body["time_budget"]["status"] == "KNOWN"
    assert body["execution"] == "RUNNING_VERIFIED"
    page = client.get("/tasks/501/observability").text
    assert 'class="meter m-done"' in page and 'class="meter m-ttl"' in page
    assert "LOCAL_OBSERVED" in page  # provenance shown as text
    assert hashlib.sha256(harness["snap_path"].read_bytes()).hexdigest() == snap_before
    assert hashlib.sha256(harness["db"].read_bytes()).hexdigest() == hdb_before
    assert _counts(tmp_path / "test.db") == counts


def test_activity_burst_does_not_change_api_percent(client, tmp_path, monkeypatch, harness):
    _seed(tmp_path)
    links = _links(tmp_path, [_link(pf_task_id=501)])
    harness["snap_path"].write_text(json.dumps(dict(harness["snap"],
                                    observed_at=datetime.now(timezone.utc).isoformat())))
    monkeypatch.setenv("PROJECTFLOW_OBS_HARNESS_LINKS", str(links))
    monkeypatch.setenv("PROJECTFLOW_HARNESS_PROGRESS_JSON", str(harness["snap_path"]))
    first = client.get("/api/tasks/501/observability").json()["completion_percent"]
    db = sqlite3.connect(tmp_path / "test.db")
    now = datetime.now(timezone.utc).isoformat()
    db.executemany("""INSERT INTO agent_sessions(task_id,workspace_id,agent,command_profile,cwd,status,
                      started_at,last_activity_at) VALUES(501,77,'claude','claude','x','RUNNING',?,?)""",
                   [(now, now)] * 40)
    db.commit()
    db.close()
    assert client.get("/api/tasks/501/observability").json()["completion_percent"] == first


def test_routes_are_get_only(client):
    paths = {"/api/tasks/{tid}/observability", "/api/tasks/{tid}/observability/events",
             "/tasks/{tid}/observability"}
    for route in client.app.routes:
        if getattr(route, "path", None) in paths:
            assert route.methods <= {"GET", "HEAD"}, route.path


def test_page_has_no_fixed_width_overflow_sources():
    view = obs.harness_view_for_task(obs.derive_task_view([], at=NOW), task_id=1, at=NOW,
                                     snapshot_path=None, links_path=None)
    page = obs.render_operator_page(1, view, [])
    assert 'name="viewport" content="width=device-width,initial-scale=1"' in page
    assert "overflow-wrap:anywhere" in page and "minmax(0,1fr)" in page
    import re
    assert not re.search(r"(?<!max-)width:\s*\d{3,}px", page)  # no fixed wide elements


# ---------------------------------------------------------------- 5. cross-tenant

def test_cross_org_observability_is_404(two_org_fixture):
    f = two_org_fixture
    for path in (f"/api/tasks/{f['tid_b']}/observability",
                 f"/api/tasks/{f['tid_b']}/observability/events",
                 f"/tasks/{f['tid_b']}/observability"):
        denied = f["member"].get(path)
        assert denied.status_code == 404, (path, denied.text)
        assert f["outsider"].get(path).status_code == 200, path


def test_unauthenticated_observability_is_401(two_org_fixture):
    from fastapi.testclient import TestClient
    fresh = TestClient(two_org_fixture["owner"].app)
    assert fresh.get(f"/api/tasks/{two_org_fixture['tid_a']}/observability").status_code == 401


# ---------------------------------------------------------------- 6. T004 draft FeatureSpec

def test_draft_featurespec_is_valid_unique_draft_and_gated(tmp_path):
    import shutil
    from pathlib import Path
    from app.services.spec_gate import SpecGate
    from app.services.spec_registry import SpecRegistry
    repo = Path(__file__).resolve().parents[1]
    draft = repo / "docs/spec-drafts/agent-observability-recovery.yaml"
    real_before = SpecRegistry(repo / "specs").load().baseline_digest()
    tree = tmp_path / "specs"
    shutil.copytree(repo / "specs", tree)
    shutil.copy(draft, tree / "features" / "agent-observability-recovery.yaml")
    reg = SpecRegistry(tree).load()  # raises on duplicate ids / missing fields
    feat = reg.feature("FEAT-AGENT-OBSERVABILITY")
    assert feat["status"] == "draft"
    assert {"REQ-AO-001", "AC-AO-001", "INV-AO-001"} <= set(reg.requirements) | set(reg.acceptance) | set(reg.invariants)
    outcome = SpecGate(tree).evaluate({
        "spec_change_classification": "NEW_FEATURE", "spec_feature_id": "FEAT-AGENT-OBSERVABILITY",
        "spec_requirement_ids": json.dumps(["REQ-AO-001"]), "spec_acceptance_ids": json.dumps(["AC-AO-001"]),
        "spec_invariant_ids": "[]"})
    assert outcome["outcome"] == "SPEC_NOT_APPROVED"
    # the canonical tree is untouched: draft lives outside specs/
    assert SpecRegistry(repo / "specs").load().baseline_digest() == real_before
    assert "FEAT-AGENT-OBSERVABILITY" not in SpecRegistry(repo / "specs").load().features
