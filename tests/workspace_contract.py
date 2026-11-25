"""Input for tasks that create real Git workspaces in workflow tests."""


def workspace_contract(title):
    return {
        "spec_change_classification": "NEW_FEATURE",
        "spec_feature_id": "FEAT-ORCHESTRATION-STANDARD",
        "spec_version": "1",
        "spec_requirement_ids": "REQ-OS-001",
        "spec_acceptance_ids": "AC-OS-001",
        "spec_invariant_ids": "INV-OS-001",
        "implementation_prompt": f"""## OBJECTIVE
For {title}, create exactly one owned workspace and verify its lifecycle against the task record.

## BASELINE
The disposable demo repository has a main branch. Its first commit contains PROJECT.yaml and is created by tests/conftest.py before the task starts.

## SCOPE
In scope are task, workspace and evidence records for this test in tests/conftest.py. Changes to other tasks and external services are out of scope.

## EXECUTION_PLAN
1. Run git status in the disposable repository and record the initial commit.
2. Register the repository, submit this task and check the assigned workspace identity.
3. Run pytest -q tests/test_real_merge.py tests/test_cleanup_and_restart.py and check the HTTP results and task records.

## CONSTRAINTS
Dependencies are the disposable Git repository and the per-test database. Production credentials are outside the test boundary. Rollback discards the isolated test environment.

## ACCEPTANCE_DOD
Run pytest -q tests/test_real_merge.py tests/test_cleanup_and_restart.py and verify the expected changes affect only the selected task and its workspace.

## EVIDENCE
Record the commit and the database and HTTP results asserted in tests/test_real_merge.py and tests/test_cleanup_and_restart.py.
""",
    }


def configure_backlog_task(client, task_id, title):
    fields = workspace_contract(title)
    response = client.post(
        f"/api/tasks/{task_id}/prompt",
        data={"implementation_prompt": fields["implementation_prompt"]},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    trace = {key.removeprefix("spec_"): value for key, value in fields.items()
             if key.startswith("spec_") and key != "spec_change_classification"}
    trace["classification"] = fields["spec_change_classification"]
    response = client.post(f"/api/tasks/{task_id}/spec", data=trace, follow_redirects=False)
    assert response.status_code == 303, response.text
