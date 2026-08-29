"""Hardening pass after the first real Task #5 DEV deployment (section
12 of the spec this implements): Sandbox vs DEV UI clarity, real
artifact evidence persistence (version/image/digest/package/sha256,
matching mesflow's now-fixed build-release.sh contract), an explicit
artifact/source consistency check, and real rollback to a previous
VERIFIED deployment's exact retained image -- never a rebuild."""
from __future__ import annotations

import pytest

from tests.test_deployment import (
    FakeGh, FakeResp, PROJECT_YAML_TEMPLATE, create_task, register,
    submit_and_review, run, latest_deployment_of,
)


ARTIFACT_YAML_EXTRA = """
artifacts:
  strategy: immutable
  directory: ../artifacts/releases
  metadata: ../artifacts/latest/demo.json
"""


def make_repo_with_artifact_build(root, name="demo", commit_in_metadata="$(git rev-parse HEAD)"):
    """A deployable repo whose `build` command writes a REAL metadata
    file matching mesflow's fixed contract (source_commit/version/
    image_name/image_tag/image/image_digest/package_filename/
    package_sha256/built_at) -- `commit_in_metadata` is a shell
    expression substituted into the written JSON's source_commit field,
    so a test can deliberately write a WRONG commit to exercise the
    mismatch guard."""
    from pathlib import Path
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    run(repo, "git", "init", "-b", "main")
    run(repo, "git", "config", "user.email", "test@example.invalid")
    run(repo, "git", "config", "user.name", "Test")
    (repo / "README.md").write_text("base\n")
    yaml_text = PROJECT_YAML_TEMPLATE.format(name=name, smoke_cmd="echo SMOKE_OK").replace(
        "commands:",
        ARTIFACT_YAML_EXTRA + "\ncommands:",
    ).replace(
        'command: "echo BUILD_OK"',
        'command: "mkdir -p ../artifacts/latest && printf \'{\\"source_commit\\":\\"%s\\",\\"version\\":\\"1.0.0\\",\\"image_name\\":\\"demo\\",\\"image_tag\\":\\"1.0.0\\",\\"image\\":\\"demo:1.0.0\\",\\"image_digest\\":\\"sha256:fakedigest\\",\\"package_filename\\":\\"demo-1.0.0.zip\\",\\"package_sha256\\":\\"fakepkgsha\\",\\"built_at\\":\\"2026-01-01T00:00:00Z\\"}\\n\' \\"' + commit_in_metadata + '\\" > ../artifacts/latest/demo.json"',
    )
    (repo / "PROJECT.yaml").write_text(yaml_text)
    run(repo, "git", "add", ".")
    run(repo, "git", "commit", "-m", "base + artifact build")
    run(repo, "git", "remote", "add", "origin", str(repo))
    return repo


def done_task_with_artifact(client, root, repo_name="demo", commit_in_metadata="$(git rev-parse HEAD)"):
    repo = make_repo_with_artifact_build(root, repo_name, commit_in_metadata)
    register(client, repo, repo_name)
    rid = [r for r in client.get("/api/repositories").json() if r["repo_name"] == repo_name][0]["id"]
    tid = create_task(client, f"Deploy {repo_name}", rid, risk="LOW")
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    submit_and_review(client, w, "PASS")
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["status"] == "DONE"
    mr = client.app.state.db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?", (tid, rid))
    return tid, rid, mr, repo


# --------------------------------------------------------- sandbox vs dev
def test_sandbox_and_dev_labels_are_distinct(client, git_repo):
    from tests.test_deployment import done_task
    root, _ = git_repo
    tid, rid, mr, repo = done_task(client, root, "svc-h1")
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "DEV"}, follow_redirects=False)
    html = client.get(f"/tasks/{tid}").text
    assert "DEV Deployment" in html
    assert "Persistent environment" in html
    assert "does not reset existing application data or user passwords" in html


# ------------------------------------------------------ artifact evidence
def test_artifact_metadata_parsed_from_real_build_output(client, git_repo):
    root, _ = git_repo
    tid, rid, mr, repo = done_task_with_artifact(client, root, "svc-h2")
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "DEV"}, follow_redirects=False)
    dep = latest_deployment_of(client, tid, rid)
    assert dep["status"] == "VERIFIED"
    assert dep["artifact_version"] == "1.0.0"
    assert dep["artifact_image"] == "demo:1.0.0"
    assert dep["artifact_digest"] == "sha256:fakedigest"
    assert dep["artifact_filename"] == "demo-1.0.0.zip"
    assert dep["artifact_sha256"] == "fakepkgsha"


