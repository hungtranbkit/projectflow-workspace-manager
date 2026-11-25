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
    ('HTTP 403: API rate limit exceeded', 'RATE_LIMITED'),
    ('HTTP 429: Too many requests', 'RATE_LIMITED'),
    ('Could not resolve host: github.com', 'OFFLINE'),
    ('Permission denied (publickey)', 'AUTH_REQUIRED'),
    ('Permission denied: workspace', 'ACL_DENIED'),
    ('STATUS_PROBE_TIMEOUT', 'STATUS_PROBE_TIMEOUT'),
    ('NODE_UNREACHABLE', 'OFFLINE'),
    ('sudo: a password is required', 'SUDO_REQUIRED'),
    ('unexpected argument --x', 'CLI_UNSUPPORTED'),
    ('trust this folder', 'TRUST_REQUIRED'),
    ('To get started with GitHub CLI, please run: gh auth login', 'AUTH_REQUIRED'),
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


def test_canary_cleanup_respects_existing_pre_push_policy(git_repo):
    from app.services.git_workspace import GitWorkspaceService
    root, repo = git_repo
    remote = root / 'remote.git'
    subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin', str(remote)], check=True)
    hooks = root / 'policy-hooks'
    hooks.mkdir()
    hook = hooks / 'pre-push'
    hook.write_text('#!/bin/sh\nwhile read -r local_ref local_oid remote_ref remote_oid; do\n'
                    '  [ "$local_ref" != "(delete)" ] || exit 17\ndone\n')
    hook.chmod(0o700)
    subprocess.run(['git', '-C', str(repo), 'config', 'core.hooksPath', str(hooks)], check=True)
    result = GitWorkspaceService(root).push_canary(repo)
    assert result['cleanup'] == 'NOT_RUN'
    assert result['code'] == 'BLOCKED'
    assert Path(result['worktree']).is_dir()
    assert subprocess.check_output(['git', '--git-dir', str(remote), 'rev-parse', result['branch']], text=True).strip() == result['pushed_sha']


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


def test_unexpected_errors_are_redacted(monkeypatch, capsys):
    monkeypatch.setattr(fleet().FleetProfile, 'audit', lambda self: (_ for _ in ()).throw(RuntimeError('ghp_secret_message')))
    assert fleet().main(['audit']) == 1
    assert 'ghp_secret' not in capsys.readouterr().out


@pytest.mark.parametrize('failure', ['AUTH_REQUIRED', 'GIT_WRITE_DENIED', 'OFFLINE', 'DISK_LOW', 'ACL_DENIED'])
def test_preflight_failure_never_dispatches(monkeypatch, tmp_path, failure):
    manager = fleet().FleetProfile(tmp_path)
    monkeypatch.setattr(manager, 'check', lambda: {'code': 'PASS'})
    checks = {k: {'code': 'PASS'} for k in ['workspace','repository','gh_auth','gh_identity','github_push','git_helper','disk','codex_auth','codex_version']}
    checks['gh_identity'] = {'code': failure}
    monkeypatch.setattr(manager, 'audit', lambda: {'checks': checks})
    monkeypatch.setattr(fleet(), 'probe', lambda *a, **kw: fleet().Probe('PASS',0,0,'--sandbox --config --cd'))
    assert manager.launch('codex', 'must not run')['execution'] == 'NOT_DISPATCHED'
    assert not manager.path.with_suffix('.checkpoint.json').exists()


def test_docker_refuses_managed_policy(monkeypatch, tmp_path):
    manager = fleet().FleetProfile(tmp_path)
    monkeypatch.setattr(manager, 'check', lambda: {'code':'PASS'})
    checks = {k: {'code':'PASS'} for k in ['workspace','repository','gh_auth','gh_identity','github_push','git_helper','disk','codex_auth','codex_version','docker']}
    checks['managed_policy'] = {'present':['/etc/codex/requirements.toml']}
    monkeypatch.setattr(manager, 'audit', lambda: {'checks':checks})
    monkeypatch.setattr(fleet(), 'probe', lambda *a, **kw: fleet().Probe('PASS',0,0,'--sandbox --config --cd --ephemeral --skip-git-repo-check'))
    assert manager.preflight('codex', 'docker', 'sha256:'+'a'*64)['failures']['managed_policy'] == 'TRUST_REQUIRED'


def test_windows_core_identity_rejects_admin_or_wrong_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(fleet().platform, 'system', lambda:'Windows')
    monkeypatch.setattr(fleet(), 'probe', lambda *a, **kw: fleet().Probe('ACL_DENIED',1,1))
    with pytest.raises(fleet().FleetError, match='ACL_DENIED'):
        fleet().FleetProfile(tmp_path).identity()


def test_external_config_directory_is_not_trusted(git_repo, tmp_path):
    root, repo = git_repo
    from app.services.git_workspace import GitWorkspaceService
    _, wt, _ = GitWorkspaceService(root).create_agent(repo, 'codex', 'config-link')
    external = tmp_path/'external-config'
    external.mkdir()
    (external/'settings.json').write_text('{}')
    (wt/'.claude').symlink_to(external, target_is_directory=True)
    with pytest.raises(fleet().FleetError, match='TRUST_REQUIRED'):
        fleet().FleetProfile(wt, tmp_path/'state').identity()


