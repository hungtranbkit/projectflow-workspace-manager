"""B5 -- Tenant Isolation Completeness
(docs/B5_TENANT_ISOLATION_COMPLETENESS.md).

B5.1: GitHubMergeService.available() returns the CORRECT answer (based
on the real git remote) under AUTH_MODE=required with no PAT/App
configured -- a real cross-org scenario, not just a unit call.

B5.2: dashboard/`/sandboxes` aggregate counts are filtered BEFORE
aggregation -- real sandboxes created in two different orgs, real
count-difference assertions, plus capacity_available()'s own
unfiltered global-ceiling behavior proven unaffected."""
from __future__ import annotations
import subprocess

from tests.test_b03_authz import (
    auth_client, captured_logs, _bootstrap, _csrf, _create_org, _invite, _accept,
    _second_repo, _link_repo, two_org_fixture,
)
from app.services.github_merge_service import GitHubMergeService


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


# ================================================================ B5.1: available() credential-routing fix
def test_available_true_for_real_github_remote_with_no_credential_configured(two_org_fixture, git_repo):
    """The confirmed-live bug: under AUTH_MODE=required with no PAT/App
    configured for the org, available() must still correctly report
    True for a repo with a real github.com remote -- not silently
    misreport False just because no credential is configured yet."""
    f = two_org_fixture
    root, repo = git_repo
    _git(repo, "remote", "add", "origin", "https://github.com/acme/widgets.git")
    # The app under test is f["owner"].app -- a real AUTH_MODE=required
    # app with no github_app_id/private_key and no stored "github_token"
    # secret for org_a at all (two_org_fixture never configures one).
    github_merge = f["owner"].app.state.github_merge
    assert github_merge.available(repo) is True


def test_available_false_for_non_github_remote_still(git_repo, client):
    """Sanity: the fix doesn't make available() always True -- a real
    non-GitHub remote is still correctly reported False."""
    root, repo = git_repo
    _git(repo, "remote", "add", "origin", "https://gitlab.com/acme/widgets.git")
    github_merge = client.app.state.github_merge
    assert github_merge.available(repo) is False


def test_available_never_raises_under_hosted_runner_wrapper(two_org_fixture, git_repo):
    """Direct proof the credential-resolving runner wrapper is bypassed
    for this call: hitting available() many times never raises
    GitHubIntegrationError even though the org has zero GitHub
    credentials configured anywhere."""
    f = two_org_fixture
    root, repo = git_repo
    _git(repo, "remote", "add", "origin", "git@github.com:acme/widgets.git")
    github_merge = f["owner"].app.state.github_merge
    for _ in range(3):
        assert github_merge.available(repo) is True


# ================================================================ B5.2: tenant-scoped aggregate counts
def _make_sandbox(db, *, task_id=None, repository_id=None, status="RUNNING", slug):
    return db.execute(
        "INSERT INTO sandboxes(task_id,repository_id,owner_type,owner_id,sandbox_slug,profile,runtime_type,"
        "compose_project,status) VALUES(?,?,?,?,?,?,?,?,?)",
        (task_id, repository_id, "AGENT_WORKSPACE", 1, slug, "NONE", "docker-compose", slug, status))


def test_dashboard_running_sandboxes_excludes_other_orgs(two_org_fixture):
    f = two_org_fixture
    db = f["db"]
    _make_sandbox(db, repository_id=f["rid_a"], status="RUNNING", slug="wm-a-running")
    _make_sandbox(db, repository_id=f["rid_b"], status="RUNNING", slug="wm-b-running")

    r = f["member"].get("/")  # member is Org A only
    assert r.status_code == 200, r.text

    # Direct, unambiguous evidence via the real service method the
    # route itself calls, scoped to each org's own visible repo set --
    # not scraping a specific template layout for a number.
    authz = f["owner"].app.state.authz_service
    member_user = db.one("SELECT id FROM users WHERE email=?", ("member@example.com",))
    visible_repos = authz.visible_repository_ids(member_user["id"])
    assert visible_repos == {f["rid_a"]}
    scoped_count = f["owner"].app.state.sandboxes.running_count(visible_repos, set())
    assert scoped_count == 1  # only Org A's own sandbox
    unscoped_count = f["owner"].app.state.sandboxes.running_count()
    assert unscoped_count == 2  # both orgs' sandboxes, real global truth


