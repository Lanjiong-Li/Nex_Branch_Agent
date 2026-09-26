"""Archive-backed, transactional working-session compaction.

At a new Runner boundary, closed tool chains may be summarized even when the
user request still awaits its final answer. Original HistoryRecord and old
SessionItem generations remain readable; a summary cannot confirm business state.
"""
from __future__ import annotations
from copy import deepcopy
import json
import time
import uuid
from .context import BudgetExceeded, input_budget, tokens, unwrap
from .records import new_record, now_utc
from .schemas import digest
from .prompts import stage_agent


def event(store, project, name, payload, task=None, run=None, conversation=None):
    with store.transaction():
        store.advisory_lock(f'{project}:event_sequence')
        key=f'{project}:sequence:events'
        seq=store.projection_get(key) or {'project_id':project,'value':0}
        seq['value']+=1;store.projection_put(key,seq)
        return store.put(new_record('runtime_event',project,sequence=seq['value'],event_name=name,
            task_id=task,run_id=run,conversation_id=conversation,payload=payload))


def request_groups(items):
    groups=[]
    for item in items:
        if not groups or item.get('role')=='user':groups.append([])
        groups[-1].append(item)
    return groups


def complete_tools(items):
    calls={i['call_id'] for i in items if i.get('type')=='function_call'}
    results={i['call_id'] for i in items if i.get('type')=='function_call_output'}
    return calls==results


def clean_archived(item):
    # Opaque provider continuation state is meaningful to the Responses API,
    # not prose the summarizer can interpret. Keep its fixed archive reference
    # and actual public summary; never mutate the stored SDK item or response.
    if isinstance(item,dict) and item.get('type')=='reasoning':
        return {key:value for key,value in item.items() if key!='encrypted_content'}
    if isinstance(item,dict) and item.get('role')=='user' and isinstance(item.get('content'),str):
        try:
            body=json.loads(item['content'])
            if isinstance(body,dict) and '_harness_materials' in body:
                return {**item,'content':body['request']}
        except (ValueError,KeyError):pass
    return item


def compaction_layout(items, config, instructions, tool_defs, output_schema, request):
    """Plan at the next Runner boundary without deleting or truncating any item."""
    model=config['model']['name'];cap=input_budget(config)
    def size(history):
        return tokens({'instructions':instructions,'input':history+[{'role':'user','content':request}],
            'tools':tool_defs,'output_schema':output_schema},model)
    total=size(items);history_size=tokens(items,model);groups=request_groups(items)
    keep=min(len(groups),max(1,config['context']['recent_turns']))
    fixed=max(0,total-history_size)
    target=max(0,min(config['context']['history_token_cap'],int(cap*config['compaction']['target_ratio'])-fixed))
    reserve=config['summary']['target_tokens']*2
    for item in items:
        if item.get('role')=='user' and isinstance(item.get('content'),str):
            try:
                if isinstance(json.loads(item['content']),dict) and 'working_summary' in json.loads(item['content']):
                    reserve=max(reserve,tokens(item,model))
            except ValueError:pass
    while keep>1 and tokens([i for g in groups[-keep:] for i in g],model)+config['summary']['target_tokens']>target:
        keep-=1
    tail=[i for g in groups[-keep:] for i in g] if keep else []
    tail_input_tokens=size(tail)
    # Keeping a large recent tool result must not force repeated summaries of
    # only the older prefix. The original current request is separately resent.
    if keep and tail_input_tokens+reserve>cap and complete_tools(items):
        keep=0;tail=[]
    prefix=[i for g in (groups[:-keep] if keep else groups) for i in g]
    return {'prefix':prefix,'tail':tail,'keep':keep,'total':total,'history_tokens':history_size,
        'fixed_input_tokens':size([]),'tail_input_tokens':tail_input_tokens,
        'summary_reserve_tokens':reserve,'input_budget':cap}


def summary_problem(service,pid,value,cfg,covered):
    """Plain summaries have no model-authored schema or evidence envelope."""
    if not isinstance(value,str) or not value.strip():
        return {'code':'summary_empty','message':'摘要正文为空'}
    return None


