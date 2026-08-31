"""B2 -- Release Concurrency Correctness & Residual Security
Verification (docs/B2_RELEASE_CONCURRENCY_AND_RESIDUAL_SECURITY.md).

B2.1: real threading.Thread concurrency against a real SQLite file DB
(same established pattern as tests/test_productization_audit.py's own
test_concurrent_plan_creation_never_collides_on_revision) -- proves
ReleaseService.create_release()'s auto-increment version race is fixed
with the same bounded-retry-on-collision shape already proven for
plans.revision/execution_waves.wave_number, and that an EXPLICIT
version's race window still surfaces a clean ReleaseError, never a
raw exception.

B2.2: a real render of task_detail.html (the actual Jinja template,
actual route, actual DB) proving the resume_form/block_form `|safe`
usage cannot echo a non-whitelisted agent value as raw HTML."""
from __future__ import annotations
import subprocess
import threading

from tests.test_autonomous_execution import register, new_change, materialize_task
from tests.test_worktree_manager import _select_and_create_workspace
from tests.test_review_fix_loop import set_fake, PASS
from tests.test_release_pipeline import make_release_repo
from app.services.release_service import ReleaseError


def _db(client):
    return client.app.state.db


def _merged_task(client, repo, rid, title, unique_file):
    """Adapted from test_release_pipeline.py's own
    _reviewed_and_integrated_task, with two changes needed to build
    SEVERAL independent merged tasks against the SAME repo (that helper
    is only ever called once per repo in its own file): (a) its own
    Change per task -- materialize_task() hardcodes plan revision=1,
    which collides if two tasks share one Change; a real Release
    doesn't require its tasks to share a Change (ReleaseService.
    create_release() only reads task_ids[0]'s own change_id for
    baseline-WorkProduct linkage); (b) each task writes its OWN new
    file rather than editing the shared src.py/test_src.py the base
    repo fixture ships -- editing the same lines a prior task in this
    same test already merged into main leaves nothing to commit."""
    cid = new_change(client, f"B2 change for {title}", project_id=rid)
    tid, _ = materialize_task(client, cid, title=title, scope_hints=[unique_file])
    w = _select_and_create_workspace(client, tid, rid, agent="claude")
    worktree = client.app.state.git.validate_worktree(w["worktree_path"])
    (worktree / unique_file).write_text(f"def {unique_file[:-3]}():\n    return {title!r}\n")
    subprocess.run(["git", "add", unique_file], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", f"add {unique_file}"], cwd=worktree, check=True, capture_output=True)
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"}, follow_redirects=False)
    set_fake(client, PASS)
    review = client.post(f"/api/tasks/{tid}/review/code").json()
    assert review["outcome"] == "REVIEWED" and review["verdict"] == "PASS", review
    integ = client.post(f"/api/tasks/{tid}/integrate").json()
    assert integ["outcome"] == "INTEGRATED", integ
    return tid


# ================================================================ B2.1: auto-increment version race
def test_concurrent_auto_version_releases_never_collide(client, git_repo):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel-b2-1", port=19101)
    rid = register(client, repo, "demo")

    n = 4
    task_ids = [_merged_task(client, repo, rid, title=f"B2 feature {i}", unique_file=f"feature_a{i}.py") for i in range(n)]

    svc = client.app.state.release_service
    results, errors = [], []

    def run(tid):
        try:
            r = svc.create_release(rid, [tid])  # version=None -- the racy auto-increment path
            results.append(r)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(tid,)) for tid in task_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, errors
    assert len(results) == n, results
    versions = sorted(r["version"] for r in results)
    assert len(set(versions)) == n, f"collided versions: {versions}"
    assert versions == [f"v{i}" for i in range(1, n + 1)], versions

    # No orphaned RELEASE_MANIFEST WorkProduct -- exactly one per
    # actually-created release, never one left behind by a retried,
    # ultimately-losing attempt.
    wp_count = _db(client).one(
        "SELECT COUNT(*) c FROM work_products WHERE kind='RELEASE_MANIFEST'")["c"]
    assert wp_count == n, wp_count


