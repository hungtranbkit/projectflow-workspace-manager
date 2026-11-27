"""Fail-closed ownership tracking for Compose resources created by pytest.

This module deliberately has no session-wide prefix sweeps. A project is
reserved before `compose up`, checked for collisions, and only exact resource
IDs observed and verified after that invocation can be removed later.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


_PROJECT_LABEL = "com.docker.compose.project"
_WORKDIR_LABEL = "com.docker.compose.project.working_dir"
_CONFIG_LABEL = "com.docker.compose.project.config_files"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


class DockerOwnershipError(RuntimeError):
    """Docker state could not be proven to belong to this pytest run."""


@dataclass(frozen=True)
class ResourceIdentity:
    kind: str
    resource_id: str
    name: str
    created: str
    project: str
    working_dir: str | None = None
    config_files: str | None = None


@dataclass
class ProjectRecord:
    project: str
    run_id: str
    compose_file: str
    project_directory: str
    resources: dict[str, dict[str, ResourceIdentity]] = field(
        default_factory=lambda: {"container": {}, "network": {}, "volume": {}}
    )
    captured: bool = False
    in_progress: bool = False
    capture_error: str | None = None


class ComposeRunOwnershipRegistry:
    """Track only resources created by compose calls in one pytest process."""

    def __init__(self, *, docker_bin: str = "docker", run: Callable = subprocess.run,
                 run_id: str):
        if not run_id or not _NAME_RE.fullmatch(run_id):
            raise ValueError("pytest Docker run id must be a bounded safe identifier")
        self.docker_bin = docker_bin
        self.run = run
        self.run_id = run_id
        self.projects: dict[str, ProjectRecord] = {}
        self._lock = threading.RLock()

    def _command(self, argv: list[str]):
        try:
            result = self.run(argv, capture_output=True, text=True, timeout=15)
        except Exception as exc:
            raise DockerOwnershipError(
                f"Docker ownership command could not run ({argv[1] if len(argv) > 1 else 'unknown'})"
            ) from exc
        if result.returncode != 0:
            operation = "cleanup command" if argv[1:3] in (["rm", "-f"], ["network", "rm"], ["volume", "rm"]) else "inventory command"
            raise DockerOwnershipError(
                f"Docker ownership {operation} failed ({argv[1] if len(argv) > 1 else 'unknown'}, "
                f"exit {result.returncode})"
            )
        return result

    def _ids(self, kind: str, project: str) -> set[str]:
        if kind == "container":
            argv = [self.docker_bin, "ps", "-aq", "--no-trunc", "--filter",
                    f"label={_PROJECT_LABEL}={project}"]
        elif kind == "network":
            argv = [self.docker_bin, "network", "ls", "-q", "--no-trunc", "--filter",
                    f"label={_PROJECT_LABEL}={project}"]
        elif kind == "volume":
            argv = [self.docker_bin, "volume", "ls", "-q", "--filter",
                    f"label={_PROJECT_LABEL}={project}"]
        else:
            raise ValueError(f"unsupported Compose resource kind: {kind}")
        return {line.strip() for line in self._command(argv).stdout.splitlines() if line.strip()}

    def _inspect(self, kind: str, resource_id: str) -> dict | None:
        if kind == "container":
            argv = [self.docker_bin, "inspect", "--type", "container", resource_id]
        elif kind == "network":
            argv = [self.docker_bin, "network", "inspect", resource_id]
        else:
            argv = [self.docker_bin, "volume", "inspect", resource_id]
        try:
            result = self.run(argv, capture_output=True, text=True, timeout=15)
        except Exception as exc:
            raise DockerOwnershipError(f"Docker {kind} inspect could not run") from exc
        if result.returncode != 0:
            missing_markers = ("no such object", "no such container", "no such network", "no such volume")
            if any(marker in (result.stderr or "").lower() for marker in missing_markers):
                return None
            raise DockerOwnershipError(f"Docker {kind} inspect failed (exit {result.returncode})")
        try:
            payload = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DockerOwnershipError(f"Docker {kind} inspect returned invalid JSON") from exc
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise DockerOwnershipError(f"Docker {kind} inspect returned an unexpected shape")
        return payload[0]

    @staticmethod
    def _identity(kind: str, payload: dict) -> ResourceIdentity:
        if kind == "container":
            labels = ((payload.get("Config") or {}).get("Labels") or {})
            resource_id = payload.get("Id")
            name = payload.get("Name")
            created = payload.get("Created")
        elif kind == "volume":
            labels = payload.get("Labels") or {}
            name = payload.get("Name")
            resource_id = name
            created = payload.get("CreatedAt") or payload.get("Created")
        else:
            labels = payload.get("Labels") or {}
            resource_id = payload.get("Id")
            name = payload.get("Name")
            created = payload.get("Created")
        project = labels.get(_PROJECT_LABEL)
        if not all(isinstance(value, str) and value for value in (resource_id, name, created, project)):
            raise DockerOwnershipError(f"Docker {kind} is missing ID, name, creation time, or Compose project label")
        return ResourceIdentity(
            kind=kind,
            resource_id=resource_id,
            name=name.lstrip("/"),
            created=created,
            project=project,
            working_dir=labels.get(_WORKDIR_LABEL),
            config_files=labels.get(_CONFIG_LABEL),
        )

    def _verify_identity(self, record: ProjectRecord, expected: ResourceIdentity,
                         payload: dict) -> ResourceIdentity:
        observed = self._identity(expected.kind, payload)
        if observed.resource_id != expected.resource_id or observed.project != record.project:
            raise DockerOwnershipError("Docker resource identity does not match reserved project and ID")
        if observed.name != expected.name or observed.created != expected.created:
            raise DockerOwnershipError("Docker resource was replaced or changed identity after registration")
        if observed.working_dir and observed.working_dir != record.project_directory:
            raise DockerOwnershipError("Docker resource working-directory label does not match its Compose invocation")
        if observed.config_files and observed.config_files != record.compose_file:
            raise DockerOwnershipError("Docker resource config-file label does not match its Compose invocation")
        return observed

    def _reserve(self, project: str, compose_file: Path, project_directory: Path) -> ProjectRecord:
        if not isinstance(project, str) or not _NAME_RE.fullmatch(project) or not project.startswith("wm-"):
            raise DockerOwnershipError("refusing Compose project outside the test application's wm-* namespace")
        compose_file_resolved = str(compose_file.resolve())
        project_directory_resolved = str(project_directory.resolve())
        with self._lock:
            record = self.projects.get(project)
            if record is None:
                containers = self._ids("container", project)
                networks = self._ids("network", project)
                volumes = self._ids("volume", project)
                if containers or networks or volumes:
                    raise DockerOwnershipError("Compose project already has Docker resources; refusing collision")
                record = ProjectRecord(
                    project=project,
                    run_id=self.run_id,
                    compose_file=compose_file_resolved,
                    project_directory=project_directory_resolved,
                )
                self.projects[project] = record  # reserve before compose up
            elif record.in_progress:
                raise DockerOwnershipError("Compose project already has an active creation attempt in this pytest run")
            else:
                if record.compose_file != compose_file_resolved or record.project_directory != project_directory_resolved:
                    raise DockerOwnershipError("Compose project reuse changed its source identity")
                known = {kind: set(record.resources[kind]) for kind in record.resources}
                current = {kind: self._ids(kind, project) for kind in record.resources}
                if any(not current[kind].issubset(known[kind]) for kind in known):
                    raise DockerOwnershipError("Compose project reuse found an unregistered Docker resource")
                for kind in known:
                    for resource_id in known[kind]:
                        payload = self._inspect(kind, resource_id)
                        if payload is None:
                            continue
                        expected = record.resources[kind][resource_id]
                        self._verify_identity(record, expected, payload)
                        if resource_id not in current[kind]:
                            raise DockerOwnershipError("registered resource label changed during project reuse")
            record.in_progress = True
            record.captured = False
            record.capture_error = None
            return record

    def _capture(self, record: ProjectRecord) -> None:
        try:
            current = {kind: self._ids(kind, record.project) for kind in record.resources}
            for kind, ids in current.items():
                for resource_id in ids:
                    payload = self._inspect(kind, resource_id)
                    if payload is None:
                        raise DockerOwnershipError(f"listed {kind} disappeared before ownership capture")
                    observed = self._identity(kind, payload)
                    existing = record.resources[kind].get(resource_id)
                    if existing is not None:
                        self._verify_identity(record, existing, payload)
                        continue
                    if observed.project != record.project:
                        raise DockerOwnershipError("Docker resource identity does not match reserved project")
                    if observed.working_dir and observed.working_dir != record.project_directory:
                        raise DockerOwnershipError("new Docker resource working directory differs from Compose invocation")
                    if observed.config_files and observed.config_files != record.compose_file:
                        raise DockerOwnershipError("new Docker resource config file differs from Compose invocation")
                    record.resources[kind][resource_id] = observed
            record.captured = True
            record.capture_error = None
        except Exception as exc:
            record.capture_error = str(exc)
            raise
        finally:
            record.in_progress = False

    def compose_up(self, compose_up: Callable, compose_project: str, *, compose_file: Path,
                   env_file: Path, cwd: Path, services: list[str], sandbox_id: int,
                   project_directory: Path | None = None):
        project_directory = project_directory or cwd
        record = self._reserve(compose_project, compose_file, project_directory)
        try:
            result = compose_up(
                compose_project, compose_file, env_file, cwd, services, sandbox_id,
                project_directory=project_directory,
            )
        except BaseException as original:
            try:
                self._capture(record)
            except Exception as capture_error:
                original.add_note(f"pytest could not verify partial Compose resources: {capture_error}")
            raise
        self._capture(record)
        return result

    def verify_project_for_operation(self, project: str) -> dict[str, set[str]]:
        """Verify existing Compose resources before stop/down/reset operations."""
        with self._lock:
            record = self.projects.get(project)
            current = {kind: self._ids(kind, project) for kind in ("container", "network", "volume")}
            if record is None:
                if any(current.values()):
                    raise DockerOwnershipError(
                        "Compose operation has resources that were not created by this pytest run"
                    )
                return current
            if not record.captured or record.in_progress:
                raise DockerOwnershipError("Compose operation refused because ownership capture is incomplete")
            for kind, ids in current.items():
                unknown = ids - set(record.resources[kind])
                if unknown:
                    raise DockerOwnershipError("Compose operation found an unregistered Docker resource")
            for kind, identities in record.resources.items():
                for resource_id, expected in identities.items():
                    payload = self._inspect(kind, resource_id)
                    if payload is None:
                        continue
                    self._verify_identity(record, expected, payload)
                    if resource_id not in current[kind]:
                        raise DockerOwnershipError("registered resource label changed before Compose operation")
            return current

    def run_project_operation(self, operation: Callable, project: str, *,
                              allow_missing_after: bool = False, **kwargs):
        record = self.projects.get(project)
        if record is not None:
            compose_file = kwargs.get("compose_file")
            cwd = kwargs.get("cwd")
            project_directory = kwargs.get("project_directory") or cwd
            if compose_file is None or project_directory is None:
                raise DockerOwnershipError("Compose operation is missing its recorded source paths")
            if str(Path(compose_file).resolve()) != record.compose_file:
                raise DockerOwnershipError("Compose operation changed the registered config file")
            if str(Path(project_directory).resolve()) != record.project_directory:
                raise DockerOwnershipError("Compose operation changed the registered project directory")
        before = self.verify_project_for_operation(project)
        result = operation(**kwargs)
        after = self.verify_project_for_operation(project)
        if not allow_missing_after and after != before:
            raise DockerOwnershipError("Compose stop changed the registered resource inventory")
        for kind, ids in after.items():
            if not ids.issubset(before[kind]):
                raise DockerOwnershipError("Compose operation created an unregistered Docker resource")
        return result

    def _remove_project_resources(self, record: ProjectRecord) -> None:
        if not record.captured or record.in_progress:
            raise DockerOwnershipError(
                f"project {record.project} has incomplete ownership capture; leaving resources untouched"
            )
        current = {kind: self._ids(kind, record.project) for kind in record.resources}
        expected_ids = {kind: set(record.resources[kind]) for kind in record.resources}
        for kind in current:
            unknown = current[kind] - expected_ids[kind]
            if unknown:
                raise DockerOwnershipError(
                    f"project {record.project} has an unregistered Docker resource; leaving project untouched"
                )

        present: dict[str, list[ResourceIdentity]] = {kind: [] for kind in record.resources}
        for kind, resource_ids in record.resources.items():
            for resource_id, expected in resource_ids.items():
                payload = self._inspect(kind, resource_id)
                if payload is None:
                    continue
                observed = self._verify_identity(record, expected, payload)
                present[kind].append(observed)

        # Validate every extant object before the first deletion. The commands
        # below use only recorded immutable Docker IDs, never a name/prefix.
        for kind in ("container", "network", "volume"):
            for identity in present[kind]:
                argv = ([self.docker_bin, "rm", "-f", identity.resource_id]
                        if kind == "container" else
                        [self.docker_bin, "network", "rm", identity.resource_id]
                        if kind == "network" else
                        [self.docker_bin, "volume", "rm", identity.resource_id])
                self._command(argv)

    def cleanup(self) -> None:
        errors = []
        for record in list(self.projects.values()):
            try:
                self._remove_project_resources(record)
            except Exception as exc:
                errors.append(f"{record.project}: {exc}")
        if errors:
            raise DockerOwnershipError("pytest Docker cleanup refused/failed: " + "; ".join(errors))
