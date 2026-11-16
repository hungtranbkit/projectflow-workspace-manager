#!/usr/bin/env python3
"""Trusted-repository CI, independent of GitHub Actions. One local worker."""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

REPO = 'hungtranbkit/projectflow-workspace-manager'
OWNER = 'hungtranbkit'
CONTEXT = 'local-ci/verify'
IMAGE = 'projectflow-local-ci:1'
SHA = re.compile(r'^[0-9a-f]{40}$')
ROOT = Path(__file__).resolve().parent


def command(args, *, cwd=None, timeout=60, check=True, data=None):
    return subprocess.run(args, cwd=cwd, input=data, text=True,
                          capture_output=True, timeout=timeout, check=check)


def api(path, payload=None):
    args = ['gh', 'api', path]
    if payload is not None:
        args += ['--method', 'POST', '--input', '-']
    out = command(args, data=json.dumps(payload) if payload is not None else None)
    return json.loads(out.stdout or '{}')


def trusted(pr):
    return (pr.get('state') == 'open' and not pr.get('draft')
            and pr.get('user', {}).get('login') == OWNER
            and (pr.get('head', {}).get('repo') or {}).get('full_name') == REPO
            and pr.get('base', {}).get('ref') == 'main'
            and bool(SHA.fullmatch(pr.get('head', {}).get('sha', '')))
            and bool(SHA.fullmatch(pr.get('base', {}).get('sha', ''))))


def identity(pr):
    return pr['head']['sha'], pr['base']['sha']


def current(pr):
    latest = api(f'repos/{REPO}/pulls/{pr["number"]}')
    return trusted(latest) and identity(latest) == identity(pr)


def status(head, state, description):
    return api(f'repos/{REPO}/statuses/{head}', {
        'state': state, 'context': CONTEXT, 'description': description[:140]})


def evidence_valid(folder):
    count = 0
    skips = []
    for tier in ('fast', 'remaining'):
        root = ET.parse(folder / (tier + '.xml')).getroot()
        cases = list(root.iter('testcase'))
        if not cases:
            raise ValueError(f'{tier}: missing test cases')
        for case in cases:
            if case.find('failure') is not None or case.find('error') is not None:
                raise ValueError(f'{tier}: failing test in JUnit')
            if case.find('skipped') is not None:
                name = case.get('classname', '')
                skips.append(name + '::' + case.get('name', ''))
                if any(x in name for x in ('test_b0', 'test_b1', 'test_b2', 'test_b3',
                                          'test_b4', 'test_b5', 'test_b6', 'test_b7',
                                          'test_state_invariants', 'test_sandbox')):
                    raise ValueError(f'security/isolation test skipped: {name}')
        count += len(cases)
    return {'tests': count, 'skipped': skips}


def remove_owned(name):
    if not name or not re.fullmatch(r'pf-ci-[0-9a-f]{16}', name):
        return
    found = command(['docker', 'inspect', '--format',
                     '{{index .Config.Labels "projectflow.local-ci"}}', name], check=False)
    if found.returncode == 0:
        if found.stdout.strip() != 'owned':
            raise RuntimeError('refusing cleanup of container without ownership label')
        command(['docker', 'rm', '-fv', name])


def atomic_json(path, obj):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(obj, indent=2) + '\n')
    tmp.replace(path)


