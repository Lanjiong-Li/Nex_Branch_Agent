"""Real Agents SDK against an explicit local mock HTTP provider, never live credentials."""
import json
import uuid
import psycopg
import pytest
import httpx
from openai import AsyncOpenAI
from branch_agent.storage import Store
from branch_agent.records import new_record
from branch_agent.configuration import ConfigService
from branch_agent.model_service import ModelService, ModelRunError, PublicReplyStream, append_history
from branch_agent.workflow import Workflow, ref


@pytest.fixture
def runtime(tmp_path):
    schema='test_sdk_'+uuid.uuid4().hex
    conn=psycopg.connect('postgresql:///branch_agent_local',autocommit=True);conn.execute(f'CREATE SCHEMA "{schema}"')
    store=Store(f'postgresql:///branch_agent_local?options=-csearch_path%3D{schema}',tmp_path/'blobs');store.migrate()
    project=store.put(new_record('project',None,title='SDK Integration',owner_account_id='sdk-test'))
    pid=project['id'];c=store.put(new_record('conversation',pid,title='SDK test'))
    h=append_history(store,pid,c['id'],'你好',role='user',kind='message',visibility='conversation')
    task=store.put(new_record('task',pid,conversation_id=c['id'],requested_by_message_id=h['id'],intent='query',state='running'))
    session=store.put(new_record('work_session',pid,conversation_id=c['id'],session_key='coordinator'))
    cfg=ConfigService(store).resolve(pid,'coordinator')
    run=store.put(new_record('run',pid,task_id=task['id'],agent_key='conversation_coordinator',session_id=session['id'],config_version_id=cfg['id'],state='running'))
    yield store,task,run,session,cfg['values']
    store.close();conn.execute(f'DROP SCHEMA "{schema}" CASCADE');conn.close()


def provider_response():
    value={'result_kind':'ready','payload':{'reply':'你好，请提交原作。','source_message_kind':'request',
        'task_requests':[]},'questions':[],'evidence_refs':[],'notes':[]}
    return {'id':'resp_test_'+uuid.uuid4().hex,'object':'response','created_at':1,'status':'completed','model':'deepseek-flash',
      'output':[{'id':'msg_test','type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':json.dumps(value,ensure_ascii=False),'annotations':[]}]}],
      'parallel_tool_calls':False,'usage':{'input_tokens':100,'output_tokens':40,'total_tokens':140,
      'input_tokens_details':{'cached_tokens':0},'output_tokens_details':{'reasoning_tokens':0}}}


def plain_provider_response(text):
    value=provider_response()
    value['output'][0]['content'][0]['text']=text
    return value


def streamed_provider_response(response, chunks, *, terminal_type='response.completed'):
    events=[{'type':'response.output_text.delta','sequence_number':index+1,
             'item_id':response['output'][0]['id'],'output_index':0,'content_index':0,
             'logprobs':[],'delta':chunk} for index,chunk in enumerate(chunks)]
    events.append({'type':terminal_type,'sequence_number':len(events)+1,'response':response})
    body=''.join('data: '+json.dumps(item,ensure_ascii=False)+'\n\n' for item in events)+'data: [DONE]\n\n'
    return httpx.Response(200,headers={'content-type':'text/event-stream'},content=body.encode())


def test_public_reply_stream_decodes_only_direct_payload_reply_and_split_emoji():
    raw=('{'+'"notes":["reply: private"],"payload":{"other":{"reply":"private"},'
         '"reply":"第一段\\n😀 \\ud83d\\ude00","task_requests":[]}}')
    decoder=PublicReplyStream()
    output=''.join(decoder.feed(raw[index:index+3]) for index in range(0,len(raw),3))
    assert output=='第一段\n😀 😀'
    assert 'private' not in output


