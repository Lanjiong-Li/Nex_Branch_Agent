"""Structured user actions use isolated PostgreSQL and never call a live model."""
from copy import deepcopy
import asyncio
from uuid import uuid4

import pytest

from branch_agent.actions import ActionService
from branch_agent.records import new_record, canonical_bytes, usage
from branch_agent.workflow import all_records, body, ref, update, WorkflowBlocked, WHOLE
from test_runtime import _synthetic, runtime, source_response, seed_knowledge_asset
from test_split_source_workflow import _output as split_source_output


def initial_task(runtime, stage=1):
    engine,model,pid,cid=runtime
    engine.import_source(pid,cid,'甲见乙。','原作')
    with engine.store.transaction():
        message=engine._message(pid,cid,'请完成改编',role='user')
        root=engine._new_task(pid,cid,message,'generate',is_workflow=True,stages=list(range(1,12)),request='请完成改编')
        child=engine._dispatch(root,stage)
    return root,child


def pending_question(runtime, prompts=('甲','乙')):
    engine,_,pid,cid=runtime
    _,task=initial_task(runtime)
    with engine.store.transaction():
        shown=engine._message(pid,cid,'请选择并补充',task=task['id'])
        data=engine._task_data(task)
        data['pending_user_items']=[dict(engine._wait_item(task,'question',shown,prompt,question_id=str(i)),
            suggested_answers=['保留','修改'],reason='决定剧情',target_field='/payload/goal',blocking_scope='当前阶段') for i,prompt in enumerate(prompts)]
        engine._save_task_data(task,data)
        task=engine._transition(task,'waiting_user')
    return task


def card(service,pid,cid,identity):
    return next(c for c in service.list_cards(pid,cid)['cards'] if c['id']==identity)


def submit(service,pid,cid,current,action,values=None):
    return service.submit(pid,cid,current['id'],action,current['revision'],values or {})


def test_answer_binds_exact_question_and_waits_for_the_rest(runtime):
    engine,model,pid,cid=runtime;service=ActionService(engine)
    task=pending_question(runtime)
    pending=engine._task_data(task)['pending_user_items']
    shown=card(service,pid,cid,'pending:'+pending[0]['id'])
    assert shown['actions'][0]['fields'][0]['options'][0]['value']=='保留'
    receipt=submit(service,pid,cid,shown,'answer',{'answer':'保留'})
    data=engine._task_data(task)
    assert data['pending_user_items'][0]['answer_message_ids']==[receipt['message_id']]
    assert data['pending_user_items'][1]['state']=='open'
    assert engine.store.get(task['id'],pid)['state']=='waiting_user'
    assert engine.store.get(receipt['message_id'],pid)['role']=='user'
    second=card(service,pid,cid,'pending:'+pending[1]['id'])
    submit(service,pid,cid,second,'answer',{'answer':'__custom__','text':'保留人物，但修改结局'})
    assert engine.store.get(task['id'],pid)['state']=='queued'
    assert all(p['state']=='resolved' for p in engine._task_data(task)['pending_user_items'])
    assert model.calls==[]


def test_action_stale_is_atomic_and_cannot_cross_conversation(runtime):
    engine,_,pid,cid=runtime;service=ActionService(engine);task=pending_question(runtime,('唯一问题',))
    item=engine._task_data(task)['pending_user_items'][0]
    old=card(service,pid,cid,'pending:'+item['id'])
    with engine.store.transaction():engine._transition(engine.store.get(task['id'],pid),'stopped','user_stop')
    before=len(engine.status(pid)['tasks'])
    with pytest.raises(WorkflowBlocked,match='action_stale'):
        submit(service,pid,cid,old,'answer',{'answer':'保留'})
    assert len(engine.status(pid)['tasks'])==before
    other=engine.store.put(new_record('conversation',pid,title='Other'))
    with pytest.raises(WorkflowBlocked,match='action_not_found'):
        submit(service,pid,other['id'],old,'answer',{'answer':'保留'})


def pending_confirmation(runtime):
    engine,_,pid,cid=runtime;_,task=initial_task(runtime,2)
    with engine.store.transaction():
        baseline=seed_knowledge_asset(engine,pid)
        result=deepcopy(body(engine.store,baseline))
        result['payload']['premise']='相遇'
        version=engine.workflow.save(pid,'source_knowledge_asset',result,stage=2,
            inputs=baseline['source_refs'])
        shown=engine._message(pid,cid,'请确认v1',task=task['id'])
        targets=[{'subject':ref(version),'selections':[WHOLE]}]
        data=engine._task_data(task);data['result_ref']=ref(version)
        data['pending_user_items']=[engine._wait_item(task,'confirmation',shown,'确认分析',targets)]
        engine._save_task_data(task,data);engine._transition(task,'waiting_user')
    return task,version


def test_confirmation_revision_ignores_unrelated_task_record_updates(runtime):
    engine,_,pid,cid=runtime;service=ActionService(engine);task,_=pending_confirmation(runtime)
    item=engine._task_data(task)['pending_user_items'][0]
    before=card(service,pid,cid,'pending:'+item['id'])
    with engine.store.transaction():
        current=engine.store.get(task['id'],pid)
        update(engine.store,current,latest_checkpoint_id=current['latest_checkpoint_id'])
    after=card(service,pid,cid,before['id'])
    assert after['revision']==before['revision']
    assert after['actions']==before['actions']


