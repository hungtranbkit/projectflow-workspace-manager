"""Baseline failure evidence + gate waivers (Task #5 demo gaps 10-21):
real reproduction against a real base commit in a disposable detached
worktree, real fingerprint matching, real READY_FOR_MAIN semantics
(PASS vs PASS_WITH_APPROVED_BASELINE_WAIVER vs blocked). No Docker
needed -- the disposable repo's 'test' gate is a plain, fast pytest
command against a tiny probe test file, not a sandbox."""
from __future__ import annotations
import subprocess
import time

import pytest


def run(path, *args):
    return subprocess.run(list(args), cwd=path, text=True, capture_output=True, check=True)


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


def make_repo(root, name, broken=True, second_broken=False):
    """A disposable repo whose PROJECT.yaml 'test' gate is a fast,
    deterministic pytest run against tests_probe/ -- `broken` controls
    whether test_known_broken fails; `second_broken` adds a SECOND,
    always-different failing test for the mixed-failures scenario."""
    repo = root / name
    repo.mkdir(parents=True)
    run(repo, "git", "init", "-b", "main")
    run(repo, "git", "config", "user.email", "t@t")
    run(repo, "git", "config", "user.name", "t")
    (repo / "PROJECT.yaml").write_text(
        "schema_version: 1\nproject: {code: " + name + "}\nsource: {root: .}\n"
        "commands:\n  preflight: {command: 'true'}\n  test: {command: 'python3 -B -m pytest -q -p no:cacheprovider tests_probe'}\n"
        "ci: {required: [preflight, test]}\n"
    )
    (repo / "tests_probe").mkdir()
    body = "def test_known_broken():\n"
    body += "    pass\n" if not broken else "    assert False, 'always broken, unrelated to any task'\n"
    if second_broken:
        body += "\n\ndef test_second_thing():\n    assert False, 'a brand new, task-caused failure'\n"
    (repo / "tests_probe" / "test_x.py").write_text(body)
    (repo / "README.md").write_text("x\n")
    run(repo, "git", "add", ".")
    run(repo, "git", "commit", "-m", "base")
    return repo


def build_task_with_integration(client, root, name, broken=True, second_broken=False):
    repo = make_repo(root, name, broken=broken, second_broken=second_broken)
    register(client, repo, name)
    rid = next(r["id"] for r in client.get("/api/repositories").json() if r["repo_name"] == name)
    r = client.post("/api/tasks/create", data={"title": f"{name} task", "repository_id": rid, "agent": "codex", "sandbox_profile": "NONE", "risk_profile": "NORMAL"}, follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "PASS"})
    assert client.post(f"/api/tasks/{tid}/integrations", follow_redirects=False).status_code == 303
    iid = client.get("/api/integrations").json()[0]["id"]
    return tid, iid


def run_integration_test_and_wait(client, iid, timeout=15):
    client.post(f"/api/integrations/{iid}/test")
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.app.state.db.one("SELECT status FROM integration_workspaces WHERE id=?", (iid,))["status"]
        if st != "TESTING":
            return st
        time.sleep(0.05)
    raise AssertionError("integration test did not finish")


