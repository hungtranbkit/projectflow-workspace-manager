from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitSafetyError(ValueError): pass
class GitCommandError(RuntimeError):
    def __init__(self, message, result=None): super().__init__(message); self.result = result

SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
BRANCH = re.compile(r"^(?:agent/[a-z0-9-]+/[a-z0-9-]+|integration/[a-z0-9-]+|main|master|develop)$")


def slugify(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not value or not SLUG.fullmatch(value) or ".." in value: raise GitSafetyError("Invalid or empty name")
    return value


@dataclass
class CommandResult:
    argv: list[str]; cwd: str; returncode: int; stdout: str; stderr: str


class GitWorkspaceService:
    def __init__(self, workspace_root: Path, timeout: int = 60, worktree_root: Path | None = None):
        self.root = workspace_root.resolve()
        self.worktree_root = (worktree_root or (self.root / ".worktrees")).resolve()
        if self.worktree_root != self.root and self.root not in self.worktree_root.parents:
            raise GitSafetyError("Worktree root is outside workspace root")
        self.timeout = timeout; self.audit: list[CommandResult] = []
    def validate_repo(self, path: str | Path) -> Path:
        p = Path(path).resolve()
        if p != self.root and self.root not in p.parents: raise GitSafetyError("Repository is outside workspace root")
        if not p.is_dir(): raise GitSafetyError("Repository does not exist")
        result = self._run(["git", "rev-parse", "--show-toplevel"], p, check=False)
        if result.returncode or Path(result.stdout.strip()).resolve() != p: raise GitSafetyError("Path is not a git repository root")
        return p
    def validate_branch(self, branch: str) -> str:
        if not BRANCH.fullmatch(branch) or ".." in branch: raise GitSafetyError("Unsafe branch name")
        return branch
    def validate_worktree(self, path: str | Path) -> Path:
        p = Path(path).resolve(); allowed = self.worktree_root
        if allowed not in p.parents: raise GitSafetyError("Worktree must be below .worktrees")
        return p
    def repo_slug(self, repo: Path) -> str:
        return slugify(repo.name)
    def repo_fingerprint(self, path: str | Path) -> str | None:
        """B7.1 (docs/B7_WORKSPACE_REPOSITORY_IDENTITY.md): DERIVED_
        TRUTH -- a real, local, no-network git call, never the remote
        URL alone (survives a rename/move/re-clone/remote change,
        works for a local-only repo with no remote at all, never reads
        or leaks anything from the remote config). `git rev-list
        --max-parents=0 HEAD` returns every root commit reachable from
        HEAD (almost always exactly one; more than one only for a
        history that merged in genuinely unrelated roots) -- sorted
        (order-independent) and SHA-256'd together into one comparable
        value. None on any failure (no commits yet, not a git repo,
        HEAD unborn) -- never a guessed/partial fingerprint. Known,
        accepted limitation (not fixed here): a SHALLOW clone's
        synthetic grafted root is not the true history root, so a
        shallow and a full clone of the same repo can fingerprint
        differently -- ProjectFlow's own registration flow always
        operates on operator-supplied local checkouts, not CI-style
        shallow clones, so this is real but low-probability for this
        codebase's actual usage."""
        try:
            result = self._run(["git", "rev-list", "--max-parents=0", "HEAD"], Path(path), check=False)
        except Exception:
            return None
        if result.returncode != 0:
            return None
        roots = sorted(line.strip() for line in result.stdout.splitlines() if line.strip())
        if not roots:
            return None
        return hashlib.sha256(",".join(roots).encode("utf-8")).hexdigest()
    def _run(self, argv: list[str], cwd: Path, check=True, timeout=None) -> CommandResult:
        try: proc = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=timeout or self.timeout, shell=False)
        except subprocess.TimeoutExpired as exc: raise GitCommandError(f"Command timed out: {argv[0:2]}") from exc
        result = CommandResult(argv, str(cwd), proc.returncode, proc.stdout, proc.stderr); self.audit.append(result)
        if check and proc.returncode: raise GitCommandError(proc.stderr.strip() or proc.stdout.strip(), result)
        return result
    def git(self, repo, *args, check=True): return self._run(["git", *args], Path(repo), check=check)
    def branch_exists(self, repo, branch): return self.git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{self.validate_branch(branch)}", check=False).returncode == 0
    def base_exists(self, repo, branch):
        self.validate_branch(branch)
        return self.git(repo, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}", check=False).returncode == 0
    def head(self, repo, branch="HEAD"): return self.git(repo, "rev-parse", f"{branch}^{{commit}}").stdout.strip()
    def status(self, repo): return self.git(repo, "status", "--porcelain=v1").stdout
    def create_agent(self, repo, agent, task, base="main"):
        repo = self.validate_repo(repo); agent, task = slugify(agent), slugify(task)
        branch = self.validate_branch(f"agent/{agent}/{task}"); path = self.validate_worktree(self.worktree_root / f"{self.repo_slug(repo)}-{agent}-{task}")
        if self.branch_exists(repo, branch): raise GitSafetyError("Branch already exists")
        if path.exists(): raise GitSafetyError("Worktree path already exists")
        if not self.base_exists(repo, base): raise GitSafetyError("Base branch does not exist")
        base_commit = self.head(repo, base); path.parent.mkdir(parents=True, exist_ok=True)
        self.git(repo, "worktree", "add", str(path), "-b", branch, base)
        return branch, path, base_commit
    def create_integration(self, repo, name, base="main"):
        repo = self.validate_repo(repo); name = slugify(name); branch = self.validate_branch(f"integration/{name}"); path = self.validate_worktree(self.worktree_root / f"{self.repo_slug(repo)}-integration-{name}")
        if self.branch_exists(repo, branch): raise GitSafetyError("Branch already exists")
        if path.exists(): raise GitSafetyError("Worktree path already exists")
        if not self.base_exists(repo, base): raise GitSafetyError("Base branch does not exist")
        commit = self.head(repo, base); path.parent.mkdir(parents=True, exist_ok=True)
        self.git(repo, "worktree", "add", str(path), "-b", branch, base)
        return branch, path, commit
    def merge(self, integration_path, source_branch):
        path = self.validate_worktree(integration_path); self.validate_branch(source_branch)
        return self.git(path, "merge", "--no-ff", "--no-edit", source_branch, check=False)
    def conflict_files(self, path): return [x for x in self.git(self.validate_worktree(path), "diff", "--name-only", "--diff-filter=U", check=False).stdout.splitlines() if x]
    def changed_files(self, path, base_commit):
        """All paths that differ between base_commit and the current
        worktree state (index + working tree combined) -- everything
        touched since this Workspace was created, committed or not.
        Used by Scope Guard (E8.18) to detect out-of-scope modifications
        against a real diff, never a self-reported file list."""
        return [x for x in self.git(self.validate_worktree(path), "diff", "--name-only", base_commit, check=False).stdout.splitlines() if x]
    def is_ancestor(self, path, commit): return self.git(self.validate_worktree(path), "merge-base", "--is-ancestor", commit, "HEAD", check=False).returncode == 0
    def create_baseline_probe(self, repo, commit):
        """A disposable, detached-HEAD worktree checked out at an exact
        historical commit -- used only to reproduce a required-gate
        failure against a Task's own base commit (never a branch, never
        anything a human is meant to work in). Reused if already probed
        for this exact commit, so repeated waiver checks don't pile up
        worktrees. Removal is always --force (see remove_baseline_probe)
        since nothing meaningful can ever be committed here."""
        repo = self.validate_repo(repo)
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", (commit or "").strip()):
            raise GitSafetyError("Invalid commit sha")
        path = self.validate_worktree(self.worktree_root / f"{self.repo_slug(repo)}-baseline-{commit[:12]}")
        if path.exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git(repo, "worktree", "add", "--detach", str(path), commit)
        return path

    def repair_worktrees(self, repo: str | Path, worktree_paths: list) -> None:
        """P0-2 (docs/CORE_USABILITY_QUALIFICATION.md): a real git
        limitation, reproduced directly -- every existing worktree's own
        `.git` file (and the main repo's `.git/worktrees/<name>/gitdir`
        back-reference) stores an ABSOLUTE path. Renaming/moving the
        MAIN repo directory (now a real, supported operation since
        B7.1's own repository rebind) leaves every worktree pointing at
        a `.git/worktrees/...` path that no longer exists -- `git
        status` there fails with `fatal: not a git repository` even
        though nothing about the worktree's own files or commits
        changed at all. `git worktree repair <path>...` is git's own
        built-in fix for exactly this (re-links both directions using
        the worktree's own still-valid on-disk location) -- called here
        with the repository's now-current path so callers never have to
        reason about this git internal themselves. Best-effort per
        path: `check=False`, never raises -- one broken/already-removed
        worktree path must never block repairing the others, and a
        rebind's own success must never depend on git worktree
        internals succeeding."""
        repo = Path(repo)
        for wt in worktree_paths:
            if not Path(wt).is_dir():
                continue
            self._run(["git", "worktree", "repair", str(wt)], repo, check=False)

    def remove_baseline_probe(self, repo, path):
        """Force-remove a baseline probe worktree (see create_baseline_probe)
        -- unlike close(), never checks for a dirty tree, since a probe is
        by definition disposable and never a place real work happens."""
        repo = self.validate_repo(repo)
        path = self.validate_worktree(path)
        self.git(repo, "worktree", "remove", "--force", str(path), check=False)

    def close(self, repo, path):
        repo = self.validate_repo(repo); path = self.validate_worktree(path)
        if self.status(path).strip(): raise GitSafetyError("Worktree is dirty; close blocked")
        self.git(repo, "worktree", "remove", str(path))
    def details(self, path):
        path = self.validate_worktree(path)
        status = self.status(path).splitlines()
        commits = self.git(path, "log", "-10", "--pretty=format:%h %s").stdout.splitlines()
        return {"head": self.head(path), "status": status, "modified": [x for x in status if not x.startswith("??")], "untracked": [x for x in status if x.startswith("??")], "commits": commits}