def test_confirmation_uses_exact_version_and_real_user_source(runtime):
    engine,model,pid,cid=runtime;service=ActionService(engine);task,version=pending_confirmation(runtime)
    item=engine._task_data(task)['pending_user_items'][0]
    receipt=submit(service,pid,cid,card(service,pid,cid,'pending:'+item['id']),'confirm')
    confirmation=engine.store.list(pid,'confirmation')[0]
    assert confirmation['subject']==ref(version)
    assert confirmation['source_message_ids']==[receipt['message_id']]
    assert engine.workflow.state(version)['confirmation_status']=='confirmed'
    assert engine.store.get(task['id'],pid)['state']=='succeeded'
    assert model.calls==[]


def failed_output(runtime, *, remote_unknown=False, unknown_cost=True):
    engine,_,pid,cid=runtime;root,task=initial_task(runtime)
    with engine.store.transaction():
        task,run,session,config=engine._start_run(task,1,[])
        # An explicit synthetic checkpoint is enough for this action-only test.
        from branch_agent.records import canonical_bytes
        import hashlib
        contents={'instructions':{'storage':'inline_text','text':'test'},'input_items':{'storage':'inline_json','value':[]},
                  'tool_definitions':{'storage':'inline_json','value':[]}}
        snapshot=engine.store.put(new_record('context_snapshot',pid,task_id=task['id'],run_id=run['id'],session_id=session['id'],
            config_version_id=run['config_version_id'],model=config['model']['name'],reasoning_effort=config['model']['reasoning_effort'],
            **contents,output_schema=engine.workflow.catalog.binding('source_global_events',config.get('schemas')),
            content_sha256=hashlib.sha256(canonical_bytes(contents)).hexdigest(),input_token_estimate=1,input_token_budget=10000))
        measured={'input_tokens':5,'output_tokens':10,'cached_input_tokens':0,'reasoning_tokens':0,'estimated_cost':None if unknown_cost else {'amount':'0.01','currency':'USD'}}
        from branch_agent.records import usage
        call=engine.store.put(new_record('model_call',pid,task_id=task['id'],run_id=run['id'],context_snapshot_id=snapshot['id'],
            operation_id=run['id'],attempt=1,turn_index=1,state='unknown' if remote_unknown else 'failed',usage={**usage(),**measured},
            error={'code':'output_limit_exceeded','message':'截断','retryable':False,'details':{'known_outcome':True,'terminal_status':'incomplete'}}))
        engine._close_run(task,run,'paused',{'code':'output_limit_exceeded','message':'截断','retryable':False,'details':{}})
        task=engine.store.get(task['id'],pid);engine._transition(task,'paused','output_limit_exceeded')
        root=engine.store.get(root['id'],pid);engine._transition(root,'paused','child_blocked')
    return root,task,call,run


def paused_step5_unknown(runtime, *, prior_rejected_candidate=False):
    """An old Step5 has no committed output and one unresolved provider call."""
    import hashlib
    from branch_agent.graph import schema

    engine, _, pid, cid = runtime
    root, task = initial_task(runtime, stage=5)
    with engine.store.transaction():
        knowledge = seed_knowledge_asset(engine, pid)
        contract = schema('adaptation_plan')
        payload = _synthetic(contract['properties']['payload']['anyOf'][0], contract, ref(knowledge))
        payload['source_knowledge_asset_ref'] = ref(knowledge)
        engine.workflow.save(pid, 'adaptation_plan', {
            'result_kind': 'ready', 'payload': payload, 'questions': [],
            'evidence_refs': [ref(knowledge)], 'notes': [],
        }, stage=4, inputs=[ref(knowledge)], effective=True, origin='program')
        materials = engine.workflow.materials(pid, 5)
        if prior_rejected_candidate:
            task, previous, _, _ = engine._start_run(task, 5, materials)
            engine._save_projection(pid, 'run_result', previous['id'],
                {'output': {'result_kind': 'ready', 'payload': {'invalid_anchor': True}}})
            engine._close_run(task, previous, 'paused', {'code': 'source_anchor_ambiguous',
                'message': '候选原文锚点未通过校验', 'retryable': False, 'details': {}})
            task = engine._transition(engine.store.get(task['id'], pid), 'paused', 'source_anchor_ambiguous')
        task, run, session, config = engine._start_run(task, 5, materials)
        contents = {'instructions': {'storage': 'inline_text', 'text': 'test'},
                    'input_items': {'storage': 'inline_json', 'value': []},
                    'tool_definitions': {'storage': 'inline_json', 'value': []}}
        snapshot = engine.store.put(new_record('context_snapshot', pid, task_id=task['id'],
            run_id=run['id'], session_id=session['id'], config_version_id=run['config_version_id'],
            model=config['model']['name'], reasoning_effort=config['model']['reasoning_effort'],
            output_schema=engine.workflow.catalog.binding('game_event_view', config.get('schemas')),
            **contents, content_sha256=hashlib.sha256(canonical_bytes(contents)).hexdigest(),
            input_token_estimate=1, input_token_budget=10000))
        call = engine.store.put(new_record('model_call', pid, task_id=task['id'],
            run_id=run['id'], context_snapshot_id=snapshot['id'], operation_id=str(uuid4()),
            state='unknown', usage=usage(), error={'code': 'CancelledError',
                'message': '本地执行取消，远端结果未知', 'retryable': False, 'details': {}}))
        engine._close_run(task, run, 'paused', {'code': 'active_time_limit',
            'message': '累计活动时间达到上限', 'retryable': False, 'details': {}})
        task = engine._transition(engine.store.get(task['id'], pid), 'paused', 'active_time_limit')
        root = engine._transition(engine.store.get(root['id'], pid), 'paused', 'child_blocked')
    return root, task, call, run