def test_deployment_detail_renders_complete_evidence(client, git_repo):
    root, _ = git_repo
    tid, rid, mr, repo = done_task_with_artifact(client, root, "svc-h3")
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "DEV"}, follow_redirects=False)
    dep = latest_deployment_of(client, tid, rid)
    html = client.get(f"/deployments/{dep['id']}").text
    assert "1.0.0" in html
    assert "demo:1.0.0" in html
    assert "sha256:fakedigest" in html
    assert "demo-1.0.0.zip" in html
    assert "fakepkgsha" in html


def test_source_artifact_mismatch_blocks_deployment(client, git_repo):
    root, _ = git_repo
    # build writes a hard-coded WRONG source_commit, never the real HEAD.
    tid, rid, mr, repo = done_task_with_artifact(client, root, "svc-h4", commit_in_metadata="0000000000000000000000000000000000wrong")
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    r = client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "DEV"}, follow_redirects=False)
    assert r.status_code == 303
    dep = latest_deployment_of(client, tid, rid)
    assert dep["status"] == "FAILED"
    assert "ARTIFACT_SOURCE_MISMATCH" in dep["error"]


# ------------------------------------------------------------- rollback
def _verified_deployment(client, root, repo_name):
    tid, rid, mr, repo = done_task_with_artifact(client, root, repo_name)
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "DEV"}, follow_redirects=False)
    dep = latest_deployment_of(client, tid, rid)
    assert dep["status"] == "VERIFIED"
    return tid, rid, repo, dep


def test_rollback_unavailable_without_prior_verified_deployment(client, git_repo):
    root, _ = git_repo
    tid, rid, repo, dep1 = _verified_deployment(client, root, "svc-r1")
    # dep1 is the ONLY deployment -- nothing earlier to roll back to.
    assert client.app.state.deployer.rollback_target(dep1) is None
    html = client.get(f"/tasks/{tid}").text
    assert "Rollback" not in html


def test_rollback_uses_exact_prior_artifact_and_never_rebuilds(client, git_repo):
    root, _ = git_repo
    tid, rid, repo, dep1 = _verified_deployment(client, root, "svc-r2")
    # A second, failed deployment (simulate by forcing health failure).
    client.app.state.deployer.http_get = lambda *a, **k: (_ for _ in ()).throw(OSError("refused"))
    client.app.state.deployer.health_attempts = 1
    client.app.state.deployer.health_delay = 0.01
    client.post(f"/api/deployments/{dep1['id']}/redeploy", follow_redirects=False)
    dep2 = latest_deployment_of(client, tid, rid)
    assert dep2["status"] == "FAILED"

    client.app.state.deployer.image_exists = lambda ref: ref == dep1["artifact_image"]
    calls = []
    real_run_command = client.app.state.deployer._run_command
    def spy_run_command(deployment_id, phase, cmd, repo_path, raise_on_fail=True, env=None):
        calls.append((phase, env))
        return real_run_command(deployment_id, phase, cmd, repo_path, raise_on_fail=raise_on_fail, env=env)
    client.app.state.deployer._run_command = spy_run_command
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)

    r = client.post(f"/api/deployments/{dep2['id']}/rollback", follow_redirects=False)
    assert r.status_code == 303
    dep3 = latest_deployment_of(client, tid, rid)
    assert dep3["status"] == "ROLLED_BACK"
    assert dep3["source_commit"] == dep1["source_commit"]
    assert dep3["artifact_image"] == dep1["artifact_image"]
    assert dep3["rollback_of"] == dep2["id"]
    assert dep3["rollback_to_deployment_id"] == dep1["id"]
    # never a BUILDING phase -- rollback redeploys the retained image only.
    phases = [p for p, _ in calls]
    assert "BUILDING" not in phases
    deploy_calls = [env for phase, env in calls if phase == "DEPLOYING"]
    assert deploy_calls and deploy_calls[0] == {"MESFLOW_IMAGE": dep1["artifact_image"]}
    # historical VERIFIED deployment #1 is never mutated.
    assert client.app.state.db.one("SELECT status FROM deployments WHERE id=?", (dep1["id"],))["status"] == "VERIFIED"