def saved_summary_page(service,parent,original,rows,config,cfg,archives,seen,previous,plan_hash):
    """Recover only actual provider results bound to the exact next archive range."""
    from .model_service import all_records
    from .prompts import instructions
    store=service.store;pid=parent['project_id'];matches=[]
    remaining=archives[len(seen):]
    children=all_records(store,pid,'task',{'parent_task_id':parent['id']})
    expected_prompt=instructions('aux.summary',cfg)
    schema=None
    for child in children:
        runtime=store.projection_get(f'{pid}:task_runtime:{child["id"]}') or {}
        if not runtime.get('parent_owned') or runtime.get('stage')!='aux.summary':continue
        plans=all_records(store,pid,'runtime_event',{'task_id':child['id'],'event_name':'session.compaction_planned'})
        if plans:
            if not any(e['payload']['plan_hash']==plan_hash for e in plans):continue
        else:
            # Compatibility for pre-checkpoint attempts: the unchanged generation
            # and archived source contents must establish the same fixed plan.
            failures=all_records(store,pid,'runtime_event',{'task_id':child['id'],'event_name':'session.compaction_failed'})
            if not any(e['payload']['session_id']==original['id'] and e['payload']['preserved_generation']==original['generation'] for e in failures):continue
            if any(row['created_at']>child['created_at'] for row in rows):continue
        for run in all_records(store,pid,'run',{'task_id':child['id']}):
            fixed=store.get(run['config_version_id'],pid)
            if digest(fixed['values'])!=digest(cfg):continue
            if not any(digest(store.get(i,pid)['values'])==digest(config) for i in fixed['resolved_from_ids']):continue
            calls=all_records(store,pid,'model_call',{'run_id':run['id']})
            saved=store.projection_get(f'{pid}:run_result:{run["id"]}')
            for call in calls:
                snapshot=store.get(call['context_snapshot_id'],pid)
                if unwrap(snapshot['instructions'],store,pid)!=expected_prompt or snapshot['output_schema'] is not None:continue
                inputs=unwrap(snapshot['input_items'],store,pid)
                try:
                    envelope=json.loads(next(i['content'] for i in reversed(inputs) if i.get('role')=='user'))
                    message=json.loads(envelope['request']);window=message['original_archives']
                except (StopIteration,KeyError,ValueError,TypeError):continue
                if not window or len(window)>len(remaining) or digest(window)!=digest(remaining[:len(window)]):continue
                if message.get('prior_page_summary')!=previous or message.get('required_covered_message_ids')!=seen+[a['record_ref']['record_id'] for a in window]:continue
                if call['state'] in ('pending','running','unknown'):
                    raise BudgetExceeded('summary_result_unknown','该摘要页已有结果未核对的调用，禁止重新发送')
                if call['state']!='succeeded':continue
                if any(call['usage'][k] is None for k in ('input_tokens','output_tokens')) or not (call['usage']['estimated_cost'] or call['usage']['reported_cost']):
                    raise BudgetExceeded('summary_usage_unknown','摘要页用量尚未核对，禁止重复请求')
                if not saved or saved.get('model_call_id')!=call['id'] or saved.get('context_snapshot_id')!=snapshot['id']:continue
                proved=False
                for identity in call['response_history_ids']:
                    raw=unwrap(store.get(identity,pid)['content'],store,pid)
                    try:
                        texts=[p.get('text','') for item in raw['output'] if item.get('type')=='message' for p in item.get('content',[]) if p.get('type')=='output_text']
                        proved=''.join(texts)==saved['output']
                    except (KeyError,TypeError):continue
                    if proved:break
                if proved:matches.append({'run':run,'window':window,'summary':saved['output'],'created_at':call['created_at']})
    return max(matches,key=lambda x:x['created_at']) if matches else None


