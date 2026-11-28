"""Archive projection regression tests; no network or controller required."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from archive_live_snapshot import project

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def fixture():
    counts = {"dell-linux": 20, "hp-linux": 9, "m910": 10, "macbook": 12, "nec1": 4}
    offline = ["local", "win", "dell-5530", "mac17-macbook-pro"]
    items = [
        {"label": name, "status": "online", "open_sessions": n,
         "recent_activity_signals": 0, "private": "DO_NOT_PUBLISH"}
        for name, n in counts.items()
    ] + [{"label": name, "status": "offline", "open_sessions": 0,
          "recent_activity_signals": 0} for name in offline]
    return {
        "schema_version": 2, "kind": "terminal-mcp-public-aggregate",
        "observed_at": NOW.isoformat(), "fresh_window_seconds": 300,
        "nodes": {"total": 9, "online": 5, "offline": 4, "unknown": 0,
                  "degraded": 0, "items": items},
        "sessions": {"open": 55, "recent_activity_signals": 0,
                     "observation_source": "terminal_mcp_authenticated_fleet",
                     "coverage": {"covered_nodes": list(counts), "unreachable_nodes": offline},
                     "items": [{"prompt": "DO_NOT_PUBLISH"}], "proven_running": 2},
        "notes": ["DO_NOT_PUBLISH"],
    }


class ArchiveTests(unittest.TestCase):
    def test_same_inventory_and_privacy(self):
        data = fixture()
        result = project(data, NOW)
        self.assertEqual(result["sessions"]["open"], 55)
        self.assertEqual(result["nodes"]["items"][-4]["open_sessions"], 0)
        self.assertEqual(result["observed_at"], data["observed_at"])
        self.assertIsNone(result["sessions"]["running_verified"])
        self.assertNotIn("DO_NOT_PUBLISH", json.dumps(result))
        self.assertNotIn("items", result["sessions"])

    def test_reject_incomplete_or_registry_only(self):
        data = fixture()
        data["sessions"]["coverage"]["covered_nodes"].remove("m910")
        with self.assertRaises(ValueError):
            project(data, NOW)
        data = fixture()
        data["sessions"]["observation_source"] = "registry_only"
        with self.assertRaises(ValueError):
            project(data, NOW)

    def test_reject_offline_ghost_and_count_mismatch(self):
        data = fixture()
        data["nodes"]["items"][-4]["open_sessions"] = 9
        data["sessions"]["open"] = 64
        with self.assertRaises(ValueError):
            project(data, NOW)
        data = fixture()
        data["sessions"]["open"] = 18
        with self.assertRaises(ValueError):
            project(data, NOW)

    def test_reject_stale_and_future(self):
        for delta in (-121, 31):
            data = fixture()
            data["observed_at"] = (NOW + timedelta(seconds=delta)).isoformat()
            with self.assertRaises(ValueError):
                project(data, NOW)

    def test_degraded_projects_as_unknown(self):
        data = fixture()
        data["nodes"]["items"][0]["status"] = "degraded"
        data["nodes"]["online"] = 4
        data["nodes"]["degraded"] = 1
        result = project(data, NOW)
        self.assertEqual(result["nodes"]["unknown"], 1)
        self.assertEqual(result["sessions"]["open"], 55)

    def test_reject_duplicate_or_unapproved_label(self):
        for name in ("dell-linux", "private-hostname"):
            data = fixture()
            data["nodes"]["items"][1]["label"] = name
            with self.assertRaises(ValueError):
                project(data, NOW)

    def test_failed_cli_preserves_previous_archive(self):
        script = Path(__file__).with_name("archive_live_snapshot.py")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live-status.json"
            path.write_text("previous archive")
            proc = subprocess.run([sys.executable, str(script), "--output", str(path)],
                                  input="{}", text=True, capture_output=True)
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(path.read_text(), "previous archive")
            proc = subprocess.run([sys.executable, str(script), "--output", str(path)],
                                  input=json.dumps(fixture()), text=True, capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(path.read_text())["sessions"]["open"], 55)


if __name__ == "__main__":
    unittest.main()
