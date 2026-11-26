"""Safe unit regressions for the pytest Docker cleanup registry.

These tests use a synthetic Docker command boundary. They never contact the
host daemon and can be run without loading tests/conftest.py.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.docker_compose_ownership import ComposeRunOwnershipRegistry


class FakeDocker:
    def __init__(self):
        self.containers = {}
        self.networks = {}
        self.volumes = {}
        self.commands = []
        self.fail_list = False
        self.fail_remove = False
        self.fail_inspect = False
        self.force_filtered_ids = {"container": {}, "network": {}, "volume": {}}

    @staticmethod
    def _result(argv, stdout="", returncode=0, stderr=""):
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    def run(self, argv, **_kwargs):
        argv = list(argv)
        self.commands.append(argv)
        if argv[:3] == ["docker", "ps", "-aq"]:
            if self.fail_list:
                return self._result(argv, returncode=1, stderr="daemon unavailable")
            project = self._filter_project(argv)
            ids = [rid for rid, item in self.containers.items()
                   if item["Config"]["Labels"].get("com.docker.compose.project") == project]
            ids.extend(self.force_filtered_ids["container"].get(project, set()))
            return self._result(argv, "\n".join(sorted(set(ids))))
        if argv[:4] == ["docker", "network", "ls", "-q"]:
            if self.fail_list:
                return self._result(argv, returncode=1, stderr="daemon unavailable")
            project = self._filter_project(argv)
            ids = [rid for rid, item in self.networks.items()
                   if item["Labels"].get("com.docker.compose.project") == project]
            ids.extend(self.force_filtered_ids["network"].get(project, set()))
            return self._result(argv, "\n".join(sorted(set(ids))))
        if argv[:3] == ["docker", "volume", "ls"]:
            if self.fail_list:
                return self._result(argv, returncode=1, stderr="daemon unavailable")
            project = self._filter_project(argv)
            ids = [rid for rid, item in self.volumes.items()
                   if item["Labels"].get("com.docker.compose.project") == project]
            ids.extend(self.force_filtered_ids["volume"].get(project, set()))
            return self._result(argv, "\n".join(sorted(set(ids))))
        if argv[:3] == ["docker", "inspect", "--type"]:
            if self.fail_inspect:
                return self._result(argv, returncode=1, stderr="inspect denied")
            item = self.containers.get(argv[-1])
            if item is None:
                return self._result(argv, returncode=1, stderr="No such object")
            return self._result(argv, json.dumps([item]))
        if argv[:3] == ["docker", "network", "inspect"]:
            if self.fail_inspect:
                return self._result(argv, returncode=1, stderr="inspect denied")
            item = self.networks.get(argv[-1])
            if item is None:
                return self._result(argv, returncode=1, stderr="No such network")
            return self._result(argv, json.dumps([item]))
        if argv[:3] == ["docker", "volume", "inspect"]:
            if self.fail_inspect:
                return self._result(argv, returncode=1, stderr="inspect denied")
            item = self.volumes.get(argv[-1])
            if item is None:
                return self._result(argv, returncode=1, stderr="No such volume")
            return self._result(argv, json.dumps([item]))
        if argv[:3] == ["docker", "rm", "-f"]:
            if self.fail_remove:
                return self._result(argv, returncode=1, stderr="refused")
            self.containers.pop(argv[-1], None)
            return self._result(argv)
        if argv[:3] == ["docker", "network", "rm"]:
            if self.fail_remove:
                return self._result(argv, returncode=1, stderr="refused")
            self.networks.pop(argv[-1], None)
            return self._result(argv)
        if argv[:3] == ["docker", "volume", "rm"]:
            if self.fail_remove:
                return self._result(argv, returncode=1, stderr="refused")
            self.volumes.pop(argv[-1], None)
            return self._result(argv)
        raise AssertionError(f"unexpected Docker command: {argv}")

    @staticmethod
    def _filter_project(argv):
        value = next((x for x in argv if x.startswith("label=com.docker.compose.project=")), None)
        return value.rsplit("=", 1)[1] if value else None

    def add_container(self, resource_id, project, name=None, *, extra_labels=None):
        labels = {"com.docker.compose.project": project,
                  "com.docker.compose.service": "web",
                  "com.docker.compose.project.working_dir": "/tmp/fixture"}
        labels.update(extra_labels or {})
        self.containers[resource_id] = {
            "Id": resource_id,
            "Name": "/" + (name or f"{project}-web-1"),
            "Created": "2026-11-26T00:00:00Z",
            "Config": {"Labels": labels},
        }

    def add_network(self, resource_id, project, name=None, *, extra_labels=None):
        labels = {"com.docker.compose.project": project,
                  "com.docker.compose.network": "default"}
        labels.update(extra_labels or {})
        self.networks[resource_id] = {
            "Id": resource_id,
            "Name": name or f"{project}-default",
            "Created": "2026-11-26T00:00:00Z",
            "Labels": labels,
        }

    def add_volume(self, name, project, *, extra_labels=None):
        labels = {"com.docker.compose.project": project,
                  "com.docker.compose.volume": "data"}
        labels.update(extra_labels or {})
        self.volumes[name] = {"Name": name, "CreatedAt": "2026-11-26T00:00:00Z", "Labels": labels}


def _registry(fake):
    return ComposeRunOwnershipRegistry(docker_bin="docker", run=fake.run, run_id="pytest-run-001")


def _compose_up(fake, registry, project, create):
    def compose(*_args, **_kwargs):
        create()
        return subprocess.CompletedProcess(["docker", "compose"], 0, "", "")

    return registry.compose_up(
        compose,
        project,
        compose_file=Path("/tmp/fixture/compose.yml"),
        env_file=Path("/tmp/fixture/.env"),
        cwd=Path("/tmp/fixture"),
        services=["web"],
        sandbox_id=5,
        project_directory=Path("/tmp/fixture"),
    )


def test_cleanup_removes_only_exact_registered_ids_and_preserves_other_wm_resources():
    fake = FakeDocker()
    registry = _registry(fake)
    fake.add_container("old-prod-container-id", "wm-production-api-123", "wm-production-api-1")
    fake.add_network("old-prod-network-id", "wm-production-api-123", "wm-production-api_default")
    fake.add_volume("old-prod-volume", "wm-production-api-123")
    fake.add_container("sibling-container-id", "wm-other-pytest-555")
    fake.add_network("sibling-network-id", "wm-other-pytest-555")
    fake.add_volume("sibling-volume", "wm-other-pytest-555")

    def create():
        # The registry must have reserved/checked the exact namespace first.
        assert "wm-owned-pytest-001" in registry.projects
        fake.add_container("owned-container-id", "wm-owned-pytest-001")
        fake.add_network("owned-network-id", "wm-owned-pytest-001")
        fake.add_volume("owned-volume", "wm-owned-pytest-001")

    _compose_up(fake, registry, "wm-owned-pytest-001", create)
    registry.cleanup()

    assert set(fake.containers) == {"old-prod-container-id", "sibling-container-id"}
    assert set(fake.networks) == {"old-prod-network-id", "sibling-network-id"}
    assert set(fake.volumes) == {"old-prod-volume", "sibling-volume"}
    removals = [command for command in fake.commands
                if command[1:3] in (["rm", "-f"], ["network", "rm"], ["volume", "rm"])]
    assert removals == [
        ["docker", "rm", "-f", "owned-container-id"],
        ["docker", "network", "rm", "owned-network-id"],
        ["docker", "volume", "rm", "owned-volume"],
    ]
    inventory = [command for command in fake.commands
                 if command[1:3] == ["ps", "-aq"] or command[1:3] in (["network", "ls"], ["volume", "ls"])]
    assert inventory and all(any(arg.startswith("label=com.docker.compose.project=") for arg in command)
                             for command in inventory)
    assert not any("name=wm-" in " ".join(command) for command in inventory)


def test_existing_exact_project_collision_refuses_before_compose_creation():
    fake = FakeDocker()
    registry = _registry(fake)
    fake.add_container("preexisting-id", "wm-owned-pytest-001")
    called = []

    with pytest.raises(RuntimeError, match="already has Docker resources"):
        _compose_up(fake, registry, "wm-owned-pytest-001", lambda: called.append(True))

    assert called == []
    assert "preexisting-id" in fake.containers
    assert not any(command[1:3] in (["rm", "-f"], ["volume", "rm"]) for command in fake.commands)


def test_second_pytest_run_refuses_a_project_owned_by_another_run():
    fake = FakeDocker()
    first = ComposeRunOwnershipRegistry(docker_bin="docker", run=fake.run, run_id="pytest-run-001")
    second = ComposeRunOwnershipRegistry(docker_bin="docker", run=fake.run, run_id="pytest-run-002")
    _compose_up(fake, first, "wm-owned-pytest-001",
                lambda: fake.add_container("first-run-container", "wm-owned-pytest-001"))
    called = []

    with pytest.raises(RuntimeError, match="already has Docker resources"):
        _compose_up(fake, second, "wm-owned-pytest-001", lambda: called.append(True))

    assert called == []
    assert set(fake.containers) == {"first-run-container"}
    assert not any(command[1:3] == ["rm", "-f"] for command in fake.commands)
    first.cleanup()


def test_non_wm_project_is_rejected_before_any_docker_command():
    fake = FakeDocker()
    registry = _registry(fake)
    with pytest.raises(RuntimeError, match="refusing Compose project outside"):
        _compose_up(fake, registry, "production-project", lambda: pytest.fail("must not create"))
    assert fake.commands == []


def test_partial_compose_failure_is_registered_then_cleaned_by_exact_id():
    fake = FakeDocker()
    registry = _registry(fake)

    def compose(*_args, **_kwargs):
        fake.add_container("partial-container-id", "wm-owned-pytest-001")
        raise RuntimeError("synthetic compose failure after create")

    with pytest.raises(RuntimeError, match="synthetic compose failure"):
        registry.compose_up(
            compose, "wm-owned-pytest-001", compose_file=Path("/tmp/fixture/compose.yml"),
            env_file=Path("/tmp/fixture/.env"), cwd=Path("/tmp/fixture"), services=["web"],
            sandbox_id=5, project_directory=Path("/tmp/fixture"),
        )

    registry.cleanup()
    assert fake.containers == {}
    assert [c for c in fake.commands if c[1:3] == ["rm", "-f"]] == [
        ["docker", "rm", "-f", "partial-container-id"]
    ]


def test_unregistered_resource_appearing_under_reserved_project_blocks_all_cleanup():
    fake = FakeDocker()
    registry = _registry(fake)
    _compose_up(fake, registry, "wm-owned-pytest-001",
                lambda: fake.add_container("owned-container-id", "wm-owned-pytest-001"))
    fake.add_container("spoofed-later-id", "wm-owned-pytest-001", "wm-spoofed")

    with pytest.raises(RuntimeError, match="unregistered Docker resource"):
        registry.cleanup()

    assert "owned-container-id" in fake.containers
    assert "spoofed-later-id" in fake.containers
    assert not any(command[1:3] == ["rm", "-f"] for command in fake.commands)


def test_missing_or_mismatched_compose_identity_fails_closed_without_deletion():
    fake = FakeDocker()
    registry = _registry(fake)
    with pytest.raises(RuntimeError, match="working directory differs"):
        _compose_up(fake, registry, "wm-owned-pytest-001",
                    lambda: fake.add_container(
                        "wrong-label-id", "wm-owned-pytest-001",
                        extra_labels={"com.docker.compose.project.working_dir": "/tmp/other"},
                    ))

    with pytest.raises(RuntimeError, match="incomplete ownership capture"):
        registry.cleanup()

    assert "wrong-label-id" in fake.containers
    assert not any(command[1:3] == ["rm", "-f"] for command in fake.commands)


def test_inventory_failure_refuses_compose_before_any_create_or_cleanup():
    fake = FakeDocker()
    fake.fail_list = True
    registry = _registry(fake)
    called = []

    with pytest.raises(RuntimeError, match="inventory command failed"):
        _compose_up(fake, registry, "wm-owned-pytest-001", lambda: called.append(True))

    assert called == []
    assert not any(command[1:3] == ["rm", "-f"] for command in fake.commands)


def test_cleanup_command_failure_is_reported_and_not_swallowed():
    fake = FakeDocker()
    registry = _registry(fake)
    _compose_up(fake, registry, "wm-owned-pytest-001",
                lambda: fake.add_container("owned-container-id", "wm-owned-pytest-001"))
    fake.fail_remove = True

    with pytest.raises(RuntimeError, match="cleanup command failed"):
        registry.cleanup()

    assert "owned-container-id" in fake.containers


def test_inspect_failure_fails_closed_and_leaves_registered_resource():
    fake = FakeDocker()
    registry = _registry(fake)
    _compose_up(fake, registry, "wm-owned-pytest-001",
                lambda: fake.add_container("owned-container-id", "wm-owned-pytest-001"))
    fake.fail_inspect = True

    with pytest.raises(RuntimeError, match="cleanup refused/failed.*inspect failed"):
        registry.cleanup()

    assert "owned-container-id" in fake.containers


def test_second_concurrent_attempt_for_same_project_is_refused():
    fake = FakeDocker()
    registry = _registry(fake)
    inner_called = []

    def outer_create():
        with pytest.raises(RuntimeError, match="active creation attempt"):
            _compose_up(fake, registry, "wm-owned-pytest-001", lambda: inner_called.append(True))

    _compose_up(fake, registry, "wm-owned-pytest-001", outer_create)
    registry.cleanup()
    assert inner_called == []


def test_listed_but_missing_resource_makes_capture_incomplete_and_leaves_it():
    fake = FakeDocker()
    registry = _registry(fake)

    def stale_inventory():
        fake.force_filtered_ids["container"]["wm-owned-pytest-001"] = {"missing-container-id"}

    with pytest.raises(RuntimeError, match="disappeared before ownership capture"):
        _compose_up(fake, registry, "wm-owned-pytest-001", stale_inventory)

    with pytest.raises(RuntimeError, match="incomplete ownership capture"):
        registry.cleanup()
    assert not any(command[1:3] == ["rm", "-f"] for command in fake.commands)


def test_compose_down_is_guarded_and_may_remove_only_registered_project_resources():
    fake = FakeDocker()
    registry = _registry(fake)
    _compose_up(fake, registry, "wm-owned-pytest-001", lambda: (
        fake.add_container("owned-container-id", "wm-owned-pytest-001"),
        fake.add_network("owned-network-id", "wm-owned-pytest-001"),
        fake.add_volume("owned-volume", "wm-owned-pytest-001"),
    ))
    fake.add_container("sibling-container-id", "wm-other-pytest-555")
    fake.add_network("sibling-network-id", "wm-other-pytest-555")
    fake.add_volume("sibling-volume", "wm-other-pytest-555")

    def down(**_kwargs):
        fake.containers.pop("owned-container-id")
        fake.networks.pop("owned-network-id")
        fake.volumes.pop("owned-volume")
        return subprocess.CompletedProcess(["docker", "compose", "down"], 0, "", "")

    result = registry.run_project_operation(
        down, "wm-owned-pytest-001", allow_missing_after=True,
        compose_file=Path("/tmp/fixture/compose.yml"), cwd=Path("/tmp/fixture"),
        project_directory=Path("/tmp/fixture"),
    )
    registry.cleanup()

    assert result.returncode == 0
    assert set(fake.containers) == {"sibling-container-id"}
    assert set(fake.networks) == {"sibling-network-id"}
    assert set(fake.volumes) == {"sibling-volume"}


def test_compose_stop_or_down_refuses_unregistered_project_resource_before_command():
    fake = FakeDocker()
    registry = _registry(fake)
    _compose_up(fake, registry, "wm-owned-pytest-001",
                lambda: fake.add_container("owned-container-id", "wm-owned-pytest-001"))
    fake.add_network("unregistered-network", "wm-owned-pytest-001")
    called = []

    with pytest.raises(RuntimeError, match="unregistered Docker resource"):
        registry.run_project_operation(
            lambda **_kwargs: called.append(True), "wm-owned-pytest-001",
            compose_file=Path("/tmp/fixture/compose.yml"), cwd=Path("/tmp/fixture"),
            project_directory=Path("/tmp/fixture"),
        )

    assert called == []
    assert fake.containers and fake.networks


def test_compose_operation_cannot_switch_to_another_config_or_worktree():
    fake = FakeDocker()
    registry = _registry(fake)
    _compose_up(fake, registry, "wm-owned-pytest-001",
                lambda: fake.add_container("owned-container-id", "wm-owned-pytest-001"))
    called = []

    with pytest.raises(RuntimeError, match="changed the registered config file"):
        registry.run_project_operation(
            lambda **_kwargs: called.append(True), "wm-owned-pytest-001",
            compose_file=Path("/tmp/other/compose.yml"), cwd=Path("/tmp/fixture"),
            project_directory=Path("/tmp/fixture"),
        )
    assert called == []


def test_compose_stop_keeps_registered_inventory_unchanged():
    fake = FakeDocker()
    registry = _registry(fake)
    _compose_up(fake, registry, "wm-owned-pytest-001",
                lambda: fake.add_container("owned-container-id", "wm-owned-pytest-001"))

    result = registry.run_project_operation(
        lambda **_kwargs: subprocess.CompletedProcess(["docker", "compose", "stop"], 0, "", ""),
        "wm-owned-pytest-001",
        compose_file=Path("/tmp/fixture/compose.yml"), cwd=Path("/tmp/fixture"),
        project_directory=Path("/tmp/fixture"),
    )
    registry.cleanup()
    assert result.returncode == 0
    assert fake.containers == {}
