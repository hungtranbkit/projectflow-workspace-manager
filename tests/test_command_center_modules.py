"""Synthetic contract tests; no project data, network, or runtime services."""
import copy
import json

import pytest

from app.services.command_center_modules import (
    SCHEMA, ModuleManifestError, canonical_json, stable_key, validate_manifest
)

PROJECT = "example/repo"
SHA = "a" * 40


def sample():
    return dict(schema=SCHEMA, project=PROJECT, source_commit=SHA,
                modules=[
                    dict(id="core", label="Core", parent=None, presence="observed"),
                    dict(id="queue", label="Queue", parent="core", presence="unknown"),
                    dict(id="future", label="Future", parent=None, presence="target-only"),
                ], dependencies=[{"from": "core", "to": "queue"}, {"from": "queue", "to": "core"}])


def valid(m):
    return validate_manifest(m, expected_project=PROJECT, expected_commit=SHA)


def test_roundtrip_order_and_dependency_cycles():
    m = sample()
    encoded = canonical_json(m, expected_project=PROJECT, expected_commit=SHA)
    m["modules"].reverse()
    m["dependencies"].reverse()
    assert canonical_json(m, expected_project=PROJECT, expected_commit=SHA) == encoded
    assert valid(json.loads(encoded)) == json.loads(encoded)
    assert len(valid(m)["dependencies"]) == 2


def test_rename_preserves_identity_and_does_not_mutate_input():
    m = sample()
    before = copy.deepcopy(m)
    result = valid(m)
    result["modules"][0]["label"] = "edited result"
    assert m == before
    m["modules"][1]["label"] = "Renamed queue"
    assert stable_key(PROJECT, m["modules"][1]["id"]) == (PROJECT, "queue")
    assert stable_key(PROJECT, "queue") != stable_key("other/repo", "queue")


@pytest.mark.parametrize("kind", [
    "duplicate", "parent-cycle", "self-parent", "missing-parent", "missing-endpoint",
    "duplicate-edge", "self-edge", "cross-project", "wrong-sha", "schema",
    "extra-field", "bad-id", "bad-label", "bad-presence", "cross-project-endpoint",
    "module-limit", "edge-limit",
])
def test_reject_invalid(kind):
    m = sample()
    if kind == "duplicate": m["modules"].append(copy.deepcopy(m["modules"][0]))
    elif kind == "parent-cycle": m["modules"][0]["parent"] = "queue"
    elif kind == "self-parent": m["modules"][0]["parent"] = "core"
    elif kind == "missing-parent": m["modules"][1]["parent"] = "missing"
    elif kind == "missing-endpoint": m["dependencies"][0]["to"] = "missing"
    elif kind == "duplicate-edge": m["dependencies"].append(copy.deepcopy(m["dependencies"][0]))
    elif kind == "self-edge": m["dependencies"][0]["to"] = "core"
    elif kind == "cross-project": m["project"] = "other/repo"
    elif kind == "wrong-sha": m["source_commit"] = "b" * 40
    elif kind == "schema": m["schema"] = "unknown"
    elif kind == "extra-field": m["modules"][0]["health"] = "PASS"
    elif kind == "bad-id": m["modules"][0]["id"] = "../core"
    elif kind == "bad-label": m["modules"][0]["label"] = "bad\nlabel"
    elif kind == "bad-presence": m["modules"][0]["presence"] = "healthy"
    elif kind == "cross-project-endpoint": m["dependencies"][0]["to"] = "other/repo:queue"
    elif kind == "module-limit": m["modules"] = m["modules"] * 1000
    elif kind == "edge-limit": m["dependencies"] = m["dependencies"] * 3000
    with pytest.raises(ModuleManifestError):
        valid(m)


def test_empty_and_deep_tree_without_recursion():
    m = sample()
    m["modules"], m["dependencies"] = [], []
    assert valid(m)["modules"] == []
    m["modules"] = [dict(id="m"+str(i), label=str(i), parent="m"+str(i-1) if i else None,
                         presence="unknown") for i in range(1000)]
    assert len(valid(m)["modules"]) == 1000