@pytest.mark.asyncio
async def test_streamed_coordinator_emits_public_reply_and_audits_terminal_response(runtime):
    store,task,run,session,values=runtime
    response=provider_response()
    raw=response['output'][0]['content'][0]['text']
    chunks=[raw[index:index+7] for index in range(0,len(raw),7)]
    requests=[]
    def handler(request):
        data=json.loads(request.content);requests.append(data)
        assert data['stream'] is True
        return streamed_provider_response(response,chunks)
    client=AsyncOpenAI(api_key='local-mock-not-real',
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),max_retries=0)
    result=await ModelService(store,client).run('coordinator',task,run,session,values,[],'你好',live_events=True)
    assert result['payload']['reply']=='你好，请提交原作。'
    events=sorted(store.list(task['project_id'],'runtime_event'),key=lambda item:item['sequence'])
    names=[item['event_name'] for item in events]
    assert 'chat.reply.started' in names and 'chat.reply.completed' in names
    assert 'chat.reply.failed' not in names
    deltas=[item['payload']['delta'] for item in events if item['event_name']=='chat.reply.delta']
    assert ''.join(deltas)=='你好，请提交原作。'
    assert all('task_requests' not in delta for delta in deltas)
    assert any(item['event_name']=='chat.activity' and item['payload']['kind']=='model' for item in events)
    assert all(item['conversation_id']==task['conversation_id'] for item in events)
    calls=store.list(task['project_id'],'model_call')
    assert len(calls)==1 and calls[0]['state']=='succeeded' and calls[0]['usage']['input_tokens']==100
    assert store.list(task['project_id'],'session_item')
    await client.close()


@pytest.mark.asyncio
async def test_streamed_coordinator_retries_pre_event_429_with_one_logical_turn(runtime):
    store,task,run,session,values=runtime
    response=provider_response();raw=response['output'][0]['content'][0]['text']
    values['retry']['max_retries']=1
    attempts=[]
    def handler(request):
        attempts.append(json.loads(request.content))
        if len(attempts)==1:
            return httpx.Response(429,json={'error':{'message':'test rate limit','type':'rate_limit_error'}})
        return streamed_provider_response(response,[raw[:20],raw[20:]])
    client=AsyncOpenAI(api_key='local-mock-not-real',
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),max_retries=0)
    result=await ModelService(store,client).run('coordinator',task,run,session,values,[],'你好',live_events=True)
    assert result['payload']['reply']=='你好，请提交原作。'
    calls=store.list(task['project_id'],'model_call')
    assert len(attempts)==2 and len(calls)==2
    assert {call['attempt'] for call in calls}=={1,2}
    assert len({call['operation_id'] for call in calls})==1
    assert len({call['turn_index'] for call in calls})==1
    assert store.get(run['id'],task['project_id'])['model_turns_used']==1
    await client.close()


@pytest.mark.asyncio
async def test_streamed_coordinator_marks_partial_reply_failed_on_incomplete_response(runtime):
    store,task,run,session,values=runtime
    response=provider_response();raw=response['output'][0]['content'][0]['text']
    response['status']='incomplete';response['incomplete_details']={'reason':'max_output_tokens'}
    def handler(request):
        return streamed_provider_response(response,[raw],terminal_type='response.incomplete')
    client=AsyncOpenAI(api_key='local-mock-not-real',
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),max_retries=0)
    with pytest.raises(ModelRunError) as error:
        await ModelService(store,client).run('coordinator',task,run,session,values,[],'你好',live_events=True)
    assert error.value.code=='output_limit_exceeded'
    events=store.list(task['project_id'],'runtime_event')
    names=[item['event_name'] for item in events]
    assert 'chat.reply.started' in names and 'chat.reply.failed' in names
    assert 'chat.reply.completed' not in names
    calls=store.list(task['project_id'],'model_call')
    assert len(calls)==1 and calls[0]['state']=='failed'
    await client.close()


@pytest.mark.asyncio
async def test_streamed_coordinator_discards_draft_when_final_reply_differs(runtime):
    store,task,run,session,values=runtime
    response=provider_response()
    raw=response['output'][0]['content'][0]['text']
    raw=raw.replace('"reply": "你好，请提交原作。"',
                    '"reply": "临时草稿", "reply": "最终回复"')
    response['output'][0]['content'][0]['text']=raw
    def handler(request):
        return streamed_provider_response(response,[raw[:80],raw[80:]])
    client=AsyncOpenAI(api_key='local-mock-not-real',
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),max_retries=0)
    result=await ModelService(store,client).run('coordinator',task,run,session,values,[],'你好',live_events=True)
    assert result['payload']['reply']=='最终回复'
    names=[event['event_name'] for event in store.list(task['project_id'],'runtime_event')]
    assert 'chat.reply.started' in names and 'chat.reply.failed' in names
    assert 'chat.reply.completed' not in names
    await client.close()


