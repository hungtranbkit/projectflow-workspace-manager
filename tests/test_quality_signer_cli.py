"""Standalone signer CLI must resolve its trusted package under isolated Python."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def test_signer_cli_imports_trusted_project_from_unrelated_cwd(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "agent_quality_sign_pilot.py"
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PROJECTFLOW_QUALITY_ATTESTATION_KEY", None)
    env.pop("PROJECTFLOW_QUALITY_ATTESTATION_PRIVATE_KEY_FILE", None)
    result = subprocess.run(
        [sys.executable, "-I", str(script), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--receipt-dir" in result.stdout
