#!/usr/bin/env python3
"""Install the audited, project-local UIFix source without running npm lifecycle hooks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / ".runtime"
TARGET = RUNTIME / "uifix-v1.2.6"
REPOSITORY = "https://github.com/yasiral/uifix.git"
COMMIT = "97c4b9d83684c7d607cb8c4b146e3f8aa5356e53"


def run(argv: list[str], cwd: Path | None = None, *, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, check=True, text=True, capture_output=capture)


def verify_source(path: Path) -> None:
    actual = run(["git", "rev-parse", "HEAD"], path, capture=True).stdout.strip()
    if actual != COMMIT:
        raise RuntimeError(f"UIFix source SHA mismatch: expected {COMMIT}, got {actual}")
    manifest = json.loads((path / "package.json").read_text(encoding="utf-8"))
    if manifest.get("version") != "1.2.6" or manifest.get("license") != "MIT":
        raise RuntimeError("UIFix must be version 1.2.6 with an MIT license")
    if "MIT License" not in (path / "LICENSE").read_text(encoding="utf-8"):
        raise RuntimeError("UIFix LICENSE file does not contain the MIT license text")
    scripts = manifest.get("scripts", {})
    if scripts.get("build") != "tsc":
        raise RuntimeError("Unexpected UIFix build script; review the pinned source before building")
    lifecycle = {"preinstall", "install", "postinstall"}.intersection(scripts)
    if lifecycle:
        raise RuntimeError("UIFix declares npm lifecycle hooks; refusing to install: " + ", ".join(sorted(lifecycle)))
    if not (path / "package-lock.json").is_file():
        raise RuntimeError("Pinned UIFix package-lock.json is missing")


def main() -> int:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if TARGET.exists():
        verify_source(TARGET)
        print(f"UIFix {COMMIT} already installed at {TARGET}")
        return 0
    stage = Path(tempfile.mkdtemp(prefix="uifix-stage-", dir=RUNTIME))
    try:
        source = stage / "source"
        run(["git", "clone", "--no-checkout", "--filter=blob:none", REPOSITORY, str(source)])
        run(["git", "-C", str(source), "checkout", "--detach", COMMIT])
        verify_source(source)
        npm = shutil.which("npm")
        if not npm:
            raise RuntimeError("npm is required to build pinned UIFix")
        run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], source)
        run([npm, "run", "build", "--ignore-scripts"], source)
        run([sys.executable, str(ROOT / "tools/visual_qa/verify_uifix.py"), str(source)])
        source.rename(TARGET)
        print(f"Installed UIFix {COMMIT} at {TARGET}")
        return 0
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"ERROR: UIFix install failed closed: {exc}", file=sys.stderr)
        return 1
    finally:
        if stage.exists():
            shutil.rmtree(stage)


if __name__ == "__main__":
    raise SystemExit(main())
