import json
import pytest
from test_sdk_integration import runtime
from branch_agent.model_service import ModelService, PersistentSession
from branch_agent.compaction import compact_session, complete_tools, compaction_layout
from branch_agent.context import BudgetExceeded, tokens
from branch_agent.schemas import SchemaCatalog


class SummaryModel(ModelService):
    invalid=False
    async def run(self,stage,task,run,session,config,materials,message,control=None):
        assert stage=='aux.summary'
        return '' if self.invalid else '已讨论人物动机；尚无新增确认或待办。'


@pytest.mark.asyncio
async def test_fixed_material_dominance_does_not_repeat_soft_compaction(runtime):
    store,task,run,row,cfg=runtime
    cfg['context'].update(recent_turns=1,history_token_cap=1000)
    session=PersistentSession(store,row,task,run)
    original=[{'role':'user','content':'早期问题。'*20},{'role':'assistant','content':'已答复。'*20},
              {'role':'user','content':'当前问题'},{'role':'assistant','content':'尚待最终回复'}]
    await session.add_items(original)
    output=SchemaCatalog().output_type('coordinator_response');instruction='固定必需材料。'*350
    plan=compaction_layout(original,cfg,instruction,[],output.json_schema(),'继续')
    cfg['context']['input_token_cap']=plan['total']+100
    plan=compaction_layout(original,cfg,instruction,[],output.json_schema(),'继续')
    assert plan['total']>plan['input_budget']*cfg['compaction']['trigger_ratio']
    assert plan['fixed_input_tokens']>plan['input_budget']*cfg['compaction']['target_ratio']
    assert plan['history_tokens']<=cfg['context']['history_token_cap']
    await compact_session(SummaryModel(store),session,'coordinator',cfg,instruction,[],output,'继续')
    assert store.get(row['id'],task['project_id'])['generation']==1
    assert await session.get_items()==original
    assert not store.list(task['project_id'],'artifact_version')


@pytest.mark.asyncio
async def test_summary_only_prefix_skips_soft_target_but_never_hard_cap(runtime):
    store,task,run,row,cfg=runtime
    cfg['context'].update(recent_turns=1,history_token_cap=100)
    session=PersistentSession(store,row,task,run)
    await session.add_items([{'role':'user','content':'早期原档。'*500},
        {'role':'assistant','content':'完整早期结论。'*400},
        {'role':'user','content':'当前尚未答复的问题。'*100}])
    class Counted(SummaryModel):
        calls=0
        async def run(self,*args,**kwargs):
            self.calls+=1
            return await super().run(*args,**kwargs)
    service=Counted(store);output=SchemaCatalog().output_type('coordinator_response')
    await compact_session(service,session,'coordinator',cfg,'instructions',[],output,'继续')
    old_items=await session.get_items();old_calls=service.calls
    assert tokens(old_items,cfg['model']['name'])>cfg['context']['history_token_cap']
    await compact_session(service,session,'coordinator',cfg,'instructions',[],output,'继续')
    assert service.calls==old_calls and await session.get_items()==old_items
    assert store.get(row['id'],task['project_id'])['generation']==2
    plan=compaction_layout(old_items,cfg,'instructions',[],output.json_schema(),'继续')
    cfg['context']['input_token_cap']=plan['total']-1
    await compact_session(service,session,'coordinator',cfg,'instructions',[],output,'继续')
    assert service.calls>old_calls and store.get(row['id'],task['project_id'])['generation']==3


