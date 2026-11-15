"""No live services. Test state semantics before connecting Terminal MCP."""
from datetime import datetime, timedelta, timezone
import pytest
from app.services.agent_harness_mvp import HarnessStore, HarnessError

T = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    return HarnessStore(tmp_path / 'harness.db')


def create(store, key='mvp-1', child='make a single demo artifact', ttl=timedelta(hours=24)):
    return store.create(key=key, main_goal='Finish one independently accepted task',
                        child_goal=child, dod='artifact + test + runtime readback',
                        agent='codex', node='dell-linux', created_at=T, ttl=ttl)


def test_fake_agent_end_to_end(store):
    task = create(store)
    assert task.state == 'NOT_DISPATCHED'
    task = store.dispatch(task.id, external_task_id='terminal-mcp-task-123', at=T + timedelta(minutes=1))
    assert task.state == 'DISPATCHED'  # receipt alone != running
    task = store.observe(task.id, backend_state='RUNNING', process_alive=True,
                         at=T + timedelta(minutes=3), progress_at=T + timedelta(minutes=2),
                         evidence_ref='pane/heartbeat-hash')
    assert task.state == 'RUNNING_VERIFIED'
    task = store.observe(task.id, backend_state='FINISHED', process_alive=False, at=T + timedelta(minutes=4))
    assert task.state == 'NOT_RUNNING'
    task = store.accept(task.id, at=T + timedelta(minutes=5),
                        artifact_ref='sha256:demo', test_evidence='pytest exit=0',
                        runtime_readback='fixture stdout sha256:pass',
                        verifier=lambda artifact, test, runtime: all((artifact, test, runtime)))
    assert task.state == 'DONE_VERIFIED'
    task = store.cleanup(task.id, at=T + timedelta(minutes=6), executor_stopped=True,
                         no_active_refs=True, dirty_worktree=False, evidence_preserved=True,
                         deletion_verified=True)
    assert task.cleanup_state == 'CLEANED_VERIFIED'
    assert [e['event'] for e in store.events(task.id)] == [
        'CREATED', 'DISPATCHED', 'OBSERVED', 'OBSERVED', 'DONE_VERIFIED', 'CLEANED_VERIFIED']


def test_goal_lock_and_duplicate_guard(store):
    t = create(store)
    with pytest.raises(HarnessError, match='duplicate active'):
        create(store, key='another-key')
    with pytest.raises(HarnessError, match='duplicate idempotency'):
        create(store, key=t.key, child='different task')
    assert store.get(t.id).main_goal == 'Finish one independently accepted task'


def test_24_hour_limit_and_no_restart_after_expiry(store):
    with pytest.raises(HarnessError, match='TTL'):
        create(store, ttl=timedelta(hours=25))
    t = create(store)
    with pytest.raises(HarnessError, match='EXPIRED'):
        store.dispatch(t.id, external_task_id='abc', at=T + timedelta(hours=24))
    assert store.get(t.id).state == 'EXPIRED'
    assert store.events(t.id)[-1]['event'] == 'EXPIRED'


def test_requires_evidence_not_just_receipt(store):
    t = create(store)
    t = store.dispatch(t.id, external_task_id='x', at=T)
    with pytest.raises(HarnessError, match='observed execution'):
        store.accept(t.id, at=T + timedelta(minutes=1), artifact_ref='artifact', test_evidence='test', runtime_readback='readback', verifier=lambda a,b,c: True)
    assert store.get(t.id).state == 'DISPATCHED'


def test_freshness_and_idle_and_unknown(store):
    t = create(store)
    store.dispatch(t.id, external_task_id='x', at=T)
    x = store.observe(t.id, backend_state='RUNNING', process_alive=True, at=T+timedelta(hours=2),
                      progress_at=T, evidence_ref='hash')
    assert x.state == 'STALLED'
    x = store.observe(t.id, backend_state='RUNNING', process_alive=None, at=T+timedelta(hours=3),
                      progress_at=T+timedelta(hours=3), evidence_ref='hash')
    assert x.state == 'UNKNOWN'
    x = store.observe(t.id, backend_state='IDLE', process_alive=False, at=T+timedelta(hours=4))
    assert x.state == 'NOT_RUNNING'


def test_fail_closed_missing_acceptance(store):
    t = create(store)
    store.dispatch(t.id, external_task_id='x', at=T)
    store.observe(t.id, backend_state='FINISHED', process_alive=False, at=T+timedelta(minutes=3))
    with pytest.raises(HarnessError, match='evidence required'):
        store.accept(t.id, at=T+timedelta(minutes=4), artifact_ref='file', test_evidence='', runtime_readback='', verifier=lambda a,b,c: True)
    assert store.get(t.id).state != 'DONE_VERIFIED'


def test_dirty_worktree_prevents_cleanup_claim(store):
    t = create(store)
    store.fail(t.id, at=T+timedelta(minutes=1), reason='test failed')
    t = store.cleanup(t.id, at=T+timedelta(minutes=2), executor_stopped=True,
                      no_active_refs=True, dirty_worktree=True, evidence_preserved=True,
                      deletion_verified=False)
    assert t.cleanup_state == 'CLEANUP_PENDING'


def test_cleanup_requires_terminal_state(store):
    t = create(store)
    with pytest.raises(HarnessError, match='unfinished'):
        store.cleanup(t.id, at=T, executor_stopped=True, no_active_refs=True,
                      dirty_worktree=False, evidence_preserved=True, deletion_verified=True)


