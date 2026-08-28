from __future__ import annotations
import json
import subprocess
from pathlib import Path

"""Real GitHub PR/merge execution via the `gh` CLI -- the same
authenticated integration already used interactively in this
environment (`gh auth status`), never a hand-rolled OAuth flow or a
raw access token handled by this app. Every call is scoped to a
specific, already-registered repository's real filesystem path; there
is no code path that accepts a repo owner/name or PR number from the
browser (section 18) -- callers always derive those from the
Repository row + the persisted MergeRecord."""

PR_FIELDS = "number,url,state,mergeable,mergeStateStatus,headRefOid,baseRefName,statusCheckRollup,mergedAt,mergeCommit,title"

FAIL_CONCLUSIONS = {"FAILURE", "CANCELLED", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED"}

STRATEGY_FLAGS = {"MERGE_COMMIT": "--merge", "SQUASH": "--squash", "REBASE": "--rebase"}


class GitHubIntegrationError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _default_runner(argv: list[str], cwd, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(cwd), text=True, capture_output=True, timeout=timeout)


class GitHubMergeService:
    """`runner` is injectable (argv, cwd) -> CompletedProcess, same DI
    pattern as AgentSessionManager.which / TerminalLauncherService --
    tests substitute a fake that never actually shells out to `gh`,
    while production uses the real subprocess.run wrapper."""

    def __init__(self, runner=_default_runner):
        self.runner = runner

    def available(self, repo_path) -> bool:
        """Cheap, local-only check (no network): the repo's origin
        remote must actually be a github.com URL. Whether `gh` is
        authenticated is discovered by the real API calls themselves
        (create_pr/pr_status/merge_pr), never assumed true just because
        the remote looks right."""
        try:
            r = self.runner(["git", "remote", "get-url", "origin"], repo_path)
            return r.returncode == 0 and "github.com" in (r.stdout or "")
        except Exception:
            return False

    def push_branch(self, repo_path, branch: str) -> None:
        """Real `git push` of the exact verified source branch to origin
        -- Create PR always pushes first. Neither a Builder Workspace's
        branch nor a Task Integration branch is ever pushed anywhere
        automatically elsewhere in this app (both only ever exist as
        local worktree branches until this point), so `gh pr create`
        would otherwise fail with GitHub's own honest 'no commits
        between main and <branch>' / 'Head ref must be a branch' error
        -- exactly what it does when the ref simply doesn't exist on the
        remote yet. Never force-pushed (this app never force-pushes
        anywhere); a genuinely diverged remote branch fails loudly here
        rather than being silently overwritten."""
        r = self.runner(["git", "push", "origin", f"{branch}:{branch}"], repo_path)
        if r.returncode != 0:
            raise GitHubIntegrationError("PUSH_FAILED", (r.stderr or r.stdout or "git push failed").strip())

    def find_existing_pr(self, repo_path, head_branch: str, base_branch: str) -> dict | None:
        r = self.runner(
            ["gh", "pr", "list", "--head", head_branch, "--base", base_branch, "--state", "all",
             "--json", "number,url,state", "--limit", "1"],
            repo_path,
        )
        if r.returncode != 0:
            raise GitHubIntegrationError("GH_CLI_ERROR", (r.stderr or r.stdout or "gh pr list failed").strip())
        rows = json.loads(r.stdout or "[]")
        return rows[0] if rows else None

    def create_pr(self, repo_path, head_branch: str, base_branch: str, title: str, body: str) -> dict:
        """Never creates a duplicate: callers must check find_existing_pr()
        first (section 3), but `gh pr create` itself also refuses a
        second PR for the same head/base and this surfaces that as a
        GitHubIntegrationError rather than silently succeeding."""
        r = self.runner(
            ["gh", "pr", "create", "--head", head_branch, "--base", base_branch, "--title", title, "--body", body],
            repo_path,
        )
        if r.returncode != 0:
            raise GitHubIntegrationError("PR_CREATE_FAILED", (r.stderr or r.stdout or "gh pr create failed").strip())
        lines = [l for l in (r.stdout or "").strip().splitlines() if l.strip()]
        url = lines[-1] if lines else None
        number = None
        if url and "/pull/" in url:
            try:
                number = int(url.rsplit("/pull/", 1)[1].split("/")[0].strip())
            except ValueError:
                number = None
        if number is None:
            raise GitHubIntegrationError("PR_CREATE_UNPARSEABLE", f"could not parse PR number from: {r.stdout!r}")
        return self.pr_status(repo_path, number)

    def pr_status(self, repo_path, pr_number: int) -> dict:
        r = self.runner(["gh", "pr", "view", str(pr_number), "--json", PR_FIELDS], repo_path)
        if r.returncode != 0:
            raise GitHubIntegrationError("PR_FETCH_FAILED", (r.stderr or r.stdout or "gh pr view failed").strip())
        return self._parse(json.loads(r.stdout))

    def merge_pr(self, repo_path, pr_number: int, strategy: str) -> dict:
        """The real merge call -- never a state-only DB write standing in
        for it. Returns the fresh, post-merge PR status (including the
        real merge commit SHA read back from GitHub, never guessed)."""
        flag = STRATEGY_FLAGS.get(strategy, "--merge")
        r = self.runner(["gh", "pr", "merge", str(pr_number), flag, "--delete-branch=false"], repo_path)
        if r.returncode != 0:
            raise GitHubIntegrationError("MERGE_FAILED", (r.stderr or r.stdout or "gh pr merge failed").strip())
        return self.pr_status(repo_path, pr_number)

    def target_head(self, repo_path, base_branch: str) -> str | None:
        """Current remote HEAD of the target branch, after a real fetch
        -- used for TARGET_BRANCH_CHANGED detection and for the manual
        external-merge ancestry check."""
        self.runner(["git", "fetch", "origin", base_branch], repo_path)
        r = self.runner(["git", "rev-parse", f"origin/{base_branch}"], repo_path)
        return r.stdout.strip() if r.returncode == 0 else None

    def is_ancestor(self, repo_path, commit: str, ref: str) -> bool:
        r = self.runner(["git", "merge-base", "--is-ancestor", commit, ref], repo_path)
        return r.returncode == 0

    def _parse(self, data: dict) -> dict:
        checks = data.get("statusCheckRollup") or []
        if any((c.get("conclusion") or "").upper() in FAIL_CONCLUSIONS for c in checks):
            ci_status = "FAIL"
        elif any((c.get("status") or "").upper() != "COMPLETED" for c in checks):
            ci_status = "PENDING"
        elif checks:
            ci_status = "PASS"
        else:
            ci_status = "UNKNOWN"  # no checks configured on this PR at all
        return {
            "pr_number": data.get("number"),
            "pr_url": data.get("url"),
            "pr_state": data.get("state"),
            "mergeability": (data.get("mergeable") or "UNKNOWN").upper(),
            "merge_state_status": data.get("mergeStateStatus"),
            "head_sha": data.get("headRefOid"),
            "base_branch": data.get("baseRefName"),
            "ci_status": ci_status,
            "merged_commit": (data.get("mergeCommit") or {}).get("oid"),
        }
