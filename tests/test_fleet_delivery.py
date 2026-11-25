"""Delivery fixtures use disposable local bare remotes only."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.fleet_delivery import FleetDelivery, DeliveryError
from app.services.fleet_permissions import Probe
from app.services.git_workspace import GitWorkspaceService, noninteractive_git_env


def git(cwd, *args):
    return subprocess.check_output(['git', *args], cwd=cwd, text=True).strip()


@pytest.fixture
def shipment(tmp_path):
    remote = tmp_path / 'remote.git'
    remote.mkdir()
    git(remote, 'init', '--bare', '--initial-branch=main')
    repo = tmp_path / 'repo'
    git(tmp_path, 'clone', str(remote), str(repo))
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    git(repo, 'config', 'user.name', 'Fixture')
    (repo / 'PROJECT.yaml').write_text('schema_version: 1\n')
    git(repo, 'add', 'PROJECT.yaml')
    git(repo, 'commit', '-m', 'base')
    base = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'push', 'origin', 'main')
    branch = 'agent/codex/delivery-fixture'
    git(repo, 'checkout', '-b', branch)
    (repo / 'hello.py').write_text('print("OK")\n')
    git(repo, 'add', 'hello.py')
    git(repo, 'commit', '-m', 'code [skip ci]')
    sha = git(repo, 'rev-parse', 'HEAD')
    bundle = tmp_path / 'code.bundle'
    git(repo, 'bundle', 'create', '--version=2', str(bundle), branch, '^' + base)
    manifest = dict(schema_version=1, request_key='delivery-fixture-1', task_id='83-fixture',
                    source_node='dell-linux', repo='owner/project', origin_url=str(remote),
                    source_branch=branch, base_sha=base, commit_sha=sha,
                    remote_ref='refs/heads/' + branch, allowed_paths=['hello.py'],
                    bundle_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest())
    git(repo, 'checkout', 'main')
    calls = []
    def gh(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ['api', 'user']:
            return Probe('PASS', 0, 1, json.dumps({'login': 'fixture-owner'}))
        if argv[1:3] == ['api', 'repos/owner/project']:
            return Probe('PASS', 0, 1, json.dumps({'permissions': {'push': True}}))
        if argv[1:3] == ['pr', 'list']:
            created = any(c[1:3] == ['pr', 'create'] for c in calls)
            return Probe('PASS', 0, 1, json.dumps([{'url': 'https://github.com/owner/project/pull/1', 'headRefOid': sha, 'headRefName': branch, 'baseRefName': 'main', 'state': 'OPEN'}] if created else []))
        if argv[1:3] == ['pr', 'create']:
            return Probe('PASS', 0, 1, 'https://github.com/owner/project/pull/1\n')
        raise AssertionError(argv)
    delivery = FleetDelivery(repo, tmp_path / 'state', [[sys.executable, 'hello.py']], gh_runner=gh)
    return delivery, manifest, bundle, remote, calls


def test_delivery_push_readback_pr_and_deduplicated_resume(shipment):
    delivery, manifest, bundle, remote, calls = shipment
    result = delivery.deliver(manifest, bundle)
    assert result['code'] == 'PASS'
    assert result['hp_sha'] == manifest['commit_sha']
    assert git(remote, 'rev-parse', manifest['remote_ref']) == manifest['commit_sha']
    assert result['pr_url'].endswith('/pull/1')
    assert delivery.deliver(manifest, bundle)['code'] == 'PASS'
    assert len([c for c in calls if c[1:3] == ['pr', 'create']]) == 1
    assert (delivery.state / 'requests').stat().st_mode & 0o077 == 0


@pytest.mark.parametrize('field,value', [('commit_sha', '0'*40), ('base_sha', '1'*40), ('allowed_paths', ['other.py']), ('remote_ref', 'refs/heads/main'), ('bundle_sha256', '2'*64)])
def test_wrong_manifest_rejected_without_push(shipment, field, value):
    delivery, manifest, bundle, remote, calls = shipment
    manifest[field] = value
    with pytest.raises(DeliveryError):
        delivery.deliver(manifest, bundle)
    assert git(remote, 'for-each-ref', '--format=%(refname)', 'refs/heads/agent') == ''
    assert not any(c[1:3] == ['pr', 'create'] for c in calls)


def test_auth_failure_before_push(shipment):
    delivery, manifest, bundle, remote, _ = shipment
    delivery.gh_runner = lambda *a, **k: Probe('AUTH_REQUIRED', 1, 1, 'HTTP 401 secret-not-to-persist')
    result = delivery.deliver(manifest, bundle)
    assert result['code'] == 'AUTH_REQUIRED'
    assert git(remote, 'for-each-ref', '--format=%(refname)', 'refs/heads/agent') == ''
    assert 'secret-not-to-persist' not in ''.join(p.read_text() for p in delivery.state.rglob('*.json'))


def test_failed_check_blocks_push(shipment):
    delivery, manifest, bundle, remote, calls = shipment
    delivery.checks = [[sys.executable, '-c', 'raise SystemExit(3)']]
    result = delivery.deliver(manifest, bundle)
    assert result['code'] == 'CHECK_FAILED'
    assert git(remote, 'for-each-ref', '--format=%(refname)', 'refs/heads/agent') == ''


def test_fail_fast_ssh_always_strict():
    env = noninteractive_git_env({'GIT_SSH_COMMAND': 'ssh -oStrictHostKeyChecking=no'})
    assert 'StrictHostKeyChecking=yes' in env['GIT_SSH_COMMAND']
    assert 'StrictHostKeyChecking=no' not in env['GIT_SSH_COMMAND']


def test_unknown_push_is_reconciled_without_redispatch(shipment):
    from app.services.git_workspace import GitCommandError
    delivery, manifest, bundle, remote, calls = shipment
    pushes = []
    def lost(*args):
        pushes.append(args)
        raise GitCommandError('timeout')
    delivery.git.delivery_push = lost
    assert delivery.deliver(manifest, bundle)['code'] == 'EXECUTION_UNKNOWN'
    assert delivery.deliver(manifest, bundle)['code'] == 'EXECUTION_UNKNOWN'
    assert len(pushes) == 1


def test_push_lost_response_reconciles_exact_sha(shipment):
    from app.services.git_workspace import GitCommandError
    delivery, manifest, bundle, remote, calls = shipment
    original = delivery.git.delivery_push
    def landed(*args):
        original(*args)
        raise GitCommandError('lost transport response')
    delivery.git.delivery_push = landed
    assert delivery.deliver(manifest, bundle)['code'] == 'PASS'


def test_explicit_rejected_push_can_resume(shipment):
    delivery, manifest, bundle, remote, calls = shipment
    hook = remote / 'hooks' / 'pre-receive'
    hook.write_text('#!/bin/sh\necho rejected >&2\nexit 1\n')
    hook.chmod(0o700)
    assert delivery.deliver(manifest, bundle)['code'] == 'PROBE_FAILED'
    assert not any(c[1:3] == ['pr', 'create'] for c in calls)
    hook.unlink()
    assert delivery.deliver(manifest, bundle)['code'] == 'PASS'


def test_pr_unknown_never_creates_twice(shipment):
    delivery, manifest, bundle, remote, calls = shipment
    original = delivery.gh_runner
    creates = []
    def gh(argv, **kwargs):
        if argv[1:3] == ['pr', 'create']:
            creates.append(argv)
            return Probe('PROBE_TIMEOUT', None, 100, '')
        if argv[1:3] == ['pr', 'list']:
            return Probe('PASS', 0, 1, '[]')
        return original(argv, **kwargs)
    delivery.gh_runner = gh
    assert delivery.deliver(manifest, bundle)['code'] == 'EXECUTION_UNKNOWN'
    assert delivery.deliver(manifest, bundle)['code'] == 'EXECUTION_UNKNOWN'
    assert len(creates) == 1


def test_duplicate_request_with_changed_scope_rejected(shipment):
    delivery, manifest, bundle, remote, calls = shipment
    assert delivery.deliver(manifest, bundle)['code'] == 'PASS'
    manifest['task_id'] = 'different-task'
    with pytest.raises(DeliveryError, match='REQUEST_CONFLICT'):
        delivery.deliver(manifest, bundle)


def test_conflicting_remote_is_preserved(shipment):
    delivery, manifest, bundle, remote, calls = shipment
    git(remote, 'update-ref', manifest['remote_ref'], manifest['base_sha'])
    assert delivery.deliver(manifest, bundle)['code'] == 'DELIVERY_REMOTE_CONFLICT'
    assert git(remote, 'rev-parse', manifest['remote_ref']) == manifest['base_sha']


def test_wrong_effective_push_destination_rejected(shipment, tmp_path):
    delivery, manifest, bundle, remote, calls = shipment
    alternate = tmp_path / 'alternate.git'
    alternate.mkdir()
    git(alternate, 'init', '--bare')
    git(delivery.repo, 'remote', 'set-url', '--push', 'origin', str(alternate))
    with pytest.raises(DeliveryError, match='DESTINATION_MISMATCH'):
        delivery.deliver(manifest, bundle)
    assert git(alternate, 'for-each-ref') == ''


@pytest.mark.parametrize('filename,content', [('.env', 'anything'), ('keys/private.pem', 'anything'), ('hello.py', '-----BEGIN PRIVATE KEY-----\n'), ('link.py', None)])
def test_export_rejects_sensitive_or_nonregular_files(shipment, tmp_path, filename, content):
    delivery, manifest, bundle, remote, calls = shipment
    git(delivery.repo, 'checkout', '-b', 'agent/codex/rejected')
    path = delivery.repo / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    if content is None:
        path.symlink_to('/tmp/not-owned')
    else:
        path.write_text(content)
    git(delivery.repo, 'add', filename)
    git(delivery.repo, 'commit', '-m', 'fixture')
    sha = git(delivery.repo, 'rev-parse', 'HEAD')
    from app.services.git_workspace import GitSafetyError
    with pytest.raises(GitSafetyError):
        delivery.git.delivery_export(delivery.repo, 'agent/codex/rejected', manifest['base_sha'], sha, [filename], tmp_path / 'reject.bundle')
    assert not (tmp_path / 'reject.bundle').exists()


def test_export_exact_code_delta(shipment, tmp_path):
    delivery, manifest, bundle, remote, calls = shipment
    exported = tmp_path / 'export.bundle'
    sha = delivery.git.delivery_export(delivery.repo, manifest['source_branch'], manifest['base_sha'], manifest['commit_sha'], manifest['allowed_paths'], exported)
    assert sha == hashlib.sha256(exported.read_bytes()).hexdigest()
    assert exported.stat().st_mode & 0o077 == 0
    assert git(delivery.repo, 'bundle', 'list-heads', str(exported)) == manifest['commit_sha'] + ' refs/heads/' + manifest['source_branch']


def test_service_path_only_prefixes_owned_safe_bin(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    path = tmp_path / '.local' / 'bin'
    path.mkdir(parents=True)
    path.chmod(0o755)
    path.parent.chmod(0o755)
    assert noninteractive_git_env({'PATH': '/usr/bin'})['PATH'] == str(path) + ':/usr/bin'
    path.chmod(0o777)
    assert noninteractive_git_env({'PATH': '/usr/bin'})['PATH'] == '/usr/bin'


def test_delivery_lease_blocks_overlapping_ref(shipment):
    import fcntl
    import os
    from app.services.fleet_delivery import fingerprint
    delivery, manifest, bundle, remote, calls = shipment
    key = fingerprint([manifest['origin_url'], manifest['remote_ref']])
    fd = os.open(delivery.state / 'requests' / ('ref-' + key + '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert delivery.deliver(manifest, bundle)['code'] == 'DELIVERY_IN_PROGRESS'
        assert git(remote, 'for-each-ref', 'refs/heads/agent') == ''
    finally:
        os.close(fd)


@pytest.mark.parametrize('code', ['GIT_WRITE_DENIED', 'OFFLINE', 'PROBE_TIMEOUT'])
def test_auth_failures_are_distinct(shipment, code):
    delivery, manifest, bundle, remote, calls = shipment
    delivery.gh_runner = lambda *a, **k: Probe(code, 1, 1, '')
    assert delivery.deliver(manifest, bundle)['code'] == code
    assert git(remote, 'for-each-ref', 'refs/heads/agent') == ''


def test_normal_delivery_respects_existing_push_hook(shipment):
    delivery, manifest, bundle, remote, calls = shipment
    hook = delivery.repo / '.git' / 'hooks' / 'pre-push'
    hook.write_text('#!/bin/sh\nexit 1\n')
    hook.chmod(0o700)
    assert delivery.deliver(manifest, bundle)['code'] != 'PASS'
    assert git(remote, 'for-each-ref', 'refs/heads/agent') == ''


def test_git_command_timeout_kills_child_process_group(tmp_path):
    import time
    from app.services.git_workspace import GitCommandError
    service = GitWorkspaceService(tmp_path, timeout=0.1)
    script = 'import subprocess,sys,time,pathlib; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(4)"]); pathlib.Path("child.pid").write_text(str(p.pid)); time.sleep(10)'
    started = time.monotonic()
    with pytest.raises(GitCommandError):
        service._run([sys.executable, '-c', script], tmp_path)
    assert time.monotonic() - started < 2.0
    if sys.platform == 'linux':
        import os
        import signal
        child_pid = int((tmp_path / 'child.pid').read_text())
        status = Path(f'/proc/{child_pid}/stat')
        alive = status.exists() and status.read_text().split()[2] != 'Z'
        if alive:  # Clean only the disposable child this test created.
            os.kill(child_pid, signal.SIGKILL)
        assert not alive


def test_advertised_ref_race_cannot_fast_forward_other_work(shipment):
    delivery, manifest, bundle, remote, calls = shipment
    original = delivery.git.delivery_push
    def raced(*args):
        git(remote, 'update-ref', manifest['remote_ref'], manifest['base_sha'])
        return original(*args)
    delivery.git.delivery_push = raced
    assert delivery.deliver(manifest, bundle)['code'] != 'PASS'
    assert git(remote, 'rev-parse', manifest['remote_ref']) == manifest['base_sha']