@pytest.mark.asyncio
async def test_summary_projection_omits_only_opaque_reasoning_preserving_originals(runtime):
    from branch_agent.context import unwrap
    store,task,run,row,cfg=runtime;pid=task['project_id']
    cfg['context'].update(input_token_cap=5000,recent_turns=1,history_token_cap=100)
    session=PersistentSession(store,row,task,run)
    ciphertext='provider-opaque-not-readable-0123456789'*1500
    original=[{'role':'user','content':'读取完整固定原文；最终回答仍未完成'},
        {'id':'rs_public','type':'reasoning','encrypted_content':ciphertext,
         'summary':[{'type':'summary_text','text':'供应商实际公开摘要'}]},
        {'id':'rs_empty','type':'reasoning','encrypted_content':ciphertext,'summary':[]},
        {'type':'function_call','call_id':'preserved-tool','name':'read_record','arguments':'{}'},
        {'type':'function_call_output','call_id':'preserved-tool','output':'完整工具证据；不得截断。'}]
    await session.add_items(original)
    archives=store.list(pid,'history_record',limit=1000)
    sdk_originals=store.list(pid,'session_item',limit=1000)
    class VerifyProjection(SummaryModel):
        async def run(self,stage,task,run,session,config,materials,message,control=None):
            data=json.loads(message);window=data['original_archives']
            assert len(window)==len(original)
            projected=[]
            for entry in window:
                raw=unwrap(store.resolve_ref(entry['record_ref'],pid)['content'],store,pid)
                expected={k:v for k,v in raw.items() if k!='encrypted_content'} if raw.get('type')=='reasoning' else raw
                assert entry['content']==expected
                projected.append(entry['content'])
            assert ciphertext not in message
            assert projected[1]['summary']==original[1]['summary']
            assert projected[2]['summary']==[]
            assert complete_tools(projected)
            assert data['required_covered_message_ids']==[a['record_ref']['record_id'] for a in window]
            return await super().run(stage,task,run,session,config,materials,message,control)
    await compact_session(VerifyProjection(store),session,'coordinator',cfg,'instructions',[],
        SchemaCatalog().output_type('coordinator_response'),'继续原问题')
    assert store.get(row['id'],pid)['generation']==2
    assert store.list(pid,'history_record',limit=1000)==archives
    assert [r for r in store.list(pid,'session_item',limit=1000) if r['generation']==1]==sdk_originals
    raw_items=[unwrap(r['content'],store,pid) for r in archives]
    raw_reasoning=[i for i in raw_items if isinstance(i,dict) and i.get('type')=='reasoning']
    assert len(raw_reasoning)==2 and all(i['encrypted_content']==ciphertext for i in raw_reasoning)


@pytest.mark.asyncio
async def test_compaction_preserves_archives_and_recent_complete_tool_chain(runtime):
    store,task,run,row,cfg=runtime
    cfg['context'].update(recent_turns=1,history_token_cap=100)
    session=PersistentSession(store,row,task,run)
    await session.add_items([{'role':'user','content':'人物讨论。'*500},
        {'role':'assistant','content':'先讨论动机。'*400},
        {'role':'user','content':'继续'},
        {'type':'function_call','call_id':'c1','name':'read_record','arguments':'{}'},
        {'type':'function_call_output','call_id':'c1','output':'记录'},
        {'role':'assistant','content':'已读取'}])
    before=store.list(task['project_id'],'history_record',limit=1000)
    service=SummaryModel(store);output=SchemaCatalog().output_type('coordinator_response')
    await compact_session(service,session,'coordinator',cfg,'instructions',[],output,'继续')
    live=store.get(row['id'],task['project_id']);assert live['generation']==2
    assert live['latest_summary_ref']
    items=await session.get_items()
    assert len(items)==5 and complete_tools(items)
    assert len(store.list(task['project_id'],'history_record',limit=1000))==len(before)
    assert len(store.list(task['project_id'],'session_item',limit=1000))==11
    versions=store.list(task['project_id'],'artifact_version')
    assert len(versions)==1
    assert versions[0]['output_schema'] is None
    assert versions[0]['content']['storage']=='inline_text'
    assert versions[0]['source_refs']
    assert {r['record_id'] for r in versions[0]['source_refs']}<={r['id'] for r in before}


@pytest.mark.asyncio
async def test_summary_run_records_its_configured_agent_key(runtime):
    store,task,run,row,cfg=runtime;pid=task['project_id']
    cfg['context'].update(recent_turns=1,history_token_cap=100)
    cfg['auxiliary_configs']['aux.summary']['prompts']['stage_agents']['aux.summary']='source_global_parser'
    session=PersistentSession(store,row,task,run)
    await session.add_items([{'role':'user','content':'早期真实讨论。'*500},
                             {'role':'assistant','content':'保留未完成的问题。'*250},
                             {'role':'user','content':'继续'}])
    await compact_session(SummaryModel(store),session,'coordinator',cfg,'instructions',[],
        SchemaCatalog().output_type('coordinator_response'),'继续')
    summary_runs=[r for r in store.list(pid,'run') if r['task_id']!=task['id']]
    assert summary_runs and {r['agent_key'] for r in summary_runs}=={'source_global_parser'}