@pytest.mark.asyncio
async def test_streamed_coordinator_reports_tool_lifecycle_without_arguments_or_results(runtime):
    store,task,run,session,values=runtime
    responses=[]
    def handler(request):
        data=json.loads(request.content);responses.append(data)
        response=provider_response()
        if len(responses)==1:
            response['output']=[{'id':'fc_read','type':'function_call','call_id':'call_read',
                'name':'read_record','arguments':json.dumps({'record_id':task['requested_by_message_id'],
                    'version':None,'cursor':None}),'status':'completed'}]
            events=[{'type':'response.completed','sequence_number':1,'response':response}]
            body=''.join('data: '+json.dumps(item,ensure_ascii=False)+'\n\n' for item in events)+'data: [DONE]\n\n'
            return httpx.Response(200,headers={'content-type':'text/event-stream'},content=body.encode())
        assert any(item.get('type')=='function_call_output' and item['call_id']=='call_read' for item in data['input'])
        raw=response['output'][0]['content'][0]['text']
        return streamed_provider_response(response,[raw[:30],raw[30:]])
    client=AsyncOpenAI(api_key='local-mock-not-real',
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),max_retries=0)
    result=await ModelService(store,client).run('coordinator',task,run,session,values,[],
        '查看我刚才说了什么',live_events=True)
    assert result['payload']['reply']=='你好，请提交原作。'
    events=store.list(task['project_id'],'runtime_event')
    activities=[item['payload'] for item in events if item['event_name']=='chat.activity']
    assert any(item['kind']=='tool' and item['status']=='started' and item['tool_name']=='read_record'
               for item in activities)
    assert any(item['kind']=='tool' and item['status']=='finished' for item in activities)
    assert all('arguments' not in item and 'result' not in item and task['requested_by_message_id'] not in item['text']
               for item in activities)
    assert len(store.list(task['project_id'],'model_call'))==2
    assert len(store.list(task['project_id'],'tool_call'))==1
    await client.close()


@pytest.mark.asyncio
async def test_real_sdk_audits_input_output_usage_and_session(runtime):
    store,task,run,session,values=runtime;requests=[]
    def handler(request):
        data=json.loads(request.content);requests.append(data)
        assert data['model']=='deepseek-flash'
        assert data['text']['format']['type']=='json_schema'
        assert data['text']['format']['strict'] is False
        return httpx.Response(200,json=provider_response())
    client=AsyncOpenAI(api_key='local-mock-not-real',http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),max_retries=0)
    result=await ModelService(store,client).run('coordinator',task,run,session,values,[],'你好')
    assert result['payload']['reply']=='你好，请提交原作。'
    pid=task['project_id'];calls=store.list(pid,'model_call');assert len(calls)==1
    assert calls[0]['state']=='succeeded' and calls[0]['usage']['input_tokens']==100
    snapshots=store.list(pid,'context_snapshot');assert len(snapshots)==1 and snapshots[0]['input_token_estimate']>0
    assert snapshots[0]['output_schema']['schema_id']=='coordinator_response'
    assert store.list(pid,'session_item') and store.list(pid,'history_record')
    assert store.get(run['id'],pid)['model_turns_used']==1
    spans=store._connection().execute(
        'SELECT kind,name,metadata FROM sdk_trace_spans WHERE project_id=%s AND run_id=%s',
        (pid,run['id'])).fetchall()
    assert any(span['kind']=='agent' for span in spans)
    assert any(span['kind']=='response' for span in spans)
    assert '你好' not in str(spans)  # Prompt text stays in the existing audited records.
    await client.close()


