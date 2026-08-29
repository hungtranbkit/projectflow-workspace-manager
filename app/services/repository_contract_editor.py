from __future__ import annotations
import hashlib
import io
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from app.services.sandbox_contract import load_sandbox_contract, SandboxContractError

"""The ONE place PROJECT.yaml's `sandbox:` block is ever written from the
UI (Repository Runtime & Sandbox Settings, section 5 of the spec this
implements). PROJECT.yaml stays the single source of truth for sandbox
semantics -- this never writes a parallel DB copy of the same settings
(section 4/22); the DB only ever stores audit history (via db.event(),
reusing the existing generic audit mechanism -- see main.py's
REPOSITORY_CONTRACT_UPDATED event).

Uses ruamel.yaml (round-trip mode) instead of the PyYAML the rest of the
app reads with, specifically so a Save preserves comments/formatting/
every unrelated key untouched (section 6) -- PyYAML's safe_load/dump
would silently normalize/reorder the whole file. Reading elsewhere in
the app (project_contract.py, sandbox_contract.py) stays on PyYAML;
nothing about their read-only contract is changed by this file existing."""


class ContractEditError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


VALID_DEPENDENCY_MODES = {"KNOWN_GOOD_MAIN", "PINNED_COMMIT", "PINNED_IMAGE"}

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 100000  # never line-wrap a long value mid-edit


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RepositoryContractEditor:
    def __init__(self, git_service):
        # git_service.validate_repo() is reused as the one trust boundary
        # for "is this a real, registered repository root" -- the exact
        # same check every other git-touching service in this app uses.
        self.git = git_service

    def load(self, repo_path: Path) -> dict:
        repo_path = self.git.validate_repo(repo_path)
        path = repo_path / "PROJECT.yaml"
        if not path.is_file():
            raise ContractEditError(["PROJECT.yaml not found in this repository"])
        raw = path.read_text(encoding="utf-8")
        data = _yaml.load(raw)
        if data is None:
            data = CommentedMap()
        return {"path": path, "raw_text": raw, "data": data, "sha256": _sha256(raw)}

    def read_sandbox_settings(self, repo_path: Path) -> dict:
        """Normal-user view of the current sandbox: block -- an honest
        'not configured' shape when none exists (section 17's empty
        state reads this directly, never guesses)."""
        loaded = self.load(repo_path)
        block = loaded["data"].get("sandbox")
        empty = {
            "enabled": False, "profile_name": None, "compose_file": None, "services": [],
            "runtime_dependencies": [], "seed_default": None, "auto_provision": False,
            "ports": {}, "health": {}, "outputs": {},
        }
        if not block:
            return empty
        default_profile = block.get("default_profile")
        profile_spec = (block.get("profiles") or {}).get(default_profile, {}) if default_profile else {}
        return {
            "enabled": True,
            "profile_name": default_profile,
            "compose_file": block.get("compose_file"),
            "services": list(profile_spec.get("services") or []),
            "runtime_dependencies": [dict(d) for d in (block.get("runtime_dependencies") or [])],
            "seed_default": (block.get("seed") or {}).get("default"),
            "auto_provision": bool(block.get("auto_provision", False)),
            "ports": {k: dict(v) for k, v in (block.get("ports") or {}).items()},
            "health": {k: dict(v) for k, v in (block.get("health") or {}).items()},
            "outputs": {k: dict(v) for k, v in (block.get("outputs") or {}).items()},
        }

    def validate(self, settings: dict, *, self_repo_name: str, registered_repo_names: set[str]) -> list[str]:
        """Settings' own internal consistency only -- cross-repo
        dependency-cycle checks need every OTHER registered repo's own
        contract too, so that lives one layer up (main.py's
        check_dependency_cycle()) where the full repository registry is
        already available (section 7/23)."""
        errors: list[str] = []
        if not settings.get("enabled"):
            return errors
        profile = (settings.get("profile_name") or "").strip()
        if not profile:
            errors.append("Profile name is required")
        elif not profile.replace("_", "").isalnum():
            errors.append("Profile name must contain only letters, digits and underscores")
        seen = set()
        for dep in settings.get("runtime_dependencies") or []:
            name = (dep.get("repo") or "").strip()
            if not name:
                errors.append("Runtime dependency repository name is required")
                continue
            if name == self_repo_name:
                errors.append(f"A repository cannot declare itself as a runtime dependency: {name}")
            if name not in registered_repo_names:
                errors.append(f"Runtime dependency is not a repository registered in ProjectFlow: {name}")
            if name in seen:
                errors.append(f"Duplicate runtime dependency: {name}")
            seen.add(name)
            mode = dep.get("mode") or "KNOWN_GOOD_MAIN"
            if mode not in VALID_DEPENDENCY_MODES:
                errors.append(f"Unsupported dependency source mode: {mode} (must be one of {', '.join(sorted(VALID_DEPENDENCY_MODES))})")
            dep_profile = (dep.get("profile") or "").strip()
            if not dep_profile:
                errors.append(f"Runtime dependency {name} needs a profile")
        return errors

    def write_sandbox_settings(self, repo_path: Path, settings: dict, *, self_repo_name: str, registered_repo_names: set[str]) -> dict:
        """Validates, then atomically writes the sandbox: block --
        preserving every other key, comment and unrelated section of
        PROJECT.yaml untouched (section 6). No write happens at all if
        validation fails (section 7). Verifies the file it just wrote
        round-trips through the SAME loader the rest of the app reads
        sandbox contracts with (sandbox_contract.load_sandbox_contract)
        before returning success -- restores the backup instead of ever
        leaving a broken contract on disk."""
        errors = self.validate(settings, self_repo_name=self_repo_name, registered_repo_names=registered_repo_names)
        if errors:
            raise ContractEditError(errors)
        loaded = self.load(repo_path)
        data = loaded["data"]
        before_text = loaded["raw_text"]

        if not settings.get("enabled"):
            data.pop("sandbox", None)
        else:
            block = CommentedMap()
            block["compose_file"] = settings.get("compose_file") or "compose.sandbox.yml"
            block["default_profile"] = settings["profile_name"]
            profiles = CommentedMap()
            prof = CommentedMap()
            prof["services"] = list(settings.get("services") or [settings["profile_name"].lower()])
            profiles[settings["profile_name"]] = prof
            block["profiles"] = profiles
            if settings.get("ports"):
                block["ports"] = settings["ports"]
            if settings.get("health"):
                block["health"] = settings["health"]
            if settings.get("outputs"):
                block["outputs"] = settings["outputs"]
            if settings.get("seed_default"):
                block["seed"] = CommentedMap({"default": settings["seed_default"]})
            block["auto_provision"] = bool(settings.get("auto_provision", False))
            deps = settings.get("runtime_dependencies") or []
            if deps:
                block["runtime_dependencies"] = [
                    CommentedMap({"repo": d["repo"], "profile": d.get("profile", "BACKEND"), "mode": d.get("mode", "KNOWN_GOOD_MAIN")})
                    for d in deps
                ]
            data["sandbox"] = block

        path = loaded["path"]
        # No on-disk .bak file: PROJECT.yaml is a real, git-tracked
        # source file (section 20) -- git itself (git diff / git
        # checkout -- PROJECT.yaml) is the durable undo path, and never
        # leaves a stray, potentially-committable file in the repo's own
        # working tree. before_text is still returned below for the
        # in-memory round-trip-failure revert and for the caller's own
        # audit/diff display.
        buf = io.StringIO()
        _yaml.dump(data, buf)
        new_text = buf.getvalue()
        path.write_text(new_text, encoding="utf-8")

        try:
            load_sandbox_contract(repo_path)
        except SandboxContractError as exc:
            path.write_text(before_text, encoding="utf-8")
            raise ContractEditError([f"Round-trip verification failed -- change discarded: {exc}"])

        return {
            "before_sha256": loaded["sha256"], "after_sha256": _sha256(new_text),
            "before_text": before_text, "after_text": new_text,
        }
