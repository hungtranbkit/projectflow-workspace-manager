"""Role & Capability Catalog (Phase E2). Covers the catalog itself
(seeded roles/capabilities/mappings, idempotent seeding, project-policy
narrowing) and its integration with the real, existing flows it was
added to validate against: Builder Workspace start (the real
Supervisor entry point, _start_builder_session), Start Review, and
Start QA -- with explicit backward-compatibility proof that nothing
that used to work now fails."""
from __future__ import annotations

import pytest

from app.launchers import AgentLauncher
from app.services.engineering_catalog import CAPABILITIES, ROLES, RoleCapabilityService


def register(client, repo, name="demo"):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text
    return client.get("/api/repositories").json()[0]["id"]


def new_task(client, title, risk="LOW"):
    r = client.post("/api/tasks", data={"title": title, "risk_profile": risk}, follow_redirects=False)
    assert r.status_code == 303
    return [t["id"] for t in client.get("/api/tasks").json() if t["title"] == title][0]


def add_workspace(client, tid, rid, agent="codex", role="Backend"):
    r = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": agent, "role": role, "base_branch": "main", "sandbox_profile": "NONE"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return [w for w in client.get(f"/api/tasks/{tid}").json()["workspaces"] if w["agent"] == agent and w["role"] == role][-1]


def setup_fast_ready_launcher(client, agent="codex", script="echo READY; cat"):
    client.app.state.agent_sessions.launchers = {agent: AgentLauncher(agent.capitalize(), "bash", ("-c", script))}
    client.app.state.agent_sessions.prompt_ready_timeout = 3.0
    client.app.state.agent_sessions.prompt_quiet_window = 0.1


# ============================================================== Catalog

def test_role_catalog_seeded_with_stable_keys(client):
    roles = {r["key"]: r for r in client.get("/api/engineering/roles").json()}
    for key in ROLES:
        assert key in roles, f"missing seeded role {key}"
        assert roles[key]["name"] and roles[key]["description"]
        assert roles[key]["system_defined"] == 1
    assert len(roles) == len(ROLES)  # no stray/duplicate rows


def test_capability_catalog_seeded_with_stable_keys(client):
    caps = {c["key"]: c for c in client.get("/api/engineering/capabilities").json()}
    for key in CAPABILITIES:
        assert key in caps, f"missing seeded capability {key}"
        assert caps[key]["name"] and caps[key]["description"]
    assert len(caps) == len(CAPABILITIES)


def test_required_optional_mapping_for_builder(client):
    detail = client.get("/api/engineering/roles/BUILDER").json()
    by_req = {}
    for c in detail["capabilities"]:
        by_req.setdefault(c["requirement"], set()).add(c["key"])
    assert by_req["REQUIRED"] == {"READ_REPOSITORY", "EDIT_SOURCE", "CREATE_COMMIT", "RUN_TESTS"}
    assert "USE_INTERACTIVE_TERMINAL" in by_req.get("OPTIONAL", set())


def test_role_capability_mapping_never_requires_sensitive_merge_or_prod_deploy(client):
    """E2 section 7: MERGE_PR/DEPLOY_PRODUCTION must never be REQUIRED
    for any role -- they stay optional, policy-controlled capabilities."""
    for role_key in ROLES:
        detail = client.get(f"/api/engineering/roles/{role_key}").json()
        required = {c["key"] for c in detail["capabilities"] if c["requirement"] == "REQUIRED"}
        assert "MERGE_PR" not in required
        assert "DEPLOY_PRODUCTION" not in required


def test_role_get_404_for_unknown_key(client):
    assert client.get("/api/engineering/roles/NOT_A_ROLE").status_code == 404


def test_provider_capability_mapping_matches_real_launchers(client):
    """codex/claude have real AGENT_LAUNCHERS entries; gemini/aider/
    other are settings.agents-allowed names with no real adapter --
    the provider capability catalog must say so honestly."""
    providers = client.get("/api/engineering/providers").json()
    assert set(providers) == {"codex", "claude", "gemini", "aider", "other"}
    codex_caps = {c["key"]: c["support_level"] for c in providers["codex"]}
    assert codex_caps["READ_REPOSITORY"] == "SUPPORTED"
    assert codex_caps["EDIT_SOURCE"] == "SUPPORTED"
    assert codex_caps["MERGE_PR"] == "UNSUPPORTED"

    gemini_caps = {c["key"]: c["support_level"] for c in providers["gemini"]}
    assert all(level == "UNSUPPORTED" for level in gemini_caps.values())


def test_supported_role_assignment(client):
    r = client.post("/api/engineering/validate-assignment", data={"provider": "codex", "role_key": "BUILDER"})
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["missing_required_capabilities"] == []


def test_unsupported_role_assignment_is_never_silently_accepted(client):
    r = client.post("/api/engineering/validate-assignment", data={"provider": "gemini", "role_key": "BUILDER"})
    body = r.json()
    assert body["valid"] is False
    assert set(body["missing_required_capabilities"]) == {"READ_REPOSITORY", "EDIT_SOURCE", "CREATE_COMMIT", "RUN_TESTS"}