def test_step5_uncertain_rerun_retains_prior_rejected_candidate(runtime):
    engine, _, pid, cid = runtime
    service = ActionService(engine)
    _, task, call, _ = paused_step5_unknown(runtime, prior_rejected_candidate=True)
    prior = next(run for run in all_records(engine.store, pid, 'run', task_id=task['id'])
                 if (run.get('error') or {}).get('code') == 'source_anchor_ambiguous')
    shown = card(service, pid, cid, 'task:' + task['id'])
    assert next(item for item in shown['actions'] if item['id'] == 'rerun_published')['disabled_reason'] is None
    receipt = submit(service, pid, cid, shown, 'rerun_published', {
        'max_cost_usd': '3', 'max_active_seconds': 18000,
        'accept_unknown_model_outcome_and_cost': True})
    assert receipt['stage_task_id'] != task['id']
    assert engine._projection(pid, 'run_result', prior['id'])['output']['payload']['invalid_anchor'] is True
    assert engine.store.get(call['id'], pid)['state'] == 'unknown'


def test_step5_uncertain_model_can_be_explicitly_rerun_in_new_budget(runtime):
    engine, model, pid, cid = runtime
    service = ActionService(engine)
    root, task, call, run = paused_step5_unknown(runtime)
    old_inputs = [ref(engine.workflow.resolve(pid, kind)) for kind in
                  ('source_global_events', 'source_character_events',
                   'source_knowledge_asset', 'adaptation_plan')]
    shown = card(service, pid, cid, 'task:' + task['id'])
    rerun = next(item for item in shown['actions'] if item['id'] == 'rerun_published')
    assert rerun['disabled_reason'] is None
    assert next(item for item in shown['actions'] if item['id'] == 'resume')['disabled_reason']
    assert next(item for item in shown['actions'] if item['id'] == 'restart')['disabled_reason']
    assert 'accept_unknown_model_outcome_and_cost' in {item['name'] for item in rerun['fields']}
    values = {'max_cost_usd': '3', 'max_active_seconds': 18000}
    with pytest.raises(WorkflowBlocked, match='action_value_required'):
        submit(service, pid, cid, shown, 'rerun_published', values)
    receipt = submit(service, pid, cid, shown, 'rerun_published', {
        **values, 'accept_unknown_model_outcome_and_cost': True})
    newroot = engine.store.get(receipt['task_id'], pid)
    assert newroot['budget_root_task_id'] == newroot['id']
    assert newroot['budget']['max_active_seconds'] == 18000
    assert engine._task_data(newroot)['stages'] == list(range(5, 12))
    assert engine.store.get(call['id'], pid)['state'] == 'unknown'
    assert engine.store.get(call['id'], pid)['usage'] == usage()
    assert engine.store.get(run['id'], pid)['state'] == 'paused'
    assert engine._task_data(root)['superseded_by_task_id'] == newroot['id']
    assert engine._task_data(task)['superseded_by_task_id'] == newroot['id']
    assert engine.store.get(receipt['stage_task_id'], pid)['scope']['stage'] == 5
    assert old_inputs == [ref(engine.workflow.resolve(pid, kind)) for kind in
                          ('source_global_events', 'source_character_events',
                           'source_knowledge_asset', 'adaptation_plan')]
    audit = next(event for event in all_records(engine.store, pid, 'runtime_event')
                 if event['event_name'] == 'recovery.uncertain_model_rerun_authorized')
    assert audit['payload']['old_model_call_refs'] == [ref(call)]
    assert audit['payload']['old_remote_outcome'] == 'unknown'
    assert audit['payload']['old_usage'] == 'unknown'
    assert audit['payload']['source_message_id'] == receipt['message_id']
    assert model.calls == []

    # The replacement completes Step5 and advances directly to Step6. Its
    # Step1–4 inputs stay on their exact old versions.
    from branch_agent.graph import schema
    with engine.store.transaction():
        contract = schema('game_event_view')
        payload = _synthetic(contract['properties']['payload']['anyOf'][0], contract,
                             ref(engine.workflow.resolve(pid, 'source_knowledge_asset')))
        engine.workflow.save(pid, 'game_event_view', {
            'result_kind': 'ready', 'payload': payload, 'questions': [],
            'evidence_refs': [], 'notes': [],
        }, stage=5, inputs=old_inputs, effective=True, origin='program')
        child = engine.store.get(receipt['stage_task_id'], pid)
        engine._transition(child, 'succeeded')
        engine._advance_root(newroot)
    replacement_children = all_records(engine.store, pid, 'task', parent_task_id=newroot['id'])
    assert {item['scope']['stage'] for item in replacement_children} == {5, 6}
    assert next(item for item in replacement_children if item['scope']['stage'] == 6)['state'] == 'queued'
    assert old_inputs == [ref(engine.workflow.resolve(pid, kind)) for kind in
                          ('source_global_events', 'source_character_events',
                           'source_knowledge_asset', 'adaptation_plan')]


def test_step5_uncertain_rerun_still_requires_ack_when_cost_gate_is_off(runtime):
    engine, _, pid, cid = runtime
    _, task, call, _ = paused_step5_unknown(runtime)
    engine.cost_gates_enabled = False
    service = ActionService(engine)
    shown = card(service, pid, cid, 'task:' + task['id'])
    fields = {item['name'] for item in next(action for action in shown['actions']
              if action['id'] == 'rerun_published')['fields']}
    assert fields == {'max_active_seconds', 'accept_unknown_model_outcome_and_cost'}
    receipt = submit(service, pid, cid, shown, 'rerun_published', {
        'max_active_seconds': 18000, 'accept_unknown_model_outcome_and_cost': True})
    newroot = engine.store.get(receipt['task_id'], pid)
    assert newroot['budget']['max_active_seconds'] == 18000
    assert newroot['budget']['max_cost'] is None
    assert engine.store.get(call['id'], pid)['state'] == 'unknown'


