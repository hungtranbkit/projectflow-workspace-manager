from __future__ import annotations

import time
import json
import hashlib
import textwrap
import pytest

from app.launchers import AgentLauncher
from app.services.completion_report_parser import parse_completion_report, parse_executor_completion_report

TITLE = "Calculator API: record canary work"
PROMPT = f"""## OBJECTIVE
For {TITLE}, implement and commit a calculator canary change so README.md visibly contains the real note.

## BASELINE
The repository path is `docs/README.md`, branch main at its current baseline commit. The observed baseline contains only base text.

## SCOPE
In scope: change `docs/README.md` only. Out of scope: application code, tests, production and unrelated files.

## EXECUTION_PLAN
1. Inspect `docs/README.md` and run `git status --short`.
2. Append the canary note to `docs/README.md` and commit the scoped change.
3. Run `pytest -q` through ProjectFlow and capture its exact result.

## CONSTRAINTS
Dependencies: existing Git and Python only. Security boundary: do not expose secrets or edit production. Rollback: revert the single README commit if acceptance fails.

## ACCEPTANCE_DOD
Automated test: run `pytest -q`; expected exit code 0 and the committed `docs/README.md` note is present.

## EVIDENCE
Record the exact commit SHA, changed `docs/README.md` path, real test result, report and resulting status.
"""