@pytest.mark.asyncio
async def test_completed_schema_failure_exposes_exact_archived_format_finding(runtime):
    store, task, run, session, values = runtime
    response = provider_response()
    value = json.loads(response['output'][0]['content'][0]['text'])
    value['description'] = ''
    response['output'][0]['content'][0]['text'] = json.dumps(value, ensure_ascii=False)
    client = AsyncOpenAI(api_key='local-mock-not-real',
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=response))), max_retries=0)
    with pytest.raises(ModelRunError) as failure:
        await ModelService(store, client).run('coordinator', task, run, session, values, [], '你好')
    issue = failure.value.details['output_failure']
    assert issue['category'] == 'format'
    assert issue['schema_id'] == 'coordinator_response'
    assert issue['problems'][0]['validator'] == 'additionalProperties'
    assert issue['problems'][0]['unexpected_properties'] == ['description']
    assert store.get(issue['history_record_id'], task['project_id'])['record_type'] == 'history_record'
    await client.close()


@pytest.mark.asyncio
async def test_unique_fenced_json_is_recovered_without_second_model_call(runtime):
    store, task, run, session, values = runtime
    response = provider_response()
    raw = response['output'][0]['content'][0]['text']
    response['output'][0]['content'][0]['text'] = '结果：\n```json\n' + raw + '\n```'
    client = AsyncOpenAI(api_key='local-mock-not-real',
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=response))), max_retries=0)
    result = await ModelService(store, client).run('coordinator', task, run, session, values, [], '你好')
    assert result['payload']['reply'] == '你好，请提交原作。'
    assert len(store.list(task['project_id'], 'model_call')) == 1
    await client.close()


@pytest.mark.asyncio
async def test_provider_routing_uses_isolated_clients_and_keeps_openai_available(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','mock-deepseek-key')
    monkeypatch.setenv('OPENAI_API_KEY','mock-openai-key')
    service=ModelService(None)
    deepseek=service._client_for('deepseek-flash')
    assert service._client_for('deepseek-v4-pro') is deepseek
    openai=service._client_for('gpt-5.6-sol')
    assert openai is not deepseek
    assert str(deepseek.base_url).rstrip('/')=='https://api.deepseek.com'
    assert str(openai.base_url)=='https://api.openai.com/v1/'
    await deepseek.close()
    await openai.close()


@pytest.mark.asyncio
async def test_step2_uses_structured_knowledge_asset_schema(runtime):
    store,base_task,_,_,_=runtime;pid=base_task['project_id'];requests=[]
    workflow=Workflow(store)
    with store.transaction():
        source=workflow.save(pid,'source_text','甲见乙。',origin='import',effective=True)
        anchor={'source_ref':ref(source),'start_utf16':0,'end_utf16':4,
            'exact_quote':None,'prefix':None,'suffix':None}
        global_events={'result_kind':'ready','payload':{'source_ref':ref(source),'global_events':[
            {'event_id':'GEV-1','title':'相遇','summary':'甲见乙。','analysis':'相遇建立人物关系。',
             'narrative_order':1,'story_time':None,'character_ids':[],
             'source_anchors':[anchor]}],
            'covered_source_anchors':[anchor],'remaining_source_anchors':[]},
            'questions':[],'evidence_refs':[ref(source)],'notes':[]}
        character_events={'result_kind':'ready','payload':{'source_ref':ref(source),'character_views':[],
            'covered_source_anchors':[],'remaining_source_anchors':[]},
            'questions':[],'evidence_refs':[ref(source)],'notes':[]}
        global_view=workflow.save(pid,'source_global_events',global_events,stage=1,inputs=[ref(source)],effective=True)
        character_view=workflow.save(pid,'source_character_events',character_events,stage=1,inputs=[ref(source)],effective=True)
    config=ConfigService(store).resolve(pid,'step2')
    task=store.put(new_record('task',pid,conversation_id=base_task['conversation_id'],
        requested_by_message_id=base_task['requested_by_message_id'],intent='generate',state='running'))
    session=store.put(new_record('work_session',pid,conversation_id=base_task['conversation_id'],session_key='source_knowledge_asset'))
    run=store.put(new_record('run',pid,task_id=task['id'],agent_key='source_knowledge_analyst',session_id=session['id'],
        config_version_id=config['id'],state='running',input_refs=[ref(global_view),ref(character_view)]))
    def handler(request):
        data=json.loads(request.content);requests.append(data)
        from test_runtime import step2_response
        return httpx.Response(200,json=plain_provider_response(json.dumps(step2_response(),ensure_ascii=False)))
    client=AsyncOpenAI(api_key='local-mock-not-real',http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),max_retries=0)
    result=await ModelService(store,client).run('step2',task,run,session,config['values'],workflow.materials(pid,2),'分析原作')
    assert result['payload']['premise']=='甲与乙相遇。'
    snapshot=store.list(pid,'context_snapshot')[-1]
    assert snapshot['output_schema']['schema_id']=='source_knowledge_asset'
    assert requests and requests[0]['text']['format']['type']=='json_schema'
    await client.close()