def test_acknowledged_old_unknown_does_not_block_later_recovery(runtime):
    engine, _, pid, cid = runtime
    service = ActionService(engine)
    _, task, old_call, _ = paused_step5_unknown(runtime)
    receipt = submit(service, pid, cid, card(service, pid, cid, 'task:' + task['id']),
                     'rerun_published', {'max_cost_usd': '3', 'max_active_seconds': 18000,
                                         'accept_unknown_model_outcome_and_cost': True})
    with engine.store.transaction():
        child = engine.store.get(receipt['stage_task_id'], pid)
        engine._transition(child, 'paused', 'repair_exhausted')
        newroot = engine.store.get(receipt['task_id'], pid)
        engine._transition(newroot, 'paused', 'child_blocked')
    later = card(service, pid, cid, 'task:' + child['id'])
    assert next(item for item in later['actions'] if item['id'] == 'rerun_published')['disabled_reason'] is None
    assert engine.store.get(old_call['id'], pid)['state'] == 'unknown'

    with engine.store.transaction():
        tool = engine.store.put(new_record('tool_call', pid, task_id=task['id'],
            run_id=old_call['run_id'], model_call_id=old_call['id'],
            operation_id=str(uuid4()), provider_tool_call_id='late-tool',
            tool_name='read_record', arguments={'storage': 'inline_json', 'value': {}},
            state='unknown', started_at=engine.store.now()))
    tool_blocked = card(service, pid, cid, 'task:' + child['id'])
    assert next(item for item in tool_blocked['actions'] if item['id'] == 'rerun_published')['disabled_reason']
    with engine.store.transaction():
        update(engine.store, tool, state='succeeded', finished_at=engine.store.now())
    assert next(item for item in card(service, pid, cid, 'task:' + child['id'])['actions']
                if item['id'] == 'rerun_published')['disabled_reason'] is None

    # Another, unacknowledged model request in the archived Run is still a
    # live uncertainty for future actions.
    with engine.store.transaction():
        engine.store.put(new_record('model_call', pid, task_id=task['id'],
            run_id=old_call['run_id'], context_snapshot_id=old_call['context_snapshot_id'],
            operation_id=str(uuid4()), state='unknown', usage=usage(),
            error={'code': 'CancelledError', 'message': '另一笔远端结果未知',
                   'retryable': False, 'details': {}}))
    blocked = card(service, pid, cid, 'task:' + child['id'])
    assert next(item for item in blocked['actions'] if item['id'] == 'rerun_published')['disabled_reason']


def test_step5_rerun_keeps_only_authorized_later_stages(runtime):
    engine, _, pid, cid = runtime
    service = ActionService(engine)
    root, task, _, _ = paused_step5_unknown(runtime)
    with engine.store.transaction():
        data = engine._task_data(root)
        data['stages'] = [1, 2, 3, 4, 5, 7]
        engine._save_task_data(root, data)
    receipt = submit(service, pid, cid, card(service, pid, cid, 'task:' + task['id']),
                     'rerun_published', {'max_cost_usd': '3', 'max_active_seconds': 18000,
                                         'accept_unknown_model_outcome_and_cost': True})
    newroot = engine.store.get(receipt['task_id'], pid)
    assert engine._task_data(newroot)['stages'] == [5, 7]


def test_step5_uncertain_waiver_freezes_existing_unrelated_queue(runtime):
    engine, _, pid, cid = runtime
    service = ActionService(engine)
    _, task, old_call, _ = paused_step5_unknown(runtime)
    message = engine.submit_message(pid, cid, '稍后处理的另一项请求')
    queued = next(item for item in all_records(engine.store, pid, 'queued_request')
                  if item['source_message_id'] == message['id'])
    assert queued['state'] == 'pending'
    assert next(item for item in card(service, pid, cid, 'queue:' + queued['id'])['actions']
                if item['id'] == 'release_one')['disabled_reason']
    receipt = submit(service, pid, cid, card(service, pid, cid, 'task:' + task['id']),
                     'rerun_published', {'max_cost_usd': '3', 'max_active_seconds': 18000,
                                         'accept_unknown_model_outcome_and_cost': True})
    assert engine.store.get(queued['id'], pid)['state'] == 'blocked'
    assert engine.store.get(queued['id'], pid)['blocked_reason'] == 'queue_hold'
    event = next(item for item in all_records(engine.store, pid, 'runtime_event')
                 if item['event_name'] == 'recovery.uncertain_model_rerun_authorized')
    assert event['payload']['frozen_queue_request_ids'] == [queued['id']]
    assert engine.store.get(old_call['id'], pid)['state'] == 'unknown'
    assert next(item for item in card(service, pid, cid, 'queue:' + queued['id'])['actions']
                if item['id'] == 'release_one')['disabled_reason'] is None
    assert engine.store.get(receipt['stage_task_id'], pid)['state'] == 'queued'
    assert engine._control(pid, cid)['holds'] == []
    new_message = engine.submit_message(pid, cid, '恢复后新提交的请求')
    new_request = next(item for item in all_records(engine.store, pid, 'queued_request')
                       if item['source_message_id'] == new_message['id'])
    assert new_request['state'] == 'pending'