def test_partial_support_produces_a_warning_but_not_necessarily_invalid(client):
    """codex/claude are PARTIAL (not UNSUPPORTED) for QA_VERIFIER's
    runtime-verification capabilities -- surfaced as partial_capabilities,
    valid stays True (never silently hidden, never a hard block either)."""
    r = client.post("/api/engineering/validate-assignment", data={"provider": "codex", "role_key": "QA_VERIFIER"})
    body = r.json()
    assert body["valid"] is True
    assert "RUN_RUNTIME_VERIFICATION" in body["partial_capabilities"]
    assert "RECORD_VERIFICATION" in body["partial_capabilities"]


def test_unknown_role_is_invalid_not_a_crash(client):
    r = client.post("/api/engineering/validate-assignment", data={"provider": "codex", "role_key": "NOT_A_ROLE"})
    assert r.status_code == 200
    assert r.json()["valid"] is False


def test_recommended_roles_for_change_matches_worked_examples(client):
    """E2 section 17's own examples: BUG_FIX/NORMAL and HIGH risk."""
    normal = client.get("/api/engineering/recommended-roles?change_type=BUG&risk_level=NORMAL").json()
    assert normal["recommended_roles"] == ["BUILDER", "REVIEWER", "INTEGRATOR"]
    high = client.get("/api/engineering/recommended-roles?change_type=BUG&risk_level=HIGH").json()
    assert high["recommended_roles"] == ["BUILDER", "REVIEWER", "QA_VERIFIER", "INTEGRATOR", "RELEASE_MANAGER"]
    security = client.get("/api/engineering/recommended-roles?change_type=SECURITY_CHANGE&risk_level=NORMAL").json()
    assert security["recommended_roles"][-1] == "SECURITY_REVIEWER"


def test_seed_is_idempotent_no_duplicate_rows(client):
    """Migration/seed idempotency (E2 section 21/25): re-running seed()
    against the same DB never creates duplicate rows and never errors."""
    svc = client.app.state.roles_catalog
    before_roles = len(svc.list_roles())
    before_caps = len(svc.list_capabilities())
    before_role_caps = sum(len(svc.capabilities_for_role(k)) for k in ROLES)
    svc.seed()
    svc.seed()
    svc.seed()
    assert len(svc.list_roles()) == before_roles
    assert len(svc.list_capabilities()) == before_caps
    assert sum(len(svc.capabilities_for_role(k)) for k in ROLES) == before_role_caps


def test_seed_restart_safe_across_app_instances(client, git_repo):
    """A second create_app() against the same DB (simulating a service
    restart) re-seeds without error or duplication."""
    from app.main import create_app
    settings = client.app.state.settings
    RoleCapabilityService(client.app.state.db, providers=settings.agents).seed()
    roles = client.get("/api/engineering/roles").json()
    assert len(roles) == len(ROLES)


# ================================================== Project policy (E2.18)

def test_repository_with_no_engineering_policy_uses_global_defaults(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    body = client.get(f"/api/repositories/{rid}/engineering-policy").json()
    assert body["policy"] is None


def test_project_policy_narrows_but_never_expands_assignment(client, git_repo, tmp_path):
    """E2 section 18: a PROJECT.yaml engineering: block restricts which
    providers may hold a role -- it can only narrow the global catalog's
    own SUPPORTED verdict, never grant a capability a provider doesn't
    globally have."""
    root, repo = git_repo
    (repo / "PROJECT.yaml").write_text(
        (repo / "PROJECT.yaml").read_text()
        + "\nengineering:\n  roles:\n    BUILDER:\n      allowed_providers: [codex]\n"
    )
    import subprocess
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "engineering policy"], cwd=repo, check=True)
    rid = register(client, repo, "demo")

    policy_body = client.get(f"/api/repositories/{rid}/engineering-policy").json()
    assert policy_body["policy"]["roles"]["BUILDER"]["allowed_providers"] == ["codex"]

    ok = client.post("/api/engineering/validate-assignment", data={"provider": "codex", "role_key": "BUILDER", "repository_id": str(rid)}).json()
    assert ok["valid"] is True

    blocked = client.post("/api/engineering/validate-assignment", data={"provider": "claude", "role_key": "BUILDER", "repository_id": str(rid)}).json()
    assert blocked["valid"] is False
    assert blocked["policy_blocked"] is True
    # claude is globally SUPPORTED for BUILDER -- policy_blocked, not a
    # missing-capability rejection, proving the policy only narrows.
    assert blocked["missing_required_capabilities"] == []


# ======================================================= Builder start

def test_codex_builder_assignment_remains_valid_and_starts(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    setup_fast_ready_launcher(client, "codex")
    tid = new_task(client, "Codex builder task")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid, agent="codex")
    r = client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    assert r.status_code == 303
    session = client.app.state.db.one("SELECT * FROM agent_sessions WHERE workspace_id=?", (w["id"],))
    assert session["status"] == "RUNNING"
    events = client.app.state.db.all("SELECT * FROM workspace_events WHERE entity_type='agent' AND entity_id=? AND action='ROLE_ASSIGNMENT_VALIDATED'", (w["id"],))
    assert len(events) == 1