def wait_evidence(client, repository_id, base_commit, gate, test_identifier, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = client.app.state.db.one(
            "SELECT * FROM baseline_failure_evidence WHERE repository_id=? AND base_commit=? AND gate=? AND test_identifier=? ORDER BY id DESC LIMIT 1",
            (repository_id, base_commit, gate, test_identifier))
        if row:
            return row
        time.sleep(0.1)
    raise AssertionError("baseline evidence was never recorded")


def test_unknown_failure_blocks_and_next_action_is_fix_integration(client, git_repo):
    root, _ = git_repo
    tid, iid = build_task_with_integration(client, root, "unk-fail")
    run_integration_test_and_wait(client, iid)

    d = client.get(f"/api/tasks/{tid}/decision").json()
    repo_row = d["integration_repos"][0]
    gs = repo_row["gate_status"]
    assert gs["tests_status"] == "FAIL"
    assert gs["failures"][0]["classification"] == "UNKNOWN"  # no evidence exists yet -- never inferred
    assert d["next_action"]["action"] == "FIX_INTEGRATION_FAILURE"
    assert d["ready_for_main"] is False

    r = client.post(f"/api/integrations/{iid}/ready-for-main", follow_redirects=False)
    assert r.status_code == 409  # still genuinely blocked


def test_reproduce_baseline_then_waive_unblocks_ready_for_main(client, git_repo):
    root, _ = git_repo
    tid, iid = build_task_with_integration(client, root, "waive-flow")
    run_integration_test_and_wait(client, iid)

    d = client.get(f"/api/tasks/{tid}/decision").json()
    repo_row = d["integration_repos"][0]
    failure = repo_row["gate_status"]["failures"][0]
    assert failure["test_identifier"] == "tests_probe/test_x.py::test_known_broken"

    r = client.post(f"/api/integrations/{iid}/reproduce-baseline", data={"gate": failure["stage"], "test_identifier": failure["test_identifier"]}, follow_redirects=False)
    assert r.status_code == 303
    evidence = wait_evidence(client, repo_row["repository_id"], repo_row["base_commit"], failure["stage"], failure["test_identifier"])
    assert evidence["failure_fingerprint"] == failure["fingerprint"]  # real reproduction, real matching fingerprint

    d2 = client.get(f"/api/tasks/{tid}/decision").json()
    repo_row2 = d2["integration_repos"][0]
    failure2 = repo_row2["gate_status"]["failures"][0]
    assert failure2["classification"] == "BASELINE_FAILURE"  # evidence now exists and matches
    assert d2["next_action"]["action"] == "REVIEW_BASELINE_FAILURE"

    r = client.post(f"/api/integrations/{iid}/waive-baseline-failure", data={"gate": failure["stage"], "test_identifier": failure["test_identifier"], "reason": "Pre-existing, verified on base commit."}, follow_redirects=False)
    assert r.status_code == 303
    waiver = client.app.state.db.one("SELECT * FROM gate_waivers WHERE integration_id=? ORDER BY id DESC LIMIT 1", (iid,))
    assert waiver and waiver["approved_by"] and waiver["reason"] and waiver["failure_fingerprint"] == failure["fingerprint"]

    d3 = client.get(f"/api/tasks/{tid}/decision").json()
    repo_row3 = d3["integration_repos"][0]
    assert repo_row3["gate_status"]["tests_status"] == "PASS_WITH_APPROVED_BASELINE_WAIVER"  # never plain PASS
    # _integration_ok still checks the PERSISTED READY_FOR_MAIN status,
    # which only exists once ready-for-main actually runs (TESTING alone
    # is never proof of anything, matching the codebase's existing rule) --
    # the waiver alone unblocks the *gate*, not the persisted flag yet.
    r = client.post(f"/api/integrations/{iid}/ready-for-main", follow_redirects=False)
    assert r.status_code == 303  # the waiver is exactly what makes this succeed now
    d4 = client.get(f"/api/tasks/{tid}/decision").json()
    assert d4["ready_for_main"] is True
    assert d4["status"] == "READY_FOR_MAIN"
    assert d4["next_action"]["action"] == "PREPARE_PR"


def test_waiver_refused_when_fingerprint_does_not_match_evidence(client, git_repo):
    """Section 13/17: a waiver must be refused if the CURRENT failure's
    fingerprint no longer matches the stored evidence -- never reused
    across a materially different failure."""
    root, _ = git_repo
    tid, iid = build_task_with_integration(client, root, "fp-mismatch")
    run_integration_test_and_wait(client, iid)
    d = client.get(f"/api/tasks/{tid}/decision").json()
    failure = d["integration_repos"][0]["gate_status"]["failures"][0]

    # Fabricate evidence for the SAME test_identifier but a DIFFERENT
    # reason/fingerprint -- simulates 'evidence exists but the real
    # failure has since changed'.
    client.app.state.db.execute(
        "INSERT INTO baseline_failure_evidence(repository_id,base_commit,gate,test_identifier,failure_fingerprint,evidence) VALUES(?,?,?,?,?,?)",
        (d["integration_repos"][0]["repository_id"], d["integration_repos"][0]["base_commit"], failure["stage"], failure["test_identifier"], "deadbeefdeadbeef", "unrelated old evidence"))

    d2 = client.get(f"/api/tasks/{tid}/decision").json()
    failure2 = d2["integration_repos"][0]["gate_status"]["failures"][0]
    assert failure2["classification"] == "NEW_FAILURE"  # evidence exists but doesn't match -> never BASELINE_FAILURE

    r = client.post(f"/api/integrations/{iid}/waive-baseline-failure", data={"gate": failure["stage"], "test_identifier": failure["test_identifier"]}, follow_redirects=False)
    assert r.status_code == 409  # refused, not silently accepted


def test_multiple_mixed_failures_one_waived_one_new_still_blocks(client, git_repo):
    """Section 18: partial waiver coverage never hides a remaining new
    failure -- READY_FOR_MAIN stays NO until every unresolved failure is
    either fixed or individually, correctly waived."""
    root, _ = git_repo
    tid, iid = build_task_with_integration(client, root, "mixed", second_broken=True)
    run_integration_test_and_wait(client, iid)

    d = client.get(f"/api/tasks/{tid}/decision").json()
    repo_row = d["integration_repos"][0]
    failures = repo_row["gate_status"]["failures"]
    assert len(failures) == 2
    baseline_target = next(f for f in failures if f["test_identifier"].endswith("test_known_broken"))

    client.post(f"/api/integrations/{iid}/reproduce-baseline", data={"gate": baseline_target["stage"], "test_identifier": baseline_target["test_identifier"]})
    wait_evidence(client, repo_row["repository_id"], repo_row["base_commit"], baseline_target["stage"], baseline_target["test_identifier"])
    client.post(f"/api/integrations/{iid}/waive-baseline-failure", data={"gate": baseline_target["stage"], "test_identifier": baseline_target["test_identifier"]})

    d2 = client.get(f"/api/tasks/{tid}/decision").json()
    gs2 = d2["integration_repos"][0]["gate_status"]
    classes = {f["test_identifier"]: f["classification"] for f in gs2["failures"]}
    assert classes[baseline_target["test_identifier"]] == "WAIVED"
    assert any(v == "UNKNOWN" for k, v in classes.items() if k != baseline_target["test_identifier"])
    assert gs2["tests_status"] == "FAIL"  # one real, unresolved failure remains
    assert d2["ready_for_main"] is False
    assert d2["next_action"]["action"] == "FIX_INTEGRATION_FAILURE"

    r = client.post(f"/api/integrations/{iid}/ready-for-main", follow_redirects=False)
    assert r.status_code == 409