def test_no_running_claim_for_running_without_fresh_proof(store):
    t = create(store)
    store.dispatch(t.id, external_task_id='x', at=T)
    x=store.observe(t.id, backend_state='RUNNING', process_alive=True,
                    at=T+timedelta(minutes=3), progress_at=T+timedelta(minutes=2), evidence_ref=None)
    assert x.state == 'STALLED'


def test_timezone_required(store):
    with pytest.raises(HarnessError, match='timezone-aware'):
        store.create(
            key='n', main_goal='a', child_goal='b', dod='c', agent='codex', node='dell-linux',
            created_at=datetime(2026,11,15))


def test_verifier_is_mandatory(store):
    t = create(store)
    store.dispatch(t.id, external_task_id='x', at=T)
    store.observe(t.id, backend_state='FINISHED', process_alive=False, at=T+timedelta(minutes=1))
    with pytest.raises(HarnessError, match='verifier'):
        store.accept(t.id, at=T+timedelta(minutes=2), artifact_ref='a',
                     test_evidence='exit0', runtime_readback='r')
    with pytest.raises(HarnessError, match='verifier'):
        store.accept(t.id, at=T+timedelta(minutes=2), artifact_ref='a',
                     test_evidence='exit0', runtime_readback='r', verifier=lambda a,b,c: False)
    assert store.get(t.id).state != 'DONE_VERIFIED'


def test_reconcile_due_expires_every_overdue_child_and_requires_cleanup(store):
    a = create(store, key='overdue-a', child='task a', ttl=timedelta(hours=1))
    b = create(store, key='overdue-b', child='task b', ttl=timedelta(hours=2))
    c = create(store, key='not-due', child='task c', ttl=timedelta(hours=4))
    store.dispatch(a.id, external_task_id='running-a', at=T)
    store.dispatch(b.id, external_task_id='running-b', at=T)
    at = T + timedelta(hours=3)
    changed = store.reconcile_due(at=at)
    assert [t.id for t in changed] == [a.id, b.id]
    assert all(t.state == 'EXPIRED' and t.cleanup_state == 'CLEANUP_PENDING' for t in changed)
    assert store.get(c.id).state == 'NOT_DISPATCHED'
    assert store.reconcile_due(at=at) == []
    assert sum(e['event'] == 'EXPIRED' for e in store.events(a.id)) == 1
    with pytest.raises(HarnessError, match='EXPIRED'):
        store.dispatch(a.id, external_task_id='resend-is-forbidden', at=at)


def test_reconcile_does_not_expire_already_accepted_child(store):
    a = create(store, key='accepted', child='accepted result', ttl=timedelta(hours=1))
    store.dispatch(a.id, external_task_id='test-a', at=T)
    store.observe(a.id, backend_state='FINISHED', process_alive=False,
                  at=T+timedelta(minutes=1))
    store.accept(a.id, at=T+timedelta(minutes=2),
                 artifact_ref='a', test_evidence='t', runtime_readback='r',
                 verifier=lambda a,t,r: True)
    assert store.reconcile_due(at=T+timedelta(hours=3)) == []
    assert store.get(a.id).state == 'DONE_VERIFIED'


def test_expire_single_path_also_flags_cleanup_pending(store):
    a = create(store, key='expired-dispatch', child='expiring', ttl=timedelta(minutes=5))
    with pytest.raises(HarnessError):
        store.dispatch(a.id, external_task_id='late', at=T+timedelta(minutes=6))
    assert store.get(a.id).state == 'EXPIRED'
    assert store.get(a.id).cleanup_state == 'CLEANUP_PENDING'


def test_parent_tasks_snapshot_filter(store):
    a = create(store, key='a', child='one')
    b = store.create(key='b', main_goal='different locked goal', child_goal='other',
                     dod='one result', agent='codex', node='dell-linux', created_at=T)
    assert [t.id for t in store.list_tasks()] == [a.id, b.id]
    assert [t.id for t in store.list_tasks(main_goal=a.main_goal)] == [a.id]


def test_parent_wip_limit_two_children_and_cleanup_release(store):
    a = create(store, key='wip-a', child='first')
    b = create(store, key='wip-b', child='second')
    with pytest.raises(HarnessError, match='WIP limit'):
        create(store, key='wip-c', child='third')
    # A terminal task whose cleanup is still pending must keep its slot.
    store.fail(a.id, at=T+timedelta(minutes=1), reason='failed test')
    store.cleanup(a.id, at=T+timedelta(minutes=2),
                  executor_stopped=False, no_active_refs=False,
                  dirty_worktree=True, evidence_preserved=True,
                  deletion_verified=False)
    with pytest.raises(HarnessError, match='WIP limit'):
        create(store, key='wip-c', child='third')
    store.cleanup(a.id, at=T+timedelta(minutes=3),
                  executor_stopped=True, no_active_refs=True,
                  dirty_worktree=False, evidence_preserved=True,
                  deletion_verified=True)
    c = create(store, key='wip-c', child='third')
    assert c.state == 'NOT_DISPATCHED'
    assert b.state == 'NOT_DISPATCHED'


def test_overdue_cleanup_occupies_parent_slot(store):
    a = create(store, key='exp-a', child='first', ttl=timedelta(minutes=1))
    create(store, key='exp-b', child='second')
    store.reconcile_due(at=T+timedelta(minutes=2))
    assert store.get(a.id).state == 'EXPIRED'
    with pytest.raises(HarnessError, match='WIP limit'):
        create(store, key='exp-c', child='third')
