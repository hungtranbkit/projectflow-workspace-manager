import json
import os
import tempfile
import time
import threading
import pytest
from app.services.fleet_dispatch_gate import DispatchGateError, wait_for_dispatch_gate


def receipt(tmp_path, **changes):
    return {'approved': True, 'request_key': 'request-1', 'workspace': str(tmp_path.resolve()),
            'created_at': time.time(), 'expires_at': time.time() + 10, **changes}


def write(path, data):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, 'w') as stream:
        stream.write(json.dumps(data))
    os.replace(temporary, path)


def test_missing_gate_never_authorizes(tmp_path):
    tmp_path.chmod(0o700)
    start = time.monotonic()
    with pytest.raises(DispatchGateError, match='GRANT_HANDOFF_TIMEOUT'):
        wait_for_dispatch_gate(tmp_path/'gate', 'request-1', tmp_path, timeout=0.1)
    assert time.monotonic() - start < 1


def test_gate_waits_until_actual_authorization(tmp_path):
    tmp_path.chmod(0o700)
    gate = tmp_path/'gate'
    timer = threading.Timer(0.05, lambda: write(gate, receipt(tmp_path)))
    timer.start()
    try:
        wait_for_dispatch_gate(gate, 'request-1', tmp_path, timeout=1)
    finally:
        timer.join()


@pytest.mark.parametrize('changes', [{'approved': False}, {'request_key': 'other'}, {'workspace': '/other'},
    {'expires_at': 0}, {'created_at': 0}, {'expires_at': time.time() + 1000}])
def test_wrong_stale_or_unapproved_gate_denied(tmp_path, changes):
    tmp_path.chmod(0o700)
    gate = tmp_path/'gate'
    write(gate, receipt(tmp_path, **changes))
    with pytest.raises(DispatchGateError, match='TRUST_REQUIRED'):
        wait_for_dispatch_gate(gate, 'request-1', tmp_path)


def test_nonprivate_and_symlink_gate_denied(tmp_path):
    tmp_path.chmod(0o700)
    gate = tmp_path/'gate'
    write(gate, receipt(tmp_path))
    gate.chmod(0o644)
    with pytest.raises(DispatchGateError, match='ACL_DENIED'):
        wait_for_dispatch_gate(gate, 'request-1', tmp_path)
    gate.chmod(0o600)
    link = tmp_path/'link'
    link.symlink_to(gate)
    with pytest.raises(DispatchGateError, match='ACL_DENIED'):
        wait_for_dispatch_gate(link, 'request-1', tmp_path)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process-group behavior; Windows runtime remains unverified')
def test_cli_sigterm_reaps_owned_child_and_retry_does_not_redispatch(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from app.services.fleet_permissions import digest
    state = tmp_path/'state'
    state.mkdir(mode=0o700)
    child_pid = tmp_path/'child.pid'
    child_code = 'import os,time,pathlib;pathlib.Path(' + repr(str(child_pid)) + ').write_text(str(os.getpid()));time.sleep(60)'
    script = tmp_path/'wrapper.py'
    script.write_text('''import sys
from pathlib import Path
from app.services import fleet_permissions as f
f.FleetProfile.preflight=lambda *a,**k: {'code':'PASS'}
f.FleetProfile.identity=lambda *a,**k: {'workspace':str(Path.cwd())}
f.GitWorkspaceService.head=lambda *a,**k:'a'*40
f.agent_argv=lambda *a: [sys.executable,'-c',CHILD]
raise SystemExit(f.main())
'''.replace('CHILD', repr(child_code)))
    prompt = tmp_path/'prompt'
    prompt.write_text('test')
    command = [sys.executable, str(script), 'launch', '--workspace', str(tmp_path), '--state', str(state),
               '--scope', 'local', '--prompt-file', str(prompt), '--request-key', 'owned-stop']
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])
    wrapper = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        deadline = time.monotonic() + 5
        while not child_pid.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert child_pid.exists()
        pid = int(child_pid.read_text())
        wrapper.terminate()
        stdout, stderr = wrapper.communicate(timeout=5)
        assert wrapper.returncode == 1, stderr
        assert json.loads(stdout)['code'] == 'EXECUTION_UNKNOWN'
        def child_alive():
            status = Path(f'/proc/{pid}/stat')
            return status.exists() and status.read_text().split()[2] != 'Z'
        assert not child_alive()
        assert unrelated.poll() is None
        saved = state/(digest(str(tmp_path).encode())[:20]+'.request-owned-stop.json')
        assert json.loads(saved.read_text())['state'] == 'STARTING'
        repeat = subprocess.run(command, capture_output=True, text=True, env=env, timeout=5)
        result = json.loads(repeat.stdout)
        assert result['code'] == 'EXECUTION_UNKNOWN'
        assert result['replayed'] is True
        assert int(child_pid.read_text()) == pid
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait()
        if child_pid.exists():
            try:
                os.kill(int(child_pid.read_text()), 9)
            except ProcessLookupError:
                pass
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_docker_delivery_is_rejected_even_when_all_host_checks_pass(tmp_path, monkeypatch):
    from app.services import fleet_permissions as f
    manager = f.FleetProfile(tmp_path)
    monkeypatch.setattr(manager, 'check', lambda: {'code': 'PASS'})
    checks = {name: {'code': 'PASS'} for name in ('workspace', 'repository', 'gh_auth', 'gh_identity',
        'github_push', 'git_helper', 'disk', 'codex_auth', 'codex_version', 'docker')}
    monkeypatch.setattr(manager, 'audit', lambda: {'checks': checks})
    monkeypatch.setattr(f, 'probe', lambda *a, **k: f.Probe('PASS', 0, 0,
        '--sandbox --config --cd --ephemeral --skip-git-repo-check'))
    monkeypatch.setattr(f, 'fleet_docker_probe', lambda *a, **k: f.Probe('PASS', 0, 0, ''))
    report = manager.preflight('codex', runtime='docker', image='sha256:'+'a'*64, scope='delivery')
    assert report['failures']['delivery'] == 'TRUST_REQUIRED'
    assert report['execution'] == 'NOT_DISPATCHED'


def test_cli_restores_handlers_after_interruption(tmp_path, monkeypatch, capsys):
    import signal
    from app.services import fleet_permissions as f
    prompt = tmp_path/'prompt'
    prompt.write_text('test')
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    monkeypatch.setattr(f.FleetProfile, 'launch', lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert f.main(['launch', '--workspace', str(tmp_path), '--prompt-file', str(prompt)]) == 1
    assert json.loads(capsys.readouterr().out)['code'] == 'EXECUTION_UNKNOWN'
    assert all(signal.getsignal(sig) == handler for sig, handler in previous.items())
