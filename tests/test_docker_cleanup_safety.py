"""Stdlib-only regressions for fail-closed test Docker cleanup."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import ModuleType
import unittest
import uuid
from unittest.mock import patch

from test_support.docker_test_safety import (
    DockerTestOwnershipError,
    RegisteredSandbox,
    cleanup_registered_sandboxes,
    load_verified_docker_test_owner,
    register_test_sandbox,
    verify_exact_compose_project_resources,
)


class _ManagerDb:
    def __init__(self, row):
        self.row = row
        self.queries = []

    def one(self, query, params):
        self.queries.append((query, params))
        return self.row


class _Manager:
    def __init__(self, row):
        self.db = _ManagerDb(row)


class DockerOwnerTests(unittest.TestCase):
    def test_unconfigured_unit_run_makes_no_docker_query(self):
        calls = []
        owner = load_verified_docker_test_owner({}, runner=lambda *a, **k: calls.append(a))
        self.assertIsNone(owner)
        self.assertEqual([], calls)

    def test_partial_owner_config_fails_before_docker_query(self):
        calls = []
        with self.assertRaises(DockerTestOwnershipError):
            load_verified_docker_test_owner(
                {"PROJECTFLOW_TEST_DOCKER_RUN_ID": "run-12345678"},
                runner=lambda *a, **k: calls.append(a),
            )
        self.assertEqual([], calls)

    def test_mismatched_owner_manifest_fails_before_docker_query(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o700)
            socket_path = root / "docker.sock"
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(socket_path))
            socket_path.chmod(0o600)
            run_id = uuid.uuid4().hex
            endpoint = f"unix://{socket_path}"
            owner_file = root / "owner.json"
            owner_file.write_text(json.dumps({"run_id": "other-run-12345678", "endpoint": endpoint, "engine_id": "engine-123"}))
            owner_file.chmod(0o600)
            env = {
                "PROJECTFLOW_TEST_DOCKER_RUN_ID": run_id,
                "PROJECTFLOW_TEST_DOCKER_ENDPOINT": endpoint,
                "PROJECTFLOW_TEST_DOCKER_OWNER_FILE": str(owner_file),
                "PROJECTFLOW_TEST_DOCKER_BIN": "/test/bin/docker",
                "DOCKER_HOST": endpoint,
            }
            try:
                with self.assertRaises(DockerTestOwnershipError):
                    load_verified_docker_test_owner(env, runner=lambda *a, **k: calls.append(a))
            finally:
                sock.close()
        self.assertEqual([], calls)

    def test_private_run_socket_and_engine_id_are_verified(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o700)
            socket_path = root / "docker.sock"
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(socket_path))
            socket_path.chmod(0o600)
            run_id = uuid.uuid4().hex
            endpoint = f"unix://{socket_path}"
            owner_file = root / "owner.json"
            owner_file.write_text(json.dumps({"run_id": run_id, "endpoint": endpoint, "engine_id": "engine-123"}))
            owner_file.chmod(0o600)
            env = {
                "PROJECTFLOW_TEST_DOCKER_RUN_ID": run_id,
                "PROJECTFLOW_TEST_DOCKER_ENDPOINT": endpoint,
                "PROJECTFLOW_TEST_DOCKER_OWNER_FILE": str(owner_file),
                "PROJECTFLOW_TEST_DOCKER_BIN": "/test/bin/docker",
                "DOCKER_HOST": endpoint,
            }

            def runner(argv, **kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, stdout="engine-123\n", stderr="")

            try:
                owner = load_verified_docker_test_owner(env, runner=runner)
            finally:
                sock.close()
        self.assertEqual(run_id, owner.run_id)
        self.assertEqual(endpoint, owner.endpoint)
        self.assertEqual([["/test/bin/docker", "--host", endpoint, "info", "--format", "{{.ID}}"]], calls)

    def test_shared_endpoint_and_mismatched_manifest_fail_closed(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o700)
            socket_path = root / "docker.sock"
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(socket_path))
            socket_path.chmod(0o600)
            run_id = uuid.uuid4().hex
            endpoint = f"unix://{socket_path}"
            owner_file = root / "owner.json"
            owner_file.write_text(json.dumps({"run_id": "another-run-12345678", "endpoint": endpoint, "engine_id": "engine-123"}))
            owner_file.chmod(0o600)
            env = {
                "PROJECTFLOW_TEST_DOCKER_RUN_ID": run_id,
                "PROJECTFLOW_TEST_DOCKER_ENDPOINT": endpoint,
                "PROJECTFLOW_TEST_DOCKER_OWNER_FILE": str(owner_file),
                "PROJECTFLOW_TEST_DOCKER_BIN": "/test/bin/docker",
                "DOCKER_HOST": "unix:///var/run/docker.sock",
            }
            try:
                with self.assertRaises(DockerTestOwnershipError):
                    load_verified_docker_test_owner(env, runner=lambda *a, **k: calls.append(a))
            finally:
                sock.close()
        self.assertEqual([], calls)

    def test_engine_id_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o700)
            socket_path = root / "docker.sock"
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(socket_path))
            socket_path.chmod(0o600)
            run_id = uuid.uuid4().hex
            endpoint = f"unix://{socket_path}"
            owner_file = root / "owner.json"
            owner_file.write_text(json.dumps({"run_id": run_id, "endpoint": endpoint, "engine_id": "expected-engine"}))
            owner_file.chmod(0o600)
            env = {
                "PROJECTFLOW_TEST_DOCKER_RUN_ID": run_id,
                "PROJECTFLOW_TEST_DOCKER_ENDPOINT": endpoint,
                "PROJECTFLOW_TEST_DOCKER_OWNER_FILE": str(owner_file),
                "PROJECTFLOW_TEST_DOCKER_BIN": "/test/bin/docker",
                "DOCKER_HOST": endpoint,
            }
            try:
                with self.assertRaises(DockerTestOwnershipError):
                    load_verified_docker_test_owner(
                        env,
                        runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="different-engine\n", stderr=""),
                    )
            finally:
                sock.close()


class ExactResourceCleanupTests(unittest.TestCase):
    def setUp(self):
        self.owner = type("Owner", (), {"docker_bin": "/test/bin/docker", "endpoint": "unix:///isolated/run/docker.sock"})()

    def test_exact_project_labels_are_inspected_and_no_prefix_scan_occurs(self):
        calls = []
        project = "wm-test-run-123"

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[3:5] == ["ps", "-aq"]:
                return subprocess.CompletedProcess(argv, 0, stdout="container-run-owned\n", stderr="")
            if argv[3:6] == ["network", "ls", "-q"]:
                return subprocess.CompletedProcess(argv, 0, stdout="network-run-owned\n", stderr="")
            if argv[3:6] == ["volume", "ls", "-q"]:
                return subprocess.CompletedProcess(argv, 0, stdout="volume-run-owned\n", stderr="")
            if "inspect" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout=project + "\n", stderr="")
            return subprocess.CompletedProcess(argv, 2, stdout="", stderr="unexpected command")

        self.assertTrue(verify_exact_compose_project_resources(self.owner, project, runner=runner))
        listing = [argv for argv in calls if "--filter" in argv]
        self.assertEqual(3, len(listing))
        self.assertTrue(all(argv[argv.index("--filter") + 1] == f"label=com.docker.compose.project={project}" for argv in listing))
        self.assertFalse(any("--format" in argv and "{{.Name}}" in argv for argv in calls))

    def test_mismatched_resource_label_or_command_error_rejects_cleanup(self):
        project = "wm-test-run-123"
        seen = 0

        def mismatched_runner(argv, **kwargs):
            nonlocal seen
            seen += 1
            if argv[3:5] == ["ps", "-aq"]:
                return subprocess.CompletedProcess(argv, 0, stdout="foreign-id\n", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="wm-other-project\n", stderr="")

        self.assertFalse(verify_exact_compose_project_resources(self.owner, project, runner=mismatched_runner))
        self.assertGreater(seen, 1)
        self.assertFalse(verify_exact_compose_project_resources(self.owner, project, runner=lambda *a, **k: (_ for _ in ()).throw(OSError("offline"))))

    def test_empty_registry_never_reads_owner_or_calls_cleanup(self):
        calls = []
        cleanup_registered_sandboxes(
            [],
            current_owner=lambda: calls.append("owner"),
            verify_project=lambda *_: calls.append("verify"),
            cleanup=lambda *_: calls.append("cleanup"),
        )
        self.assertEqual([], calls)

    def test_unregistered_foreign_wm_name_is_never_cleaned(self):
        calls = []
        cleanup_registered_sandboxes(
            [],  # a foreign resource with a wm-* name was never registered by this run
            current_owner=lambda: calls.append("owner"),
            verify_project=lambda *_: calls.append("verify"),
            cleanup=lambda *_: calls.append("cleanup"),
        )
        self.assertEqual([], calls)


class ConftestFixtureIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.owner = type("Owner", (), {"docker_bin": "/test/bin/docker", "endpoint": "unix:///isolated/run/docker.sock"})()

    class Runtime:
        docker_bin = "/test/bin/docker"

        def compose_up(self, *args, **kwargs):
            return None

    class Manager:
        def __init__(self, row):
            self.db = _ManagerDb(row)
            self.runtime = ConftestFixtureIntegrationTests.Runtime()
            self.provision_calls = []
            self.cleanup_calls = []

        def provision(self, sandbox_id):
            self.provision_calls.append(sandbox_id)

        def cleanup(self, sandbox_id, force=False):
            self.cleanup_calls.append((sandbox_id, force))

    def _fixture(self, *, owner_loader, verifier, cleanup_registered=cleanup_registered_sandboxes):
        conftest = Path(__file__).with_name("conftest.py")
        tree = ast.parse(conftest.read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_docker_sandbox_safety_net")
        node.decorator_list = []
        namespace = {
            "cleanup_registered_sandboxes": cleanup_registered,
            "load_verified_docker_test_owner": owner_loader,
            "register_test_sandbox": register_test_sandbox,
            "verify_exact_compose_project_resources": verifier,
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(conftest), "exec"), namespace)
        app_pkg = ModuleType("app")
        app_pkg.__path__ = []
        services_pkg = ModuleType("app.services")
        services_pkg.__path__ = []
        manager_module = ModuleType("app.services.sandbox_manager")
        manager_module.SandboxManager = self.Manager
        runtime_module = ModuleType("app.services.sandbox_runtime")
        runtime_module.SandboxRuntimeService = self.Runtime
        return namespace["_docker_sandbox_safety_net"], {
            "app": app_pkg,
            "app.services": services_pkg,
            "app.services.sandbox_manager": manager_module,
            "app.services.sandbox_runtime": runtime_module,
        }

    def test_ordinary_session_teardown_does_not_probe_docker(self):
        owner_calls = []
        verify_calls = []
        cleanup_calls = []
        fixture, modules = self._fixture(
            owner_loader=lambda **kwargs: owner_calls.append(kwargs),
            verifier=lambda *args, **kwargs: verify_calls.append(args),
        )
        with patch.dict(sys.modules, modules):
            gen = fixture()
            next(gen)
            with self.assertRaises(StopIteration):
                next(gen)
        self.assertEqual([], owner_calls)
        self.assertEqual([], verify_calls)
        self.assertEqual([], cleanup_calls)

    def test_real_provision_is_rejected_before_docker_without_owner(self):
        owner_calls = []
        fixture, modules = self._fixture(
            owner_loader=lambda **kwargs: owner_calls.append(kwargs) or None,
            verifier=lambda *args, **kwargs: self.fail("unowned endpoint must not be enumerated"),
        )
        with patch.dict(sys.modules, modules):
            gen = fixture()
            next(gen)
            manager = self.Manager({"status": "CREATED", "compose_project": "wm-test-run-123"})
            with self.assertRaises(DockerTestOwnershipError):
                manager.provision(17)
            with self.assertRaises(StopIteration):
                next(gen)
        self.assertEqual([{"docker_bin": "/test/bin/docker"}], owner_calls)
        self.assertEqual([], manager.provision_calls)
        self.assertEqual([], manager.cleanup_calls)

    def test_fixture_cleans_only_registered_id_after_exact_label_verification(self):
        owner = type("Owner", (), {"docker_bin": "/test/bin/docker", "endpoint": "unix:///isolated/run/docker.sock"})()
        verifier_calls = []
        fixture, modules = self._fixture(
            owner_loader=lambda **kwargs: owner,
            verifier=lambda actual_owner, project: verifier_calls.append((actual_owner, project)) or True,
        )
        with patch.dict(sys.modules, modules):
            gen = fixture()
            next(gen)
            manager = self.Manager({"status": "CREATED", "compose_project": "wm-this-run-123"})
            foreign = self.Manager({"status": "CREATED", "compose_project": "wm-foreign-123"})
            manager.provision(17)
            with self.assertRaises(StopIteration):
                next(gen)
        self.assertEqual([17], manager.provision_calls)
        self.assertEqual([(owner, "wm-this-run-123")], verifier_calls)
        self.assertEqual([(17, True)], manager.cleanup_calls)
        self.assertEqual([], foreign.cleanup_calls)

    def test_sandbox_cannot_be_registered_without_matching_owner(self):
        project = "wm-test-run-123"
        manager = _Manager({"status": "PROVISIONING", "compose_project": project})
        registry = {}
        with self.assertRaises(DockerTestOwnershipError):
            register_test_sandbox(manager, 41, registry, current_owner=lambda: None)
        self.assertEqual({}, registry)
        self.assertEqual([], manager.db.queries)

    def test_registration_tracks_exact_test_created_sandbox_and_project(self):
        project = "wm-this-run-123"
        manager = _Manager({"status": "PROVISIONING", "compose_project": project})
        registry = {}
        owner = self.owner
        resource = register_test_sandbox(manager, 41, registry, current_owner=lambda: owner)
        self.assertIs(registry[(id(manager), 41)], resource)
        self.assertEqual((41, project, owner), (resource.sandbox_id, resource.compose_project, resource.owner))

    def test_invalid_compose_project_cannot_be_registered(self):
        manager = _Manager({"status": "PROVISIONING", "compose_project": "wm-foreign/name"})
        registry = {}
        with self.assertRaises(DockerTestOwnershipError):
            register_test_sandbox(manager, 41, registry, current_owner=lambda: self.owner)
        self.assertEqual({}, registry)

    def test_only_registered_run_owned_sandbox_id_is_cleaned(self):
        project = "wm-test-run-123"
        manager = _Manager({"status": "STOPPED", "compose_project": project})
        resource = RegisteredSandbox(manager, 41, project, self.owner)
        calls = []
        cleanup_registered_sandboxes(
            [resource],
            current_owner=lambda: self.owner,
            verify_project=lambda owner, exact_project: calls.append(("verify", exact_project)) or True,
            cleanup=lambda mgr, sid: calls.append(("cleanup", mgr, sid)),
        )
        self.assertEqual([("verify", project), ("cleanup", manager, 41)], calls)
        self.assertEqual([("SELECT * FROM sandboxes WHERE id=?", (41,))], manager.db.queries)

    def test_project_mismatch_or_failed_verification_never_cleans(self):
        self_project = "wm-test-run-123"
        manager = _Manager({"status": "STOPPED", "compose_project": "wm-foreign-project"})
        resource = RegisteredSandbox(manager, 41, self_project, self.owner)
        calls = []
        with self.assertRaises(DockerTestOwnershipError):
            cleanup_registered_sandboxes(
                [resource],
                current_owner=lambda: self.owner,
                verify_project=lambda *_: calls.append("verify") or True,
                cleanup=lambda *_: calls.append("cleanup"),
            )
        self.assertEqual([], calls)

        manager.db.row = {"status": "STOPPED", "compose_project": self_project}
        with self.assertRaises(DockerTestOwnershipError):
            cleanup_registered_sandboxes(
                [resource],
                current_owner=lambda: self.owner,
                verify_project=lambda *_: False,
                cleanup=lambda *_: calls.append("cleanup"),
            )
        self.assertEqual([], calls)

    def test_owner_change_and_verification_exception_fail_closed(self):
        project = "wm-test-run-123"
        manager = _Manager({"status": "STOPPED", "compose_project": project})
        resource = RegisteredSandbox(manager, 41, project, self.owner)
        calls = []
        with self.assertRaises(DockerTestOwnershipError):
            cleanup_registered_sandboxes(
                [resource],
                current_owner=lambda: None,
                verify_project=lambda *_: calls.append("verify") or True,
                cleanup=lambda *_: calls.append("cleanup"),
            )
        self.assertEqual([], calls)
        with self.assertRaises(OSError):
            cleanup_registered_sandboxes(
                [resource],
                current_owner=lambda: self.owner,
                verify_project=lambda *_: (_ for _ in ()).throw(OSError("inspect failed")),
                cleanup=lambda *_: calls.append("cleanup"),
            )
        self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
