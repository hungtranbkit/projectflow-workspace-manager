#!/usr/bin/python3 -I
"""Versioned owner-run Debian SSH prerequisite and optional read-only admin scope.

This module is also the standalone owner artifact. It never invokes sudo. The
normal application can audit/prepare it; only an administrator can install and
execute a reviewed root-owned copy. No task launcher calls privileged methods.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import syslog

VERSION = 2
STATE = '/var/lib/projectflow/fleet-admin-v2'
POLICY = '/etc/projectflow/owned-admin.json'
WRAPPER = '/usr/local/sbin/projectflow-owned-admin'
SUDOERS = '/etc/sudoers.d/projectflow-owned-admin'
OPERATIONS = {
    'ssh-status': ['/usr/bin/systemctl', 'is-active', 'ssh.service'],
    'ssh-config-test': ['/usr/sbin/sshd', '-t'],
}
ENV = {'PATH': '/usr/sbin:/usr/bin', 'LANG': 'C', 'LC_ALL': 'C',
       'DEBIAN_FRONTEND': 'noninteractive'}


class AdminError(RuntimeError):
    """Safe error code, deliberately excludes command output and file contents."""


def run_fixed(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, env=ENV, cwd='/', timeout=300, check=False)
    except subprocess.TimeoutExpired:
        raise AdminError('STATUS_PROBE_TIMEOUT') from None
    except OSError:
        raise AdminError('EXECUTABLE_UNAVAILABLE') from None


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def trusted_path(path: Path, *, root: Path = Path('/'), uid: int = 0) -> None:
    """Every existing component must have the expected owner and no write grants.

    root is the filesystem boundary (only '/' in CLI); injectable for isolated
    tests. Symlinks are rejected, including final files and parent components.
    """
    try:
        path.relative_to(root)
    except ValueError:
        raise AdminError('UNTRUSTED_PATH') from None
    for part in [path, *path.parents]:
        if part == root.parent and root != Path('/'):
            break
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
            raise AdminError('UNTRUSTED_PATH')
        if part == root:
            break


def require_admin(source: Path) -> None:
    if os.geteuid() != 0:
        raise AdminError('OWNER_BOOTSTRAP_REQUIRED')
    trusted_path(source.absolute())
    trusted_path(Path('/usr/bin/python3').resolve())


class AdminBootstrap:
    def __init__(self, *, root: Path = Path('/'), runner=run_fixed, owner_uid: int = 0):
        self.root = root
        self.runner = runner
        self.owner_uid = owner_uid

    def path(self, path: str) -> Path:
        return self.root / path.lstrip('/')

    def trusted(self, path: str) -> Path:
        resolved = self.path(path)
        trusted_path(resolved, root=self.root, uid=self.owner_uid)
        return resolved

    def command(self, argv: list[str], *, check: bool = False):
        binary = self.trusted(argv[0])
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise AdminError('EXECUTABLE_UNAVAILABLE')
        result = self.runner(argv)
        if check and result.returncode != 0:
            raise AdminError('COMMAND_FAILED')
        return result

    def ensure_directory(self, directory: Path, mode: int = 0o755) -> None:
        trusted_path(directory, root=self.root, uid=self.owner_uid)
        if not directory.exists():
            self.ensure_directory(directory.parent)
            directory.mkdir(mode=mode)
        trusted_path(directory, root=self.root, uid=self.owner_uid)

    def private_dir(self) -> Path:
        directory = self.trusted(STATE)
        self.ensure_directory(directory, 0o700)
        if directory.stat().st_mode & 0o077:
            raise AdminError('UNTRUSTED_STATE')
        return directory

    def write(self, target: Path, data: bytes, mode: int) -> None:
        trusted_path(target, root=self.root, uid=self.owner_uid)
        self.ensure_directory(target.parent)
        # Exclusive creation never replaces somebody else's files or follows links.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def save(self, name: str, record: dict) -> None:
        directory = self.private_dir()
        target = self.trusted(f'{STATE}/{name}.json')
        pending = directory / f'{name}.pending'
        self.write(pending, (json.dumps(record, sort_keys=True) + '\n').encode(), 0o600)
        os.replace(pending, target)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def load(self, name: str) -> dict | None:
        target = self.trusted(f'{STATE}/{name}.json')
        return json.loads(target.read_text()) if target.exists() else None

    def ssh_files(self) -> dict[str, str]:
        directory = self.trusted('/etc/ssh')
        result = {}
        if directory.exists():
            for path in sorted(directory.rglob('*')):
                trusted_path(path, root=self.root, uid=self.owner_uid)
                if path.is_file():
                    result[str(path.relative_to(self.root))] = digest(path)
        return result

    def audit(self) -> dict:
        release = self.path('/etc/os-release')
        if release.is_symlink():
            # Distribution-standard metadata link only; not a general symlink exception.
            self.trusted('/etc')
            if release.lstat().st_uid != self.owner_uid or release.resolve() != self.path('/usr/lib/os-release'):
                raise AdminError('UNTRUSTED_PATH')
            release = self.trusted('/usr/lib/os-release')
        else:
            release = self.trusted('/etc/os-release')
        os_release = release.read_text()
        if not re.search(r'^(ID|ID_LIKE)=["\']?(debian|ubuntu)([ "\']|$)', os_release, re.M):
            raise AdminError('UNSUPPORTED_PLATFORM')
        package = self.command(['/usr/bin/dpkg-query', '-W', '-f=${Status}', 'openssh-server'])
        installed = package.returncode == 0 and package.stdout.strip() == 'install ok installed'
        active = self.command(['/usr/bin/systemctl', 'is-active', 'ssh.service'])
        enabled = self.command(['/usr/bin/systemctl', 'is-enabled', 'ssh.service'])
        return {'version': VERSION, 'status': 'READY' if installed and active.returncode == 0 else 'OWNER_BOOTSTRAP_REQUIRED',
                'package_installed': installed, 'active': active.returncode == 0,
                'enabled': enabled.returncode == 0, 'enabled_state': enabled.stdout.strip(),
                'effective_uid': os.geteuid(), 'ssh_verified': False}

    def plan(self) -> dict:
        before = self.audit()
        operations = ([] if before['package_installed'] else ['apt-update', 'install-openssh-server'])
        operations += ['sshd-config-test']
        if not before['enabled']:
            operations += ['enable-ssh']
        if not before['active']:
            operations += ['start-ssh']
        return {'version': VERSION, 'before': before, 'operations': operations,
                'preserves': ['existing SSH configuration and keys', 'authorized_keys', 'firewall'],
                'rollback': 'Restore only recorded service enable/active state after drift check; keep package and host keys.'}

    def bootstrap(self) -> dict:
        previous = self.load('ssh')
        if previous:
            if previous['state'] == 'APPLIED' and previous['after_files'] == self.ssh_files():
                current = self.audit()
                if any(current[key] != previous['after'][key] for key in ('package_installed', 'active', 'enabled')):
                    raise AdminError('SERVICE_DRIFT_OWNER_REVIEW_REQUIRED')
                return {'status': 'UNCHANGED', 'receipt': f'{STATE}/ssh.json'}
            raise AdminError('CHECKPOINT_EXISTS_REVIEW_REQUIRED')
        before = self.audit()
        if before['enabled_state'] not in ('enabled', 'disabled', '', 'not-found'):
            raise AdminError('SERVICE_STATE_REQUIRES_OWNER_REVIEW')
        original = self.ssh_files()
        backup = self.private_dir() / 'ssh-before'
        backup.mkdir(mode=0o700)
        for relative in original:
            target = backup / relative
            self.write(target, self.path('/' + relative).read_bytes(), 0o600)
        record = {'version': VERSION, 'state': 'STARTED', 'before': before,
                  'before_files': original, 'after_files': {}, 'completed': []}
        self.save('ssh', record)
        commands = []
        if not before['package_installed']:
            commands += [(['/usr/bin/apt-get', 'update'], 'apt-update'),
                         (['/usr/bin/apt-get', '-o', 'Dpkg::Options::=--force-confold',
                           'install', '-y', 'openssh-server'], 'install-openssh-server')]
        commands += [(['/usr/sbin/sshd', '-t'], 'sshd-config-test')]
        if not before['enabled']:
            commands += [(['/usr/bin/systemctl', 'enable', 'ssh.service'], 'enable-ssh')]
        if not before['active']:
            commands += [(['/usr/bin/systemctl', 'start', 'ssh.service'], 'start-ssh')]
        for argv, operation in commands:
            record['pending_operation'] = operation
            self.save('ssh', record)
            self.command(argv, check=True)
            record['completed'].append(operation)
            record['pending_operation'] = None
            self.save('ssh', record)
        after_files = self.ssh_files()
        if any(after_files.get(path) != value for path, value in original.items()):
            raise AdminError('EXISTING_CONFIG_DRIFT_REVIEW_BACKUP')
        after = self.audit()
        if not after['package_installed'] or not after['active'] or not after['enabled']:
            raise AdminError('POSTCHECK_FAILED')
        record.update(state='APPLIED', after_files=after_files, after=after)
        self.save('ssh', record)
        return {'status': 'APPLIED', 'receipt': f'{STATE}/ssh.json', 'ssh_verified': False}

    def rollback(self) -> dict:
        record = self.load('ssh')
        if record is None or record['state'] == 'ROLLED_BACK':
            return {'status': 'UNCHANGED'}
        if record['state'] != 'APPLIED':
            raise AdminError('PARTIAL_BOOTSTRAP_OWNER_REVIEW_REQUIRED')
        if record['after_files'] != self.ssh_files():
            raise AdminError('DRIFT_ROLLBACK_REFUSED')
        current = self.audit()
        if any(current[key] != record['after'][key] for key in ('package_installed', 'active', 'enabled')):
            raise AdminError('SERVICE_DRIFT_ROLLBACK_REFUSED')
        if not record['before']['active']:
            self.command(['/usr/bin/systemctl', 'stop', 'ssh.service'], check=True)
        if not record['before']['enabled']:
            self.command(['/usr/bin/systemctl', 'disable', 'ssh.service'], check=True)
        record['state'] = 'ROLLED_BACK'
        self.save('ssh', record)
        return {'status': 'ROLLED_BACK', 'retained': ['package', 'host keys', 'private backup', 'receipt'],
                'warning': 'Stopping SSH can close the current route; execute rollback from local console.'}

    def install_scope(self, account: str, account_uid: int, operations: list[str]) -> dict:
        if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', account) or account_uid <= 0:
            raise AdminError('INVALID_ACCOUNT')
        if not operations or len(set(operations)) != len(operations) or set(operations) - OPERATIONS.keys():
            raise AdminError('INVALID_SCOPE')
        policy = {'version': VERSION, 'account': account, 'uid': account_uid, 'operations': sorted(operations)}
        previous = self.load('scope')
        if previous:
            if previous['state'] == 'APPLIED' and previous['policy'] == policy:
                self.check_hashes(previous['files'])
                return {'status': 'UNCHANGED'}
            raise AdminError('CHECKPOINT_EXISTS_REVIEW_REQUIRED')
        for destination in (POLICY, WRAPPER, SUDOERS):
            if self.trusted(destination).exists():
                raise AdminError('EXISTING_FILE_PRESERVED')
        wrapper = Path(__file__).read_bytes()
        sudoers = (account + ' ALL=(root) NOPASSWD: ' + ', '.join(WRAPPER + ' ' + op for op in sorted(operations)) + '\n').encode()
        staged = self.private_dir() / 'sudoers.review'
        if staged.exists():
            if staged.read_bytes() != sudoers:
                raise AdminError('CHECKPOINT_EXISTS_REVIEW_REQUIRED')
        else:
            self.write(staged, sudoers, 0o440)
        # Validate exact candidate before installing any privilege grant.
        self.command(['/usr/sbin/visudo', '-cf', str(staged)], check=True)
        record = {'version': VERSION, 'state': 'STARTED', 'policy': policy, 'files': {}}
        self.save('scope', record)
        for destination, data, mode in ((POLICY, (json.dumps(policy) + '\n').encode(), 0o644),
                                        (WRAPPER, wrapper, 0o755), (SUDOERS, sudoers, 0o440)):
            record['pending_path'] = destination
            self.save('scope', record)
            self.write(self.trusted(destination), data, mode)
            record['files'][destination] = hashlib.sha256(data).hexdigest()
            record['pending_path'] = None
            self.save('scope', record)
        record['state'] = 'APPLIED'
        self.save('scope', record)
        return {'status': 'APPLIED', 'operations': sorted(operations)}

    def check_hashes(self, files: dict) -> None:
        for path, expected in files.items():
            actual = self.trusted(path)
            if not actual.is_file() or digest(actual) != expected:
                raise AdminError('DRIFT_ROLLBACK_REFUSED')

    def rollback_scope(self) -> dict:
        record = self.load('scope')
        if record is None or record['state'] == 'ROLLED_BACK':
            return {'status': 'UNCHANGED'}
        if record['state'] != 'APPLIED':
            raise AdminError('PARTIAL_SCOPE_OWNER_REVIEW_REQUIRED')
        self.check_hashes(record['files'])
        # Revoke the grant first. Never delete changed or unrelated files.
        for path in (SUDOERS, WRAPPER, POLICY):
            self.trusted(path).unlink()
        record['state'] = 'ROLLED_BACK'
        self.save('scope', record)
        return {'status': 'ROLLED_BACK'}

    def scoped_operation(self, operation: str, args: list[str], *, caller_uid: int) -> dict:
        if args or operation not in OPERATIONS:
            raise AdminError('INVALID_OPERATION_OR_ARGUMENTS')
        policy = json.loads(self.trusted(POLICY).read_text())
        if policy.get('version') != VERSION or operation not in policy.get('operations', []):
            raise AdminError('SCOPE_DENIED')
        if caller_uid != policy.get('uid'):
            raise AdminError('CALLER_DENIED')
        result = self.command(OPERATIONS[operation])
        syslog.openlog('projectflow-owned-admin', facility=syslog.LOG_AUTHPRIV)
        syslog.syslog(syslog.LOG_NOTICE, f'operation={operation} uid={caller_uid} exit={result.returncode}')
        return {'operation': operation, 'exit_code': result.returncode}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='audit', choices=[
        'audit', 'dry-run', 'artifact', 'bootstrap', 'rollback', 'install-scope', 'rollback-scope'])
    parser.add_argument('--account')
    parser.add_argument('--operation', action='append', default=[])
    parser.add_argument('--confirm-scope', choices=['openssh-server', 'read-only-ssh'])
    args = parser.parse_args(argv)
    try:
        engine = AdminBootstrap()
        if args.action == 'artifact':
            sys.stdout.write(Path(__file__).read_text())
            return 0
        if args.action in ('bootstrap', 'rollback', 'install-scope', 'rollback-scope'):
            require_admin(Path(__file__))
        if args.action in ('bootstrap', 'rollback') and args.confirm_scope != 'openssh-server':
            raise AdminError('EXPLICIT_SCOPE_REQUIRED')
        if args.action in ('install-scope', 'rollback-scope') and args.confirm_scope != 'read-only-ssh':
            raise AdminError('EXPLICIT_SCOPE_REQUIRED')
        if args.action == 'install-scope':
            try:
                account = pwd.getpwnam(args.account or '')
            except KeyError:
                raise AdminError('INVALID_ACCOUNT') from None
            result = engine.install_scope(account.pw_name, account.pw_uid, args.operation)
        else:
            method = {'audit': 'audit', 'dry-run': 'plan', 'bootstrap': 'bootstrap',
                      'rollback': 'rollback', 'rollback-scope': 'rollback_scope'}[args.action]
            result = getattr(engine, method)()
        print(json.dumps(result, sort_keys=True))
        return 0
    except (AdminError, OSError, ValueError) as exc:
        code = str(exc) if isinstance(exc, AdminError) else 'OWNER_REVIEW_REQUIRED'
        print(json.dumps({'status': 'BLOCKED', 'reason': code}))
        return 2


if __name__ == '__main__':
    if Path(__file__).name == 'projectflow-owned-admin':
        try:
            require_admin(Path(__file__))
            operation = sys.argv[1] if len(sys.argv) > 1 else ''
            result = AdminBootstrap().scoped_operation(operation, sys.argv[2:], caller_uid=int(os.environ.get('SUDO_UID', '-1')))
            print(json.dumps(result))
            sys.exit(result['exit_code'])
        except (AdminError, ValueError, OSError):
            print('{"status":"BLOCKED","reason":"SCOPE_DENIED"}')
            sys.exit(2)
    sys.exit(main())