def run_pr(pr, state):
    head, base = identity(pr)
    key = f'pr-{pr["number"]}-{head[:12]}-{base[:12]}'
    folder = state / 'runs' / key
    result_path = folder / 'result.json'
    if result_path.exists():
        old = json.loads(result_path.read_text())
        # Never rerun the same failing revision forever. Retry requires a new
        # commit or deliberate removal of this result by the operator.
        if old.get('finished_at'):
            if not old.get('published'):
                status(head, old['status'], old['description'])
                old['published'] = True
                atomic_json(result_path, old)
            return
        remove_owned(old.get('container'))
    folder.mkdir(parents=True, exist_ok=True)
    result = {'repository': REPO, 'pr': pr['number'], 'head': head, 'base': base,
              'started_at': time.time(), 'status': 'pending', 'description': key}
    result['container'] = 'pf-ci-' + uuid.uuid4().hex[:16]
    atomic_json(result_path, result)
    status(head, 'pending', f'Local CI running: {key}')
    name = result['container']
    try:
        # Public clone: no host credentials or existing production worktree copied.
        with tempfile.TemporaryDirectory(prefix='checkout-', dir=state) as tmp:
            repo = Path(tmp) / 'repo'
            command(['git', 'clone', '--no-checkout', f'https://github.com/{REPO}.git', str(repo)], timeout=180)
            command(['git', 'fetch', 'origin', head, base], cwd=repo, timeout=120)
            command(['git', 'checkout', '--detach', head], cwd=repo)
            command(['git', '-c', 'user.name=Local CI', '-c', 'user.email=ci@example.invalid',
                     'merge', '--no-edit', '--no-ff', base], cwd=repo)
            result['tested_commit'] = command(['git', 'rev-parse', 'HEAD'], cwd=repo).stdout.strip()
            result['tested_tree'] = command(['git', 'rev-parse', 'HEAD^{tree}'], cwd=repo).stdout.strip()
            result['image_id'] = command(['docker', 'image', 'inspect', IMAGE, '--format', '{{.Id}}']).stdout.strip()
            if not current(pr):
                raise RuntimeError('SUPERSEDED before execution')
            command(['docker', 'run', '-d', '--name', name, '--privileged',
                     '--label', 'projectflow.local-ci=owned', '--cpus', '2', '--memory', '4g',
                     '--pids-limit', '2048', '--entrypoint', 'dockerd', '-e', 'DOCKER_TLS_CERTDIR=',
                     '-e', 'DOCKER_HOST=unix:///var/run/docker.sock',
                     result['image_id'], '--host=unix:///var/run/docker.sock'], timeout=90)
            deadline = time.monotonic() + 90
            while command(['docker', 'exec', name, 'docker', 'info'], check=False).returncode:
                if time.monotonic() > deadline:
                    raise TimeoutError('isolated Docker daemon did not become ready')
                time.sleep(2)
            command(['docker', 'exec', name, 'mkdir', '-p', '/work'])
            command(['docker', 'cp', str(repo), name + ':/work/repo'], timeout=120)
            command(['docker', 'cp', str(ROOT / 'guest.sh'), name + ':/work/guest.sh'])
            with (folder / 'run.log').open('w') as log:
                process = subprocess.Popen(['docker', 'exec', name, 'bash', '/work/guest.sh'],
                                           stdout=log, stderr=subprocess.STDOUT)
                deadline = time.monotonic() + 3600
                try:
                    while process.poll() is None:
                        time.sleep(15)
                        if time.monotonic() > deadline:
                            raise TimeoutError('local CI exceeded 60 minutes')
                        if not current(pr):
                            raise RuntimeError('SUPERSEDED during execution')
                    if process.returncode:
                        raise RuntimeError(f'test process exited {process.returncode}')
                finally:
                    # Stop this one owned container before waiting on its exec client.
                    if process.poll() is None:
                        command(['docker', 'stop', '-t', '5', name], check=False)
                        process.wait(timeout=30)
            command(['docker', 'cp', name + ':/work/evidence/.', str(folder)], timeout=60)
            result['evidence'] = evidence_valid(folder)
            if not current(pr):
                raise RuntimeError('SUPERSEDED before publishing evidence')
            result.update(status='success', description=f'Local CI PASS ({result["evidence"]["tests"]} tests); {head[:12]}')
    except Exception as exc:
        result.update(status='failure', description=f'Local CI FAIL: {str(exc)[:100]}')
        (folder / 'error.txt').write_text(str(exc) + '\n')
    finally:
        # Never scan/delete wm-* resources on the HOST. The guest daemon owns them.
        try:
            command(['docker', 'cp', name + ':/work/evidence/.', str(folder)], check=False)
        except Exception as exc:
            result['evidence_copy_error'] = str(exc)
        try:
            remove_owned(name)
        except Exception as exc:
            result.update(status='failure', description='Local CI FAIL: owned-container cleanup failed')
            result['cleanup_error'] = str(exc)
        result['finished_at'] = time.time()
        result['log_sha256'] = hashlib.sha256((folder / 'run.log').read_bytes()).hexdigest() if (folder / 'run.log').exists() else None
        atomic_json(result_path, result)
        # If publishing fails, local evidence remains and next tick retries publishing.
        status(head, result['status'], result['description'])
        result['published'] = True
        atomic_json(result_path, result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    state = args.state.resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / 'worker.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        prs = api(f'repos/{REPO}/pulls?state=open&base=main&per_page=100&sort=updated&direction=desc')
        for pr in prs:
            if trusted(pr):
                run_pr(pr, state)
        atomic_json(state / 'heartbeat.json', {'checked_at': time.time(), 'open_prs': len(prs)})


if __name__ == '__main__':
    main()