@pytest.mark.asyncio
async def test_disabling_stage_output_type_changes_real_sdk_request(runtime, monkeypatch):
    monkeypatch.delenv('BRAVE_SEARCH_API_KEY', raising=False)
    store,base_task,_,_,_=runtime;pid=base_task['project_id'];requests=[]
    workflow=Workflow(store)
    with store.transaction():
        source=workflow.save(pid,'source_text','甲见乙。',origin='import',effective=True)
    config=ConfigService(store).resolve(pid,'step1')
    config['values']['output']['structured']['step1.global']=False
    config['values']['model'].update(name='gpt-5.6-luna',reasoning_effort='high',max_output_tokens=32000)
    config['values']['tools']['enabled']=['read_record']
    task=store.put(new_record('task',pid,conversation_id=base_task['conversation_id'],
        requested_by_message_id=base_task['requested_by_message_id'],intent='generate',state='running'))
    session=store.put(new_record('work_session',pid,conversation_id=base_task['conversation_id'],session_key='step1'))
    run=store.put(new_record('run',pid,task_id=task['id'],agent_key='source_parser',session_id=session['id'],
        config_version_id=config['id'],state='running',input_refs=[ref(source)]))
    def handler(request):
        data=json.loads(request.content);requests.append(data)
        assert data['model']=='gpt-5.6-luna'
        assert data['reasoning']['effort']=='high'
        assert data['max_output_tokens']==32000
        assert data.get('text',{}).get('format',{}).get('type')!='json_schema'
        assert [tool['name'] for tool in data['tools']]==['read_record']
        return httpx.Response(200,json=plain_provider_response('原始文本调试输出'))
    client=AsyncOpenAI(api_key='local-mock-not-real',http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),max_retries=0)
    result=await ModelService(store,client).run('step1',task,run,session,config['values'],workflow.materials(pid,1),
        '切片',step1_view='global',live_events=True)
    assert result=='原始文本调试输出'
    assert store.list(pid,'context_snapshot')[-1]['output_schema'] is None
    assert requests
    activities=[item['payload'] for item in store.list(pid,'runtime_event')
                if item['event_name']=='chat.activity']
    assert activities and all(item['view']=='global' for item in activities)
    assert any('全局事件 Agent' in item['text'] for item in activities)
    await client.close()


@pytest.mark.asyncio
async def test_retry_has_same_operation_and_turn(runtime):
    store,task,run,session,values=runtime;attempts=[]
    def handler(request):
        attempts.append(1)
        if len(attempts)==1:return httpx.Response(429,json={'error':{'message':'test rate limit','type':'rate_limit_error'}})
        return httpx.Response(200,json=provider_response())
    client=AsyncOpenAI(api_key='local-mock-not-real',http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),max_retries=0)
    await ModelService(store,client).run('coordinator',task,run,session,values,[],'你好')
    calls=store.list(task['project_id'],'model_call');assert len(calls)==2
    assert len({c['operation_id'] for c in calls})==1
    assert {c['attempt'] for c in calls}=={1,2}
    assert {c['turn_index'] for c in calls}=={1}
    assert store.get(run['id'],task['project_id'])['model_turns_used']==1
    await client.close()


