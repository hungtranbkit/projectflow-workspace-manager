"""Fail-closed Docker ownership checks used only by the pytest harness."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from typing import Callable, Iterable


class DockerTestOwnershipError(RuntimeError):
    """The test run cannot prove ownership of its Docker endpoint/resources."""


@dataclass(frozen=True)
class DockerTestOwner:
    run_id: str
    endpoint: str
    engine_id: str
    owner_file: Path
    docker_bin: str


@dataclass(frozen=True)
class RegisteredSandbox:
    manager: object
    sandbox_id: int
    compose_project: str
    owner: DockerTestOwner


def register_test_sandbox(
    manager: object,
    sandbox_id: int,
    registry: dict,
    *,
    current_owner: Callable[[], DockerTestOwner | None],
) -> RegisteredSandbox:
    """Record one sandbox row only after the dedicated endpoint is verified."""
    owner = current_owner()
    if owner is None:
        raise DockerTestOwnershipError("real Docker tests require a verified dedicated test-run endpoint")
    row = manager.db.one("SELECT * FROM sandboxes WHERE id=?", (sandbox_id,))
    if row is None or not isinstance(row["compose_project"], str):
        raise DockerTestOwnershipError("cannot register a sandbox without its exact Compose project")
    compose_project = row["compose_project"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", compose_project):
        raise DockerTestOwnershipError("sandbox has an invalid Compose project identifier")
    resource = RegisteredSandbox(manager, sandbox_id, compose_project, owner)
    registry[(id(manager), sandbox_id)] = resource
    return resource


def load_verified_docker_test_owner(
    environ: dict[str, str] | None = None,
    *,
    runner: Callable = subprocess.run,
    docker_bin: str | None = None,
    uid: int | None = None,
) -> DockerTestOwner | None:
    """Verify an explicitly provisioned per-run Docker socket.

    No configuration means ordinary unit tests have no Docker owner and
    therefore make no Docker calls. Partial, shared, or mismatched
    configuration fails closed. The runner's manifest is mode 0600 under a
    mode 0700 directory and binds the run ID, socket endpoint, and engine ID.
    """
    env = os.environ if environ is None else environ
    required = (
        "PROJECTFLOW_TEST_DOCKER_RUN_ID",
        "PROJECTFLOW_TEST_DOCKER_ENDPOINT",
        "PROJECTFLOW_TEST_DOCKER_OWNER_FILE",
        "DOCKER_HOST",
    )
    values = {key: env.get(key, "") for key in required}
    if not any(values.values()):
        return None
    if any(not value for value in values.values()):
        raise DockerTestOwnershipError("incomplete dedicated Docker test-run configuration")

    run_id = values[required[0]]
    endpoint = values[required[1]]
    owner_path = Path(values[required[2]])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{7,127}", run_id):
        raise DockerTestOwnershipError("invalid test-run ID")
    if not endpoint.startswith("unix://") or values["DOCKER_HOST"] != endpoint:
        raise DockerTestOwnershipError("Docker endpoint must be the explicit owned Unix socket")
    socket_path = Path(endpoint[len("unix://"):])
    if not owner_path.is_absolute() or not socket_path.is_absolute():
        raise DockerTestOwnershipError("owner manifest and socket paths must be absolute")

    current_uid = os.getuid() if uid is None else uid
    try:
        owner_lstat = owner_path.lstat()
        root_path = owner_path.parent
        root_stat = root_path.lstat()
        socket_lstat = socket_path.lstat()
        if stat.S_ISLNK(owner_lstat.st_mode) or not stat.S_ISREG(owner_lstat.st_mode):
            raise DockerTestOwnershipError("owner manifest must be a regular non-symlink file")
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise DockerTestOwnershipError("owner directory must be a real directory")
        if stat.S_ISLNK(socket_lstat.st_mode) or not stat.S_ISSOCK(socket_lstat.st_mode):
            raise DockerTestOwnershipError("Docker endpoint must be a real Unix socket")
        if root_stat.st_uid != current_uid or stat.S_IMODE(root_stat.st_mode) & 0o077:
            raise DockerTestOwnershipError("owner directory must be private and owned by the test runner")
        if owner_lstat.st_uid != current_uid or stat.S_IMODE(owner_lstat.st_mode) & 0o077:
            raise DockerTestOwnershipError("owner manifest must be private and owned by the test runner")
        if owner_lstat.st_size > 4096:
            raise DockerTestOwnershipError("owner manifest exceeds the size limit")
        if socket_lstat.st_uid != current_uid or stat.S_IMODE(socket_lstat.st_mode) & 0o077:
            raise DockerTestOwnershipError("Docker socket must be private and owned by the test runner")
        resolved_root = root_path.resolve(strict=True)
        resolved_socket = socket_path.resolve(strict=True)
        resolved_socket.relative_to(resolved_root)
        if owner_path.resolve(strict=True).parent != resolved_root:
            raise DockerTestOwnershipError("owner manifest must be inside the private run directory")
        manifest = json.loads(owner_path.read_text(encoding="utf-8"))
    except DockerTestOwnershipError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise DockerTestOwnershipError("could not verify dedicated Docker owner files") from exc

    if not isinstance(manifest, dict):
        raise DockerTestOwnershipError("owner manifest must be a JSON object")
    manifest_endpoint = manifest.get("endpoint")
    manifest_run_id = manifest.get("run_id")
    engine_id = manifest.get("engine_id")
    if manifest_run_id != run_id or manifest_endpoint != endpoint or not isinstance(engine_id, str) or not engine_id.strip():
        raise DockerTestOwnershipError("owner manifest does not match this test run and endpoint")

    selected_docker = docker_bin or env.get("PROJECTFLOW_TEST_DOCKER_BIN") or shutil.which("docker")
    if not selected_docker or not Path(selected_docker).is_absolute():
        raise DockerTestOwnershipError("Docker CLI must resolve to an absolute executable path")
    try:
        result = runner(
            [selected_docker, "--host", endpoint, "info", "--format", "{{.ID}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        raise DockerTestOwnershipError("dedicated Docker endpoint health check failed") from exc
    if result.returncode != 0 or result.stdout.strip() != engine_id:
        raise DockerTestOwnershipError("Docker engine ID does not match the private owner manifest")
    return DockerTestOwner(run_id, endpoint, engine_id, owner_path.resolve(strict=True), selected_docker)


def verify_exact_compose_project_resources(
    owner: DockerTestOwner,
    compose_project: str,
    *,
    runner: Callable = subprocess.run,
) -> bool:
    """Verify only resources bearing one exact, test-registered Compose label."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", compose_project):
        return False
    project_filter = f"label=com.docker.compose.project={compose_project}"
    resource_commands = (
        ("container", ["ps", "-aq"], ["inspect", "--format", '{{ index .Config.Labels "com.docker.compose.project" }}']),
        ("network", ["network", "ls", "-q"], ["network", "inspect", "--format", '{{ index .Labels "com.docker.compose.project" }}']),
        ("volume", ["volume", "ls", "-q"], ["volume", "inspect", "--format", '{{ index .Labels "com.docker.compose.project" }}']),
    )
    try:
        for _kind, listing, inspect in resource_commands:
            listed = runner(
                [owner.docker_bin, "--host", owner.endpoint, *listing, "--filter", project_filter],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if listed.returncode != 0:
                return False
            for resource_id in listed.stdout.split():
                checked = runner(
                    [owner.docker_bin, "--host", owner.endpoint, *inspect, resource_id],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if checked.returncode != 0 or checked.stdout.strip() != compose_project:
                    return False
    except Exception:
        return False
    return True


def cleanup_registered_sandboxes(
    resources: Iterable[RegisteredSandbox],
    *,
    current_owner: Callable[[], DockerTestOwner | None],
    verify_project: Callable[[DockerTestOwner, str], bool],
    cleanup: Callable[[object, int], None],
) -> None:
    """Clean only IDs registered by this test run after ownership rechecks."""
    for resource in resources:
        row = resource.manager.db.one("SELECT * FROM sandboxes WHERE id=?", (resource.sandbox_id,))
        if row is None:
            raise DockerTestOwnershipError("registered sandbox row disappeared before cleanup")
        if row["status"] == "CLOSED":
            continue
        owner = current_owner()
        if owner is None or owner != resource.owner:
            raise DockerTestOwnershipError("dedicated Docker owner changed before cleanup")
        if row["compose_project"] != resource.compose_project:
            raise DockerTestOwnershipError("registered sandbox Compose project changed before cleanup")
        if not verify_project(owner, resource.compose_project):
            raise DockerTestOwnershipError("exact Compose resource labels could not be verified")
        cleanup(resource.manager, resource.sandbox_id)
