from __future__ import annotations

from app.services.spec_gate import SpecGate
from app.services.task_contract import REQUIRED_SECTIONS, validate_task_contract
from app.services.task_dispatch_gate import TaskDispatchGate
from app.services.recovery_decision_service import RecoveryDecisionService
from app.launchers import AgentLauncher


def _register(client, repo):
    response = client.post("/api/repositories", data={
        "repo_path": str(repo), "repo_name": "calculator", "default_branch": "main",
    })
    assert response.status_code in (200, 303), response.text
    return client.get("/api/repositories").json()[0]["id"]


def _task_with_approved_spec(client, title="Calculator API: return arithmetic results"):
    response = client.post("/api/tasks", data={"title": title, "risk_profile": "LOW"}, follow_redirects=False)
    assert response.status_code == 303
    task_id = client.get("/api/tasks").json()[0]["id"]
    client.post(f"/api/tasks/{task_id}/select", follow_redirects=False)
    linked = client.post(f"/api/tasks/{task_id}/spec", data={
        "classification": "NEW_FEATURE",
        "feature_id": "FEAT-ORCHESTRATION-STANDARD",
        "spec_version": "1",
        "requirement_ids": "REQ-OS-001",
        "acceptance_ids": "AC-OS-001",
        "invariant_ids": "INV-OS-001",
    }, follow_redirects=False)
    assert linked.status_code == 303
    return task_id


def test_incomplete_new_task_is_refused_before_workspace_or_worktree_creation(client, git_repo):
    _, repo = git_repo
    repository_id = _register(client, repo)
    task_id = _task_with_approved_spec(client)

    response = client.post(f"/api/tasks/{task_id}/workspaces", data={
        "repository_id": str(repository_id), "agent": "claude", "role": "Backend",
        "base_branch": "main", "sandbox_profile": "NONE",
    }, follow_redirects=False)

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "TASK_CONTRACT_INCOMPLETE"
    assert body["contract"]["missing_sections"] == [
        "OBJECTIVE", "BASELINE", "SCOPE", "EXECUTION_PLAN",
        "CONSTRAINTS", "ACCEPTANCE_DOD", "EVIDENCE",
    ]
    assert client.app.state.db.one("SELECT id FROM agent_workspaces WHERE task_id=?", (task_id,)) is None
    assert list((repo.parent / ".worktrees").glob("*")) == []


def _valid_prompt(title="Calculator API: return arithmetic results"):
    return f"""## OBJECTIVE
For {title}, implement a calculator endpoint that returns the exact JSON result for each valid numeric input.

## BASELINE
The repository is `app/main.py`, branch integration/orchestrator-v1-20261123 at commit `26c1d3c`. Currently GET /api/calculator returns 404. The Python version is 3.14; assumptions are that FastAPI is already installed.

## SCOPE
In scope: change `app/main.py` and add `tests/test_calculator_api.py` for the calculator response. Out of scope: production deployment, authentication changes, and unrelated repositories.

## EXECUTION_PLAN
1. Inspect `app/main.py` and run `git status --short` before editing.
2. Reproduce the missing response with `pytest -q tests/test_calculator_api.py`.
3. Implement the calculator route in `app/main.py` and preserve the existing auth middleware.
4. Verify the implementation with `pytest -q tests/test_calculator_api.py` and `./scripts/preflight.sh`.

## CONSTRAINTS
Dependencies: existing FastAPI and pytest only. Security boundary: do not expose secrets or weaken authentication. Production must remain untouched. Rollback: revert the scoped `app/main.py` and test changes if the acceptance checks fail.

## ACCEPTANCE_DOD
Automated test: run `pytest -q tests/test_calculator_api.py`; expected exit code 0 and GET /api/calculator returns 200 with the exact sum. Verify invalid input returns 422.

## EVIDENCE
Record command output, exit status, test report, and resulting commit SHA in `docs/orchestration-standard/ORCH-STD-20261123/EVIDENCE.json`; retain the API result and rollback proof.
"""


def test_task_contract_accepts_complete_task_specific_seven_sections():
    result = validate_task_contract({"title": "Calculator API: return arithmetic results", "implementation_prompt": _valid_prompt()})
    assert result["ready"] is True
    assert result["required_sections"] == list(REQUIRED_SECTIONS)
    assert result["missing_sections"] == []