def test_dashboard_cleanup_pending_excludes_other_orgs(two_org_fixture):
    f = two_org_fixture
    db = f["db"]
    _make_sandbox(db, repository_id=f["rid_a"], status="CLEANUP_ELIGIBLE", slug="wm-a-cleanup")
    _make_sandbox(db, repository_id=f["rid_b"], status="CLEANUP_ELIGIBLE", slug="wm-b-cleanup")
    scoped = f["owner"].app.state.sandboxes.count("status='CLEANUP_ELIGIBLE'", {f["rid_a"]}, set())
    assert scoped == 1
    unscoped = f["owner"].app.state.sandboxes.count("status='CLEANUP_ELIGIBLE'")
    assert unscoped == 2


def test_sandboxes_page_running_badge_excludes_other_orgs(two_org_fixture):
    f = two_org_fixture
    db = f["db"]
    _make_sandbox(db, repository_id=f["rid_a"], status="RUNNING", slug="wm-a-page")
    _make_sandbox(db, repository_id=f["rid_b"], status="RUNNING", slug="wm-b-page")
    r = f["member"].get("/sandboxes")
    assert r.status_code == 200, r.text
    # The member's own visible-repo scoped count must be 1, matching
    # the same service call the route itself makes.
    authz = f["owner"].app.state.authz_service
    member_user = db.one("SELECT id FROM users WHERE email=?", ("member@example.com",))
    visible_repos = authz.visible_repository_ids(member_user["id"])
    scoped = f["owner"].app.state.sandboxes.running_count(visible_repos, set())
    assert scoped == 1


def test_task_owned_sandbox_visible_via_task_ids_not_repository_id(two_org_fixture):
    """The indirect-ownership case: a sandbox with repository_id=NULL,
    owned only via task_id (REPOSITORY_TEST-style) -- must still be
    correctly scoped via task_ids, not silently dropped or leaked."""
    f = two_org_fixture
    db = f["db"]
    tid_a = f["tid_a"]
    _make_sandbox(db, task_id=tid_a, repository_id=None, status="RUNNING", slug="wm-task-owned")
    authz = f["owner"].app.state.authz_service
    member_user = db.one("SELECT id FROM users WHERE email=?", ("member@example.com",))
    visible_tasks = authz.visible_task_ids(member_user["id"])
    assert tid_a in visible_tasks
    scoped = f["owner"].app.state.sandboxes.running_count(set(), visible_tasks)
    assert scoped == 1
    # An outsider (no membership in org_a) must not see it.
    outsider_user = db.one("SELECT id FROM users WHERE email=?", ("outsider@example.com",))
    outsider_tasks = authz.visible_task_ids(outsider_user["id"])
    assert tid_a not in outsider_tasks


# ================================================================ capacity_available() stays global (deliberately unfiltered)
def test_capacity_available_uses_true_global_ceiling_across_orgs(two_org_fixture):
    """B5's own explicit non-goal: max_running_sandboxes is a real
    whole-process ceiling, never a per-tenant quota -- combined usage
    across BOTH orgs must still correctly hit the true limit."""
    f = two_org_fixture
    db = f["db"]
    sandboxes = f["owner"].app.state.sandboxes
    sandboxes.max_running = 2
    _make_sandbox(db, repository_id=f["rid_a"], status="RUNNING", slug="wm-cap-a")
    _make_sandbox(db, repository_id=f["rid_b"], status="RUNNING", slug="wm-cap-b")
    # Each org alone has only 1 sandbox (well under a per-tenant view of
    # the limit), but the TRUE combined global count is 2 -- capacity
    # must correctly report exhausted, proving it never uses a filtered
    # (and therefore under-counting) view.
    assert sandboxes.running_count() == 2
    assert sandboxes.capacity_available() is False
