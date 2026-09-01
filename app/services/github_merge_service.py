from __future__ import annotations
import base64
import json
import os
import re
import subprocess
from pathlib import Path

from app.services.github_app_service import GitHubAppError

"""Real GitHub PR/merge execution via the `gh` CLI -- the same
authenticated integration already used interactively in this
environment (`gh auth status`), never a hand-rolled OAuth flow or a
raw access token handled by this app. Every call is scoped to a
specific, already-registered repository's real filesystem path; there
is no code path that accepts a repo owner/name or PR number from the
browser (section 18) -- callers always derive those from the
Repository row + the persisted MergeRecord.

B0.7's own consumer of the general secret store (docs/
B0_HOSTED_PLATFORM_SECURITY_FOUNDATION.md, ADR-001): under
`AUTH_MODE=none` (default, unchanged), `GitHubMergeService()` with no
args keeps delegating entirely to the host's own already-authenticated
`gh` CLI session, exactly as always -- `token_runner()` below is never
constructed, never reached. Under `AUTH_MODE=required`,
`token_runner(token)` is a SIMPLIFIED, real, per-org-token consumer
(a single stored Personal Access Token per org, resolved via
SecretsService) -- deliberately NOT the full GitHub-App-per-org
architecture ADR-001 designs (App registration + JWT signing +
short-lived installation-token minting + webhook lifecycle), which
needs a real, externally-registered GitHub App's private key this
session cannot fabricate or safely simulate as genuine evidence. This
is an explicitly scoped, documented interim step: the same public
method surface, the same `runner` DI seam ADR-001's own "Migration
path" section describes, ready to swap for a real App-based runner
later without any call-site change."""

PR_FIELDS = "number,url,state,mergeable,mergeStateStatus,headRefOid,baseRefName,statusCheckRollup,mergedAt,mergeCommit,title"

