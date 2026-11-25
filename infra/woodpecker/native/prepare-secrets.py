#!/usr/bin/env python3
"""Prepare protected, incomplete Woodpecker server.env on the selected Linux user.

Only creates a NEW file if missing; never overwrites, prints, or transmits
secrets. GitHub OAuth / HTTPS placeholders intentionally block server startup.
"""
from pathlib import Path
import os
import secrets
import stat
import sys

def main() -> int:
    target = Path.home() / ".config" / "projectflow-woodpecker" / "server.env"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    if target.exists() or target.is_symlink():
        print("PRESERVED_EXISTING_CONFIG", str(target))
        return 0
    data = "\n".join((
        "# Generated, protected secrets. Edit ONLY real hostname/OAuth values later.",
        "WOODPECKER_HOST=https://ci.example.com",
        "WOODPECKER_GITHUB=true",
        "WOODPECKER_GITHUB_PUBLIC_ONLY=true",
        "WOODPECKER_GITHUB_CLIENT=REPLACE_WITH_REAL_GITHUB_OAUTH_CLIENT",
        "WOODPECKER_GITHUB_SECRET=REPLACE_WITH_REAL_GITHUB_OAUTH_SECRET",
        "WOODPECKER_AGENT_SECRET=" + secrets.token_hex(32),
        "WOODPECKER_GRPC_SECRET=" + secrets.token_hex(32),
        "WOODPECKER_ADMIN=hungtranbkit",
        "WOODPECKER_REPO_OWNERS=hungtranbkit",
        "WOODPECKER_OPEN=false",
        "",
    ))
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        print("PRESERVED_CONCURRENT_CONFIG", str(target))
        return 0
    try:
        with os.fdopen(fd, "w", encoding="utf8") as stream:
            stream.write(data)
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    except BaseException:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    print("CREATED_SECURE_SERVER_ENV", str(target), "mode=0600")
    print("EXPECTED_BLOCK: edit HTTPS hostname and real GitHub OAuth values before starting")
    return 0

if __name__ == "__main__":
    sys.exit(main())
