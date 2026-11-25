import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def fleet():
    from app.services import fleet_permissions
    return fleet_permissions


@pytest.mark.parametrize('text,expected', [
    ('could not read Username: terminal prompts disabled', 'AUTH_REQUIRED'),
    ('HTTP 403: Resource not accessible', 'GIT_WRITE_DENIED'),
    ('Permission denied (publickey)', 'AUTH_REQUIRED'),
    ('Permission denied: workspace', 'ACL_DENIED'),
    ('STATUS_PROBE_TIMEOUT', 'STATUS_PROBE_TIMEOUT'),
    ('NODE_UNREACHABLE', 'OFFLINE'),
    ('sudo: a password is required', 'SUDO_REQUIRED'),
    ('unexpected argument --x', 'CLI_UNSUPPORTED'),
    ('trust this folder', 'TRUST_REQUIRED'),
])
def test_failure_classification_is_not_success(text, expected):
    assert fleet().classify(1, text) == expected


def test_probe_times_out_and_never_records_secret_output():
    result = fleet().probe([sys.executable, '-c', 'import time; print("ghp_secret",flush=True); time.sleep(5)'], timeout=.1)
    assert result.code == 'PROBE_TIMEOUT'
    assert 'ghp_secret' not in json.dumps(result.public())


def test_setup_repeat_rollback_preserves_auth_and_detects_conflict(git_repo, tmp_path):
    root, repo = git_repo
    from app.services.git_workspace import GitWorkspaceService
    _, wt, _ = GitWorkspaceService(root).create_agent(repo, 'codex', 'fleet-test')
    state = tmp_path / 'state'
    manager = fleet().FleetProfile(wt, state)
    first = manager.apply(trust_owned=True)
    assert first['changed']
    assert not manager.apply(trust_owned=True)['changed']
    assert manager.path.stat().st_mode & 0o777 == 0o600
    assert manager.check()['code'] == 'PASS'
    manager.path.write_text('{}')
    with pytest.raises(fleet().FleetError, match='ROLLBACK_CONFLICT'):
        manager.rollback()


def test_rollback_removes_only_owned_profile(git_repo, tmp_path):
    root, repo = git_repo
    from app.services.git_workspace import GitWorkspaceService
    _, wt, _ = GitWorkspaceService(root).create_agent(repo, 'codex', 'rollback-test')
    manager = fleet().FleetProfile(wt, tmp_path / 'state')
    manager.apply(trust_owned=True)
    manager.rollback()
    assert not manager.path.exists()
    assert manager.rollback()['changed'] is False


def test_trust_dirty_and_branch_guards(git_repo, tmp_path):
    root, repo = git_repo
    from app.services.git_workspace import GitWorkspaceService
    _, wt, _ = GitWorkspaceService(root).create_agent(repo, 'codex', 'trust-test')
    manager = fleet().FleetProfile(wt, tmp_path / 'state')
    with pytest.raises(fleet().FleetError, match='TRUST_REQUIRED'):
        manager.apply()
    (wt / 'wip').write_text('preserve')
    with pytest.raises(fleet().FleetError, match='DIRTY_WORKSPACE'):
        manager.apply(trust_owned=True)
    assert (wt / 'wip').read_text() == 'preserve'


def test_git_service_fails_fast_instead_of_calling_prompt(git_repo, monkeypatch, tmp_path):
    from app.services.git_workspace import GitWorkspaceService
    root, repo = git_repo
    monkeypatch.setenv('GIT_TERMINAL_PROMPT', '1')
    result = GitWorkspaceService(root)._run(
        [sys.executable, '-c', 'import os; print(os.environ.get("GIT_TERMINAL_PROMPT")); print(os.environ.get("GCM_INTERACTIVE"))'], repo)
    assert result.stdout.splitlines() == ['0', 'Never']


def test_unknown_cli_and_blocked_preflight_never_launch(git_repo, tmp_path):
    root, repo = git_repo
    manager = fleet().FleetProfile(repo, tmp_path / 'state')
    with pytest.raises(fleet().FleetError):
        manager.launch('codex', 'hello')


def test_codex_profile_keeps_sandbox_and_suppresses_only_update_check():
    argv = fleet().agent_argv('codex', Path('/owned'), 'hello')
    assert 'workspace-write' in argv
    assert 'approval_policy="never"' in argv
    assert 'check_for_update_on_startup=false' in argv
    assert not any('bypass' in arg or 'yolo' in arg for arg in argv)


def test_canary_push_readback_cleanup_with_real_bare_remote(git_repo, tmp_path):
    from app.services.git_workspace import GitWorkspaceService
    root, repo = git_repo
    remote = root / 'remote.git'
    subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin', str(remote)], check=True)
    before = subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain'])
    result = GitWorkspaceService(root).push_canary(repo)
    assert result['code'] == 'PASS'
    assert result['pushed_sha'] == result['readback_sha']
    assert result['cleanup'] == 'PASS'
    assert subprocess.check_output(['git', 'ls-remote', str(remote)]) == b''
    assert subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain']) == before


def test_container_launcher_confines_writes_and_is_not_root(tmp_path):
    binary = tmp_path/'codex'
    binary.write_text('test')
    auth = tmp_path/'auth.json'
    auth.write_text('{}')
    command = fleet().container_argv(tmp_path, 'hello', binary, auth, 'sha256:' + 'a'*64, 1000, 1000, 'pf83-test')
    assert command[command.index('--user')+1] == '1000:1000'
    assert '--read-only' in command
    assert 'no-new-privileges' in command
    assert not any('docker.sock' in x for x in command)
    assert '--privileged' not in command
    assert 'danger-full-access' in command  # Docker is the external sandbox.
    with pytest.raises(fleet().FleetError, match='ROOT_AGENT_DENIED'):
        fleet().container_argv(tmp_path, 'hello', binary, auth, 'sha256:'+'a'*64, 0, 0, 'pf83-test')


def test_sandbox_failure_is_blocked_even_when_model_process_exits_zero():
    assert fleet().execution_result(fleet().Probe('PASS', 0, 1, 'bwrap: No permissions to create a new namespace'))['code'] == 'ACL_DENIED'


def test_verified_gh_install_is_user_local_idempotent_and_reversible(tmp_path):
    import io
    import tarfile
    from app.services import fleet_gh
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        member = tarfile.TarInfo('gh_2.105.0_linux_amd64/bin/gh')
        data = b'#!/bin/sh\necho gh-test\n'
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    data = stream.getvalue()
    expected = fleet().digest(data)
    result = fleet_gh.install_archive(data, expected, tmp_path)
    assert result['changed']
    assert not fleet_gh.install_archive(data, expected, tmp_path)['changed']
    assert (tmp_path/'.local/bin/gh').is_symlink()
    assert fleet_gh.rollback(tmp_path)['changed']
    assert not (tmp_path/'.local/bin/gh').exists()
    with pytest.raises(fleet().FleetError, match='CHECKSUM_MISMATCH'):
        fleet_gh.install_archive(data, '0'*64, tmp_path)