# ================================================================ B2.1: explicit version -- collision is a real error, never masked
def test_explicit_version_collision_raises_clean_error_not_generic_retry_exhausted(client, git_repo):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel-b2-2", port=19102)
    rid = register(client, repo, "demo")
    tid1 = _merged_task(client, repo, rid, title="B2 first", unique_file="feature_b1.py")
    tid2 = _merged_task(client, repo, rid, title="B2 second", unique_file="feature_b2.py")

    svc = client.app.state.release_service
    r1 = svc.create_release(rid, [tid1], version="v9.9.9")
    assert r1["version"] == "v9.9.9"

    try:
        svc.create_release(rid, [tid2], version="v9.9.9")
        assert False, "expected ReleaseError on duplicate explicit version"
    except ReleaseError as exc:
        assert "already exists" in str(exc), str(exc)
        assert "concurrent creation contention" not in str(exc), \
            "explicit-version collision must give the clear duplicate message, not the generic retry-exhausted one"


def test_explicit_version_race_window_also_raises_clean_error(client, git_repo):
    """The TOCTOU race B2.1 also had to close: two callers both resolve
    the SAME explicit version, both pass the pre-check (neither has
    committed yet), and race on the INSERT itself -- proven with real
    threads, not just the sequential pre-check case above."""
    root, _ = git_repo
    repo = make_release_repo(root, name="rel-b2-3", port=19103)
    rid = register(client, repo, "demo")
    n = 3
    task_ids = [_merged_task(client, repo, rid, title=f"B2 race {i}", unique_file=f"feature_c{i}.py") for i in range(n)]

    svc = client.app.state.release_service
    results, clean_errors, dirty_errors = [], [], []

    def run(tid):
        try:
            r = svc.create_release(rid, [tid], version="v-shared-race")
            results.append(r)
        except ReleaseError as exc:
            clean_errors.append(exc)
        except Exception as exc:
            dirty_errors.append(exc)

    threads = [threading.Thread(target=run, args=(tid,)) for tid in task_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not dirty_errors, dirty_errors  # never a raw sqlite exception
    assert len(results) == 1, results  # exactly one winner
    assert len(clean_errors) == n - 1, clean_errors
    for exc in clean_errors:
        assert "already exists" in str(exc), str(exc)
    rows = _db(client).all("SELECT id FROM releases WHERE repository_id=? AND version=?", (rid, "v-shared-race"))
    assert len(rows) == 1, rows


# ================================================================ B2.2: |safe usage -- non-whitelisted agent cannot inject raw HTML
def test_resume_form_ignores_non_whitelisted_agent(client, git_repo):
    """Direct-code-audit finding (docs/B2_RELEASE_CONCURRENCY_AND_
    RESIDUAL_SECURITY.md): resume_form/block_form are built inside a
    Jinja {% set %} block, which autoescapes its own interior {{ }}
    output exactly like anywhere else -- |safe only skips re-escaping
    the already-safe result. The `w.agent in ['codex','claude']` guard
    means a non-whitelisted value renders NOTHING for resume_form at
    all. This test tampers with agent_workspaces.agent directly (bypassing
    the normal create_workspace() validation) to prove the TEMPLATE's
    own guard holds even if a value the app itself would never normally
    let through somehow reached this column -- real defense in depth,
    not just 'the create route already validates it'."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "B2 XSS verification change", project_id=rid)
    tid, _ = materialize_task(client, cid, title="XSS check task", scope_hints=["src.py"])
    w = _select_and_create_workspace(client, tid, rid, agent="claude")

    malicious = "<script>alert(document.cookie)</script>"
    _db(client).execute("UPDATE agent_workspaces SET agent=? WHERE id=?", (malicious, w["id"]))

    r = client.get(f"/tasks/{tid}")
    assert r.status_code == 200, r.text
    # Never present raw (would mean the guard/escaping failed) --
    # Jinja's own autoescape would also turn it into &lt;script&gt; if
    # it were ever rendered at all, but the real guard here is that a
    # non-whitelisted agent renders resume_form as empty in the first
    # place (see the `{% if w.agent in [...] %}` check).
    assert "<script>alert(document.cookie)</script>" not in r.text
    assert malicious not in r.text
