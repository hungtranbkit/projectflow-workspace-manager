"""Track A1.25/A1.26 -- deterministic performance-oriented regression
tests. Deliberately NOT asserting exact milliseconds (brittle across
hosts/CI runners); instead asserting BOUNDED query/evaluation counts --
the actual root cause docs/PRODUCTIZATION_AUDIT.md's P0.18 named and
scripts/benchmark_changes_list.py reproduced live (100 Changes x 5
Tasks: ~140 TaskDecisionService.evaluate() calls and 1400+ DB
connections for GET /changes before Track A1's fix). See that script
for exact-millisecond before/after numbers (kept out of pytest on
purpose, per A1.25's own instruction)."""
from __future__ import annotations

from tests.test_autonomous_execution import register, new_change


def seed_changes(client, repo, n_changes, tasks_per_change=5):
    db = client.app.state.db
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": "demo", "default_branch": "main"})
    assert r.status_code in (200, 303), r.text
    pid = client.get("/api/repositories").json()[0]["id"]
    seq = 0
    ids = []
    for i in range(n_changes):
        cid = new_change(client, f"Change {i}", project_id=pid)
        ids.append(cid)
        client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "AGENTIC_STANDARD"})
        plan_id = db.execute(
            "INSERT INTO plans(change_id,revision,status,planner_provider,input_context_digest) VALUES(?,?,?,?,?)",
            (cid, 1, "MATERIALIZED", "claude", "x"))
        for j in range(tasks_per_change):
            seq += 1
            tid = db.execute(
                "INSERT INTO tasks(slug,title,status,change_id,task_type) VALUES(?,?,?,?,?)",
                (f"perf-{cid}-{j}-{seq}", f"Task {j}", "BACKLOG", cid, "IMPLEMENTATION"))
            db.execute(
                "INSERT INTO plan_items(plan_id,item_key,title,task_type,depends_on_keys,requirement_ids,scope_hints,materialized_task_id) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (plan_id, f"T{j}", f"Task {j}", "IMPLEMENTATION", "[]", "[]", "[]", tid))
    return ids


def test_task_decision_evaluate_is_memoized_within_one_evaluate_workflow_call(client, git_repo):
    """A1.4/A1.5: evaluate_workflow() must not re-run TaskDecisionService.
    evaluate()'s real DB work more than once per distinct Task -- before
    the fix it ran ~2.8x per Task (readiness(), _gate_tests_pass,
    _gate_release_ready, _gate_review_pass, _gate_security_pass, and the
    final blocked_tasks check each called it fresh)."""
    root, repo = git_repo
    seed_changes(client, repo, 1, tasks_per_change=5)
    cid = client.get("/api/changes").json()[0]["id"]
    decision = client.app.state.decision
    calls = {"n": 0}
    orig = decision._evaluate_uncached
    def counting(task_id):
        calls["n"] += 1
        return orig(task_id)
    decision._evaluate_uncached = counting
    try:
        client.app.state.workflow_service.evaluate_workflow(cid)
    finally:
        decision._evaluate_uncached = orig
    # 5 Tasks -> at most 5 REAL evaluations inside one evaluate_workflow()
    # call, never one per (Task x gate).
    assert calls["n"] <= 5, f"evaluate() re-ran real work {calls['n']} times for 5 Tasks -- memoization regressed"


def test_evaluate_many_matches_individual_evaluate(client, git_repo):
    """A1.4: evaluate_many() must return exactly what evaluate(task_id)
    returns individually -- never a second, competing decision
    implementation."""
    root, repo = git_repo
    ids = seed_changes(client, repo, 1, tasks_per_change=5)
    task_ids = [t["id"] for t in client.app.state.db.all("SELECT id FROM tasks WHERE change_id=?", (ids[0],))]
    decision = client.app.state.decision
    individually = {tid: decision.evaluate(tid) for tid in task_ids}
    batched = decision.evaluate_many(task_ids)
    assert batched == individually


def test_changes_page_query_count_bounded_at_scale(client, git_repo):
    """A1.1/A1.25: GET /changes must not scale query count linearly
    without bound as the Change count grows well past one page --
    catches a regression back to the pre-A1 per-row full-workflow
    evaluation for every row on every request. Compares 10 Changes vs
    100 Changes (default page_size=25): connection count for 100 must
    stay well under 10x the 10-Change count (pre-fix it was ~10x
    exactly, i.e. no batching/pagination benefit at all)."""
    root, repo = git_repo
    db = client.app.state.db

    def connects_for(n):
        seed_changes(client, repo, n, tasks_per_change=5)
        counts = {"n": 0}
        orig = db.connect
        def counting():
            counts["n"] += 1
            return orig()
        db.connect = counting
        try:
            r = client.get("/changes")
            assert r.status_code == 200
        finally:
            db.connect = orig
        return counts["n"]

    small = connects_for(10)
    # more Changes materialize (raw sqlite inserts above, not through
    # client.post) between calls -- re-measure against the now-larger set
    large = connects_for(90)  # brings the total to 100
    assert large < small * 5, f"connect count did not stay bounded: 10 Changes={small}, 100 Changes={large}"


def test_changes_page_pagination_bounds_page_rows(client, git_repo):
    """A1.8: default page_size caps rendered/deeply-composed rows even
    when the Change set is much larger."""
    root, repo = git_repo
    seed_changes(client, repo, 40, tasks_per_change=2)
    r = client.get("/changes")
    assert r.status_code == 200
    summary = client.app.state.change_list_summary_service.build()
    assert summary["total"] == 40
    assert len(summary["rows"]) == summary["page_size"] == 25
    assert summary["total_pages"] == 2

    r2 = client.app.state.change_list_summary_service.build(page=2)
    assert len(r2["rows"]) == 15


def test_changes_page_filters_preserved_with_pagination(client, git_repo):
    """A1.8: change_type/profile filters (cheap, no evaluate_workflow
    needed) still apply correctly across the FULL Change set, not just
    the current page."""
    root, repo = git_repo
    ids = seed_changes(client, repo, 5, tasks_per_change=1)
    db = client.app.state.db
    db.execute("UPDATE changes SET change_type='BUG' WHERE id=?", (ids[0],))
    summary = client.app.state.change_list_summary_service.build(change_type="BUG")
    assert summary["total"] == 1
    assert summary["rows"][0]["id"] == ids[0]