def test_task_contract_rejects_duplicate_placeholder_generic_and_task_mismatch():
    prompt = _valid_prompt()
    duplicate = prompt + "\n\n## OBJECTIVE\n" + _valid_prompt().split("## OBJECTIVE\n", 1)[1].split("\n\n## BASELINE", 1)[0]
    result = validate_task_contract({"title": "Calculator API: return arithmetic results", "implementation_prompt": duplicate})
    assert result["section_issues"]["OBJECTIVE"] == "DUPLICATE_SECTION"

    placeholder = prompt.replace("Currently GET /api/calculator returns 404.", "Currently GET /api/calculator returns TODO.")
    result = validate_task_contract({"title": "Calculator API: return arithmetic results", "implementation_prompt": placeholder})
    assert result["section_issues"]["BASELINE"] == "PLACEHOLDER_CONTENT"

    generic_wrapper = "Generated task wrapper.\n\n" + prompt
    result = validate_task_contract({"title": "Calculator API: return arithmetic results", "implementation_prompt": generic_wrapper})
    assert result["contract_issues"] == ["CONTENT_OUTSIDE_REQUIRED_SECTIONS"]

    result = validate_task_contract({"title": "Other task: update unrelated behavior", "implementation_prompt": prompt})
    assert result["section_issues"]["OBJECTIVE"] == "NOT_TASK_SPECIFIC"


def test_dispatch_gate_requires_both_approved_spec_trace_and_valid_contract():
    gate = TaskDispatchGate(SpecGate("specs"))
    task = {
        "title": "Calculator API: return arithmetic results",
        "implementation_prompt": _valid_prompt(),
        "task_contract_version": "V2",
        "spec_change_classification": "NEW_FEATURE",
        "spec_feature_id": "FEAT-ORCHESTRATION-STANDARD",
        "spec_requirement_ids": '["REQ-OS-001"]',
        "spec_acceptance_ids": '["AC-OS-001"]',
        "spec_invariant_ids": '["INV-OS-001"]',
    }
    ready = gate.evaluate(task)
    assert ready["ready"] is True
    assert ready["spec_gate"]["outcome"] == "PASS"

    task["spec_feature_id"] = ""
    blocked = gate.evaluate(task)
    assert blocked["ready"] is False
    assert blocked["code"] == "SPEC_REQUIRED"
    assert blocked["contract"]["ready"] is True


def test_new_inline_coding_task_is_refused_without_creating_task_or_workspace(client, git_repo):
    _, repo = git_repo
    repository_id = _register(client, repo)

    response = client.post("/api/tasks/create", data={
        "title": "Calculator API: return arithmetic results",
        "implementation_prompt": "",
        "repository_id": str(repository_id), "agent": "claude",
        "primary_role": "Backend", "primary_base_branch": "main",
        "sandbox_profile": "NONE",
    }, follow_redirects=False)

    assert response.status_code == 409
    assert response.json()["code"] == "SPEC_REQUIRED"
    assert client.get("/api/tasks").json() == []
    assert client.get("/api/workspaces").json() == []


def test_setup_and_start_refusal_keeps_backlog_and_creates_no_workspace(client, git_repo):
    _, repo = git_repo
    repository_id = _register(client, repo)
    task_id = _task_with_approved_spec(client)
    client.post(f"/api/tasks/{task_id}/select", follow_redirects=False)
    client.app.state.db.execute("UPDATE tasks SET status='BACKLOG' WHERE id=?", (task_id,))

    response = client.post(f"/api/tasks/{task_id}/setup-and-start", data={
        "repository_id": str(repository_id), "agent": "claude", "role": "Backend",
        "base_branch": "main", "sandbox_profile": "NONE",
    }, follow_redirects=False)

    assert response.status_code == 409
    assert response.json()["code"] == "TASK_CONTRACT_INCOMPLETE"
    assert client.get(f"/api/tasks/{task_id}").json()["status"] == "BACKLOG"
    assert client.app.state.db.one("SELECT id FROM agent_workspaces WHERE task_id=?", (task_id,)) is None
    assert client.app.state.db.one("SELECT id FROM agent_sessions WHERE task_id=?", (task_id,)) is None