@pytest.mark.asyncio
async def test_tool_execution_is_archived_and_second_turn_receives_result(runtime):
    store,task,run,session,values=runtime;requests=[]
    def handler(request):
        data=json.loads(request.content);requests.append(data)
        response=provider_response()
        if len(requests)==1:
            response['output']=[{'id':'fc_read','type':'function_call','call_id':'call_read','name':'read_record',
                'arguments':json.dumps({'record_id':task['requested_by_message_id'],'version':None,'cursor':None}),'status':'completed'}]
        else:
            assert any(i.get('type')=='function_call_output' and i['call_id']=='call_read' for i in data['input'])
        return httpx.Response(200,json=response)
    client=AsyncOpenAI(api_key='local-mock',http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),max_retries=0)
    await ModelService(store,client).run('coordinator',task,run,session,values,[],'查看我刚才说了什么')
    calls=store.list(task['project_id'],'tool_call');assert len(calls)==1
    assert calls[0]['state']=='succeeded' and calls[0]['history_ids']
    assert len(store.list(task['project_id'],'model_call'))==2
    assert store.get(run['id'],task['project_id'])['model_turns_used']==2
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', [
    'coordinator', 'step1', 'step2', 'aux.summary', 'aux.format_repair',
    'aux.history_answer'])
@pytest.mark.parametrize('configured', [False, True])
async def test_web_search_tool_is_available_to_every_agent_when_key_is_configured(
        runtime, monkeypatch, stage, configured):
    store, base_task, base_run, base_session, base_values = runtime
    if configured:
        monkeypatch.setenv('BRAVE_SEARCH_API_KEY', 'fake-search-key')
    else:
        monkeypatch.delenv('BRAVE_SEARCH_API_KEY', raising=False)
    pid = base_task['project_id']
    if stage == 'coordinator':
        task, run, session, values, materials = (
            base_task, base_run, base_session, base_values, [])
    else:
        config = ConfigService(store).resolve(pid, stage)
        values = config['values']
        values['output']['structured']['step1.global' if stage == 'step1' else stage] = False
        if stage == 'step1':
            with store.transaction():
                Workflow(store).save(pid, 'source_text', '甲见乙。', origin='import', effective=True)
            materials = Workflow(store).materials(pid, 1)
        elif stage == 'step2':
            workflow = Workflow(store)
            with store.transaction():
                source = workflow.save(pid, 'source_text', '甲见乙。',
                    origin='import', effective=True)
                source_ref = ref(source)
                workflow.save(pid, 'source_global_events', {
                    'result_kind': 'ready', 'payload': {
                        'source_ref': source_ref, 'global_events': [],
                        'covered_source_anchors': [], 'remaining_source_anchors': []},
                    'questions': [], 'evidence_refs': [source_ref], 'notes': []},
                    stage=1, inputs=[source_ref], effective=True)
                character_events = workflow.save(pid, 'source_character_events', {
                    'result_kind': 'ready', 'payload': {
                        'source_ref': source_ref, 'character_views': [],
                        'covered_source_anchors': [], 'remaining_source_anchors': []},
                    'questions': [], 'evidence_refs': [source_ref], 'notes': []},
                    stage=1, inputs=[source_ref], effective=True)
            materials = workflow.materials(pid, 2)
        elif stage in ('aux.summary', 'aux.history_answer'):
            archive = store.get(base_task['requested_by_message_id'], pid)
            materials = [{'builtin': 'runtime.archive_window', 'content': {
                'history_refs': [ref(archive)], 'decision_refs': [], 'artifact_refs': [],
                'tool_call_refs': []}, 'required': True}]
        else:
            materials = []
        task = store.put(new_record('task', pid, conversation_id=base_task['conversation_id'],
            requested_by_message_id=base_task['requested_by_message_id'],
            intent='generate', state='running'))
        session = store.put(new_record('work_session', pid,
            conversation_id=base_task['conversation_id'], session_key=stage + '-search-boundary'))
        run = store.put(new_record('run', pid, task_id=task['id'],
            agent_key=values['prompts']['stage_agents'][stage],
            session_id=session['id'], config_version_id=config['id'], state='running'))
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        response = (provider_response() if stage == 'coordinator'
                    else plain_provider_response('已完成'))
        return httpx.Response(200, json=response)

    client = AsyncOpenAI(api_key='local-mock', http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)), max_retries=0)
    await ModelService(store, client).run(stage, task, run, session, values,
        materials, '处理', **({'step1_view': 'global'} if stage == 'step1' else {}))
    names = {tool['name'] for tool in requests[0].get('tools', [])}
    assert ('web_search' in names) is configured
    if stage in ('aux.summary', 'aux.format_repair'):
        assert names == ({'web_search'} if configured else set())
    else:
        assert {'list_records', 'read_record'} <= names
    assert 'fake-search-key' not in str(requests)
    await client.close()


