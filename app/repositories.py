from __future__ import annotations
from pathlib import Path

def discover_repositories(root: Path):
    found = []
    candidates = [root, *[p for p in root.iterdir() if p.is_dir()]] if root.is_dir() else []
    for path in candidates:
        marker = path / ".git"
        if marker.exists(): found.append({"repo_name": path.name, "repo_path": str(path.resolve())})
    return found