async def compact_session(service, persistent, stage, config, instructions, tool_defs, output, request, control=None):
    from .model_service import all_records, ref
    if stage=='aux.summary':return
    store=service.store;pid=persistent.task['project_id'];model=config['model']['name']
    items=await persistent.get_items()
    if not items:return
    plan=compaction_layout(items,config,instructions,tool_defs,output.json_schema() if output else None,request)
    total=plan['total'];cap=plan['input_budget'];history_size=plan['history_tokens']
    # A large fixed prompt cannot be reduced by summarizing an already small
    # history. Soft ratios are targets; the complete input hard cap is separate.
    if total<=cap and history_size<=config['context']['history_token_cap'] and plan['fixed_input_tokens']>=cap*config['compaction']['target_ratio']:
        return
    if total<=cap*config['compaction']['trigger_ratio'] and history_size<=config['context']['history_token_cap']:return
    prefix=plan['prefix'];tail=plan['tail']
    if not prefix:return
    if not complete_tools(prefix) or not complete_tools(tail):return
    original=store.get(persistent.session_id,pid)
    rows=all_records(store,pid,'session_item',{'session_id':original['id'],'generation':original['generation']})
    rows.sort(key=lambda r:r['sequence'])
    # get_items performs only a one-to-one material projection, so this is exact.
    if len(rows)!=len(items):return
    old_rows=rows[:len(prefix)];tail_rows=rows[len(prefix):]
    covered=list(dict.fromkeys(i for row in old_rows for i in row['history_ids']))
    previous=original.get('latest_summary_ref')
    if previous:
        old=store.resolve_ref(previous,pid)
        content=unwrap(old.get('content',old),store,pid)
        old_covered=[reference['record_id'] for reference in old.get('source_refs',[])]
        if not old_covered and isinstance(content,dict):
            old_covered=content.get('payload',{}).get('covered_message_ids',[])
        # Do not reread all original archives solely to rewrite the same fixed
        # summary. Wait for another complete group to enter the prefix, unless
        # the full invocation itself exceeds the hard cap.
        if total<=cap and len(prefix)==1 and old_rows[0]['history_ids']==old_covered:
            try:
                if json.loads(prefix[0].get('content','')).get('working_summary')==content:return
            except (ValueError,TypeError,AttributeError):pass
        covered=list(dict.fromkeys(old_covered+covered))
    archives=[]
    for identity in covered:
        row=store.get(identity,pid)
        if not row or row['record_type']!='history_record':raise BudgetExceeded('summary_archive_missing','摘要来源归档缺失，保留原工作历史')
        archives.append({'record_ref':ref(row),'role':row['role'],'content':clean_archived(unwrap(row['content'],store,pid))})
    # All summary pages read original archives, rather than repeatedly summarizing
    # the previous prose. A later page also gets the current summary as continuity.
    cfg=deepcopy(config.get('auxiliary_configs',{}).get('aux.summary',config))
    model=cfg['model']['name']
    from .prompts import instructions as summary_instructions
    plan_state={'project_id':pid,'task_id':persistent.task['id'],'session_id':original['id'],
        'generation':original['generation'],'last_item_seq':original['last_item_seq'],
        'items':[(r['id'],r['history_ids'],digest(r['sdk_item'])) for r in rows],
        'archives_hash':digest(archives),'tail_hash':digest(tail),'parent_config_hash':digest(config),
        'summary_config_hash':digest(cfg),'prompt_hash':digest(summary_instructions('aux.summary',cfg)),
        'schema':None}
    plan_hash=digest(plan_state)
    safe=max(1000,input_budget(cfg)-6000-cfg['summary']['target_tokens']*2)
    if not archives:return
    parent=persistent.task;started=time.monotonic();child=None;summary=None;seen=[]
    try:
        with store.transaction():
            child=store.put(new_record('task',pid,conversation_id=parent['conversation_id'],
                parent_task_id=parent['id'],budget_root_task_id=parent['budget_root_task_id'],
                requested_by_message_id=parent['requested_by_message_id'],intent='summarize',scope=parent['scope'],
                state='running',budget=parent['budget']))
            runtime_key=f'{pid}:task_runtime:{child["id"]}'
            store.projection_put(runtime_key,{'project_id':pid,'stage':'aux.summary','parent_owned':True,'active_ms':0})
            event(store,pid,'session.compaction_planned',{'plan_hash':plan_hash,'plan':plan_state},task=child['id'])
        index=0
        while len(seen)<len(archives):
            if control:
                command=await control()
                if command and command.get('stop'):
                    import asyncio
                    raise asyncio.CancelledError(command.get('reason','stopped'))
            cached=saved_summary_page(service,parent,original,rows,config,cfg,archives,seen,summary,plan_hash)
            window=[];feedback=None
            if cached:
                window=cached['window']
                page_seen=seen+[a['record_ref']['record_id'] for a in window]
                feedback=summary_problem(service,pid,cached['summary'],cfg,page_seen)
                if feedback is None:
                    summary=cached['summary'];seen=page_seen;run=cached['run'];snapshot=store.get(run['config_version_id'],pid)
                    event(store,pid,'session.summary_page_reused',{'plan_hash':plan_hash,'source_run_id':run['id'],
                        'covered_message_ids':seen,'result_hash':digest(summary)},task=child['id'])
                    index+=1;continue
            if not window:
                for archive in archives[len(seen):]:
                    if window and tokens(window+[archive],model)>safe:break
                    window.append(archive)
            local_repairs=0
            while True:
                repair_key=None;repair_round=None
                if feedback:
                    repair_key=digest({'plan_hash':plan_hash,'window':window,'previous':summary})
                    repairs=[e for e in all_records(store,pid,'runtime_event',{'event_name':'session.summary_repair_requested'}) if e['payload'].get('repair_key')==repair_key]
                    dispatched=sum(bool(all_records(store,pid,'model_call',{'run_id':e['payload']['run_id']})) for e in repairs if e['payload'].get('run_id'))
                    used=max(dispatched,local_repairs)
                    if used>=cfg['repair']['max_rounds']:
                        raise BudgetExceeded('summary_repair_exhausted','摘要页校验修复次数已用尽，保留有效页与原工作历史')
                    repair_round=used+1
                with store.transaction():
                    suffix=f'{index}:{uuid.uuid4()}'
                    snapshot=store.put(new_record('config_version',pid,config_key='harness',scope_kind='run_snapshot',
                        scope_key=f'aux.summary:{child["id"]}:{suffix}',state='snapshot',values=cfg,
                        resolved_from_ids=[persistent.run['config_version_id']]))
                    session=store.put(new_record('work_session',pid,conversation_id=parent['conversation_id'],
                        session_key=f'summary:{child["id"]}:{suffix}',scope=parent['scope']))
                    run=store.put(new_record('run',pid,task_id=child['id'],agent_key=stage_agent('aux.summary',cfg),session_id=session['id'],
                        config_version_id=snapshot['id'],state='running',started_at=now_utc(),max_turns=cfg['run']['max_turns']))
                    live=store.get(child['id'],pid);live['current_run_id']=run['id'];store.update(live,live['row_version'])
                    if repair_key:
                        event(store,pid,'session.summary_repair_requested',{'plan_hash':plan_hash,'repair_key':repair_key,
                            'run_id':run['id'],'round':repair_round,'validation_feedback':feedback},task=child['id'],run=run['id'])
                page_seen=seen+[a['record_ref']['record_id'] for a in window]
                message=json.dumps({'task':'请把下列真实历史整理成简洁的纯文本工作摘要。不得改变决策或产物状态。完整工具调用与结果只说明工具已执行，不代表用户请求已有最终结论；如尚未最终答复，摘要必须保留该请求、已完成的工具结论和继续所需信息。当前用户请求将在新Runner中重新提供。不要输出JSON、字段名或证据引用。',
                    'target_tokens':cfg['summary']['target_tokens'],'required_covered_message_ids':page_seen,
                    'prior_page_summary':summary,'original_archives':window,
                    'validation_feedback':feedback},ensure_ascii=False)
                material={'builtin':'runtime.archive_window','content':{'history_refs':[a['record_ref'] for a in window],
                    'decision_refs':[],'artifact_refs':[],'tool_call_refs':[]},'required':True}
                try:
                    page_summary=await service.run('aux.summary',child,run,session,cfg,[material],message,control=control)
                except BudgetExceeded as error:
                    # The preliminary window estimate cannot know the exact size
                    # of current controls, provenance or the escaped SDK envelope.
                    # Only an entirely unsent page may be split and retried.
                    calls=all_records(store,pid,'model_call',{'run_id':run['id']})
                    page_items=all_records(store,pid,'session_item',{'session_id':session['id']})
                    if error.code!='input_budget_exceeded' or calls or page_items:raise
                    if len(window)<=1:
                        raise BudgetExceeded('summary_item_too_large','单条旧历史连同必需材料超过摘要预算，原工作历史保持不变') from error
                    split=max(1,len(window)//2)
                    live=store.get(run['id'],pid);live.update(state='failed',finished_at=now_utc());store.update(live,live['row_version'])
                    window=window[:split]
                    continue
                if feedback:local_repairs+=1
                feedback=summary_problem(service,pid,page_summary,cfg,page_seen)
                if feedback:
                    live=store.get(run['id'],pid);live.update(state='failed',finished_at=now_utc());store.update(live,live['row_version'])
                    event(store,pid,'session.summary_page_invalid',{'plan_hash':plan_hash,'source_run_id':run['id'],
                        'validation_feedback':feedback},task=child['id'])
                    continue
                summary=page_summary;seen=page_seen
                break
            live=store.get(run['id'],pid);live.update(state='succeeded',finished_at=now_utc());store.update(live,live['row_version'])
            event(store,pid,'session.summary_page_validated',{'plan_hash':plan_hash,'source_run_id':run['id'],
                'covered_message_ids':seen,'result_hash':digest(summary)},task=child['id'])
            state=store.projection_get(runtime_key);state['active_ms']=int((time.monotonic()-started)*1000);store.projection_put(runtime_key,state)
            index+=1
        replacement={'role':'user','content':json.dumps({'working_summary':summary,'note':'工作摘要只供参考；有效约束以正式记录为准。工具执行完成不表示当前用户请求完成；当前请求会另行提供。'},ensure_ascii=False)}
        if tokens([replacement]+tail,model)>=history_size:
            raise BudgetExceeded('summary_not_smaller','摘要未缩小工作历史，保留原工作历史')
        final_input=tokens({'instructions':instructions,'input':[replacement]+tail+[{'role':'user','content':request}],
            'tools':tool_defs,'output_schema':output.json_schema() if output else None},config['model']['name'])
        if final_input>cap:
            raise BudgetExceeded('summary_not_fitting','实际摘要与必需输入仍超预算，原工作历史保持不变',
                {'input_tokens':final_input,'input_budget':cap})
        with store.transaction():
            store.advisory_lock('session:'+original['id'])
            live=store.get(original['id'],pid)
            persistent.assert_writer(live)
            if live['generation']!=original['generation'] or live['last_item_seq']!=original['last_item_seq']:
                raise BudgetExceeded('summary_generation_changed','摘要期间工作历史已变化，未替换')
            artifact=store.get(previous['record_id'],pid) if previous else store.put(new_record('artifact',pid,artifact_kind='work_summary',scope=parent['scope']))
            version=artifact['latest_version']+1
            saved=store.put(new_record('artifact_version',pid,artifact_id=artifact['id'],version=version,
                parent_version=artifact['latest_version'] or None,content={'storage':'inline_text','text':summary},
                output_schema=None,source_refs=[{'record_id':i,'version':None,'item_id':None,'json_pointer':None} for i in covered],
                producer_run_id=run['id'],config_version_id=snapshot['id'],origin='model'))
            store.put(new_record('artifact_state',pid,artifact_id=artifact['id'],artifact_version_id=saved['id'],version=version,
                confirmation_status='not_required',dependency_status='valid',quality_status='passed'))
            artifact.update(latest_version=version,current_effective_version=version);store.update(artifact,artifact['row_version'])
            live['generation']+=1;live['latest_summary_ref']=ref(saved)
            for item,history_ids in [(replacement,covered)]+[(unwrap(r['sdk_item'],store,pid),r['history_ids']) for r in tail_rows]:
                live['last_item_seq']+=1
                store.put(new_record('session_item',pid,session_id=live['id'],generation=live['generation'],sequence=live['last_item_seq'],
                    sdk_item={'storage':'inline_json','value':item},history_ids=history_ids))
            persistent.row=store.update(live,live['row_version'])
            event(store,pid,'session.compacted',{'session_id':live['id'],'from_generation':original['generation'],
                'to_generation':live['generation'],'summary_ref':ref(saved),'covered_message_ids':covered,
                'before_tokens':history_size,'after_tokens':tokens([replacement]+tail,model),'target_ratio':config['compaction']['target_ratio']},
                task=child['id'],run=run['id'],conversation=parent['conversation_id'])
            live_child=store.get(child['id'],pid);live_child['state']='succeeded';store.update(live_child,live_child['row_version'])
    except BaseException:
        if child:
            with store.transaction():
                live=store.get(child['id'],pid);live.update(state='failed',pause_reason='summary_failed');store.update(live,live['row_version'])
                if live.get('current_run_id'):
                    r=store.get(live['current_run_id'],pid)
                    if r['state']=='running':r.update(state='failed',finished_at=now_utc());store.update(r,r['row_version'])
                event(store,pid,'session.compaction_failed',{'session_id':original['id'],'preserved_generation':original['generation']},task=child['id'])
        raise