@pytest.mark.asyncio
async def test_web_search_sdk_call_is_audited_and_next_turn_gets_bounded_result(runtime, monkeypatch):
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY', 'fake-search-key')
    store, task, run, session, values = runtime
    searches = []
    requests = []

    async def fake_search(self, query, **kwargs):
        searches.append((query, kwargs))
        return {'status': 'ok', 'results': [{'url': 'https://example.org/source',
                'title': '来源', 'snippets': ['已核对的短摘录'],
                'published_at': '2026-09-27', 'relative_age': 'today'}],
                'result_count': 1, 'truncated': False}

    monkeypatch.setattr('branch_agent.model_service.BraveSearchClient.search', fake_search)

    def handler(request):
        data = json.loads(request.content)
        requests.append(data)
        response = provider_response()
        if len(requests) == 1:
            assert 'web_search' in {tool['name'] for tool in data['tools']}
            response['output'] = [{'id': 'fc_web_search', 'type': 'function_call',
                'call_id': 'call_web_search', 'name': 'web_search',
                'arguments': json.dumps({'query': '最新消息', 'freshness': 'pw',
                                         'search_lang': 'zh'}), 'status': 'completed'}]
        else:
            output = next(item['output'] for item in data['input']
                          if item.get('type') == 'function_call_output'
                          and item['call_id'] == 'call_web_search')
            result = json.loads(output)
            assert result['status'] == 'ok'
            assert result['results'][0]['url'] == 'https://example.org/source'
            assert 'fake-search-key' not in output
        return httpx.Response(200, json=response)

    client = AsyncOpenAI(api_key='local-mock', http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)), max_retries=0)
    await ModelService(store, client).run('coordinator', task, run, session, values, [], '请核对最新消息')
    assert len(requests) == 2
    assert searches == [('最新消息', {'freshness': 'pw', 'search_lang': 'zh'})]
    calls = store.list(task['project_id'], 'tool_call')
    assert len(calls) == 1
    assert calls[0]['tool_name'] == 'web_search' and calls[0]['state'] == 'succeeded'
    assert calls[0]['history_ids']
    assert len(store.list(task['project_id'], 'model_call')) == 2
    assert store.get(run['id'], task['project_id'])['model_turns_used'] == 2
    assert 'fake-search-key' not in str(calls)
    assert 'fake-search-key' not in str(store.list(task['project_id'], 'session_item'))
    await client.close()


@pytest.mark.asyncio
async def test_unresolved_web_search_blocks_task_retry_before_model_call(runtime):
    store, task, run, session, values = runtime
    pid = task['project_id']
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=provider_response())

    client = AsyncOpenAI(api_key='local-mock', http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)), max_retries=0)
    service = ModelService(store, client)
    await service.run('coordinator', task, run, session, values, [], '首次调用')
    model_call = store.list(pid, 'model_call')[0]
    store.put(new_record('tool_call', pid, task_id=task['id'], run_id=run['id'],
        model_call_id=model_call['id'], operation_id=str(uuid.uuid4()),
        provider_tool_call_id='uncertain-web-search', tool_name='web_search',
        arguments={'storage': 'inline_json', 'value': {'query': 'test'}},
        state='running', attempt=1))
    with pytest.raises(ModelRunError) as caught:
        await service.run('coordinator', task, run, session, values, [], '重试')
    assert caught.value.code == 'operation_uncertain'
    assert len(requests) == 1
    await client.close()


@pytest.mark.asyncio
async def test_sdk_turn_limit_reports_the_actual_cause(runtime):
    store, task, run, session, values = runtime
    run = store.put(new_record('run', task['project_id'], task_id=task['id'],
        agent_key='conversation_coordinator', session_id=session['id'],
        config_version_id=run['config_version_id'], state='running', max_turns=1))

    def handler(_request):
        response = provider_response()
        response['output'] = [{'id': 'fc_read', 'type': 'function_call',
            'call_id': 'call_read', 'name': 'read_record',
            'arguments': json.dumps({'record_id': task['requested_by_message_id'],
                                     'version': None, 'cursor': None}), 'status': 'completed'}]
        return httpx.Response(200, json=response)

    client = AsyncOpenAI(api_key='local-mock', http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)), max_retries=0)
    with pytest.raises(ModelRunError) as caught:
        await ModelService(store, client).run('coordinator', task, run, session, values, [], '查看记录')
    assert caught.value.code == 'turn_limit'
    assert '模型轮次上限' in str(caught.value)
    assert '凭据' not in str(caught.value)
    await client.close()


