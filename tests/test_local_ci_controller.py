"""Safety and evidence contracts for the host-side local CI controller."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

spec = importlib.util.spec_from_file_location('local_ci', Path(__file__).parents[1] / 'scripts/local-ci/controller.py')
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


def pr():
    return {'number': 1, 'state': 'open', 'draft': False, 'user': {'login': ci.OWNER},
            'head': {'sha': 'a'*40, 'repo': {'full_name': ci.REPO}},
            'base': {'sha': 'b'*40, 'ref': 'main'}}


def test_trust_boundary():
    p = pr()
    assert ci.trusted(p)
    p['head']['repo'] = {'full_name': 'attacker/fork'}
    assert not ci.trusted(p)
    p['head']['repo'] = None
    assert not ci.trusted(p)
    p = pr(); p['user']['login'] = 'stranger'
    assert not ci.trusted(p)
    p = pr(); p['draft'] = True
    assert not ci.trusted(p)
    p = pr(); p['head']['sha'] = '--upload-pack=evil'
    assert not ci.trusted(p)


def test_base_drift_invalidates_evidence(monkeypatch):
    latest = pr(); latest['base']['sha'] = 'c'*40
    monkeypatch.setattr(ci, 'api', lambda *a: latest)
    assert not ci.current(pr())


def xml(folder, skipped='', failure=''):
    for tier in ('fast', 'remaining'):
        (folder / (tier+'.xml')).write_text('<testsuite><testcase classname="tests.test_b06_sandbox" name="isolation">'+skipped+failure+'</testcase></testsuite>')


def test_junit_must_exist_and_security_cannot_be_skipped(tmp_path):
    with pytest.raises(FileNotFoundError): ci.evidence_valid(tmp_path)
    xml(tmp_path, skipped='<skipped/>')
    with pytest.raises(ValueError, match='security'): ci.evidence_valid(tmp_path)
    xml(tmp_path, failure='<failure/>')
    with pytest.raises(ValueError, match='failing'): ci.evidence_valid(tmp_path)
    xml(tmp_path)
    assert ci.evidence_valid(tmp_path) == {'tests': 2, 'skipped': []}


def test_cleanup_checks_exact_name_and_label(monkeypatch):
    calls = []
    def cmd(args, **kw):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='someone-else\n')
    monkeypatch.setattr(ci, 'command', cmd)
    ci.remove_owned('production-db')
    assert calls == []
    with pytest.raises(RuntimeError, match='ownership'):
        ci.remove_owned('pf-ci-'+'a'*16)
    assert len(calls) == 1


def test_cached_failure_is_not_reexecuted_or_reposted(tmp_path, monkeypatch):
    folder = tmp_path / 'runs' / ('pr-1-'+'a'*12+'-'+'b'*12)
    folder.mkdir(parents=True)
    ci.atomic_json(folder/'result.json', {'finished_at': 1, 'status': 'failure', 'published': True})
    def forbidden(*a, **kw): raise AssertionError('must not rerun/repost')
    monkeypatch.setattr(ci, 'command', forbidden)
    monkeypatch.setattr(ci, 'status', forbidden)
    ci.run_pr(pr(), tmp_path)


@pytest.mark.parametrize('exit_code,drift,expected', [(1, False, 'failure'), (0, True, 'failure'), (0, False, 'success')])
def test_exit_code_and_exact_revision_gate(tmp_path, monkeypatch, exit_code, drift, expected):
    publications=[]; checks=[]
    monkeypatch.setattr(ci, 'status', lambda head,state,desc: publications.append((head,state)))
    def current(_):
        checks.append(1)
        return not (drift and len(checks)>1)
    monkeypatch.setattr(ci, 'current', current)
    monkeypatch.setattr(ci, 'remove_owned', lambda name: None)
    def cmd(args, **kw):
        if args[:2] == ['git','clone']: Path(args[-1]).mkdir()
        if args[:2] == ['docker','cp'] and ':/work/evidence/.' in args[2]: xml(Path(args[3]))
        return SimpleNamespace(returncode=0, stdout='d'*40+'\n')
    monkeypatch.setattr(ci, 'command', cmd)
    class Process:
        returncode = exit_code
        def poll(self): return self.returncode
    monkeypatch.setattr(ci.subprocess, 'Popen', lambda *a,**kw: Process())
    ci.run_pr(pr(), tmp_path)
    assert publications[0] == ('a'*40, 'pending')
    assert publications[-1] == ('a'*40, expected)
    data=json.loads(next((tmp_path/'runs').glob('*/result.json')).read_text())
    assert data['head']=='a'*40 and data['base']=='b'*40
    assert data['status']==expected and data['published']