@pytest.mark.asyncio
async def test_invalid_summary_keeps_original_generation(runtime):
    store,task,run,row,cfg=runtime;cfg['context'].update(recent_turns=1,history_token_cap=100)
    session=PersistentSession(store,row,task,run)
    await session.add_items([{'role':'user','content':'原始讨论。'*500},{'role':'assistant','content':'建议。'*200},
        {'role':'user','content':'继续'},{'role':'assistant','content':'答复'}])
    service=SummaryModel(store);service.invalid=True
    with pytest.raises(BudgetExceeded) as error:
        await compact_session(service,session,'coordinator',cfg,'instructions',[],SchemaCatalog().output_type('coordinator_response'),'继续')
    assert error.value.code=='summary_repair_exhausted'
    assert store.get(row['id'],task['project_id'])['generation']==1
    assert len(await session.get_items())==4
    assert not store.list(task['project_id'],'artifact_version')


@pytest.mark.asyncio
@pytest.mark.parametrize('single_item_too_large',[False,True])
async def test_actual_summary_admission_splits_only_unsent_complete_archives(runtime,single_item_too_large):
    store,task,run,row,cfg=runtime
    cfg['context'].update(recent_turns=1,history_token_cap=100)
    session=PersistentSession(store,row,task,run)
    await session.add_items([{'role':'user','content':'早期原始讨论。'*180},
        {'role':'assistant','content':'人物尚待确认。'*180},
        {'role':'user','content':'后续原始讨论。'*180},
        {'role':'assistant','content':'保留原作事实。'*180},
        {'role':'user','content':'当前问题'},{'role':'assistant','content':'当前答复'}])
    original=store.list(task['project_id'],'history_record',limit=1000)
    class ActualAdmission(SummaryModel):
        attempts=[]
        accepted=[]
        async def run(self,stage,task,run,session,config,materials,message,control=None):
            data=json.loads(message);window=data['original_archives']
            self.attempts.append([a['record_ref']['record_id'] for a in window])
            # Reproduce extra required material/escaping discovered only by the
            # real ModelService pre-send budget check, after preliminary sizing.
            if single_item_too_large or len(window)>2:
                raise BudgetExceeded('input_budget_exceeded','真实信封及必需材料超出窗口预算')
            self.accepted.extend(a['record_ref']['record_id'] for a in window)
            return await super().run(stage,task,run,session,config,materials,message,control)
    service=ActualAdmission(store)
    invocation=compact_session(service,session,'coordinator',cfg,'instructions',[],
                               SchemaCatalog().output_type('coordinator_response'),'继续')
    if single_item_too_large:
        with pytest.raises(BudgetExceeded,match='单条旧历史'):await invocation
        assert store.get(row['id'],task['project_id'])['generation']==1
        assert len(await session.get_items())==6
        assert not store.list(task['project_id'],'artifact_version')
    else:
        await invocation
        assert len(service.attempts[0])==4
        assert len(service.accepted)==len(set(service.accepted))==4
        summary=json.loads((await session.get_items())[0]['content'])['working_summary']
        assert isinstance(summary,str) and summary
        saved=store.resolve_ref(store.get(row['id'],task['project_id'])['latest_summary_ref'],task['project_id'])
        assert [r['record_id'] for r in saved['source_refs']]==service.accepted
        assert store.get(row['id'],task['project_id'])['generation']==2
    assert store.list(task['project_id'],'history_record',limit=1000)==original
    assert not store.list(task['project_id'],'model_call')


