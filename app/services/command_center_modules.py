"""Pure, versioned curated module manifest for Command Center (R1-01).

Distinct from native architecture snapshots: this is reviewed module identity
and grouping, not a scanner, status writer, runtime observation or artifact store.
All consumers must separately verify the pinned source and access scope.
"""
from __future__ import annotations

import json
import re
from collections import deque

SCHEMA = "pf-command-center-modules/1"
MAX_MODULES = 1000
MAX_EDGES = 5000
PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
MODULE = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}")
COMMIT = re.compile(r"[0-9a-f]{40}")
PRESENCE = ("observed", "target-only", "unknown")


class ModuleManifestError(ValueError):
    """Closed-schema violation; never echo untrusted values into logs."""


def _require(condition, message):
    if not condition:
        raise ModuleManifestError(message)


def _keys(value, fields, where):
    _require(type(value) is dict and set(value) == set(fields), where + ": invalid fields")


def _id(value):
    _require(isinstance(value, str) and MODULE.fullmatch(value) is not None
             and ".." not in value, "module: invalid stable ID")
    return value


def _label(value):
    _require(isinstance(value, str) and 0 < len(value) <= 160
             and bool(value.strip()) and not any(
                 ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF
                 for c in value),
             "module: invalid label")
    return value


def validate_manifest(manifest, *, expected_project, expected_commit=None):
    """Validate and normalize a bounded manifest; no IO or authority mutation.

    IDs are local to the canonical project, not labels or source paths.
    Parent edges form an acyclic hierarchy. Dependency cycles are legitimate
    graph facts; preserve them without recursively traversing them here.
    Observed presence is a curator declaration only, not health/QA proof.
    """
    _keys(manifest, ("schema", "project", "source_commit", "modules", "dependencies"), "manifest")
    _require(manifest["schema"] == SCHEMA, "manifest: unsupported schema")
    project = manifest["project"]
    _require(isinstance(project, str) and PROJECT.fullmatch(project) is not None
             and ".." not in project, "manifest: canonical project required")
    _require(project == expected_project, "manifest: project mismatch")
    commit = manifest["source_commit"]
    _require(isinstance(commit, str) and COMMIT.fullmatch(commit) is not None,
             "manifest: full source commit required")
    _require(expected_commit is None or commit == expected_commit, "manifest: source mismatch")
    modules, edges = manifest["modules"], manifest["dependencies"]
    _require(type(modules) is list and len(modules) <= MAX_MODULES, "manifest: module limit")
    _require(type(edges) is list and len(edges) <= MAX_EDGES, "manifest: dependency limit")
    by_id = {}
    for row in modules:
        _keys(row, ("id", "label", "parent", "presence"), "module")
        mid = _id(row["id"])
        _require(mid not in by_id, "module: duplicate ID")
        parent = row["parent"]
        if parent is not None:
            _id(parent)
        _require(row["presence"] in PRESENCE, "module: invalid presence")
        by_id[mid] = dict(id=mid, label=_label(row["label"]), parent=parent, presence=row["presence"])
    children = {mid: [] for mid in by_id}
    roots = deque()
    for mid, row in by_id.items():
        parent = row["parent"]
        if parent is None:
            roots.append(mid)
        else:
            _require(parent in by_id, "module: unknown parent")
            children[parent].append(mid)
    visited = 0
    while roots:
        visited += 1
        roots.extend(children[roots.popleft()])
    _require(visited == len(by_id), "module: parent cycle")
    pairs = set()
    for edge in edges:
        _keys(edge, ("from", "to"), "dependency")
        a, b = _id(edge["from"]), _id(edge["to"])
        _require(a in by_id and b in by_id, "dependency: unknown endpoint")
        _require(a != b, "dependency: self edge")
        _require((a, b) not in pairs, "dependency: duplicate edge")
        pairs.add((a, b))
    return dict(schema=SCHEMA, project=project, source_commit=commit,
                modules=[by_id[mid] for mid in sorted(by_id)],
                dependencies=[{"from": a, "to": b} for a, b in sorted(pairs)])


def canonical_json(manifest, *, expected_project, expected_commit=None):
    """Stable serialization for evidence hashing; no time- or name-based IDs."""
    return json.dumps(validate_manifest(manifest, expected_project=expected_project,
                                         expected_commit=expected_commit),
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stable_key(project, module_id):
    """Unambiguous identity across projects and module label renames."""
    _require(isinstance(project, str) and PROJECT.fullmatch(project) is not None
             and ".." not in project, "module: invalid project")
    return (project, _id(module_id))
