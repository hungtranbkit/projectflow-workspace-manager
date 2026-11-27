#!/usr/bin/env python3
"""Portable, dependency-free installer for the Image to Code Agent Skill.

Only copies audited skill files. Never runs downloaded code or alters existing
agent configuration; an existing installation requires --force to replace.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

SKILL = "image-to-code"
AGENT_PATHS = {
    "claude": (".claude/skills", ".claude/skills"),
    "opencode": (".opencode/skills", ".config/opencode/skills"),
    "freebuff": (".agents/skills", ".agents/skills"),
    "hermes": (".hermes/skills", ".hermes/skills"),
    "codex": (".agents/skills", ".agents/skills"),
    "cursor": (".cursor/skills", ".cursor/skills"),
    "gemini": (".gemini/skills", ".gemini/skills"),
    "generic": (".agents/skills", ".agents/skills"),
}
COPIED_FILES = ("SKILL.md", "LICENSE", "AUTHORS.md")
COPIED_DIRS = ("scripts", "references", "examples", "assets")
OMIT_DIRS = {"__pycache__", "node_modules", ".git", ".pytest_cache"}
OMIT_SUFFIXES = {".pyc", ".pyo"}


def bundle_files(source: Path) -> list[Path]:
    if not (source / "SKILL.md").is_file():
        raise ValueError("No SKILL.md found in the skill source directory")
    frontmatter = (source / "SKILL.md").read_text(encoding="utf-8")
    if not frontmatter.startswith("---\n") or "\nname: image-to-code\n" not in frontmatter:
        raise ValueError("SKILL.md must include Agent Skills frontmatter with name: image-to-code")
    selected = []
    for file in COPIED_FILES:
        item = source / file
        if item.exists():
            selected.append(item)
    for directory in COPIED_DIRS:
        root = source / directory
        if root.is_symlink():
            raise ValueError(f"Cannot package symlink directory: {root}")
        if not root.is_dir():
            continue
        for item in sorted(root.rglob("*")):
            if any(part in OMIT_DIRS for part in item.relative_to(root).parts):
                continue
            if item.is_symlink():
                raise ValueError(f"Cannot package symlink: {item}")
            if item.is_file() and item.suffix not in OMIT_SUFFIXES:
                selected.append(item)
    if any(item.is_symlink() for item in selected):
        raise ValueError("Symlink sources are not allowed")
    return sorted(selected, key=lambda path: path.relative_to(source).as_posix())


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def paths_for(agents: list[str], scope: str, project: Path, home: Path) -> list[tuple[Path, list[str]]]:
    mapping = {}
    for agent in agents:
        local, global_path = AGENT_PATHS[agent]
        if scope == "project":
            root = project / local
        elif agent == "opencode" and os.environ.get("XDG_CONFIG_HOME"):
            root = Path(os.environ["XDG_CONFIG_HOME"]).expanduser() / "opencode/skills"
        elif agent == "opencode" and os.name == "nt" and os.environ.get("APPDATA"):
            root = Path(os.environ["APPDATA"]) / "opencode/skills"
        else:
            root = home / global_path
        path = (root / SKILL).absolute()
        mapping.setdefault(path, []).append(agent)
    return [(path, names) for path, names in mapping.items()]


def expected_manifest(source: Path, selected: list[Path]) -> dict[str, str]:
    return {file.relative_to(source).as_posix(): digest(file) for file in selected}


def verify_installed(dest: Path, expected: dict[str, str]) -> tuple[bool, list[str]]:
    errors = []
    if not dest.is_dir():
        return False, ["not installed"]
    for name, sha in expected.items():
        item = dest / name
        if not item.is_file() or item.is_symlink():
            errors.append(f"missing or invalid: {name}")
        elif digest(item) != sha:
            errors.append(f"changed: {name}")
    return not errors, errors


def install(source: Path, target: Path, selected: list[Path], force: bool) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError(f"Refusing to replace symlink: {target}")
    if target.exists() and not force:
        raise FileExistsError(f"Already installed: {target}. Use --verify or --force (creates backup).")
    # Staging is within the target filesystem to allow an atomic rename.
    staging = Path(tempfile.mkdtemp(prefix=".image-to-code-stage-", dir=target.parent))
    backup = None
    try:
        for file in selected:
            dest = staging / file.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, dest)
        if target.exists():
            backup = target.with_name(f"{SKILL}.backup-{uuid.uuid4().hex[:8]}")
            target.rename(backup)
        try:
            staging.rename(target)
        except Exception:
            if backup is not None and not target.exists():
                backup.rename(target)
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return f"Installed: {target}" + (f" (previous installation backed up to {backup})" if backup else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install Image to Code for one or more coding agents (Python 3.10+).")
    parser.add_argument("--agent", action="append", default=[], metavar="NAME[,NAME...]", help="Agent: claude, opencode, freebuff, hermes, codex, cursor, gemini, generic, all")
    parser.add_argument("--scope", choices=("project", "global"), default="project")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="Existing target project root (default: current directory)")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent.parent, help="Source skill directory (defaults to this script's parent repo)")
    parser.add_argument("--list-agents", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Preview operations without writing files")
    parser.add_argument("--verify", action="store_true", help="Check installed files against source SHA-256")
    parser.add_argument("--force", action="store_true", help="Replace existing installation, preserving a uniquely named backup")
    args = parser.parse_args(argv)
    if args.list_agents:
        for name, (project_path, global_path) in AGENT_PATHS.items():
            print(f"{name:9} project={project_path}/{SKILL} global=~/{global_path}/{SKILL}")
        return 0
    requested = [v.strip().lower() for group in args.agent for v in group.split(",") if v.strip()]
    if not requested:
        parser.error("Specify --agent (or --list-agents). No installation was performed.")
    invalid = [a for a in requested if a not in AGENT_PATHS and a != "all"]
    if invalid:
        parser.error("Unknown agent(s): " + ", ".join(invalid))
    if "all" in requested:
        requested = list(AGENT_PATHS)
    else:
        requested = list(dict.fromkeys(requested))
    source = args.source.expanduser().resolve(strict=True)
    project = args.project.expanduser().resolve()
    if args.scope == "project" and not project.is_dir():
        parser.error(f"Project directory does not exist: {project}")
    try:
        selected = bundle_files(source)
        expected = expected_manifest(source, selected)
        destinations = paths_for(requested, args.scope, project, Path.home())
        failed = False
        for dest, users in destinations:
            context = f"[{', '.join(users)}] {dest} ({len(selected)} files)"
            if args.verify:
                passed, errors = verify_installed(dest, expected)
                print(("PASS " if passed else "FAIL ") + context)
                for msg in errors:
                    print("  - " + msg)
                failed = failed or not passed
            elif args.dry_run:
                print("WOULD INSTALL " + context)
            else:
                print(install(source, dest, selected, args.force))
        if args.scope == "project" and "hermes" in requested:
            print("Hermes project skills require explicit workspace trust: run `hermes skills trust` in the project.")
        return 1 if failed else 0
    except (ValueError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