def _new_task(client, repo):
    if not (repo / "docs/README.md").exists():
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs/README.md").write_text("base\n")
        import subprocess
        subprocess.run(["git", "add", "docs/README.md"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.email=test@example.invalid", "-c", "user.name=Test", "commit", "-m", "add scoped canary path"], cwd=repo, check=True, capture_output=True)
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": "canary", "default_branch": "main"})
    assert r.status_code in (200, 303), r.text
    rid = client.get("/api/repositories").json()[0]["id"]
    title = f"{TITLE} {client.app.state.db.one('SELECT COUNT(*) n FROM tasks')['n'] + 1}"
    r = client.post("/api/tasks/create", data={
        "title": title, "implementation_prompt": PROMPT.replace(TITLE, title), "repository_id": str(rid),
        "agent": "codex", "primary_role": "Backend", "primary_base_branch": "main",
        "sandbox_profile": "NONE", "risk_profile": "LOW",
        "spec_change_classification": "NEW_FEATURE", "spec_feature_id": "FEAT-ORCHESTRATION-STANDARD",
        "spec_version": "1", "spec_requirement_ids": "REQ-OS-001",
        "spec_acceptance_ids": "AC-OS-001", "spec_invariant_ids": "INV-OS-001",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    tid = int(r.headers["location"].rsplit("/", 1)[1])
    return tid, client.get(f"/api/tasks/{tid}").json()["workspaces"][0]


def _report_script(*, report=True, sha_mode="correct", file_path="docs/README.md"):
    sha_expr = '"$sha"' if sha_mode == "correct" else '"0000000000000000000000000000000000000000"'
    work = """printf '\\ncanary\\n' >> docs/README.md
 git add docs/README.md
 git -c user.email=canary@example.invalid -c user.name=Canary commit -m 'canary change'
 sha=$(git rev-parse HEAD)
"""
    if not report:
        return work + "echo 'Codex state=done'\n"
    return work + f"""printf '\\n----- PROJECTFLOW EXECUTOR REPORT START -----\\n'
 printf 'WORK_STATUS:\\nREADY\\n\\nWHAT_CHANGED:\\nAdded the canary README note.\\n\\nAUTOMATED_TESTS:\\nProjectFlow Run Tests: PASS.\\n\\nHOW_TO_VERIFY:\\n1. Read docs/README.md.\\n\\nEXPECTED_RESULT:\\nThe canary note is present.\\n\\nTEST_DATA:\\nREADME canary text.\\n\\nRUNTIME_REQUIREMENTS:\\nNONE\\n\\nRISKS:\\nNone known.\\n\\nFILES_CHANGED:\\n{file_path}\\n\\nCOMMIT_SHA:\\n%s\\n----- PROJECTFLOW EXECUTOR REPORT END -----\\n' {sha_expr}
"""


def _fake_codex_exec(tmp_path, *, write_artifact=True, changed_path="docs/README.md", reported_path=None, wrong_sha=False):
    executable = tmp_path / "codex-exec-fixture"
    reported_path = reported_path or changed_path
    source = textwrap.dedent('''\
        #!/usr/bin/env python3
        import pathlib, subprocess, sys, textwrap
        prompt = sys.stdin.read()
        assert "## OBJECTIVE" in prompt and "COMMIT_SHA" in prompt
        output = pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1])
        target = pathlib.Path(__CHANGED_PATH__)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((target.read_text() if target.exists() else "") + "\\nCodex exec canary\\n")
        subprocess.run(["git", "add", str(target)], check=True)
        subprocess.run(["git", "-c", "user.email=codex@example.invalid", "-c", "user.name=Codex", "commit", "-m", "Codex exec canary"], check=True, stdout=subprocess.DEVNULL)
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        report = textwrap.dedent(f"""----- PROJECTFLOW EXECUTOR REPORT START -----
        WORK_STATUS:
        READY

        WHAT_CHANGED:
        Added the calculator canary note.

        AUTOMATED_TESTS:
        ProjectFlow pytest passed.

        HOW_TO_VERIFY:
        1. Read docs/README.md.

        EXPECTED_RESULT:
        The canary note is present.

        TEST_DATA:
        README canary text.

        RUNTIME_REQUIREMENTS:
        NONE

        RISKS:
        None known.

        FILES_CHANGED:
        __REPORTED_PATH__

        COMMIT_SHA:
        __REPORT_SHA__
        ----- PROJECTFLOW EXECUTOR REPORT END -----""")
        if __WRITE_ARTIFACT__:
            output.write_text(report)
        print(prompt, flush=True)  # TUI/PTY echo must never be report authority.
        if not __WRITE_ARTIFACT__:
            print(report, flush=True)  # Echoed terminal text is not -o evidence.
        print('{"type":"turn.completed"}', flush=True)
    ''').replace("__WRITE_ARTIFACT__", repr(write_artifact))
    source = source.replace("__CHANGED_PATH__", repr(changed_path))
    source = source.replace("__REPORTED_PATH__", reported_path)
    source = source.replace("__REPORT_SHA__", "0" * 40 if wrong_sha else "{sha}")
    executable.write_text(source)
    executable.chmod(0o755)
    return executable


def _select_codex_exec_launcher(client, executable):
    client.app.state.agent_sessions.launchers = {
        "codex": AgentLauncher("Codex", str(executable), ("--yolo",), completion_mode="CODEX_EXEC")
    }


def _start_fake_session(client, workspace, script):
    manager = client.app.state.agent_sessions
    manager.launchers = {"codex": AgentLauncher("Codex", "bash", ("-c", "echo ready; sleep 0.5; " + script))}
    manager.prompt_ready_timeout = 3
    manager.prompt_quiet_window = 0.1
    response = client.post(f"/api/workspaces/{workspace['id']}/sessions", data={"mode": "INTERACTIVE"}, follow_redirects=False)
    assert response.status_code == 303, response.text
    sid = int(response.headers["location"].rsplit("/", 1)[1])
    deadline = time.time() + 8
    while time.time() < deadline:
        session = client.app.state.db.one("SELECT status FROM agent_sessions WHERE id=?", (sid,))
        if session["status"] in ("EXITED", "FAILED"):
            return sid
        time.sleep(0.05)
    raise AssertionError("session did not exit")


def _run_projectflow_tests(client, workspace):
    client.post(f"/api/workspaces/{workspace['id']}/test", follow_redirects=False)
    db = client.app.state.db
    deadline = time.time() + 10
    while time.time() < deadline:
        run = db.one("SELECT * FROM test_runs WHERE workspace_type='agent' AND workspace_id=? ORDER BY id DESC LIMIT 1", (workspace["id"],))
        if run and run["status"] in ("PASS", "FAIL", "TIMEOUT"):
            return run
        time.sleep(0.05)
    raise AssertionError("ProjectFlow test run did not finish")


def _ingest_after_tests(client, workspace):
    run = _run_projectflow_tests(client, workspace)
    assert run["status"] == "PASS" and run["exit_code"] == 0
    client.app.state.ingest_exited_executor_reports()
    return run


def test_parser_requires_final_delimited_ten_field_block_and_handles_ansi_crlf():
    fields = [
        ("WORK_STATUS", "READY"), ("WHAT_CHANGED", "Implemented arithmetic result handling."),
        ("AUTOMATED_TESTS", "pytest -q: 3 passed."), ("HOW_TO_VERIFY", "1. Run the calculator test."),
        ("EXPECTED_RESULT", "The test passes."), ("TEST_DATA", "Inputs 1 and 2."),
        ("RUNTIME_REQUIREMENTS", "NONE"), ("RISKS", "None known."),
        ("FILES_CHANGED", "README.md"), ("COMMIT_SHA", "a" * 40),
    ]
    body = "\r\n\r\n".join(f"{key}:\r\n{value}" for key, value in fields)
    transcript = "echo WORK_STATUS:\r\n" + "\x1b[32m" + "----- PROJECTFLOW EXECUTOR REPORT START -----\r\n" + body + "\r\n----- PROJECTFLOW EXECUTOR REPORT END -----"
    parsed = parse_executor_completion_report(transcript)
    assert parsed and parsed["COMMIT_SHA"] == "a" * 40
    assert parse_executor_completion_report(transcript[:-20]) is None
    assert parse_executor_completion_report(transcript.replace("\r\nRISKS:", "\r\nWHAT_CHANGED:\r\nextra\r\nRISKS:")) is None
    assert parse_executor_completion_report("----- PROJECTFLOW EXECUTOR REPORT START -----\n" + body) is None


def test_v1_report_survives_projectflow_echoed_placeholder_template():
    actual = """WORK_STATUS:
READY

WHAT_CHANGED:
Implemented the requested fix.

AUTOMATED_TESTS:
pytest: passed
"""
    echoed_template = """WORK_STATUS:
READY

WHAT_CHANGED:
Replace this with your actual change...

AUTOMATED_TESTS:
Replace this with actual tests...
"""
    parsed = parse_completion_report(actual + echoed_template)
    assert parsed and parsed["WHAT_CHANGED"] == "Implemented the requested fix."


def test_v2_codex_prompt_contains_literal_machine_report_delimiters(client, git_repo):
    _, repo = git_repo
    task_id, _workspace = _new_task(client, repo)
    prompt = client.get(f"/api/tasks/{task_id}").json()["agent_prompt"]
    assert "----- PROJECTFLOW EXECUTOR REPORT START -----" in prompt
    assert "----- PROJECTFLOW EXECUTOR REPORT END -----" in prompt
    assert "including the single space before the last five dashes" in prompt
    fields = ["WORK_STATUS", "WHAT_CHANGED", "AUTOMATED_TESTS", "HOW_TO_VERIFY", "EXPECTED_RESULT",
              "TEST_DATA", "RUNTIME_REQUIREMENTS", "RISKS", "FILES_CHANGED", "COMMIT_SHA"]
    block = prompt.split("----- PROJECTFLOW EXECUTOR REPORT START -----", 1)[1].split("----- PROJECTFLOW EXECUTOR REPORT END -----", 1)[0]
    positions = [block.find(field) for field in fields]
    assert all(position >= 0 for position in positions) and positions == sorted(positions)


def test_valid_report_and_real_projectflow_test_ingest_once_and_require_review(client, git_repo):
    _, repo = git_repo
    task_id, workspace = _new_task(client, repo)
    session_id = _start_fake_session(client, workspace, _report_script())
    session = client.app.state.db.one("SELECT * FROM agent_sessions WHERE id=?", (session_id,))
    assert session["status"] == "EXITED" and session["prompt_status"] == "DELIVERED"
    run = _ingest_after_tests(client, workspace)
    report = client.app.state.db.one("SELECT * FROM verification_reports WHERE source_session_id=?", (session_id,))
    assert report and report["task_id"] == task_id and report["workspace_id"] == workspace["id"]
    assert report["commit_sha"] == run["tested_commit"] and report["report_sha256"]
    assert report["ready_source"] == "AGENT_SESSION_REPORT"
    client.app.state.ingest_exited_executor_reports()
    assert client.app.state.db.one("SELECT COUNT(*) n FROM verification_reports WHERE source_session_id=?", (session_id,))["n"] == 1
    assert client.app.state.db.one("SELECT id FROM review_runs WHERE task_id=?", (task_id,)) is None
    decision = client.get(f"/api/tasks/{task_id}/decision").json()
    assert decision["stage"] == "REVIEW" and decision["status"] != "DONE"


def _start_codex_exec_session(client, workspace, executable):
    _select_codex_exec_launcher(client, executable)
    response = client.post(f"/api/workspaces/{workspace['id']}/sessions", data={"mode": "INTERACTIVE"}, follow_redirects=False)
    assert response.status_code == 303, response.text
    sid = int(response.headers["location"].rsplit("/", 1)[1])
    deadline = time.time() + 10
    while time.time() < deadline:
        row = client.app.state.db.one("SELECT status FROM agent_sessions WHERE id=?", (sid,))
        if row["status"] in ("EXITED", "FAILED"):
            return sid
        time.sleep(0.05)
    raise AssertionError("Codex exec fixture did not exit")


def test_codex_exec_final_message_artifact_is_ingested_once_and_requires_review(client, git_repo, tmp_path):
    _, repo = git_repo
    task_id, workspace = _new_task(client, repo)
    sid = _start_codex_exec_session(client, workspace, _fake_codex_exec(tmp_path))
    session = client.app.state.db.one("SELECT * FROM agent_sessions WHERE id=?", (sid,))
    assert session["status"] == "EXITED"
    assert session["completion_transport"] == "CODEX_EXEC"
    assert session["final_output_status"] == "CAPTURED"
    assert session["prompt_status"] == "DELIVERED"
    assert client.app.state.db.one("SELECT COUNT(*) n FROM agent_sessions WHERE task_id=?", (task_id,))["n"] == 1
    assert client.app.state.db.one("SELECT id FROM workspace_events WHERE entity_type='agent' AND entity_id=? AND action='EXECUTOR_FINAL_OUTPUT_CAPTURED'", (workspace["id"],)) is not None
    assert "Added the calculator canary note." not in session["transcript_tail"]  # only the task prompt was echoed
    run = _run_projectflow_tests(client, workspace)
    assert run["status"] == "PASS" and run["exit_code"] == 0
    final_report = parse_executor_completion_report(session["final_output_text"])
    assert final_report and run["tested_commit"] == final_report["COMMIT_SHA"], repr(session["final_output_text"])
    assert hashlib.sha256(session["final_output_text"].encode("utf-8")).hexdigest() == session["final_output_sha256"]
    client.app.state.ingest_exited_executor_reports()
    row = client.app.state.db.one("SELECT * FROM verification_reports WHERE source_session_id=?", (sid,))
    assert row and row["commit_sha"] == run["tested_commit"]
    assert row["report_sha256"]
    assert client.get(f"/api/tasks/{task_id}/decision").json()["stage"] == "REVIEW"
    assert client.app.state.db.one("SELECT id FROM review_runs WHERE task_id=?", (task_id,)) is None
    client.app.state.agent_sessions.reconcile_on_startup()
    client.app.state.ingest_exited_executor_reports()
    assert client.app.state.db.one("SELECT COUNT(*) n FROM verification_reports WHERE source_session_id=?", (sid,))["n"] == 1


def test_pty_echo_of_valid_report_without_codex_final_artifact_is_rejected(client, git_repo, tmp_path):
    _, repo = git_repo
    task_id, workspace = _new_task(client, repo)
    sid = _start_codex_exec_session(client, workspace, _fake_codex_exec(tmp_path, write_artifact=False))
    session = client.app.state.db.one("SELECT * FROM agent_sessions WHERE id=?", (sid,))
    assert session["status"] == "EXITED" and session["final_output_status"] == "MISSING"
    assert "PROJECTFLOW EXECUTOR REPORT START" in session["transcript_tail"]  # valid-looking report appeared only on PTY
    run = _run_projectflow_tests(client, workspace)
    assert run["status"] == "PASS" and run["tested_commit"] == client.app.state.git.head(workspace["worktree_path"])
    client.app.state.ingest_exited_executor_reports()
    assert client.app.state.db.one("SELECT id FROM verification_reports WHERE source_session_id=?", (sid,)) is None
    assert client.app.state.db.one("SELECT id FROM review_runs WHERE task_id=?", (task_id,)) is None
    assert client.get(f"/api/tasks/{task_id}/decision").json()["status"] != "DONE"


@pytest.mark.parametrize(("options", "reason"), [
    ({"wrong_sha": True}, "COMMIT_SHA_MISMATCH"),
    ({"changed_path": "docs/README.md.evil"}, "CHANGED_PATH_SCOPE_MISMATCH"),
])
def test_codex_final_artifact_still_rejects_wrong_sha_and_out_of_scope(client, git_repo, tmp_path, options, reason):
    _, repo = git_repo
    task_id, workspace = _new_task(client, repo)
    sid = _start_codex_exec_session(client, workspace, _fake_codex_exec(tmp_path, **options))
    run = _run_projectflow_tests(client, workspace)
    assert run["status"] == "PASS" and run["exit_code"] == 0
    client.app.state.ingest_exited_executor_reports()
    assert client.app.state.db.one("SELECT id FROM verification_reports WHERE source_session_id=?", (sid,)) is None
    events = client.app.state.db.all("SELECT details FROM workspace_events WHERE entity_id=? AND action='EXECUTOR_REPORT_INGEST_REJECTED'", (workspace["id"],))
    assert any(f"session={sid} reason={reason}" == e["details"] for e in events), [e["details"] for e in events]
    assert client.app.state.db.one("SELECT id FROM review_runs WHERE task_id=?", (task_id,)) is None


def test_wrong_sha_out_of_scope_and_missing_report_create_no_verification(client, git_repo):
    _, repo = git_repo
    for script in (_report_script(sha_mode="wrong"), _report_script(file_path="app/main.py"),
                   _report_script().replace("docs/README.md", "docs/README.md.evil"),
                   _report_script(report=False)):
        task_id, workspace = _new_task(client, repo)
        session_id = _start_fake_session(client, workspace, script)
        _ingest_after_tests(client, workspace)
        client.app.state.ingest_exited_executor_reports()
        assert client.app.state.db.one("SELECT id FROM verification_reports WHERE source_session_id=?", (session_id,)) is None
        assert client.app.state.db.one("SELECT id FROM review_runs WHERE task_id=?", (task_id,)) is None
        assert client.get(f"/api/tasks/{task_id}/decision").json()["status"] != "DONE"


def test_real_commit_and_pass_test_without_report_cannot_advance_to_review(client, git_repo):
    _, repo = git_repo
    task_id, workspace = _new_task(client, repo)
    session_id = _start_fake_session(client, workspace, _report_script(report=False))
    run = _run_projectflow_tests(client, workspace)
    assert run["status"] == "PASS" and run["exit_code"] == 0
    assert run["tested_commit"] == client.app.state.git.head(workspace["worktree_path"])
    client.app.state.ingest_exited_executor_reports()
    assert client.app.state.db.one("SELECT id FROM verification_reports WHERE source_session_id=?", (session_id,)) is None
    assert client.app.state.db.one("SELECT id FROM review_runs WHERE task_id=?", (task_id,)) is None
    decision = client.get(f"/api/tasks/{task_id}/decision").json()
    assert decision["stage"] != "REVIEW" and decision["status"] != "DONE"


def test_transcript_lost_after_restart_records_missing_report_blocker(client, git_repo):
    _, repo = git_repo
    task_id, workspace = _new_task(client, repo)
    session_id = _start_fake_session(client, workspace, _report_script())
    client.app.state.db.execute("UPDATE agent_sessions SET transcript_tail='' WHERE id=?", (session_id,))
    _run_projectflow_tests(client, workspace)
    client.app.state.ingest_exited_executor_reports()
    events=client.app.state.db.all("SELECT details FROM workspace_events WHERE entity_type='agent' AND entity_id=? AND action='EXECUTOR_REPORT_INGEST_REJECTED'",(workspace["id"],))
    assert events and any(f"session={session_id} reason=COMPLETION_REPORT_MISSING_OR_INVALID"==e["details"] for e in events)
    assert client.app.state.db.one("SELECT id FROM verification_reports WHERE source_session_id=?",(session_id,)) is None
    assert client.get(f"/api/tasks/{task_id}/decision").json()["status"] != "DONE"


def test_session_prompt_must_match_recorded_delivered_prompt(client, git_repo):
    _, repo = git_repo
    task_id, workspace = _new_task(client, repo)
    session_id = _start_fake_session(client, workspace, _report_script())
    client.app.state.db.execute("UPDATE agent_sessions SET prompt_sha256=? WHERE id=?", ("0" * 64, session_id))
    _ingest_after_tests(client, workspace)
    assert client.app.state.db.one("SELECT id FROM verification_reports WHERE source_session_id=?", (session_id,)) is None
    assert client.app.state.db.one("SELECT id FROM review_runs WHERE task_id=?", (task_id,)) is None


def test_manual_report_route_remains_unlinked_and_functional(client, git_repo):
    _, repo = git_repo
    _, workspace = _new_task(client, repo)
    response=client.post(f"/api/workspaces/{workspace['id']}/verification-report",data={
        "work_status":"READY","what_changed":"Human entered the report.",
        "automated_tests":"Not run","how_to_verify":"Inspect the workspace.",
        "expected_result":"The report remains human submitted.",
    },follow_redirects=False)
    assert response.status_code==303
    row=client.app.state.db.one("SELECT * FROM verification_reports WHERE workspace_id=? ORDER BY id DESC LIMIT 1",(workspace["id"],))
    assert row["ready_source"]=="AGENT_SUBMITTED" and row["source_session_id"] is None and row["report_sha256"] is None