@pytest.mark.parametrize('blocker', ['unresolved_tool', 'saved_response', 'saved_artifact', 'queued_sibling'])
def test_step5_uncertain_rerun_rejects_other_live_or_saved_work(runtime, blocker):
    engine, _, pid, cid = runtime
    service = ActionService(engine)
    root, task, call, run = paused_step5_unknown(runtime)
    with engine.store.transaction():
        if blocker == 'unresolved_tool':
            engine.store.put(new_record('tool_call', pid, task_id=task['id'], run_id=run['id'],
                model_call_id=call['id'], operation_id=str(uuid4()), provider_tool_call_id='tool-1',
                tool_name='read_record', arguments={'storage': 'inline_json', 'value': {}},
                state='running', started_at=engine.store.now()))
        elif blocker == 'saved_response':
            engine._save_projection(pid, 'run_result', run['id'],
                                    {'output': {'result_kind': 'ready', 'payload': {}}})
        elif blocker == 'saved_artifact':
            from branch_agent.graph import schema
            contract = schema('game_event_view')
            payload = _synthetic(contract['properties']['payload']['anyOf'][0], contract,
                                 ref(engine.workflow.resolve(pid, 'source_knowledge_asset')))
            engine.workflow.save(pid, 'game_event_view', {
                'result_kind': 'ready', 'payload': payload, 'questions': [],
                'evidence_refs': [], 'notes': [],
            }, stage=5, run=run, inputs=run['input_refs'])
        else:
            engine._dispatch(root, 4)
    shown = card(service, pid, cid, 'task:' + task['id'])
    assert next(item for item in shown['actions'] if item['id'] == 'rerun_published')['disabled_reason']
    with pytest.raises(WorkflowBlocked, match='action_unavailable'):
        submit(service, pid, cid, shown, 'rerun_published', {
            'max_cost_usd': '3', 'max_active_seconds': 18000,
            'accept_unknown_model_outcome_and_cost': True})
    assert engine.store.get(call['id'], pid)['state'] == 'unknown'


def test_restart_requires_unknown_cost_acceptance_and_explicit_new_budget(runtime):
    engine,model,pid,cid=runtime;service=ActionService(engine);root,task,call,oldrun=failed_output(runtime)
    shown=card(service,pid,cid,'task:'+task['id'])
    with pytest.raises(WorkflowBlocked,match='action_value_required'):
        submit(service,pid,cid,shown,'restart',{'max_cost_usd':'3','max_active_seconds':600})
    receipt=submit(service,pid,cid,shown,'restart',{'accept_unknown_cost':True,'max_cost_usd':'3','max_active_seconds':600})
    newroot=engine.store.get(receipt['task_id'],pid)
    assert newroot['budget_root_task_id']==newroot['id'] and newroot['budget']['max_cost']['amount']=='3'
    assert engine.store.get(call['id'],pid)['usage']['estimated_cost'] is None
    assert engine.store.get(oldrun['id'],pid)['config_version_id']==oldrun['config_version_id']
    assert engine._task_data(root)['superseded_by_task_id']==newroot['id']
    children=engine.store.list(pid,'task',filters={'parent_task_id':newroot['id']})
    assert len(children)==1 and children[0]['scope']['stage']==1
    assert engine._task_data(children[0])['config_version_id']!=oldrun['config_version_id']
    assert model.calls==[]


def test_unknown_remote_disables_restart_without_unholding(runtime):
    engine,_,pid,cid=runtime;service=ActionService(engine);root,task,_,_=failed_output(runtime,remote_unknown=True)
    before=deepcopy(engine._control(pid,cid))
    shown=card(service,pid,cid,'task:'+task['id'])
    assert next(a for a in shown['actions'] if a['id']=='restart')['disabled_reason']
    with pytest.raises(WorkflowBlocked,match='action_unavailable'):
        submit(service,pid,cid,shown,'restart',{'accept_unknown_cost':True,'max_cost_usd':'3','max_active_seconds':600})
    assert engine._control(pid,cid)==before


def test_recovery_card_lists_both_step1_views_with_source_and_validity(runtime):
    engine, _, pid, cid = runtime
    service = ActionService(engine)
    _, task, _, _ = failed_output(runtime, unknown_cost=False)
    source = engine.workflow.resolve(pid, 'source_text')
    with engine.store.transaction():
        for kind in ('source_global_events', 'source_character_events'):
            engine.workflow.save(pid, kind, split_source_output(kind, ref(source)),
                                 stage=1, inputs=[ref(source)], effective=True)
    shown = card(service, pid, cid, 'task:' + task['id'])
    details = {item['label']: item['value'] for item in shown['details']}
    assert '原作 v1' in details['Step1 全局事件']
    assert '来源仍有效' in details['Step1 全局事件']
    assert '当前生效' in details['Step1 全局事件']
    assert '原作 v1' in details['Step1 主要人物事件']
    assert '来源仍有效' in details['Step1 主要人物事件']
    assert '历史 Step1 合并分段' not in details
    with engine.store.transaction():
        engine.workflow.save(pid, 'source_text', '甲见乙后又见丙。', effective=True)
    updated = {item['label']: item['value'] for item in card(service, pid, cid,
               'task:' + task['id'])['details']}
    assert '上游已变化' in updated['Step1 全局事件']
    assert '上游已变化' in updated['Step1 主要人物事件']


def test_queue_actions_affect_only_selected_request_and_keep_holds(runtime):
    engine,model,pid,cid=runtime;service=ActionService(engine);root,task,_,_=failed_output(runtime,unknown_cost=False)
    engine.submit_message(pid,cid,'先查看进度');engine.submit_message(pid,cid,'稍后处理')
    queues=engine.store.list(pid,'queued_request')
    with engine.store.transaction():
        for q in queues:update(engine.store,q,state='blocked',blocked_reason='queue_hold')
    before=deepcopy(engine._control(pid,cid)['holds'])
    first=queues[0];second=queues[1]
    submit(service,pid,cid,card(service,pid,cid,'queue:'+first['id']),'release_one')
    assert engine.store.get(first['id'],pid)['state']=='pending'
    assert engine.store.get(second['id'],pid)['state']=='blocked'
    assert engine._control(pid,cid)['holds']==before
    submit(service,pid,cid,card(service,pid,cid,'queue:'+second['id']),'cancel')
    assert engine.store.get(second['id'],pid)['state']=='cancelled'
    assert model.calls==[]


