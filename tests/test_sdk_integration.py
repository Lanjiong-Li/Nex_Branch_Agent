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
from branch_agent.model_service import ModelService, ModelRunError, append_history
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
async def test_step2_uses_plain_text_without_output_schema(runtime):
    store,base_task,_,_,_=runtime;pid=base_task['project_id'];requests=[]
    workflow=Workflow(store)
    with store.transaction():
        source=workflow.save(pid,'source_text','甲见乙。',origin='import',effective=True)
        views={'result_kind':'ready','payload':{'source_ref':ref(source),'global_events':[],
            'character_views':[],'covered_source_anchors':[],'remaining_source_anchors':[]},
            'questions':[],'evidence_refs':[ref(source)],'notes':[]}
        view=workflow.save(pid,'source_views',views,stage=1,inputs=[ref(source)],effective=True)
    config=ConfigService(store).resolve(pid,'step2')
    task=store.put(new_record('task',pid,conversation_id=base_task['conversation_id'],
        requested_by_message_id=base_task['requested_by_message_id'],intent='generate',state='running'))
    session=store.put(new_record('work_session',pid,conversation_id=base_task['conversation_id'],session_key='adaptation_direction'))
    run=store.put(new_record('run',pid,task_id=task['id'],agent_key='adaptation_planner',session_id=session['id'],
        config_version_id=config['id'],state='running',input_refs=[ref(view)]))
    def handler(request):
        data=json.loads(request.content);requests.append(data)
        assert data.get('text',{}).get('format',{}).get('type')!='json_schema'
        return httpx.Response(200,json=plain_provider_response('故事前提\n甲与乙相遇。'))
    client=AsyncOpenAI(api_key='local-mock-not-real',http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),max_retries=0)
    result=await ModelService(store,client).run('step2',task,run,session,config['values'],workflow.materials(pid,2),'分析原作')
    assert result=='故事前提\n甲与乙相遇。'
    snapshot=store.list(pid,'context_snapshot')[-1]
    assert snapshot['output_schema'] is None
    assert requests
    await client.close()


@pytest.mark.asyncio
async def test_disabling_stage_output_type_changes_real_sdk_request(runtime):
    store,base_task,_,_,_=runtime;pid=base_task['project_id'];requests=[]
    workflow=Workflow(store)
    with store.transaction():
        source=workflow.save(pid,'source_text','甲见乙。',origin='import',effective=True)
    config=ConfigService(store).resolve(pid,'step1')
    config['values']['output']['structured']['step1']=False
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
        assert [tool['name'] for tool in data['tools']]==['read_record','ask_user']
        return httpx.Response(200,json=plain_provider_response('原始文本调试输出'))
    client=AsyncOpenAI(api_key='local-mock-not-real',http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),max_retries=0)
    result=await ModelService(store,client).run('step1',task,run,session,config['values'],workflow.materials(pid,1),'切片')
    assert result=='原始文本调试输出'
    assert store.list(pid,'context_snapshot')[-1]['output_schema'] is None
    assert requests
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
async def test_sdk_compaction_is_audited_auxiliary_run(runtime):
    from branch_agent.model_service import PersistentSession
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
            response['output'][0]['content'][0]['text']='已讨论人物动机；尚无新增确认。'
        return httpx.Response(200,json=response)
    client=AsyncOpenAI(api_key='local-mock',http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),max_retries=0)
    await ModelService(store,client).run('coordinator',task,run,session,values,[],'继续')
    assert formats==['plain','final_output']
    assert store.get(session['id'],task['project_id'])['generation']==2
    children=[t for t in store.list(task['project_id'],'task') if t['parent_task_id']==task['id']]
    assert len(children)==1 and children[0]['budget_root_task_id']==task['budget_root_task_id']
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
