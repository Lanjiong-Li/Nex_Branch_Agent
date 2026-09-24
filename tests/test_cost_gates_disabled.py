"""Test rollout bypasses billing gates, never remote-outcome or runtime guards."""
from copy import deepcopy

import pytest

from branch_agent.actions import ActionService
from branch_agent.engine import Engine
from branch_agent.workflow import WorkflowBlocked, update, all_records
from test_runtime import runtime
from test_actions import failed_output, card, submit


def test_cost_gates_are_disabled_by_default(runtime):
    engine, model, pid, _ = runtime
    default = Engine(engine.store, model, engine.config_service)
    assert default.cost_gates_enabled is False
    assert default.status(pid)['runtime_policy']['cost_gates_enabled'] is False


@pytest.mark.parametrize('amount', [None, '9999'])
def test_unknown_or_excessive_cost_does_not_block_or_change_usage(runtime, amount):
    engine, _, pid, cid = runtime
    _, task, call, _ = failed_output(runtime)
    engine.cost_gates_enabled = False  # Also overrides an existing frozen budget.
    measured = deepcopy(call['usage'])
    measured['estimated_cost'] = {'amount': amount, 'currency': 'USD'} if amount else None
    call = update(engine.store, call, usage=measured)
    before = deepcopy(call)
    engine._budget_check(task)
    assert engine.store.get(call['id'], pid) == before


@pytest.mark.parametrize('state', ['pending', 'running', 'unknown'])
def test_remote_outcome_still_blocks(runtime, state):
    engine, _, pid, cid = runtime
    engine.cost_gates_enabled = False
    _, task, call, _ = failed_output(runtime)
    update(engine.store, call, state=state)
    with pytest.raises(WorkflowBlocked, match='operation_uncertain'):
        engine._budget_check(task)
    shown = card(ActionService(engine), pid, cid, 'task:' + task['id'])
    assert all(a['disabled_reason'] for a in shown['actions'])


def test_time_limit_still_blocks(runtime):
    engine, _, pid, cid = runtime
    engine.cost_gates_enabled = False
    root, task, _, _ = failed_output(runtime)
    data = engine._task_data(task)
    data['active_ms'] = root['budget']['max_active_seconds'] * 1000
    engine._save_task_data(task, data)
    with pytest.raises(WorkflowBlocked, match='active_time_limit'):
        engine._budget_check(task)


def test_restart_needs_no_billing_fields_or_acceptance(runtime):
    engine, model, pid, cid = runtime
    engine.cost_gates_enabled = False
    _, task, call, _ = failed_output(runtime)
    service = ActionService(engine)
    shown = card(service, pid, cid, 'task:' + task['id'])
    assert not any(a['id'] == 'report_usage' for a in shown['actions'])
    names = {f['name'] for a in shown['actions'] for f in a['fields']}
    assert not names.intersection({'max_cost_usd', 'accept_unknown_cost', 'additional_cost'})
    receipt = submit(service, pid, cid, shown, 'restart', {'max_active_seconds': 600})
    newroot = engine.store.get(receipt['task_id'], pid)
    assert 'cost' in newroot['budget']['disabled_limits']
    assert newroot['budget']['max_cost'] is None
    assert engine.store.get(call['id'], pid)['usage']['estimated_cost'] is None
    assert not all_records(engine.store, pid, 'runtime_event', event_name='usage.unknown_accepted')
    assert model.calls == []


def test_old_usage_pause_can_resume_without_claiming_usage_resolved(runtime):
    engine, _, pid, cid = runtime
    _, task, call, _ = failed_output(runtime)
    with engine.store.transaction():
        engine._transition(engine.store.get(task['id'], pid), 'paused', 'usage_uncertain')
    engine.cost_gates_enabled = False
    service = ActionService(engine)
    submit(service, pid, cid, card(service, pid, cid, 'task:' + task['id']), 'resume')
    assert engine.store.get(task['id'], pid)['state'] == 'queued'
    assert engine.store.get(call['id'], pid)['usage']['estimated_cost'] is None
    assert not all_records(engine.store, pid, 'runtime_event', event_name='usage.uncertainty_resolved')


def test_queue_permit_works_with_unknown_cost_but_not_unknown_outcome(runtime):
    engine, _, pid, cid = runtime
    engine.cost_gates_enabled = False
    _, _, call, _ = failed_output(runtime)
    engine.submit_message(pid, cid, '重新改编')
    queued = all_records(engine.store, pid, 'queued_request')[0]
    queued = update(engine.store, queued, state='blocked', blocked_reason='queue_hold')
    service = ActionService(engine)
    submit(service, pid, cid, card(service, pid, cid, 'queue:' + queued['id']), 'release_one')
    queued = engine.store.get(queued['id'], pid)
    assert engine._single_queue_release_valid(queued)
    update(engine.store, call, state='unknown')
    assert not engine._single_queue_release_valid(queued)


@pytest.mark.parametrize('reason,ignored', [
    ('usage_uncertain', True), ('cost_limit', True), ('active_time_limit', False),
    ('turn_limit', False), ('output_limit_exceeded', False), ('operation_uncertain', False),
])
def test_only_legacy_billing_holds_are_ignored_without_mutation(runtime, reason, ignored):
    engine, _, pid, cid = runtime
    _, task, _, _ = failed_output(runtime)
    with engine.store.transaction():
        task = engine._transition(engine.store.get(task['id'], pid), 'paused', reason)
    before = deepcopy(engine._control(pid, cid))
    engine.cost_gates_enabled = False
    with engine.store.transaction():
        engine.store._connection().execute('SET TRANSACTION READ ONLY')
        holds = engine._effective_holds(pid, before)
        ActionService(engine).list_cards(pid, cid)
    assert any(h['task_id'] == task['id'] for h in holds) is not ignored
    assert engine._control(pid, cid) == before