def test_rollback_health_failure_marks_rollback_failed(client, git_repo):
    root, _ = git_repo
    tid, rid, repo, dep1 = _verified_deployment(client, root, "svc-r3")
    client.app.state.deployer.http_get = lambda *a, **k: (_ for _ in ()).throw(OSError("refused"))
    client.app.state.deployer.health_attempts = 1
    client.app.state.deployer.health_delay = 0.01
    client.post(f"/api/deployments/{dep1['id']}/redeploy", follow_redirects=False)
    dep2 = latest_deployment_of(client, tid, rid)
    assert dep2["status"] == "FAILED"

    client.app.state.deployer.image_exists = lambda ref: True
    # health stays broken for the rollback attempt too.
    r = client.post(f"/api/deployments/{dep2['id']}/rollback", follow_redirects=False)
    assert r.status_code == 303
    dep3 = latest_deployment_of(client, tid, rid)
    assert dep3["status"] == "ROLLBACK_FAILED"
    assert dep3["rollback_status"] == "FAILED"
    assert "HEALTH_FAILED" in dep3["rollback_error"]
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["status"] == "DONE"


def test_rollback_smoke_failure_marks_rollback_failed(client, git_repo):
    root, _ = git_repo
    tid, rid, repo, dep1 = _verified_deployment(client, root, "svc-r4")
    client.app.state.deployer.http_get = lambda *a, **k: (_ for _ in ()).throw(OSError("refused"))
    client.app.state.deployer.health_attempts = 1
    client.app.state.deployer.health_delay = 0.01
    client.post(f"/api/deployments/{dep1['id']}/redeploy", follow_redirects=False)
    dep2 = latest_deployment_of(client, tid, rid)
    assert dep2["status"] == "FAILED"

    client.app.state.deployer.image_exists = lambda ref: True
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    # patch the smoke command in PROJECT.yaml to fail for the rollback attempt
    txt = (repo / "PROJECT.yaml").read_text().replace("echo SMOKE_OK", "exit 1")
    (repo / "PROJECT.yaml").write_text(txt)
    run(repo, "git", "commit", "-am", "break smoke")

    r = client.post(f"/api/deployments/{dep2['id']}/rollback", follow_redirects=False)
    assert r.status_code == 303
    dep3 = latest_deployment_of(client, tid, rid)
    assert dep3["status"] == "ROLLBACK_FAILED"
    assert dep3["health_status"] == "PASS"
    assert dep3["smoke_status"] == "FAIL"


def test_rollback_success_is_verified_health_and_smoke(client, git_repo):
    root, _ = git_repo
    tid, rid, repo, dep1 = _verified_deployment(client, root, "svc-r5")
    client.app.state.deployer.http_get = lambda *a, **k: (_ for _ in ()).throw(OSError("refused"))
    client.app.state.deployer.health_attempts = 1
    client.app.state.deployer.health_delay = 0.01
    client.post(f"/api/deployments/{dep1['id']}/redeploy", follow_redirects=False)
    dep2 = latest_deployment_of(client, tid, rid)

    client.app.state.deployer.image_exists = lambda ref: True
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    client.post(f"/api/deployments/{dep2['id']}/rollback", follow_redirects=False)
    dep3 = latest_deployment_of(client, tid, rid)
    assert dep3["status"] == "ROLLED_BACK"
    assert dep3["health_status"] == "PASS"
    assert dep3["smoke_status"] == "PASS"
    assert dep3["rollback_status"] == "VERIFIED"
    html = client.get(f"/tasks/{tid}").text
    assert "ROLLED BACK" in html


def test_task_stays_done_across_failed_deploy_and_rollback(client, git_repo):
    root, _ = git_repo
    tid, rid, repo, dep1 = _verified_deployment(client, root, "svc-r6")
    client.app.state.deployer.http_get = lambda *a, **k: (_ for _ in ()).throw(OSError("refused"))
    client.app.state.deployer.health_attempts = 1
    client.app.state.deployer.health_delay = 0.01
    client.post(f"/api/deployments/{dep1['id']}/redeploy", follow_redirects=False)
    assert client.get(f"/api/tasks/{tid}/decision").json()["status"] == "DONE"
    dep2 = latest_deployment_of(client, tid, rid)
    client.app.state.deployer.image_exists = lambda ref: True
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    client.post(f"/api/deployments/{dep2['id']}/rollback", follow_redirects=False)
    assert client.get(f"/api/tasks/{tid}/decision").json()["status"] == "DONE"


def test_deployment_history_ordering(client, git_repo):
    root, _ = git_repo
    tid, rid, repo, dep1 = _verified_deployment(client, root, "svc-r7")
    client.post(f"/api/deployments/{dep1['id']}/redeploy", follow_redirects=False)
    dep2 = latest_deployment_of(client, tid, rid)
    html = client.get(f"/deployments/{dep2['id']}").text
    assert f"#{dep2['id']}" in html and f"#{dep1['id']}" in html
    # newest first
    assert html.index(f"#{dep2['id']}") < html.index(f"#{dep1['id']}")