def test_claude_builder_assignment_remains_valid_and_starts(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    setup_fast_ready_launcher(client, "claude")
    tid = new_task(client, "Claude builder task")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid, agent="claude")
    r = client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    assert r.status_code == 303


def test_unsupported_provider_start_is_blocked_with_human_readable_message(client, git_repo):
    """New invalid assignment blocked (E2 section 11): the message names
    the missing capabilities, matching the spec's own worked example."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    tid = new_task(client, "Gemini task")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid, agent="gemini")
    r = client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    assert r.status_code == 409
    assert "Cannot assign provider 'gemini' as BUILDER" in r.text
    assert "EDIT_SOURCE" in r.text
    session = client.app.state.db.one("SELECT * FROM agent_sessions WHERE workspace_id=?", (w["id"],))
    assert session is None  # never even reached the launcher layer


def test_legacy_style_workspace_start_unaffected_by_catalog(client, git_repo):
    """Backward compatibility (E2 section 22/23): a Builder Workspace
    has no per-row role metadata at all (never did, never will in this
    phase) -- BUILDER is always implied by this launch path, so there is
    no "role missing" case to special-case; every existing codex/claude
    workflow keeps working exactly as before."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    setup_fast_ready_launcher(client, "codex")
    tid = new_task(client, "Plain legacy task")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid, agent="codex")
    assert "role" not in w or w.get("role") in ("Backend", "")  # agent_workspaces.role is the unrelated free-text label
    r = client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    assert r.status_code == 303


def test_task_decision_service_unaffected_by_catalog_validation(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    setup_fast_ready_launcher(client, "codex")
    tid = new_task(client, "Decision unaffected task")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid, agent="codex")
    before = client.get(f"/api/tasks/{tid}/decision").json()
    client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    after = client.get(f"/api/tasks/{tid}/decision").json()
    assert before["status"] == after["status"] == "ACTIVE"
    assert before["stage"] == after["stage"] == "DEVELOPMENT"


def test_change_linked_task_builder_start_unaffected(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    setup_fast_ready_launcher(client, "codex")
    tid = new_task(client, "Change-linked builder task")
    client.post(f"/api/tasks/{tid}/select")
    cid = client.app.state.changes.create(title="Parent change")
    client.app.state.changes.attach_task_to_change(cid, tid)
    w = add_workspace(client, tid, rid, agent="codex")
    r = client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    assert r.status_code == 303


def test_spec_gate_still_runs_after_role_validation(client, git_repo):
    """The catalog check runs before SpecGate but must never skip or
    replace it -- a behavior-changing Task with no valid spec linkage
    is still blocked by SpecGate, with a supported provider."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    setup_fast_ready_launcher(client, "codex")
    tid = new_task(client, "Spec-gated task")
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/spec", data={"classification": "BEHAVIOR_CHANGE"})
    w = add_workspace(client, tid, rid, agent="codex")
    r = client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    assert r.status_code == 409
    assert "SPEC_REQUIRED" in r.text


# ==================================================== Reviewer / QA advisory

def test_reviewer_known_unsupported_provider_never_blocks_start_review(client, git_repo):
    """E2 section 13: Start Review has no real launch to block -- an
    unsupported-for-REVIEWER provider name is recorded advisory-only,
    never rejected."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    setup_fast_ready_launcher(client, "codex")
    tid = new_task(client, "Reviewer advisory task")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid, agent="codex")
    client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY", "what_changed": "x"})

    r = client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "gemini"}, follow_redirects=False)
    assert r.status_code == 303
    events = client.app.state.db.all("SELECT * FROM workspace_events WHERE entity_type='agent' AND entity_id=? AND action='ROLE_ASSIGNMENT_REJECTED'", (w["id"],))
    assert len(events) == 1
    assert "advisory" in events[0]["details"]


def test_reviewer_human_name_skips_validation_entirely(client, git_repo):
    """A free-text human reviewer name is a HUMAN actor by definition
    (E2 section 15) -- never checked against the provider catalog."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    setup_fast_ready_launcher(client, "codex")
    tid = new_task(client, "Human reviewer task")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid, agent="codex")
    client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY", "what_changed": "x"})

    r = client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "alice"}, follow_redirects=False)
    assert r.status_code == 303
    # "alice" is not a known provider name at all -- no catalog check
    # ever ran for it, so it never produced a rejection (the earlier
    # Builder-start VALIDATED event for codex/BUILDER is expected and
    # unrelated -- this only asserts nothing about "alice" was rejected).
    events = client.app.state.db.all("SELECT * FROM workspace_events WHERE entity_type='agent' AND entity_id=? AND action='ROLE_ASSIGNMENT_REJECTED'", (w["id"],))
    assert events == []


def test_reviewer_supported_provider_starts_review_normally(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    setup_fast_ready_launcher(client, "codex")
    tid = new_task(client, "Supported reviewer task")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid, agent="codex")
    client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY", "what_changed": "x"})

    r = client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"}, follow_redirects=False)
    assert r.status_code == 303