def test_manual_session_and_start_all_refuse_before_creating_any_session(client, git_repo):
    _, repo = git_repo
    repository_id = _register(client, repo)
    task_id = _task_with_approved_spec(client)

    workspaces = []
    for agent in ("claude", "codex"):
        created = client.post("/api/workspaces", data={
            "repository_id": str(repository_id), "agent": agent,
            "task_name": f"legacy-{agent}", "base_branch": "main",
        }, follow_redirects=False)
        assert created.status_code in (200, 303), created.text
        workspace = next(w for w in client.get("/api/workspaces").json() if w["agent"] == agent)
        client.post(f"/api/workspaces/{workspace['id']}/attach-task", data={"task_id": str(task_id)}, follow_redirects=False)
        workspaces.append(workspace)

    single = client.post(f"/api/workspaces/{workspaces[0]['id']}/sessions", follow_redirects=False)
    assert single.status_code == 409
    assert single.json()["code"] == "TASK_CONTRACT_INCOMPLETE"
    all_builders = client.post(f"/api/tasks/{task_id}/start-all-builders", follow_redirects=False)
    assert all_builders.status_code == 409
    assert all_builders.json()["code"] == "TASK_CONTRACT_INCOMPLETE"
    assert client.app.state.db.one("SELECT id FROM agent_sessions WHERE task_id=?", (task_id,)) is None


def test_autonomous_tick_reports_contract_block_before_workspace_or_task_activation(client, git_repo):
    _, repo = git_repo
    repository_id = _register(client, repo)
    db = client.app.state.db
    change_id = db.execute("INSERT INTO changes(project_id,title,description) VALUES(?,?,?)",
                           (repository_id, "Calculator goal", "Safe autonomous contract gate"))
    task_id = db.execute("INSERT INTO tasks(slug,title,status,change_id,task_type,spec_change_classification,spec_feature_id,"
                         "spec_requirement_ids,spec_acceptance_ids,spec_invariant_ids) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         ("calculator-auto", "Calculator API: return arithmetic results", "BACKLOG", change_id,
                          "IMPLEMENTATION", "NEW_FEATURE", "FEAT-ORCHESTRATION-STANDARD", '["REQ-OS-001"]',
                          '["AC-OS-001"]', '["INV-OS-001"]'))
    service = client.app.state.autonomous_execution_service

    readiness = service.evaluate_task(task_id)
    result = service.launch_task_if_ready(task_id)

    assert readiness["readiness"] == "WAITING_CONTRACT"
    assert result["outcome"] == "WAITING_CONTRACT"
    assert db.one("SELECT id FROM agent_workspaces WHERE task_id=?", (task_id,)) is None
    assert db.one("SELECT id FROM agent_sessions WHERE task_id=?", (task_id,)) is None
    assert db.one("SELECT status FROM tasks WHERE id=?", (task_id,))["status"] == "BACKLOG"


def test_valid_v2_spec_linked_task_creates_one_workspace_and_starts_real_session(client, git_repo):
    _, repo = git_repo
    repository_id = _register(client, repo)
    title = "Calculator API: return arithmetic results"
    response = client.post("/api/tasks/create", data={
        "title": title, "implementation_prompt": _valid_prompt(title),
        "repository_id": str(repository_id), "agent": "claude",
        "primary_role": "Backend", "primary_base_branch": "main",
        "sandbox_profile": "NONE", "spec_change_classification": "NEW_FEATURE",
        "spec_feature_id": "FEAT-ORCHESTRATION-STANDARD", "spec_version": "1",
        "spec_requirement_ids": "REQ-OS-001", "spec_acceptance_ids": "AC-OS-001",
        "spec_invariant_ids": "INV-OS-001",
    }, follow_redirects=False)
    assert response.status_code == 303, response.text
    task_id = int(response.headers["location"].rsplit("/", 1)[1])
    workspaces = client.get(f"/api/tasks/{task_id}").json()["workspaces"]
    assert len(workspaces) == 1
    workspace = workspaces[0]
    assert client.get(f"/api/tasks/{task_id}/decision").json()["dispatch_readiness"]["ready"] is True

    client.app.state.agent_sessions.launchers = {
        "claude": AgentLauncher("Claude", "bash", ("-c", "echo READY; cat")),
    }
    client.app.state.agent_sessions.prompt_ready_timeout = 3.0
    client.app.state.agent_sessions.prompt_quiet_window = 0.1
    started = client.post(f"/api/workspaces/{workspace['id']}/sessions", follow_redirects=False)

    assert started.status_code == 303, started.text
    sessions = client.app.state.db.all("SELECT * FROM agent_sessions WHERE task_id=?", (task_id,))
    assert len(sessions) == 1
    assert sessions[0]["workspace_id"] == workspace["id"]
    assert sessions[0]["status"] in ("RUNNING", "WAITING_FOR_INPUT", "EXITED")
    client.app.state.agent_sessions.stop(sessions[0]["id"])


