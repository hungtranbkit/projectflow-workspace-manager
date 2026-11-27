"""Safe synthetic reproducer for the former prefix-only cleanup selector.

This does not import pytest.conftest, access the Docker socket, or delete any
resource. Exit 1 is expected: the old startswith('wm-') criterion selects
production-like and sibling resources alongside this test's own resource.
"""

resources = [
    {"id": "test-owned-1", "name": "wm-test-project-001", "owner": "this-run"},
    {"id": "service-1", "name": "wm-production-service-001", "owner": "other-project"},
    {"id": "sibling-1", "name": "wm-sibling-pytest-001", "owner": "other-run"},
]
legacy_targets = [resource for resource in resources if resource["name"].startswith("wm-")]
unsafe = [resource["id"] for resource in legacy_targets if resource["owner"] != "this-run"]
print(f"synthetic legacy targets={','.join(item['id'] for item in legacy_targets)}")
print(f"synthetic unowned targets={','.join(unsafe)}")
raise SystemExit(1 if unsafe else 0)
