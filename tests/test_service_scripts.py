from pathlib import Path
import yaml

ROOT=Path(__file__).parents[1]

def test_systemd_security_and_restart_policy():
    unit=(ROOT/"systemd/workspace-manager.service.in").read_text()
    assert "WorkingDirectory=__PROJECT_DIR__" in unit
    assert "EnvironmentFile=-__PROJECT_DIR__/.env" in unit
    assert "Restart=on-failure" in unit
    assert "NoNewPrivileges=true" in unit
    assert "0.0.0.0" not in unit

def test_start_script_enforces_local_bind_and_has_identity_check():
    script=(ROOT/"scripts/start.sh").read_text()
    assert '--host 127.0.0.1 --port 8765' in script
    assert "ProjectFlow Workspace Manager" in script
    assert "PORT_CONFLICT" in script
    assert "kill " not in script

def test_project_service_contract():
    contract=yaml.safe_load((ROOT/"PROJECT.yaml").read_text())
    for command in ("start","status","logs"):
        assert contract["commands"][command]["command"] == f"./scripts/{command}.sh"