def test_change_goal_refuses_second_active_executor_for_another_task(client, git_repo):
    _, repo = git_repo
    repository_id = _register(client, repo)
    db = client.app.state.db
    change_id = db.execute("INSERT INTO changes(project_id,title) VALUES(?,?)", (repository_id, "One executor goal"))
    task_ids, workspace_ids = [], []
    for index in (1, 2):
        title = f"Calculator API: return arithmetic results {index}"
        workspace_path = repo.parent / f"goal-worktree-{index}"
        workspace_path.mkdir()
        task_id = db.execute(
            "INSERT INTO tasks(slug,title,status,change_id,implementation_prompt,spec_change_classification,spec_feature_id,spec_version,spec_requirement_ids,spec_acceptance_ids,spec_invariant_ids) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f"calculator-{index}", title, "ACTIVE", change_id, _valid_prompt(title), "NEW_FEATURE",
             "FEAT-ORCHESTRATION-STANDARD", 1, '["REQ-OS-001"]', '["AC-OS-001"]', '["INV-OS-001"]'),
        )
        task_ids.append(task_id)
        db.execute("INSERT INTO agent_workspaces(repository_id,agent,task_name,branch,worktree_path,base_branch,base_commit,last_commit,status,task_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (repository_id, "claude", f"calculator-{index}", f"goal-calc-{index}", str(workspace_path), "main", "base", "base", "CREATED", task_id))
        workspace_ids.append(db.one("SELECT id FROM agent_workspaces WHERE task_id=?", (task_id,))["id"])

    client.app.state.agent_sessions.launchers={"claude":AgentLauncher("Claude","bash",("-c","echo READY; sleep 15"))}
    client.app.state.agent_sessions.prompt_quiet_window=0.1
    client.app.state.agent_sessions.prompt_ready_timeout=3
    first=client.post(f"/api/workspaces/{workspace_ids[0]}/sessions",follow_redirects=False)
    assert first.status_code==303,first.text
    first_session_id=int(first.headers["location"].rsplit("/",1)[1])
    blocked = client.post(f"/api/workspaces/{workspace_ids[1]}/sessions", follow_redirects=False)
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "GOAL_EXECUTOR_ACTIVE"
    assert db.one("SELECT COUNT(*) c FROM agent_sessions WHERE task_id=?", (task_ids[1],))["c"] == 0
    client.app.state.agent_sessions.stop(first_session_id)


def test_v1_task_history_keeps_legacy_specgate_dispatch_semantics(client, git_repo):
    gate=TaskDispatchGate(SpecGate("specs"))
    result=gate.evaluate({"title":"Historical task","task_contract_version":"V1"})
    assert result["ready"] is True
    assert result["contract"]["legacy_v1"] is True
    assert result["legacy_upgrade_required"] is True


def test_change_view_escapes_owner_and_task_content_and_shows_honest_empty_state(client):
    created=client.post("/changes",data={"what":"<script>alert(1)</script>","owner_name":"<img src=x onerror=alert(1)>"},follow_redirects=False)
    assert created.status_code==303
    page=client.get(created.headers["location"])
    assert page.status_code==200
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;" in page.text
    assert "<img src=x onerror=alert(1)>" not in page.text
    assert "Spec → Plan → Agent → Verify → Accept" in page.text
    assert "No child Tasks yet" in page.text


def _make_exited_v2_recovery_task(client, repo, title="Recovery canary: verify safe task recovery"):
    db=client.app.state.db
    repository_id=_register(client,repo)
    change_id=db.execute("INSERT INTO changes(project_id,title,owner_name) VALUES(?,?,?)",
                         (repository_id,"Recovery goal","Canary Owner"))
    task_id=db.execute("INSERT INTO tasks(slug,title,status,change_id,implementation_prompt,spec_change_classification,spec_feature_id,spec_version,spec_requirement_ids,spec_acceptance_ids,spec_invariant_ids) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       ("recovery-canary",title,"ACTIVE",change_id,_valid_prompt(title),"NEW_FEATURE","FEAT-ORCHESTRATION-STANDARD",1,'["REQ-OS-001"]','["AC-OS-001"]','["INV-OS-001"]'))
    worktree=repo.parent/"recovery-worktree"; worktree.mkdir(exist_ok=True)
    workspace_id=db.execute("INSERT INTO agent_workspaces(repository_id,agent,task_name,branch,worktree_path,base_branch,base_commit,last_commit,status,task_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (repository_id,"codex","recovery-canary","recovery-canary-branch",str(worktree),"main","base","base","CREATED",task_id))
    session_id=db.execute("INSERT INTO agent_sessions(task_id,workspace_id,agent,command_profile,cwd,status,mode,exit_code) VALUES(?,?,?,?,?,?,?,?)",
                          (task_id,workspace_id,"codex","Codex",str(worktree),"EXITED","INTERACTIVE",0))
    service=client.app.state.autonomous_execution_service
    service._project_policy_resolver=lambda _change:{"autonomous_execution":{"goal_reconciliation_enabled":True}}
    # Recovery tests must never invoke the user's real Codex provider. The
    # one test that asserts a resumed session replaces this launcher below.
    client.app.state.agent_sessions.launchers={"codex":AgentLauncher("Codex","bash",("-c","echo fake recovery session; sleep 100"))}
    return db,service,change_id,task_id,session_id


def test_recovery_blocks_when_provider_is_unknown_and_stops_after_two_attempts(client,git_repo):
    _,repo=git_repo
    db,service,change_id,task_id,_=_make_exited_v2_recovery_task(client,repo)
    service.provider_available=lambda _agent:False
    offline=service.reconcile_change(change_id)[0]
    assert offline["outcome"]=="BLOCKED" and offline["code"]=="PROVIDER_UNKNOWN"

    service.provider_available=lambda _agent:True
    db.event("task",task_id,"GOAL_RESUME_ATTEMPT","attempt=1")
    db.event("task",task_id,"GOAL_RESUME_ATTEMPT","attempt=2")
    exhausted=service.reconcile_change(change_id)[0]
    assert exhausted["outcome"]=="BLOCKED" and exhausted["code"]=="RETRY_BUDGET_EXHAUSTED"
    repeated=service.reconcile_change(change_id)[0]
    assert repeated["outcome"]=="BLOCKED" and repeated["code"]=="RETRY_BUDGET_EXHAUSTED"
    assert db.one("SELECT COUNT(*) c FROM workspace_events WHERE entity_type='task' AND entity_id=? AND action='GOAL_RESUME_BLOCKED' AND details LIKE 'code=RETRY_BUDGET_EXHAUSTED%'",(task_id,))["c"]==1
    assert db.one("SELECT COUNT(*) c FROM agent_sessions WHERE task_id=?",(task_id,))["c"]==1


def test_recovery_advisor_error_keeps_factual_blocker_and_does_not_abort_tick(client,git_repo):
    _,repo=git_repo
    db,service,change_id,task_id,_=_make_exited_v2_recovery_task(client,repo)
    class BrokenAdvisor:
        def advise(self,*_args,**_kwargs):
            raise RuntimeError("provider envelope unavailable")
    service.recovery_advisor=BrokenAdvisor()
    result=service._recovery_block(change_id,task_id,"PROVIDER_UNKNOWN","Check provider auth and quota.")[0]
    assert result["outcome"]=="BLOCKED" and result["code"]=="PROVIDER_UNKNOWN"
    assert result["next_action"]=="Check provider auth and quota."
    assert result["advisory"]["reason"]=="AI_ADVISOR_ERROR"
    assert db.one("SELECT COUNT(*) c FROM workspace_events WHERE action='RECOVERY_AI_FAILED'")["c"]==1


def test_malformed_advisor_json_fallback_preserves_original_blocker(client,git_repo,tmp_path):
    _,repo=git_repo
    db,service,change_id,task_id,_=_make_exited_v2_recovery_task(client,repo)
    class MalformedInvoker:
        def model_supported(self,*_args): return False
        def advisory_toolless_capable(self,provider): return provider=="claude"
        def invoke(self,provider,prompt,schema,cwd,**kwargs):
            assert provider=="claude" and kwargs["require_toolless"] is True
            return {"text":"not JSON","provider":"claude","resolved_model":"claude-opus-5-5",
                    "effort_requested":"high","turns":1,"cost_estimate_usd":0.05,
                    "toolless_verified":True}
    service.recovery_advisor=RecoveryDecisionService(db,MalformedInvoker(),tmp_path,
        provider_available=lambda provider:provider=="claude")
    result=service._recovery_block(change_id,task_id,"PROVIDER_UNKNOWN","Check provider auth and quota.")[0]
    assert result["outcome"]=="BLOCKED" and result["code"]=="PROVIDER_UNKNOWN"
    assert result["next_action"]=="Check provider auth and quota."
    assert result["advisory"]=={"source":"DETERMINISTIC_FALLBACK","allowed_action":"VERIFY",
                               "reason":"AI_PROPOSAL_INVALID"}
    assert db.one("SELECT COUNT(*) c FROM agent_sessions WHERE task_id=?",(task_id,))["c"]==1


def test_recovery_backoff_prevents_immediate_second_attempt(client,git_repo):
    _,repo=git_repo
    db,service,change_id,task_id,_=_make_exited_v2_recovery_task(client,repo,"Recovery canary: verify backoff policy")
    service.provider_available=lambda _agent:True
    db.event("task",task_id,"GOAL_RESUME_ATTEMPT","attempt=1")
    result=service.reconcile_change(change_id)[0]
    assert result["outcome"]=="BACKOFF"
    assert 1<=result["retry_after_seconds"]<=60
    assert db.one("SELECT COUNT(*) c FROM agent_sessions WHERE task_id=?",(task_id,))["c"]==1


def test_recovery_resumes_same_task_after_confirmed_restart_failure(client,git_repo):
    _,repo=git_repo
    db,service,change_id,task_id,session_id=_make_exited_v2_recovery_task(client,repo,"Recovery canary: resume after restart")
    db.execute("UPDATE agent_sessions SET status='FAILED' WHERE id=?",(session_id,))
    service.provider_available=lambda _agent:True
    client.app.state.agent_sessions.launchers={"codex":AgentLauncher("Codex","bash",("-c","sleep 100"))}

    result=service.reconcile_change(change_id)[0]
    assert result["outcome"]=="RESUMED"
    assert result["task_id"]==task_id and result["attempt"]==1
    rows=db.all("SELECT id,status FROM agent_sessions WHERE task_id=? ORDER BY id",(task_id,))
    assert len(rows)==2
    assert rows[0]["status"]=="FAILED"
    assert rows[1]["status"] in ("RUNNING","WAITING_FOR_INPUT")
    attempts=db.all("SELECT details FROM workspace_events WHERE entity_type='task' AND entity_id=? AND action='GOAL_RESUME_ATTEMPT'",(task_id,))
    assert len(attempts)==1 and "attempt=1" in attempts[0]["details"]


def test_change_ui_shows_owner_and_blocker_after_retry_budget_exhaustion(client,git_repo):
    import re
    _,repo=git_repo
    db,_,change_id,task_id,_=_make_exited_v2_recovery_task(client,repo,"Recovery canary: show exhausted blocker")
    db.event("task",task_id,"GOAL_RESUME_ATTEMPT","attempt=2 session=3 owner=Canary Owner")
    db.event("task",task_id,"GOAL_RESUME_BLOCKED","code=RETRY_BUDGET_EXHAUSTED owner=Change owner next_action=Owner must review the existing branch and evidence; no further automatic retry is allowed.")

    page=client.get(f"/changes/{change_id}?mode=simple")
    assert page.status_code==200
    assert "Goal:</b> Blocked" in page.text
    assert "RETRY_BUDGET_EXHAUSTED" in page.text
    assert "Canary Owner" in page.text
    assert "no further automatic retry is allowed" in page.text
    assert "Goal blocked — owner action required." in page.text
    assert "No action needed. AI is working." not in page.text
    goal_section=re.search(r'<section class="panel" aria-label="Spec to acceptance orchestration">(.*?)</section>',page.text,re.S)
    assert goal_section and "Not started yet" not in goal_section.group(1)