def test_gh_interrupted_link_creation_can_retry_or_rollback(tmp_path, monkeypatch):
    import io
    import tarfile
    from app.services import fleet_gh
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        member = tarfile.TarInfo('gh_2.105.0_linux_amd64/bin/gh')
        member.size = 4
        archive.addfile(member, io.BytesIO(b'test'))
    data = stream.getvalue()
    original = Path.symlink_to
    monkeypatch.setattr(Path, 'symlink_to', lambda *a, **kw: (_ for _ in ()).throw(OSError('interrupted')))
    with pytest.raises(OSError):
        fleet_gh.install_archive(data, fleet().digest(data), tmp_path)
    monkeypatch.setattr(Path, 'symlink_to', original)
    assert fleet_gh.install_archive(data, fleet().digest(data), tmp_path)['changed']
    (tmp_path/'.local/bin/gh').unlink()
    assert fleet_gh.rollback(tmp_path)['changed']


def test_canary_preserves_concurrently_advanced_remote(git_repo, monkeypatch):
    from app.services.git_workspace import GitWorkspaceService
    root, repo = git_repo
    remote = root/'remote.git'
    subprocess.run(['git','init','--bare',str(remote)],check=True,capture_output=True)
    subprocess.run(['git','-C',str(repo),'remote','add','origin',str(remote)],check=True)
    service = GitWorkspaceService(root)
    original = service.git
    advanced = {}
    def race(path, *args, **kwargs):
        if '--delete' in args:
            branch = args[-1]
            old = subprocess.check_output(['git','--git-dir',str(remote),'rev-parse',branch],text=True).strip()
            tree = subprocess.check_output(['git','--git-dir',str(remote),'rev-parse',old+'^{tree}'],text=True).strip()
            sha = subprocess.check_output(['git','-C',str(repo),'commit-tree',tree,'-p',old,'-m','Concurrent owned work'],text=True).strip()
            subprocess.run(['git','-C',str(repo),'push','origin',sha+':refs/heads/'+branch],check=True,capture_output=True)
            advanced.update(branch=branch,sha=sha)
        return original(path,*args,**kwargs)
    monkeypatch.setattr(service,'git',race)
    result = service.push_canary(repo)
    assert result['cleanup'] != 'PASS'
    assert subprocess.check_output(['git','--git-dir',str(remote),'rev-parse',advanced['branch']],text=True).strip()==advanced['sha']


def test_rejected_canary_push_preserves_local_checkpoint(git_repo):
    from app.services.git_workspace import GitWorkspaceService
    root, repo = git_repo
    remote = root/'remote.git'
    subprocess.run(['git','init','--bare',str(remote)],check=True,capture_output=True)
    subprocess.run(['git','-C',str(repo),'remote','add','origin',str(remote)],check=True)
    hook = remote/'hooks/pre-receive'
    hook.write_text('#!/bin/sh\nexit 1\n')
    hook.chmod(0o700)
    result = GitWorkspaceService(root).push_canary(repo)
    assert result['code'] == 'BLOCKED'
    assert result['cleanup'] == 'NOT_RUN'
    assert Path(result['worktree']).is_dir()
    assert subprocess.check_output(['git','-C',result['worktree'],'rev-parse','HEAD'],text=True).strip() == result['pushed_sha']
    assert subprocess.check_output(['git','ls-remote',str(remote)]) == b''


def test_unsupported_cli_is_blocked_before_dispatch(monkeypatch, tmp_path):
    manager = fleet().FleetProfile(tmp_path)
    monkeypatch.setattr(manager,'check',lambda: {'code':'PASS'})
    checks = {k:{'code':'PASS'} for k in ['workspace','repository','gh_auth','gh_identity','github_push','git_helper','disk','codex_auth','codex_version']}
    monkeypatch.setattr(manager,'audit',lambda: {'checks':checks})
    monkeypatch.setattr(fleet(),'probe',lambda *a,**kw: fleet().Probe('PASS',0,0,'old incompatible CLI'))
    assert manager.launch('codex','must not run')['failures']['cli_options'] == 'CLI_UNSUPPORTED'
    assert not manager.path.with_suffix('.checkpoint.json').exists()


def test_canary_refuses_different_push_destination(git_repo):
    from app.services.git_workspace import GitWorkspaceService, GitSafetyError
    root, repo = git_repo
    fetch_remote, push_remote = root/'fetch.git', root/'push.git'
    for remote in (fetch_remote,push_remote):
        subprocess.run(['git','init','--bare',str(remote)],check=True,capture_output=True)
    subprocess.run(['git','-C',str(repo),'remote','add','origin',str(fetch_remote)],check=True)
    subprocess.run(['git','-C',str(repo),'config','remote.origin.pushurl',str(push_remote)],check=True)
    before = subprocess.check_output(['git','-C',str(repo),'branch'])
    with pytest.raises(GitSafetyError,match='push destination'):
        GitWorkspaceService(root).push_canary(repo)
    assert subprocess.check_output(['git','-C',str(repo),'branch']) == before