@pytest.mark.asyncio
@pytest.mark.parametrize('closed',[True,False])
async def test_large_recent_tool_chain_can_only_compact_when_closed(runtime,closed):
    store,task,run,row,cfg=runtime
    cfg['context'].update(input_token_cap=5000,recent_turns=1,history_token_cap=100)
    session=PersistentSession(store,row,task,run)
    items=[{'role':'user','content':'请核对当前批次的真实引用'},
           {'type':'function_call','call_id':'large-read','name':'get_artifact','arguments':'{}'}]
    if closed:items.append({'type':'function_call_output','call_id':'large-read','output':'完整固定版本证据。'*600})
    await session.add_items(items)
    before=store.list(task['project_id'],'history_record',limit=1000)
    output=SchemaCatalog().output_type('coordinator_response')
    plan=compaction_layout(items,cfg,'instructions',[],output.json_schema(),'继续回答原批次问题')
    class Capture(SummaryModel):
        calls=0
        async def run(self,stage,task,run,session,config,materials,message,control=None):
            self.calls+=1
            data=json.loads(message)
            assert '尚未最终答复' in data['task']
            archived=[a['content'] for a in data['original_archives']]
            assert archived==items  # Full results remain available to the summarizer.
            return '工具已读取；原用户问题仍待最终答复。'
    service=Capture(store)
    await compact_session(service,session,'coordinator',cfg,'instructions',[],output,'继续回答原批次问题')
    if closed:
        assert plan['keep']==0 and plan['tail_input_tokens']+plan['summary_reserve_tokens']>plan['input_budget']
        assert service.calls==1
        after=await session.get_items()
        assert len(after)==1 and '仍待最终答复' in after[0]['content']
        assert store.get(row['id'],task['project_id'])['generation']==2
    else:
        assert service.calls==0 and store.get(row['id'],task['project_id'])['generation']==1
        assert await session.get_items()==items
    assert store.list(task['project_id'],'history_record',limit=1000)==before


@pytest.mark.asyncio
async def test_summary_failing_actual_hard_input_budget_keeps_generation(runtime):
    store,task,run,row,cfg=runtime
    cfg['context'].update(input_token_cap=2000,recent_turns=1,history_token_cap=100)
    session=PersistentSession(store,row,task,run)
    await session.add_items([{'role':'user','content':'完整历史。'*2500}])
    class Oversized(SummaryModel):
        async def run(self,*args,**kwargs):
            return '虽变小但仍超过完整调用预算。'*150
    with pytest.raises(BudgetExceeded) as caught:
        await compact_session(Oversized(store),session,'coordinator',cfg,'instructions',[],
            SchemaCatalog().output_type('coordinator_response'),'当前请求')
    assert caught.value.code=='summary_not_fitting'
    assert store.get(row['id'],task['project_id'])['generation']==1
    assert len(await session.get_items())==1


