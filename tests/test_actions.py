"""Structured user actions use isolated PostgreSQL and never call a live model."""
from copy import deepcopy
import asyncio

import pytest

from branch_agent.actions import ActionService
from branch_agent.records import new_record
from branch_agent.workflow import body, ref, update, WorkflowBlocked, WHOLE
from test_runtime import runtime, source_response


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
        source=engine.workflow.resolve(pid,'source_text')
        result={'result_kind':'ready','payload':{'source_views_ref':ref(source),'premise':'相遇','world_rules':[],
            'themes':[],'conflicts':[],'characters':[],'key_event_refs':[],'preservation_items':[]},'questions':[],'evidence_refs':[],'notes':[]}
        version=engine.workflow.save(pid,'source_analysis',result,stage=2)
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
            **contents,output_schema=engine.workflow.catalog.binding('source_views',config.get('schemas')),
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
    source=engine.workflow.resolve(pid,'source_text')
    engine.workflow.save(pid,'source_views',source_response(task,[{'schema_id':'source_text','content':body(engine.store,source),'ref':ref(source)}]),stage=1,effective=True)
    receipt=submit(service,pid,cid,card(service,pid,cid,'task:'+task['id']),'restart',{'max_cost_usd':'2','max_active_seconds':600})
    model.responses=[source_response]
    asyncio.run(engine.tick(pid,cid))
    assert model.calls==['step1']
    children=engine.store.list(pid,'task',filters={'parent_task_id':receipt['task_id']})
    assert any(t['scope']['stage']==1 and t['state']=='succeeded' for t in children)


def test_manager_restart_returns_dispatch_to_coordinator(runtime):
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
    assert engine._task_data(replacement)['recovery_stage'] == 1
    assert not engine.store.list(pid, 'task', filters={'parent_task_id': replacement['id']})
    pending = [item for item in engine.store.list(pid, 'queued_request', filters={'conversation_id': cid})
               if item['state'] == 'pending']
    assert len(pending) == 1
    assert engine._projection(pid, 'queue_context', pending[0]['id'])['manager_resume'] is True


def test_manager_resume_leaves_stage_for_manager_tool(runtime):
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
    submit(service, pid, cid, shown, 'resume')
    assert engine.store.get(child['id'], pid)['state'] == 'queued'
    assert engine._task_data(child)['parent_owned'] is True
    pending = [item for item in engine.store.list(pid, 'queued_request', filters={'conversation_id': cid})
               if item['state'] == 'pending']
    assert len(pending) == 1
    assert engine._projection(pid, 'queue_context', pending[0]['id'])['manager_resume'] is True


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
