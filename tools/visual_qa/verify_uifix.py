#!/usr/bin/env python3
"""Post-build verification for the pinned local UIFix checkout."""
from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    required = [
        root / "dist/cli/index.js",
        root / "dist/index.js",
        root / "dist/diff/index.js",
        root / "node_modules/playwright/package.json",
        root / "node_modules/sharp/package.json",
        root / "node_modules/pixelmatch/package.json",
        root / "node_modules/ssim.js/package.json",
    ]
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        print("UIFix build missing required runtime files: " + ", ".join(missing), file=sys.stderr)
        return 1
    cli = subprocess.run(
        ["node", str(root / "dist/cli/index.js"), "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if cli.returncode != 0 or "Usage:" not in cli.stdout:
        print("UIFix CLI help did not execute successfully", file=sys.stderr)
        print(cli.stderr[-1000:], file=sys.stderr)
        return 1
    print("PASS UIFix pinned source, build outputs, runtime dependencies, and CLI --help")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
