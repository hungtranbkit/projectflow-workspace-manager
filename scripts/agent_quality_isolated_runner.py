"""Restricted, offline Docker execution of pinned QA tests.

The signer never mounts its own code, HOME, private key or Docker socket into
source-under-test. Docker sees a clean, read-only git-archive snapshot with
no .git directory, runs unprivileged, without network, and with read-only root.
Requires an explicitly pinned local image ID (sha256:<64 hex>).
"""
from __future__ import annotations

import io
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
from typing import Callable

_IMAGE = re.compile(r"sha256:[a-f0-9]{64}\Z")
_TEST = re.compile(r"tests/test_[A-Za-z0-9_]+\.py\Z")
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 50000


def make_isolated_test_runner(image_id: str) -> Callable:
    """Return a runner compatible with run_task_gate's pinned test command.

    Container image IDs, not mutable tags, are accepted. Build/provision the
    trusted image before invoking the independent QA signer. No docker build
    or pull occurs here.
    """
    if not _IMAGE.fullmatch(image_id):
        raise ValueError("QA image must be a locally verified immutable sha256 ID")

    def run(command: list[str], root: Path, _env: dict | None):
        if (len(command) < 5 or command[1:4] != ["-m", "pytest", "-q"] or
                not all(_TEST.fullmatch(name) for name in command[4:])):
            raise ValueError("QA container accepts only pinned tests/test_*.py")
        root = root.resolve()
        with tempfile.TemporaryDirectory(prefix="projectflow-qa-docker-") as tempdir:
            temp = Path(tempdir)
            archive = temp / "tracked.tar"
            export = temp / "source"
            export.mkdir(mode=0o755)
            # Never bind the agent checkout directly: .git/config could
            # contain credentials; untracked files are not test evidence.
            with archive.open("wb") as output:
                arch = subprocess.run(
                    ["git", "archive", "--format=tar", "HEAD"],
                    cwd=root, stdout=output, stderr=subprocess.PIPE, timeout=30,
                    check=False,
                )
            if arch.returncode or archive.stat().st_size > MAX_ARCHIVE_BYTES:
                raise ValueError("Source snapshot export failed or exceeded 32 MiB")
            with tarfile.open(archive, "r:") as tar:
                # Python's data extraction filter rejects special files,
                # filesystem escapes, and unsafe symlink/hardlink targets.
                tar.extractall(path=export, filter="data")
            cid = temp / "container.cid"
            docker = [
                "docker", "run", "--rm", "--pull=never",
                "--cidfile", str(cid), "--network=none", "--read-only",
                "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--pids-limit=64", "--memory=512m", "--memory-swap=512m",
                "--cpus=1", "--user=65534:65534",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
                "--mount", f"type=bind,source={export},target=/work,readonly",
                "--workdir", "/work",
                "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
                "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                image_id,
                "-m", "pytest", "-q", "-p", "no:cacheprovider",
                *command[4:],
            ]
            # No secret values are passed in Docker argv or env.
            client_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
            stdout, stderr = temp / "stdout.log", temp / "stderr.log"
            try:
                with stdout.open("wb") as out, stderr.open("wb") as err:
                    result = subprocess.run(docker, cwd=root, env=client_env,
                                            stdout=out, stderr=err, timeout=180,
                                            check=False)
                return subprocess.CompletedProcess(
                    command, result.returncode,
                    stdout.read_bytes()[-MAX_OUTPUT_BYTES:].decode("utf-8", errors="replace"),
                    stderr.read_bytes()[-MAX_OUTPUT_BYTES:].decode("utf-8", errors="replace"),
                )
            finally:
                # If Docker client times out or errors, do not leak a
                # container. No hostwide pruning or unrelated cleanup.
                if cid.is_file():
                    identifier = cid.read_text(encoding="ascii").strip()
                    if re.fullmatch(r"[a-f0-9]{64}", identifier):
                        subprocess.run(["docker", "rm", "-f", identifier],
                                       env=client_env, capture_output=True,
                                       timeout=15, check=False)

    return run