def test_report_usage_preserves_raw_unknown_and_records_source(runtime):
    engine,_,pid,cid=runtime;service=ActionService(engine);_,task,call,_=failed_output(runtime)
    result=submit(service,pid,cid,card(service,pid,cid,'task:'+task['id']),'report_usage',
        {'model_call_id':call['id'],'reported_cost_usd':'0.25','source':'供应商账单，response对应记录#1'})
    current=engine.store.get(call['id'],pid)
    assert current['usage']['reported_cost']=={'amount':'0.25','currency':'USD'}
    assert current['usage']['estimated_cost'] is None
    event=engine.store.get(result['event_id'],pid)
    assert event['payload']['source_message_id']==result['message_id']


def test_fresh_restart_rebuilds_step_one_even_when_previous_effective_exists(runtime):
    engine,model,pid,cid=runtime;service=ActionService(engine);root,task,_,_=failed_output(runtime,unknown_cost=False)
    with engine.store.transaction():
        seed_knowledge_asset(engine,pid)
    receipt=submit(service,pid,cid,card(service,pid,cid,'task:'+task['id']),'restart',{'max_cost_usd':'2','max_active_seconds':600})
    model.responses=[source_response]
    asyncio.run(engine.tick(pid,cid))
    assert len(model.calls) == 2 and all(call.startswith('step1') for call in model.calls)
    children=engine.store.list(pid,'task',filters={'parent_task_id':receipt['task_id']})
    assert any(t['scope']['stage']==1 and t['state']=='succeeded' for t in children)


def test_legacy_manager_restart_migrates_to_harness_dispatch(runtime):
    engine, _, pid, cid = runtime
    service = ActionService(engine)
    root, child = initial_task(runtime)
    with engine.store.transaction():
        data = engine._task_data(root)
        data['manager_controlled'] = True
        engine._save_task_data(root, data)
        engine._transition(child, 'paused', 'repair_exhausted')
        engine._transition(root, 'paused', 'child_blocked')
    shown = card(service, pid, cid, 'task:' + child['id'])
    receipt = submit(service, pid, cid, shown, 'restart',
                     {'max_cost_usd': '2', 'max_active_seconds': 600})
    replacement = engine.store.get(receipt['task_id'], pid)
    assert engine._task_data(replacement)['manager_controlled'] is True
    assert engine._task_data(replacement)['harness_scheduled'] is True
    assert engine._task_data(replacement)['recovery_stage'] == 1
    stage = engine.store.get(receipt['stage_task_id'], pid)
    assert stage['parent_task_id'] == replacement['id']
    assert stage['scope']['stage'] == 1
    assert not engine._task_data(stage).get('parent_owned')
    assert not [item for item in engine.store.list(pid, 'queued_request', filters={'conversation_id': cid})
                if engine._projection(pid, 'queue_context', item['id']).get('manager_resume')]


def test_legacy_manager_resume_migrates_stage_to_harness(runtime):
    engine, _, pid, cid = runtime
    service = ActionService(engine)
    root, child = initial_task(runtime)
    with engine.store.transaction():
        data = engine._task_data(root)
        data['manager_controlled'] = True
        engine._save_task_data(root, data)
        child_data = engine._task_data(child)
        child_data['parent_owned'] = True
        engine._save_task_data(child, child_data)
        engine._transition(child, 'paused', 'repair_exhausted')
        engine._transition(root, 'paused', 'child_blocked')
    shown = card(service, pid, cid, 'task:' + root['id'])
    submit(service, pid, cid, shown, 'resume')
    assert engine.store.get(child['id'], pid)['state'] == 'queued'
    assert engine._task_data(engine.store.get(root['id'], pid))['harness_scheduled'] is True
    assert not engine._task_data(child).get('parent_owned')
    assert not [item for item in engine.store.list(pid, 'queued_request', filters={'conversation_id': cid})
                if engine._projection(pid, 'queue_context', item['id']).get('manager_resume')]


def test_legacy_manager_root_without_child_resumes_by_scheduling_step1(runtime):
    engine, _, pid, cid = runtime
    service = ActionService(engine)
    imported = engine.import_source(pid, cid, '甲见乙。', '原作')
    with engine.store.transaction():
        seed_knowledge_asset(engine, pid)
        message = engine._message(pid, cid, '开始改编', role='user')
        root = engine._new_task(pid, cid, message, 'generate', is_workflow=True,
                                stages=[1], request='开始改编', source_ref=ref(imported['source']),
                                manager_controlled=True)
        engine._transition(root, 'paused', 'manager_no_progress')

    shown = card(service, pid, cid, 'task:' + root['id'])
    receipt = submit(service, pid, cid, shown, 'resume')
    assert receipt['task_id'] == root['id']
    current = engine.store.get(root['id'], pid)
    assert engine._task_data(current)['harness_scheduled'] is True
    assert engine._task_data(current)['fresh_start'] is True
    children = engine.store.list(pid, 'task', filters={'parent_task_id': root['id']})
    assert len(children) == 1
    assert children[0]['scope']['stage'] == 1 and children[0]['state'] == 'queued'
    assert not engine._task_data(children[0]).get('parent_owned')
    assert not [item for item in engine.store.list(pid, 'queued_request', filters={'conversation_id': cid})
                if engine._projection(pid, 'queue_context', item['id']).get('manager_resume')]