@pytest.mark.asyncio
@pytest.mark.parametrize('uncertain_search',[False,True])
async def test_real_sdk_plain_text_pages_resume_without_structured_repairs(runtime,uncertain_search):
    import httpx
    from openai import AsyncOpenAI
    from branch_agent.records import new_record
    from branch_agent.workflow import all_records
    from test_sdk_integration import provider_response
    store,task,run,row,cfg=runtime;pid=task['project_id']
    cfg['context'].update(recent_turns=1,history_token_cap=100)
    fixed=store.put(new_record('config_version',pid,config_key='harness',scope_kind='run_snapshot',
        scope_key='resumable-summary-test',state='snapshot',values=cfg))
    run=store.put(new_record('run',pid,task_id=task['id'],agent_key='conversation_coordinator',
        session_id=row['id'],config_version_id=fixed['id'],state='running'))
    session=PersistentSession(store,row,task,run)
    original_items=[{'role':'user','content':'第一段原始问题。'*100},
        {'role':'assistant','content':'第一段已有结论。'*100},
        {'role':'user','content':'第二段原始问题。'*100},
        {'role':'assistant','content':'第二段尚待确认。'*100},
        {'role':'user','content':'近期问题'},{'role':'assistant','content':'近期答复'}]
    await session.add_items(original_items)
    originals=all_records(store,pid,'history_record')
    requests=[];phase={'value':0}
    def handler(request):
        data=json.loads(request.content)
        payload=json.loads(json.loads(data['input'][-1]['content'])['request'])
        requests.append(payload)
        assert data.get('text',{}).get('format',{}).get('type')!='json_schema'
        response=provider_response();response['output'][0]['content'][0]['text']=f'已整理 {len(requests)} 页历史；确认仍未完成。'
        return httpx.Response(200,json=response)
    client=AsyncOpenAI(api_key='mock-only',http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),max_retries=0)
    class Sized(ModelService):
        async def run(self,stage,task,run,session,config,materials,message,control=None):
            data=json.loads(message)
            if len(data['original_archives'])>1:raise BudgetExceeded('input_budget_exceeded','force actual admission split')
            return await super().run(stage,task,run,session,config,materials,message,control)
    service=Sized(store,client)
    async def control():
        if phase['value']==0 and len(requests)>=2:raise BudgetExceeded('test_pause','pause after two validated pages')
        return {}
    output=SchemaCatalog().output_type('coordinator_response')
    with pytest.raises(BudgetExceeded) as first:
        await compact_session(service,session,'coordinator',cfg,'instructions',[],output,'继续',control)
    assert first.value.code=='test_pause' and len(requests)==2
    assert store.get(row['id'],pid)['generation']==1
    phase['value']=1
    if uncertain_search:
        import uuid
        old_call=all_records(store,pid,'model_call')[0]
        old_run=store.get(old_call['run_id'],pid)
        store.put(new_record('tool_call',pid,task_id=old_run['task_id'],run_id=old_run['id'],
            model_call_id=old_call['id'],operation_id=str(uuid.uuid4()),
            provider_tool_call_id='uncertain-web-search',tool_name='web_search',
            arguments={'storage':'inline_json','value':{'query':'test'}},state='running',attempt=1))
        with pytest.raises(BudgetExceeded) as blocked:
            await compact_session(service,session,'coordinator',cfg,'instructions',[],output,'继续',control)
        assert blocked.value.code=='operation_uncertain'
        assert len(requests)==2
        assert store.get(row['id'],pid)['generation']==1
        await client.close()
        return
    await compact_session(service,session,'coordinator',cfg,'instructions',[],output,'继续',control)
    assert len(requests)==4  # Two validated pages were reused; only two were sent.
    assert store.get(row['id'],pid)['generation']==2
    final=json.loads((await session.get_items())[0]['content'])['working_summary']
    assert isinstance(final,str) and final
    saved=store.resolve_ref(store.get(row['id'],pid)['latest_summary_ref'],pid)
    assert len(saved['source_refs'])==4 and saved['output_schema'] is None
    repairs=all_records(store,pid,'runtime_event',event_name='session.summary_repair_requested')
    assert repairs==[]
    assert len(all_records(store,pid,'runtime_event',event_name='session.summary_page_reused'))==2
    assert all(store.get(x['id'],pid)==x for x in originals)
    assert all(c['state']=='succeeded' for c in all_records(store,pid,'model_call'))
    await client.close()


@pytest.mark.asyncio
async def test_storage_invalid_record_outside_summary_evidence_is_not_repaired(runtime):
    from branch_agent.storage import InvalidRecord
    store,task,run,row,cfg=runtime;cfg['context'].update(recent_turns=1,history_token_cap=100)
    session=PersistentSession(store,row,task,run)
    await session.add_items([{'role':'user','content':'原始问题。'*500},{'role':'assistant','content':'原始答复。'*300},
        {'role':'user','content':'继续'},{'role':'assistant','content':'当前答复'}])
    class StorageFailure(SummaryModel):
        async def run(self,*args,**kwargs):raise InvalidRecord('unrelated database write failure')
    with pytest.raises(InvalidRecord,match='unrelated'):
        await compact_session(StorageFailure(store),session,'coordinator',cfg,'instructions',[],
            SchemaCatalog().output_type('coordinator_response'),'继续')
    assert store.get(row['id'],task['project_id'])['generation']==1
    assert not store.list(task['project_id'],'runtime_event',filters={'event_name':'session.summary_repair_requested'})