@pytest.mark.asyncio
async def test_post_response_pause_keeps_validated_result(runtime):
    from branch_agent.context import BudgetExceeded
    store,task,run,session,values=runtime
    client=AsyncOpenAI(api_key='local-mock',http_client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request:httpx.Response(200,json=provider_response()))),max_retries=0)
    async def control():
        if any(c['state']=='succeeded' for c in store.list(task['project_id'],'model_call')):
            raise BudgetExceeded('cost_limit','费用触顶')
        return {}
    with pytest.raises(BudgetExceeded):
        await ModelService(store,client).run('coordinator',task,run,session,values,[],'你好',control=control)
    saved=store.projection_get(f'{task["project_id"]}:run_result:{run["id"]}')
    assert saved['output']['payload']['reply']=='你好，请提交原作。'
    assert store.list(task['project_id'],'model_call')[0]['state']=='succeeded'
    await client.close()


@pytest.mark.asyncio
async def test_sdk_compaction_is_audited_auxiliary_run(runtime, monkeypatch):
    from branch_agent.model_service import PersistentSession
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY', 'fake-search-key')
    store,task,run,session,values=runtime;values['context'].update(recent_turns=1,history_token_cap=100)
    persistent=PersistentSession(store,session,task,run)
    await persistent.add_items([{'role':'user','content':'早期人物设定讨论。'*300},
        {'role':'assistant','content':'已讨论人物动机，尚无新的确认。'*300},
        {'role':'user','content':'继续'},{'role':'assistant','content':'继续当前任务。'}])
    formats=[]
    def handler(request):
        data=json.loads(request.content)
        envelope=json.loads(data['input'][-1]['content'])
        try:inner=json.loads(envelope['request'])
        except ValueError:inner={}
        summary='original_archives' in inner
        output_format=data.get('text',{}).get('format',{})
        formats.append('plain' if summary else output_format.get('name'))
        response=provider_response()
        if summary:
            assert output_format.get('type')!='json_schema'
            assert {tool['name'] for tool in data.get('tools', [])} == {'web_search'}
            response['output'][0]['content'][0]['text']='已讨论人物动机；尚无新增确认。'
        return httpx.Response(200,json=response)
    client=AsyncOpenAI(api_key='local-mock',http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),max_retries=0)
    await ModelService(store,client).run('coordinator',task,run,session,values,[],'继续')
    assert formats==['plain','final_output']
    assert store.get(session['id'],task['project_id'])['generation']==2
    children=[t for t in store.list(task['project_id'],'task') if t['parent_task_id']==task['id']]
    assert len(children)==1 and children[0]['budget_root_task_id']==task['budget_root_task_id']
    summary_runs=[r for r in store.list(task['project_id'],'run') if r['task_id']==children[0]['id']]
    assert len(summary_runs)==1 and summary_runs[0]['agent_key']=='context_summarizer'
    assert len(store.list(task['project_id'],'model_call'))==2
    await client.close()


def test_read_tool_absolute_pointer_and_item_identity(runtime):
    from branch_agent.model_service import ReadTools
    store,task,_,_,values=runtime;pid=task['project_id']
    artifact=store.put(new_record('artifact',pid,artifact_kind='read_tool_fixture'))
    store.put(new_record('artifact_version',pid,artifact_id=artifact['id'],origin='program',
        content={'storage':'inline_json','value':{'payload':{'events':[{'event_id':'event-a','text':'原文'}]}}}))
    tool=ReadTools(store,pid,values)
    target={'record_id':artifact['id'],'version':'1','item_id':'event-a','json_pointer':'/payload/events/0/text'}
    _,value=tool.resolve(target);assert value=='原文'
    with pytest.raises(ValueError):tool.resolve({**target,'item_id':'not-there'})