def test_legacy_manager_stage_rerun_dispatches_exact_failed_stage(runtime):
    engine, _, pid, cid = runtime
    service = ActionService(engine)
    root, child = initial_task(runtime, stage=2)
    with engine.store.transaction():
        seed_knowledge_asset(engine, pid)
        data = engine._task_data(root)
        data['manager_controlled'] = True
        engine._save_task_data(root, data)
        engine._transition(child, 'paused', 'repair_exhausted')
        engine._transition(root, 'paused', 'child_blocked')

    shown = card(service, pid, cid, 'task:' + child['id'])
    assert not next(a for a in shown['actions'] if a['id'] == 'rerun_published')['disabled_reason']
    receipt = submit(service, pid, cid, shown, 'rerun_published',
                     {'max_cost_usd': '2', 'max_active_seconds': 600})
    replacement = engine.store.get(receipt['task_id'], pid)
    stage = engine.store.get(receipt['stage_task_id'], pid)
    assert engine._task_data(replacement)['harness_scheduled'] is True
    assert engine._task_data(replacement)['recovery_stage'] == 2
    assert stage['parent_task_id'] == replacement['id']
    assert stage['scope']['stage'] == 2 and stage['state'] == 'queued'
    assert not engine._task_data(stage).get('parent_owned')


def test_harness_scheduled_restart_dispatches_step1_without_manager_resume(runtime):
    engine, _, pid, cid = runtime
    service = ActionService(engine)
    root, child = initial_task(runtime)
    with engine.store.transaction():
        data = engine._task_data(root)
        data.update(manager_controlled=True, harness_scheduled=True)
        engine._save_task_data(root, data)
        engine._transition(child, 'paused', 'repair_exhausted')
        engine._transition(root, 'paused', 'child_blocked')

    receipt = submit(service, pid, cid, card(service, pid, cid, 'task:' + child['id']),
                     'restart', {'max_cost_usd': '2', 'max_active_seconds': 600})
    replacement = engine.store.get(receipt['task_id'], pid)
    stage = engine.store.get(receipt['stage_task_id'], pid)
    assert engine._task_data(replacement)['harness_scheduled'] is True
    assert engine._task_data(replacement)['manager_controlled'] is True
    assert stage['parent_task_id'] == replacement['id']
    assert stage['scope']['stage'] == 1
    assert not engine._task_data(stage).get('parent_owned')
    assert not [q for q in engine.store.list(pid, 'queued_request')
                if engine._projection(pid, 'queue_context', q['id']).get('manager_resume')]


def test_harness_scheduled_resume_keeps_stage_executable_without_manager_resume(runtime):
    engine, _, pid, cid = runtime
    service = ActionService(engine)
    root, child = initial_task(runtime)
    with engine.store.transaction():
        data = engine._task_data(root)
        data.update(manager_controlled=True, harness_scheduled=True)
        engine._save_task_data(root, data)
        engine._transition(child, 'paused', 'repair_exhausted')
        engine._transition(root, 'paused', 'child_blocked')

    submit(service, pid, cid, card(service, pid, cid, 'task:' + child['id']), 'resume')
    resumed = engine.store.get(child['id'], pid)
    assert resumed['state'] == 'queued'
    assert engine._ancestors_allow(resumed)
    assert not engine._task_data(resumed).get('parent_owned')
    assert not [q for q in engine.store.list(pid, 'queued_request')
                if engine._projection(pid, 'queue_context', q['id']).get('manager_resume')]


def test_harness_scheduled_answer_requeues_stage_without_parent_ownership(runtime):
    engine, _, pid, cid = runtime
    service = ActionService(engine)
    child = pending_question(runtime, ('唯一问题',))
    root = engine.store.get(child['parent_task_id'], pid)
    with engine.store.transaction():
        data = engine._task_data(root)
        data.update(manager_controlled=True, harness_scheduled=True)
        engine._save_task_data(root, data)
        engine._transition(root, 'waiting_user')

    item = engine._task_data(child)['pending_user_items'][0]
    submit(service, pid, cid, card(service, pid, cid, 'pending:' + item['id']),
           'answer', {'answer': '保留'})
    assert engine.store.get(child['id'], pid)['state'] == 'queued'
    assert engine.store.get(root['id'], pid)['state'] == 'running'
    assert not engine._task_data(child).get('parent_owned')
    assert not [q for q in engine.store.list(pid, 'queued_request')
                if engine._projection(pid, 'queue_context', q['id']).get('manager_resume')]


def test_harness_scheduled_stage_revision_is_executable_without_manager_resume(runtime):
    engine, _, pid, cid = runtime
    service = ActionService(engine)
    child, _ = pending_confirmation(runtime)
    root = engine.store.get(child['parent_task_id'], pid)
    with engine.store.transaction():
        data = engine._task_data(root)
        data.update(manager_controlled=True, harness_scheduled=True)
        engine._save_task_data(root, data)
        engine._transition(root, 'waiting_user')

    item = engine._task_data(child)['pending_user_items'][0]
    receipt = submit(service, pid, cid, card(service, pid, cid, 'pending:' + item['id']),
                     'request_changes', {'text': '调整人物关系'})
    replacement = engine.store.get(receipt['task_id'], pid)
    assert replacement['parent_task_id'] == root['id']
    assert replacement['state'] == 'queued'
    assert engine.store.get(root['id'], pid)['state'] == 'running'
    assert not engine._task_data(replacement).get('parent_owned')
    assert not [q for q in engine.store.list(pid, 'queued_request')
                if engine._projection(pid, 'queue_context', q['id']).get('manager_resume')]


def test_resume_keeps_frozen_config_and_budget_without_silent_increment(runtime):
    engine,model,pid,cid=runtime;service=ActionService(engine);root,task,_,oldrun=failed_output(runtime,unknown_cost=False)
    budget_before=deepcopy(engine.store.get(root['id'],pid)['budget'])
    result=submit(service,pid,cid,card(service,pid,cid,'task:'+task['id']),'resume')
    assert result['task_id']==task['id']
    assert engine.store.get(root['id'],pid)['budget']==budget_before
    assert engine._task_data(task)['config_version_id']==oldrun['config_version_id']
    assert engine.store.get(root['id'],pid)['state']=='queued'
    assert engine._ancestors_allow(engine.store.get(task['id'],pid))
    assert not engine._control(pid,cid)['holds']
    assert not model.calls


