"""Owner-only preparation. Mutations use disposable roots and an injected runner."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest


def module():
    assert importlib.util.find_spec('app.services.fleet_admin') is not None
    from app.services import fleet_admin
    return fleet_admin


class Runner:
    def __init__(self):
        self.calls = []
        self.installed = False
        self.active = False
        self.enabled = False
        self.fail_validation = False

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:2] == ['/usr/bin/dpkg-query', '-W']:
            return subprocess.CompletedProcess(argv, 0 if self.installed else 1, 'install ok installed' if self.installed else '', '')
        if argv[:2] == ['/usr/bin/systemctl', 'is-active']:
            return subprocess.CompletedProcess(argv, 0 if self.active else 3, 'active' if self.active else 'inactive', '')
        if argv[:2] == ['/usr/bin/systemctl', 'is-enabled']:
            return subprocess.CompletedProcess(argv, 0 if self.enabled else 1, 'enabled' if self.enabled else 'disabled', '')
        if argv[0] == '/usr/sbin/visudo' and self.fail_validation:
            return subprocess.CompletedProcess(argv, 1, '', 'invalid')
        if argv[0] == '/usr/bin/apt-get' and 'install' in argv:
            self.installed = True
        if argv[:2] == ['/usr/bin/systemctl', 'enable']:
            self.enabled = True
        if argv[:2] == ['/usr/bin/systemctl', 'start']:
            self.active = True
        if argv[:2] == ['/usr/bin/systemctl', 'stop']:
            self.active = False
        if argv[:2] == ['/usr/bin/systemctl', 'disable']:
            self.enabled = False
        return subprocess.CompletedProcess(argv, 0, '', '')


@pytest.fixture
def admin(tmp_path):
    m = module()
    for directory in ('etc/ssh', 'etc/sudoers.d', 'usr/local/sbin', 'usr/bin', 'usr/sbin', 'var/lib'):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    (tmp_path / 'etc/os-release').write_text('ID=debian\n')
    (tmp_path / 'etc/ssh/sshd_config').write_text('# Existing settings\nPermitRootLogin no\n')
    for binary in ('usr/bin/apt-get', 'usr/bin/dpkg-query', 'usr/bin/systemctl', 'usr/sbin/sshd', 'usr/sbin/visudo'):
        (tmp_path / binary).write_text('fixture')
        (tmp_path / binary).chmod(0o755)
    for directory in tmp_path.rglob('*'):
        if directory.is_dir():
            directory.chmod(0o755)
        elif not os.access(directory, os.X_OK):
            directory.chmod(0o644)
    runner = Runner()
    return m.AdminBootstrap(root=tmp_path, runner=runner, owner_uid=os.getuid()), runner


def test_audit_and_dry_run_never_mutate(admin):
    engine, runner = admin
    before = set(engine.root.rglob('*'))
    audit = engine.audit()
    assert audit['status'] == 'OWNER_BOOTSTRAP_REQUIRED'
    assert engine.plan()['operations'] == ['apt-update', 'install-openssh-server', 'sshd-config-test', 'enable-ssh', 'start-ssh']
    assert set(engine.root.rglob('*')) == before
    assert all(x[0] not in ('/usr/bin/apt-get', '/usr/sbin/visudo') for x in runner.calls)


def test_bootstrap_repeat_and_rollback_preserve_config_and_package(admin):
    engine, runner = admin
    config = engine.root / 'etc/ssh/sshd_config'
    original = config.read_bytes()
    assert engine.bootstrap()['status'] == 'APPLIED'
    count = len([x for x in runner.calls if x[0] == '/usr/bin/apt-get'])
    assert engine.bootstrap()['status'] == 'UNCHANGED'
    assert len([x for x in runner.calls if x[0] == '/usr/bin/apt-get']) == count
    assert engine.rollback()['status'] == 'ROLLED_BACK'
    assert engine.rollback()['status'] == 'UNCHANGED'
    assert not runner.enabled and not runner.active and runner.installed
    assert config.read_bytes() == original
    assert not any('remove' in x or 'purge' in x for x in runner.calls)


def test_rollback_refuses_config_drift_before_stopping_service(admin):
    engine, runner = admin
    engine.bootstrap()
    (engine.root / 'etc/ssh/sshd_config').write_text('Changed by owner\n')
    before = len(runner.calls)
    with pytest.raises(module().AdminError, match='DRIFT'):
        engine.rollback()
    assert not any('stop' in x or 'disable' in x for x in runner.calls[before:])


def test_preexisting_active_service_never_restarted_or_stopped(admin):
    engine, runner = admin
    runner.installed = runner.active = runner.enabled = True
    engine.bootstrap()
    engine.rollback()
    assert runner.active and runner.enabled
    assert not any(x[1] in ('restart', 'start', 'stop', 'enable', 'disable') for x in runner.calls if x[0] == '/usr/bin/systemctl')


def test_scoped_install_requires_explicit_valid_account_and_exact_scope(admin):
    engine, runner = admin
    with pytest.raises(module().AdminError, match='SCOPE'):
        engine.install_scope('alice', 1001, ['restart'])
    with pytest.raises(module().AdminError, match='ACCOUNT'):
        engine.install_scope('alice;id', 1001, ['ssh-status'])
    assert engine.install_scope('alice', 1001, ['ssh-status', 'ssh-config-test'])['status'] == 'APPLIED'
    sudoers = (engine.root / 'etc/sudoers.d/projectflow-owned-admin').read_text()
    assert 'ALL=(root) NOPASSWD:' in sudoers and '*' not in sudoers
    assert 'ssh-status' in sudoers and 'ssh-config-test' in sudoers
    assert any(x[0] == '/usr/sbin/visudo' and x[1] == '-cf' for x in runner.calls)
    assert engine.install_scope('alice', 1001, ['ssh-status', 'ssh-config-test'])['status'] == 'UNCHANGED'
    assert engine.rollback_scope()['status'] == 'ROLLED_BACK'
    assert engine.rollback_scope()['status'] == 'UNCHANGED'


def test_invalid_sudoers_never_installed(admin):
    engine, runner = admin
    runner.fail_validation = True
    with pytest.raises(module().AdminError, match='COMMAND_FAILED'):
        engine.install_scope('alice', 1001, ['ssh-status'])
    assert not (engine.root / 'etc/sudoers.d/projectflow-owned-admin').exists()


@pytest.mark.parametrize('operation,args', [('restart', []), ('ssh-status', [';id']), ('ssh-status;id', [])])
def test_wrapper_rejects_unknown_and_argument_injection(admin, operation, args):
    engine, runner = admin
    engine.install_scope('alice', 1001, ['ssh-status'])
    before = len(runner.calls)
    with pytest.raises(module().AdminError):
        engine.scoped_operation(operation, args, caller_uid=1001)
    assert len(runner.calls) == before


def test_wrapper_requires_account_and_root_owned_unwritable_paths(admin):
    engine, runner = admin
    engine.install_scope('alice', 1001, ['ssh-status'])
    with pytest.raises(module().AdminError, match='CALLER'):
        engine.scoped_operation('ssh-status', [], caller_uid=999)
    assert engine.scoped_operation('ssh-status', [], caller_uid=1001)['exit_code'] == 3
    (engine.root / 'usr/bin/systemctl').chmod(0o777)
    with pytest.raises(module().AdminError, match='UNTRUSTED_PATH'):
        engine.scoped_operation('ssh-status', [], caller_uid=1001)


def test_symlink_and_writable_policy_rejected(admin):
    engine, runner = admin
    engine.install_scope('alice', 1001, ['ssh-status'])
    policy = engine.root / 'etc/projectflow/owned-admin.json'
    policy.chmod(0o666)
    with pytest.raises(module().AdminError, match='UNTRUSTED_PATH'):
        engine.scoped_operation('ssh-status', [], caller_uid=1001)
    policy.unlink()
    policy.symlink_to(engine.root / 'etc/os-release')
    with pytest.raises(module().AdminError, match='UNTRUSTED_PATH'):
        engine.scoped_operation('ssh-status', [], caller_uid=1001)


def test_scope_rollback_leaves_changed_owned_file(admin):
    engine, _ = admin
    engine.install_scope('alice', 1001, ['ssh-status'])
    wrapper = engine.root / 'usr/local/sbin/projectflow-owned-admin'
    wrapper.write_text('Owner replacement\n')
    with pytest.raises(module().AdminError, match='DRIFT'):
        engine.rollback_scope()
    assert wrapper.read_text() == 'Owner replacement\n'


def test_cli_privileged_action_rejects_nonroot_before_mutation(monkeypatch):
    m = module()
    monkeypatch.setattr(m.os, 'geteuid', lambda: 1000)
    with pytest.raises(m.AdminError, match='OWNER_BOOTSTRAP_REQUIRED'):
        m.require_admin(Path(__file__))


def test_rollback_refuses_changed_service_state(admin):
    engine, runner = admin
    engine.bootstrap()
    runner.active = False  # Another administrator changed the service after bootstrap.
    before = len(runner.calls)
    with pytest.raises(module().AdminError, match='DRIFT'):
        engine.rollback()
    assert not any('stop' in x or 'disable' in x for x in runner.calls[before:])


def test_standard_os_release_link_is_auditable_but_arbitrary_link_is_not(admin):
    engine, _ = admin
    target = engine.root / 'usr/lib/os-release'
    target.parent.mkdir(mode=0o755)
    target.write_text('ID=debian\n')
    target.chmod(0o644)
    link = engine.root / 'etc/os-release'
    link.unlink()
    link.symlink_to('../usr/lib/os-release')
    assert engine.audit()['status'] == 'OWNER_BOOTSTRAP_REQUIRED'
    link.unlink()
    link.symlink_to('../etc/ssh/sshd_config')
    with pytest.raises(module().AdminError, match='UNTRUSTED_PATH'):
        engine.audit()


def test_bounded_runner_discards_inherited_environment_and_input(monkeypatch):
    m = module()
    captured = {}
    def fake(argv, **kwargs):
        captured.update(kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
    monkeypatch.setenv('LD_PRELOAD', '/tmp/untrusted.so')
    monkeypatch.setattr(m.subprocess, 'run', fake)
    with pytest.raises(m.AdminError, match='STATUS_PROBE_TIMEOUT'):
        m.run_fixed(['/usr/bin/systemctl', 'is-active', 'ssh.service'])
    assert 'LD_PRELOAD' not in captured['env']
    assert captured['stdin'] == subprocess.DEVNULL and captured['timeout'] == 300


def test_partial_bootstrap_has_checkpoint_and_is_not_replayed(admin):
    engine, runner = admin
    original_runner = engine.runner
    def failed_install(argv):
        if argv[0] == '/usr/bin/apt-get' and 'install' in argv:
            return subprocess.CompletedProcess(argv, 100, '', 'do not expose this secret')
        return original_runner(argv)
    engine.runner = failed_install
    with pytest.raises(module().AdminError, match='COMMAND_FAILED'):
        engine.bootstrap()
    receipt = engine.load('ssh')
    assert receipt['pending_operation'] == 'install-openssh-server'
    assert receipt['completed'] == ['apt-update']
    assert 'secret' not in json.dumps(receipt)
    with pytest.raises(module().AdminError, match='CHECKPOINT_EXISTS'):
        engine.bootstrap()


def test_repeat_bootstrap_does_not_claim_success_after_service_drift(admin):
    engine, runner = admin
    engine.bootstrap()
    runner.active = False
    with pytest.raises(module().AdminError, match='DRIFT'):
        engine.bootstrap()
