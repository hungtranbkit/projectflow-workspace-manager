"""Durable owner-scoped HP delivery. Bundles contain code, never credentials.

The manifest and check argv are approved on HP, independently of the source
node. A single new commit is imported into an isolated worktree, checked, then
normally pushed. No unknown submission is blindly repeated. Receipts retain
checkpoints; rollback does not delete source work or a published branch/PR.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path

from app.services.fleet_permissions import FleetError, classify, private_write, probe
from app.services.git_workspace import GitCommandError, GitSafetyError, GitWorkspaceService


class DeliveryError(FleetError):
    pass


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def private_directory(path):
    if path.is_symlink():
        raise DeliveryError('ACL_DENIED')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise DeliveryError('ACL_DENIED')


class FleetDelivery:
    """HP-only user-level delivery; caller provides owner-reviewed check argv."""

    def __init__(self, repo, state, checks, *, gh_runner=probe, check_timeout=900):
        if os.name != 'posix' or os.geteuid() == 0:
            raise DeliveryError('ACL_DENIED')
        self.repo = Path(repo).resolve()
        self.state = Path(state).absolute()
        private_directory(self.state)
        private_directory(self.state / 'requests')
        self.git = GitWorkspaceService(self.repo.parent)
        self.git.validate_repo(self.repo)
        self.checks = checks
        self.gh_runner = gh_runner
        self.check_timeout = min(max(int(check_timeout), 1), 900)

    def _save(self, path, receipt):
        private_write(path, json.dumps(receipt, sort_keys=True, indent=2) + '\n')
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _validate(self, m, bundle):
        fields = {'schema_version', 'request_key', 'task_id', 'source_node', 'repo', 'origin_url',
                  'source_branch', 'base_sha', 'commit_sha', 'remote_ref', 'allowed_paths', 'bundle_sha256'}
        if set(m) != fields or m['schema_version'] != 1:
            raise DeliveryError('MANIFEST_INVALID')
        for field in ('request_key', 'task_id', 'source_node'):
            if not isinstance(m[field], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', m[field]):
                raise DeliveryError('MANIFEST_INVALID')
        if not isinstance(m['repo'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', m['repo']) or any(p in {'.', '..'} for p in m['repo'].split('/')):
            raise DeliveryError('MANIFEST_INVALID')
        if not isinstance(m['source_branch'], str):
            raise DeliveryError('MANIFEST_INVALID')
        branch = self.git.validate_branch(m['source_branch'])
        if not branch.startswith('agent/') or m['remote_ref'] != 'refs/heads/' + branch:
            raise DeliveryError('DELIVERY_REF_MISMATCH')
        if not all(isinstance(m[f], str) and re.fullmatch(r'[0-9a-f]{40}', m[f]) for f in ('base_sha', 'commit_sha')):
            raise DeliveryError('MANIFEST_INVALID')
        if not isinstance(m['allowed_paths'], list) or not m['allowed_paths'] or not all(isinstance(p, str) for p in m['allowed_paths']):
            raise DeliveryError('MANIFEST_INVALID')
        if not isinstance(m['origin_url'], str) or any(c in m['origin_url'] for c in '\n\r\0'):
            raise DeliveryError('MANIFEST_INVALID')
        # GitHub endpoints must resolve to this exact approved repository.
        github_urls = {
            f"https://github.com/{m['repo']}.git", f"https://github.com/{m['repo']}",
            f"git@github.com:{m['repo']}.git", f"ssh://git@github.com/{m['repo']}.git"}
        if m['origin_url'] not in github_urls and not Path(m['origin_url']).is_absolute():
            raise DeliveryError('DELIVERY_DESTINATION_MISMATCH')
        self.git.delivery_origin(self.repo, m['origin_url'])
        if not self.checks or not all(isinstance(a, list) and a and all(isinstance(s, str) and s and '\0' not in s for s in a) for a in self.checks):
            raise DeliveryError('OWNER_CHECKS_REQUIRED')
        bundle = Path(bundle)
        if bundle.is_symlink() or not bundle.is_file() or bundle.stat().st_uid != os.getuid() or bundle.stat().st_size > 32 * 1024 * 1024:
            raise DeliveryError('BUNDLE_INVALID')
        data = bundle.read_bytes()
        if hashlib.sha256(data).hexdigest() != m['bundle_sha256']:
            raise DeliveryError('BUNDLE_HASH_MISMATCH')
        header = data.split(b'\n\n', 1)[0]
        if len(header) > 16384:
            raise DeliveryError('BUNDLE_INVALID')
        try:
            lines = header.decode('utf-8').splitlines()
        except UnicodeError as exc:
            raise DeliveryError('BUNDLE_INVALID') from exc
        if (len(lines) != 3 or lines[0] != '# v2 git bundle'
            or lines[1].split(' ', 1)[0] != '-' + m['base_sha']
            or lines[2] != m['commit_sha'] + ' refs/heads/' + branch):
            raise DeliveryError('BUNDLE_SCOPE_MISMATCH')
        return data

    def _auth(self, m):
        identity = self.gh_runner(['gh', 'api', 'user'], cwd=self.repo, timeout=15)
        if identity.code != 'PASS':
            return identity.code, None
        permission = self.gh_runner(['gh', 'api', 'repos/' + m['repo']], cwd=self.repo, timeout=15)
        if permission.code != 'PASS':
            return permission.code, None
        try:
            login = json.loads(identity.output)['login']
            allowed = json.loads(permission.output)['permissions']['push'] is True
        except (KeyError, TypeError, ValueError):
            return 'AUTH_REQUIRED', None
        if not isinstance(login, str) or not login:
            return 'AUTH_REQUIRED', None
        return ('PASS' if allowed else 'GIT_WRITE_DENIED'), login

    def _pr_lookup(self, m):
        result = self.gh_runner(['gh', 'pr', 'list', '--repo', m['repo'], '--head', m['source_branch'],
                                 '--base', 'main', '--state', 'all', '--json', 'url,headRefOid,headRefName,baseRefName,state'], cwd=self.repo, timeout=20)
        if result.code != 'PASS':
            return result.code, None
        try:
            rows = json.loads(result.output)
            if not isinstance(rows, list):
                raise ValueError()
            matches = [r for r in rows if r['headRefName'] == m['source_branch'] and r['baseRefName'] == 'main']
            if not matches:
                return 'ABSENT', None
            if len(matches) != 1 or matches[0]['state'] != 'OPEN' or matches[0]['headRefOid'] != m['commit_sha']:
                return 'PR_CONFLICT', None
            url = matches[0]['url']
            if not re.fullmatch(r'https://github\.com/' + re.escape(m['repo']) + r'/pull/[0-9]+', url):
                return 'PR_CONFLICT', None
            return 'PASS', url
        except (KeyError, TypeError, ValueError):
            return 'EXECUTION_UNKNOWN', None

    def deliver(self, manifest, bundle):
        """Validate, checkpoint, import, check, push/readback, create/readback PR.

        Safe to resume with identical owner inputs. A request lease is released
        by the kernel on process death. A crash during PR creation only permits
        readback, never a duplicate create. All raw subprocess output is private.
        """
        import fcntl
        m = dict(manifest)
        try:
            data = self._validate(m, bundle)
        except GitSafetyError as exc:
            raise DeliveryError(str(exc)) from exc
        key = hashlib.sha256(m['request_key'].encode()).hexdigest()
        request = self.state / 'requests' / (key + '.json')
        lease = request.with_suffix('.lock')
        if lease.is_symlink() or request.is_symlink():
            raise DeliveryError('ACL_DENIED')
        fd = os.open(lease, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        ref_fd = None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'code': 'DELIVERY_IN_PROGRESS', 'request_key': m['request_key']}
            # Serialize this repo/ref across different request keys too. A new
            # request cannot race an existing request's push or PR creation.
            ref_key = fingerprint([m['origin_url'], m['remote_ref']])
            ref_fd = os.open(self.state / 'requests' / ('ref-' + ref_key + '.lock'),
                             os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(ref_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'code': 'DELIVERY_IN_PROGRESS', 'request_key': m['request_key']}
            receipt = json.loads(request.read_text()) if request.exists() else {}
            expected = fingerprint({'manifest': m, 'checks': self.checks, 'check_timeout': self.check_timeout, 'repo': str(self.repo)})
            if receipt and receipt.get('fingerprint') != expected:
                raise DeliveryError('DELIVERY_REQUEST_CONFLICT')
            if not receipt:
                receipt = dict(schema_version=1, fingerprint=expected, request_key=m['request_key'],
                               task_id=m['task_id'], source_node=m['source_node'], source_branch=m['source_branch'],
                               source_sha=m['commit_sha'], hp_sha=m['commit_sha'], remote_ref=m['remote_ref'],
                               repo=m['repo'], origin_url=m['origin_url'], allowed_paths=m['allowed_paths'],
                               base_sha=m['base_sha'], bundle_sha256=m['bundle_sha256'],
                               phase='INTAKE', code='PENDING', pr_url=None, checks=[])
                self._save(request, receipt)
            def checkpoint(code, **updates):
                receipt.update(code=code, **updates)
                self._save(request, receipt)
                return dict(receipt)
            # Keep a private immutable input snapshot. Validate its hash again on
            # every resume and never mutate the owner's original bundle/worktree.
            snapshot = request.with_suffix('.bundle')
            if snapshot.exists():
                if snapshot.is_symlink() or hashlib.sha256(snapshot.read_bytes()).hexdigest() != m['bundle_sha256']:
                    raise DeliveryError('DELIVERY_CHECKPOINT_CONFLICT')
            else:
                out = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(out, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            try:
                self.git.delivery_import(self.repo, snapshot, m['source_branch'], m['base_sha'], m['commit_sha'], m['allowed_paths'], key)
                auth, login = self._auth(m)
                if auth != 'PASS':
                    return checkpoint(auth)
                receipt['delivery_login'] = login
                remote = self.git.delivery_remote(self.repo, m['remote_ref'])
                if remote not in (None, m['commit_sha']):
                    return checkpoint('DELIVERY_REMOTE_CONFLICT')
                if remote is None and receipt['phase'] == 'PUSHING' and not receipt.get('push_retry_safe', False):
                    return checkpoint('EXECUTION_UNKNOWN')
                if receipt['phase'] == 'COMPLETE':
                    status, url = self._pr_lookup(m)
                    if remote != m['commit_sha'] or status != 'PASS':
                        return checkpoint('DELIVERY_READBACK_CHANGED')
                    return checkpoint('PASS', pr_url=url)
                if receipt['phase'] not in {'CHECKED', 'PUSHING', 'PUSHED', 'PR_CREATING'}:
                    worktree = self.git.delivery_worktree(self.repo, m['commit_sha'], key)
                    if not (worktree / 'PROJECT.yaml').is_file():
                        return checkpoint('PROJECT_CONTRACT_REQUIRED')
                    if self.git.status(worktree):
                        return checkpoint('DELIVERY_WORKTREE_DIRTY')
                    checkpoint('PENDING', phase='CHECKING', worktree=str(worktree), checks=[])
                    for argv in self.checks:
                        result = probe(argv, cwd=worktree, timeout=self.check_timeout)
                        receipt['checks'].append({**result.public(), 'argv_sha256': fingerprint(argv),
                                                  'output_sha256': hashlib.sha256(result.output.encode()).hexdigest()})
                        if result.code != 'PASS':
                            return checkpoint('CHECK_FAILED')
                    if self.git.head(worktree) != m['commit_sha'] or self.git.git(worktree, 'diff', '--exit-code', 'HEAD', check=False).returncode:
                        return checkpoint('DELIVERY_CHECK_MUTATED_SOURCE')
                    checkpoint('PENDING', phase='CHECKED')
                if remote is None:
                    if receipt['phase'] in {'PUSHED', 'PR_CREATING'}:
                        return checkpoint('DELIVERY_REMOTE_CONFLICT')
                    self.git.delivery_origin(self.repo, m['origin_url'])
                    checkpoint('PENDING', phase='PUSHING', push_retry_safe=False)
                    try:
                        pushed = self.git.delivery_push(self.repo, m['commit_sha'], m['remote_ref'])
                    except GitCommandError:
                        # Transport outcome unknown: reconciliation may prove it
                        # landed. Absence cannot prove a timed-out push stopped.
                        remote = self.git.delivery_remote(self.repo, m['remote_ref'])
                        if remote != m['commit_sha']:
                            return checkpoint('EXECUTION_UNKNOWN')
                    else:
                        rejected = pushed.returncode != 0 and any(token in pushed.stderr.lower() for token in
                            ('remote rejected', 'pre-receive hook declined', 'authentication failed', 'terminal prompts disabled', 'permission denied', 'protected branch'))
                        if rejected:
                            checkpoint('PENDING', push_retry_safe=True)
                        remote = self.git.delivery_remote(self.repo, m['remote_ref'])
                        if remote != m['commit_sha']:
                            return checkpoint(classify(pushed.returncode, pushed.stderr) if rejected else 'EXECUTION_UNKNOWN')
                    checkpoint('PENDING', phase='PUSHED', remote_sha=remote)
                elif receipt['phase'] != 'PR_CREATING':
                    checkpoint('PENDING', phase='PUSHED', remote_sha=remote)
                status, url = self._pr_lookup(m)
                if status == 'ABSENT':
                    if receipt['phase'] == 'PR_CREATING':
                        return checkpoint('EXECUTION_UNKNOWN')
                    body_file = request.with_suffix('.pr-body.md')
                    private_write(body_file,
                        f"Owner-scoped code-only delivery. Task {m['task_id']}; request {m['request_key']}.\n\n"
                        f"Source and HP commit: `{m['commit_sha']}`. Base: `{m['base_sha']}`. "
                        "Independent HP checks passed; no credentials transferred. Review and merge gates remain required.\n")
                    checkpoint('PENDING', phase='PR_CREATING')
                    created = self.gh_runner(['gh', 'pr', 'create', '--repo', m['repo'], '--base', 'main',
                        '--head', m['source_branch'], '--title', f"Task {m['task_id']}: code delivery from {m['source_node']}",
                        '--body-file', str(body_file)], cwd=self.repo, timeout=30)
                    status, url = self._pr_lookup(m)
                    if status != 'PASS':
                        return checkpoint('EXECUTION_UNKNOWN' if created.code in {'PASS', 'PROBE_TIMEOUT'} else created.code)
                elif status != 'PASS':
                    return checkpoint(status)
                return checkpoint('PASS', phase='COMPLETE', pr_url=url, remote_sha=m['commit_sha'])
            except GitSafetyError as exc:
                checkpoint('SCOPE_REJECTED')
                raise DeliveryError(str(exc)) from exc
            except GitCommandError as exc:
                code = classify(exc.result.returncode, exc.result.stderr) if exc.result else 'EXECUTION_UNKNOWN'
                return checkpoint(code)
        finally:
            if ref_fd is not None:
                os.close(ref_fd)
            os.close(fd)


def _owner_json(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise DeliveryError('OWNER_PRIVATE_INPUT_REQUIRED')
    return json.loads(path.read_text())


def main(argv=None):
    parser = argparse.ArgumentParser(description='Owner-scoped HP code-only delivery; normal push/PR, durable resume.')
    parser.add_argument('--repo', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--manifest', required=True, help='Owner-reviewed private JSON, never source-controlled')
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--checks', required=True, help='Owner-reviewed private JSON argv arrays matching PROJECT.yaml')
    args = parser.parse_args(argv)
    try:
        result = FleetDelivery(args.repo, args.state, _owner_json(args.checks)).deliver(_owner_json(args.manifest), args.bundle)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['code'] == 'PASS' else 2
    except (DeliveryError, GitSafetyError, ValueError, OSError) as exc:
        # Never echo paths, subprocess output or potentially secret input.
        code = str(exc) if isinstance(exc, (DeliveryError, GitSafetyError)) else 'DELIVERY_INPUT_INVALID'
        print(json.dumps({'code': code}))
        return 2
