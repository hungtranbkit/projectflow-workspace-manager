#!/usr/bin/env python3
"""Track A1 (A1.1/A1.29) -- deterministic, disposable-fixture benchmark for
GET /changes. Not a pytest file on purpose (A1.25's own rule: assert
bounded behavior in tests, keep exact-millisecond numbers in a separate,
non-brittle benchmark) -- run directly:

    python3 scripts/benchmark_changes_list.py [10 25 50 100 250]

Seeds N disposable Changes x 5 Tasks each (BACKLOG, the common "not yet
fully built" case that dominates a real /changes page) via the real
service layer (ChangeService/materialize_task-equivalent raw inserts,
same pattern tests/test_autonomous_execution.py's own materialize_task
uses), in a throwaway sqlite DB under tmp, then times GET /changes and
reports DB connection count (Database.connect() call count, a direct
proxy for query count under this codebase's one-connection-per-call
design) plus TaskDecisionService.evaluate()/WorkflowService.
evaluate_workflow() call counts for that one request.

Prints a plain before/after-shaped table; this script is meant to be
run once before a change and once after, numbers pasted into the A1
final report by hand -- it makes no PASS/FAIL claim itself."""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.db import Database


def make_repo(root: Path) -> Path:
    repo = root / "demo"
    repo.mkdir(parents=True)
    def run(*a): subprocess.run(list(a), cwd=repo, check=True, capture_output=True, text=True)
    run("git", "init", "-b", "main")
    run("git", "config", "user.email", "bench@example.invalid")
    run("git", "config", "user.name", "Bench")
    (repo / "README.md").write_text("base\n")
    (repo / "PROJECT.yaml").write_text(
        "schema_version: 1\nproject: {code: demo}\nsource: {root: .}\n"
        "commands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n")
    run("git", "add", ".")
    run("git", "commit", "-m", "base")
    return repo


def seed(client, repo: Path, n_changes: int, tasks_per_change: int = 5):
    db = client.app.state.db
    r = client.post("/api/repositories", data={"repo_path": str(repo),
                                                 "repo_name": "demo", "default_branch": "main"})
    assert r.status_code in (200, 303), r.text
    pid = client.get("/api/repositories").json()[0]["id"]
    seq = 0
    for i in range(n_changes):
        cid = client.post("/api/changes", data={"title": f"Change {i}", "project_id": str(pid)}).json()["id"]
        client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "AGENTIC_STANDARD"})
        plan_id = db.execute(
            "INSERT INTO plans(change_id,revision,status,planner_provider,input_context_digest) VALUES(?,?,?,?,?)",
            (cid, 1, "MATERIALIZED", "claude", "x"))
        for j in range(tasks_per_change):
            seq += 1
            tid = db.execute(
                "INSERT INTO tasks(slug,title,status,change_id,task_type) VALUES(?,?,?,?,?)",
                (f"bench-{cid}-{j}-{seq}", f"Task {j}", "BACKLOG", cid, "IMPLEMENTATION"))
            db.execute(
                "INSERT INTO plan_items(plan_id,item_key,title,task_type,depends_on_keys,requirement_ids,scope_hints,materialized_task_id) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (plan_id, f"T{j}", f"Task {j}", "IMPLEMENTATION", "[]", "[]", "[]", tid))
    db.execute("UPDATE changes SET updated_at=CURRENT_TIMESTAMP")


def bench(n: int, warm: bool = True):
    tmp = Path(tempfile.mkdtemp(prefix="pf-bench-"))
    try:
        root = tmp / "root"
        repo = make_repo(root)
        settings = Settings(root, "127.0.0.1", 8765, tmp / "bench.db", 30, configured_state_dir=tmp / "state")
        client = TestClient(create_app(settings))
        client.app.state.sandboxes.spawn = lambda fn, args=(): fn(*args)
        client.app.state.deployer.spawn = lambda fn, args=(): fn(*args)

        t0 = time.perf_counter()
        seed(client, repo, n)
        seed_s = time.perf_counter() - t0

        # instrument counts for the timed request only
        db: Database = client.app.state.db
        decision = client.app.state.decision
        workflow_service = client.app.state.workflow_service
        counts = {"connect": 0, "evaluate": 0, "evaluate_workflow": 0}
        orig_connect = db.connect
        orig_evaluate = decision.evaluate
        orig_eval_wf = workflow_service.evaluate_workflow

        def counting_connect():
            counts["connect"] += 1
            return orig_connect()
        def counting_evaluate(task_id):
            counts["evaluate"] += 1
            return orig_evaluate(task_id)
        def counting_eval_wf(change_id):
            counts["evaluate_workflow"] += 1
            return orig_eval_wf(change_id)

        if warm:
            client.get("/changes")  # let any first-hit lazy init happen outside the timed sample

        db.connect = counting_connect
        decision.evaluate = counting_evaluate
        workflow_service.evaluate_workflow = counting_eval_wf
        t0 = time.perf_counter()
        resp = client.get("/changes")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert resp.status_code == 200, resp.text
        db.connect = orig_connect
        decision.evaluate = orig_evaluate
        workflow_service.evaluate_workflow = orig_eval_wf

        return {"n": n, "seed_s": round(seed_s, 2), "get_changes_ms": round(elapsed_ms, 1), **counts}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    sizes = [int(a) for a in sys.argv[1:]] or [10, 25, 50, 100]
    rows = [bench(n) for n in sizes]
    header = f"{'N':>5} {'seed_s':>8} {'GET /changes ms':>16} {'db.connect()':>14} {'evaluate()':>12} {'evaluate_workflow()':>20}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(f"{r['n']:>5} {r['seed_s']:>8} {r['get_changes_ms']:>16} {r['connect']:>14} {r['evaluate']:>12} {r['evaluate_workflow']:>20}")
    print(json.dumps(rows))


if __name__ == "__main__":
    main()