def test_selected_queue_reaches_coordinator_while_other_request_stays_blocked(runtime):
    engine,model,pid,cid=runtime;service=ActionService(engine);_,task,_,_=failed_output(runtime,unknown_cost=False)
    engine.submit_message(pid,cid,'仅查询进度');engine.submit_message(pid,cid,'另一个请求')
    queues=engine.store.list(pid,'queued_request')
    with engine.store.transaction():
        for q in queues:update(engine.store,q,state='blocked',blocked_reason='queue_hold')
    first,second=queues
    before=deepcopy(engine._control(pid,cid)['holds'])
    submit(service,pid,cid,card(service,pid,cid,'queue:'+first['id']),'release_one')
    model.responses=[{'result_kind':'ready','payload':{'reply':'当前阶段暂停','source_message_kind':'request',
        'task_requests':[]},'questions':[],'evidence_refs':[],'notes':[]}]
    asyncio.run(engine.tick(pid,cid))
    assert model.calls==['coordinator']
    assert engine.store.get(first['id'],pid)['state']=='dispatched'
    assert engine.store.get(second['id'],pid)['state']=='blocked'
    assert engine._control(pid,cid)['holds']==before


def test_superseded_old_task_cannot_be_resumed_via_legacy_controls(runtime):
    engine,_,pid,cid=runtime;service=ActionService(engine);root,task,_,_=failed_output(runtime,unknown_cost=False)
    submit(service,pid,cid,card(service,pid,cid,'task:'+task['id']),'restart',{'max_cost_usd':'2','max_active_seconds':600})
    with pytest.raises(WorkflowBlocked,match='task_superseded'):
        engine.control_task(pid,root['id'],'continue')


def test_request_changes_preserves_confirmation_as_cancelled_without_approval(runtime):
    engine,model,pid,cid=runtime;service=ActionService(engine);task,version=pending_confirmation(runtime)
    item=engine._task_data(task)['pending_user_items'][0]
    receipt=submit(service,pid,cid,card(service,pid,cid,'pending:'+item['id']),'request_changes',{'text':'将主题改成重逢'})
    assert not engine.store.list(pid,'confirmation')
    assert engine._task_data(task)['pending_user_items'][0]['state']=='cancelled'
    replacement=engine.store.get(receipt['task_id'],pid)
    assert replacement['budget_root_task_id']==task['budget_root_task_id']
    assert replacement['requested_by_message_id']==receipt['message_id']
    assert engine.workflow.state(version)['confirmation_status']=='unconfirmed'
    assert not model.calls


def test_coordinator_answer_reuses_actual_message_and_existing_budget_config(runtime):
    engine,model,pid,cid=runtime;service=ActionService(engine)
    engine.submit_message(pid,cid,'帮我处理')
    model.responses=[{'result_kind':'needs_input','payload':None,'questions':[{'question_id':'intent',
        'prompt':'具体需要做什么？','reason':'确定范围','target_field':None,'suggested_answers':['仅查询'],
        'blocking_scope':'当前请求'}],'evidence_refs':[],'notes':[]}]
    asyncio.run(engine.tick(pid,cid))
    pending=engine.status(pid)['pending_user_items'][0]
    previous=engine.store.get(pending['task_id'],pid)
    fixed=engine._task_data(previous)['config_version_id']
    result=submit(service,pid,cid,card(service,pid,cid,'pending:'+pending['id']),'answer',{'answer':'仅查询','text':''})
    model.responses=[{'result_kind':'ready','payload':{'reply':'没有进行创作。','task_requests':[]},'questions':[],'evidence_refs':[],'notes':[]}]
    asyncio.run(engine.tick(pid,cid))
    child=next(t for t in engine.status(pid)['tasks'] if t['parent_task_id']==previous['id'])
    assert child['requested_by_message_id']==result['message_id']
    assert child['budget_root_task_id']==previous['budget_root_task_id']
    assert engine._task_data(child)['config_version_id']==fixed
    assert engine.store.get(previous['id'],pid)['budget']==previous['budget']


def test_usage_pause_can_resume_only_after_actual_cost_is_recorded(runtime):
    engine,_,pid,cid=runtime;service=ActionService(engine);root,task,call,_=failed_output(runtime)
    with engine.store.transaction():
        engine._transition(engine.store.get(task['id'],pid),'paused','usage_uncertain')
    shown=card(service,pid,cid,'task:'+task['id'])
    assert next(a for a in shown['actions'] if a['id']=='resume')['disabled_reason']
    submit(service,pid,cid,shown,'report_usage',{'model_call_id':call['id'],'reported_cost_usd':'0.25','source':'实际账单#25'})
    shown=card(service,pid,cid,'task:'+task['id'])
    assert next(a for a in shown['actions'] if a['id']=='resume')['disabled_reason'] is None
    submit(service,pid,cid,shown,'resume')
    assert engine.store.get(task['id'],pid)['state']=='queued'
    engine._budget_check(engine.store.get(task['id'],pid))


def test_completed_workflow_hides_archived_child_failure_card(runtime):
    engine,_,pid,cid=runtime;root,task,_,_=failed_output(runtime,unknown_cost=False)
    with engine.store.transaction():
        update(engine.store,engine.store.get(root['id'],pid),state='succeeded',pause_reason=None)
    assert not any(c['id']=='task:'+task['id'] for c in ActionService(engine).list_cards(pid,cid)['cards'])