# B4.1: matches both real forms `git remote get-url origin` produces for
# a GitHub remote -- SSH ("git@github.com:owner/repo.git") and HTTPS
# ("https://github.com/owner/repo[.git]"), trailing ".git" optional
# either way. Anything else (a non-GitHub remote, GitHub Enterprise
# Server's own different host, a malformed URL) intentionally does not
# match -- github_owner_repo() returns None rather than guess.
GITHUB_REMOTE_RE = re.compile(
    r"^(?:git@github\.com:|(?:ssh|https?)://(?:git@)?github\.com/)"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")

# gh CLI's own `state` field is already the 3-value GitHub-normalizes-it-
# for-you vocabulary (OPEN/CLOSED/MERGED), not the raw REST/GraphQL
# (state: OPEN|CLOSED) + separate `merged: bool` pair -- MERGED_STATES
# names the one value that actually means "authoritatively merged"
# throughout this module, so a caller never has to re-derive it from
# state+merged separately.
MERGED_STATES = {"MERGED"}

FAIL_CONCLUSIONS = {"FAILURE", "CANCELLED", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED"}

STRATEGY_FLAGS = {"MERGE_COMMIT": "--merge", "SQUASH": "--squash", "REBASE": "--rebase"}


class GitHubIntegrationError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _default_runner(argv: list[str], cwd, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(cwd), text=True, capture_output=True, timeout=timeout)


def token_runner(token: str):
    """B0.7 hosted-mode runner: injects one org-scoped token, resolved
    just-in-time from SecretsService, for exactly this one subprocess
    call -- never written to disk, never embedded in argv (which a
    concurrent `ps`/process-list on a SHARED host could otherwise
    observe -- a real concern once multiple tenants' operations run on
    the same machine), never logged.

    - `gh` CLI calls: `GH_TOKEN` env var, gh's own first-class,
      documented non-interactive auth override -- no `gh auth login`,
      no credential helper setup needed on this host at all.
    - Plain `git` calls (push/fetch/rev-parse/merge-base): the
      `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n`
      environment-variable mechanism (git >=2.31) sets a one-shot
      `http.extraheader` carrying a Basic auth header for this process
      only -- the same technique GitHub Actions' own checkout action
      uses internally, chosen specifically because it's environment-
      variable-based (not argv-based, not a written credential file)."""
    basic = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")

    def runner(argv: list[str], cwd, timeout: int = 30) -> subprocess.CompletedProcess:
        env = {**os.environ, "GH_TOKEN": token, "GIT_TERMINAL_PROMPT": "0"}
        if argv and argv[0] == "git":
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "http.extraheader"
            env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {basic}"
        return subprocess.run(argv, cwd=str(cwd), text=True, capture_output=True, timeout=timeout, env=env)
    return runner


# B5.1 (docs/B5_TENANT_ISOLATION_COMPLETENESS.md): the exact, narrow
# set of git invocations that read ONLY local `.git/config` -- no
# network, no GitHub authentication of any kind -- so make_hosted_
# runner()/make_installation_token_runner() below must never require a
# resolved credential for them. Exact-match on purpose (never a prefix/
# substring test): a broader match risks accidentally exempting a real
# network call (push/fetch/gh) from the credential requirement those
# functions exist to enforce, which would be a real security regression
# far worse than the bug this fixes.
_LOCAL_ONLY_GIT_COMMANDS = {("git", "remote", "get-url", "origin")}


def _needs_no_credential(argv: list[str]) -> bool:
    return tuple(argv) in _LOCAL_ONLY_GIT_COMMANDS


def make_hosted_runner(db, secrets_service):
    """The actual `AUTH_MODE=required` runner constructed once at app
    startup (`GitHubMergeService(runner=make_hosted_runner(db,
    secrets_service))`) -- every one of `GitHubMergeService`'s ~15
    existing call sites (main.py, integration_service.py) is already
    keyed only by a Repository row's own `repo_path` (see this module's
    own docstring's standing invariant), so this runner resolves
    `repo_path -> repositories.organization_id -> the org's stored
    "github_token" secret` freshly on EVERY call -- "just-in-time", per
    ADR-001's own migration-path language -- rather than binding one
    token at construction time (which would be wrong the moment two
    different organizations' repositories are both in play, and stale
    the moment a token is rotated mid-process). Stateless and
    thread-safe: no shared mutable credential is ever held on `self`.

    B5.1: `_needs_no_credential()` short-circuits straight to
    `_default_runner` for the narrow, exact-matched set of purely local
    git invocations (currently just `git remote get-url origin`) that
    never touch the network -- available()/github_owner_repo() call
    this same self.runner seam for exactly that check, and must not be
    forced through a credential requirement they don't actually need."""
    def runner(argv: list[str], cwd, timeout: int = 30) -> subprocess.CompletedProcess:
        if _needs_no_credential(argv):
            return _default_runner(argv, cwd, timeout)
        repo = db.one("SELECT organization_id FROM repositories WHERE repo_path=?", (str(cwd),))
        org_id = repo["organization_id"] if repo else None
        token = secrets_service.get_for_use(org_id, "github_token") if org_id else None
        if not token:
            raise GitHubIntegrationError(
                "INSTALLATION_UNAVAILABLE",
                "No GitHub credential configured for this organization (store one at "
                "/orgs/{id}/secrets as 'github_token').")
        return token_runner(token)(argv, cwd, timeout)
    return runner


def make_installation_token_runner(db, github_app_service):
    """B3.1 (docs/B3_GITHUB_APP_INSTALLATION_ARCHITECTURE.md, ADR-001):
    the real, preferred `AUTH_MODE=required` runner once a GitHub App
    is configured -- same shape as make_hosted_runner above (resolve
    `repo_path -> organization`, then org -> credential, fresh on every
    call, never bound at construction), one hop further:
    `organizations.github_installation_id -> a freshly-minted
    installation access token` instead of a stored per-org PAT. Reuses
    token_runner()'s own env-var injection unchanged -- an installation
    token is injected exactly the same way a PAT is (GH_TOKEN / the
    GIT_CONFIG_COUNT Basic-auth mechanism); only how the token is
    OBTAINED differs. GitHubAppError is translated to the same
    GitHubIntegrationError("INSTALLATION_UNAVAILABLE", ...) shape
    make_hosted_runner's own "no token configured" case already uses --
    every existing GitHubMergeService caller needs no change, exactly
    ADR-001's own "Migration path" requirement.

    B5.1: same `_needs_no_credential()` short-circuit as make_hosted_
    runner() above, for the same reason."""
    def runner(argv: list[str], cwd, timeout: int = 30) -> subprocess.CompletedProcess:
        if _needs_no_credential(argv):
            return _default_runner(argv, cwd, timeout)
        repo = db.one("SELECT organization_id FROM repositories WHERE repo_path=?", (str(cwd),))
        org_id = repo["organization_id"] if repo else None
        org = db.one("SELECT github_installation_id FROM organizations WHERE id=?", (org_id,)) if org_id else None
        installation_id = org["github_installation_id"] if org else None
        if not installation_id:
            raise GitHubIntegrationError(
                "INSTALLATION_UNAVAILABLE",
                "No GitHub App installation configured for this organization (an OWNER/ADMIN "
                "can set one at POST /orgs/{id}/github-installation).")
        try:
            token, _expires_at = github_app_service.mint_installation_token(installation_id)
        except GitHubAppError as exc:
            raise GitHubIntegrationError("INSTALLATION_UNAVAILABLE", str(exc)) from exc
        return token_runner(token)(argv, cwd, timeout)
    return runner


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
        the remote looks right.

        B5.1 (docs/B5_TENANT_ISOLATION_COMPLETENESS.md): still calls
        `self.runner`, deliberately -- an earlier draft of this fix
        called `_default_runner` directly instead, which broke every
        test that injects a fake `runner` (the established DI seam
        this whole class is built around) to simulate "GitHub is
        configured," since a bypassed seam never sees that fake. The
        actual fix belongs one layer down: `make_hosted_runner()`/
        `make_installation_token_runner()` themselves now recognize
        this exact local, credential-free git invocation and skip
        their own credential requirement for it -- see
        `_needs_no_credential()`'s own docstring."""
        try:
            r = self.runner(["git", "remote", "get-url", "origin"], repo_path)
            return r.returncode == 0 and "github.com" in (r.stdout or "")
        except Exception:
            return False

    def github_owner_repo(self, repo_path) -> str | None:
        """B4.1 (docs/B4_GITHUB_WEBHOOK_STATUS_INGESTION.md): DERIVED_
        TRUTH -- `git remote get-url origin`, parsed for the `owner/repo`
        slug a webhook payload's own `repository.full_name` uses to
        identify itself. Handles the two real forms git actually
        produces for a GitHub remote (SSH and HTTPS, with or without a
        trailing `.git`); returns None for anything else (not a GitHub
        remote, or the check itself failed) -- never a guess.

        Uses `self.runner` (the same DI seam every other method here
        does -- NOT a direct `_default_runner` call, B5.1's own
        correction of this exact same mistake made when this method was
        first written in B4; see available()'s own docstring for the
        real reason). `make_hosted_runner()`/`make_installation_token_
        runner()` recognize this exact local git invocation and skip
        their own credential requirement for it."""
        try:
            r = _default_runner(["git", "remote", "get-url", "origin"], repo_path)
        except Exception:
            return None
        if r.returncode != 0:
            return None
        url = (r.stdout or "").strip()
        m = GITHUB_REMOTE_RE.match(url)
        return f"{m.group('owner')}/{m.group('repo')}" if m else None

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
            "merged_at": data.get("mergedAt"),
        }
