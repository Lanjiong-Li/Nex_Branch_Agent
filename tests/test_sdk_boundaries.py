"""Boundary regressions use the real SDK and a local HTTP mock, never live API keys."""
import json

import httpx
import pytest
from openai import AsyncOpenAI

from branch_agent.context import unwrap
from branch_agent.model_service import ModelRunError, ModelService, append_history
from test_sdk_integration import runtime, provider_response


def steer_updates(items):
    updates = []
    for item in items:
        if item.get('role') != 'user' or not isinstance(item.get('content'), str):
            continue
        try:
            value = json.loads(item['content'])
        except ValueError:
            continue
        if isinstance(value, dict) and 'harness_steer_message_id' in value:
            updates.append(value)
    return updates


@pytest.mark.asyncio
async def test_adopted_steer_is_present_once_in_every_model_turn(runtime):
    store, task, run, session, config = runtime
    project = task['project_id']
    steer_text = '新增约束：请保留B结局，接下来的分析都按这个要求。'
    steer = append_history(store, project, task['conversation_id'], steer_text,
                           role='user', kind='message', visibility='conversation')
    requests = []

    async def control():
        # Runtime continues returning this pending message until its receipt commits.
        return {'stop': False, 'steer_messages': [steer]}

    def handler(request):
        data = json.loads(request.content)
        requests.append(data)
        response = provider_response()
        if len(requests) == 1:
            response['output'] = [{'id': 'fc_steer_read', 'type': 'function_call',
                'call_id': 'steer_read', 'name': 'read_record', 'status': 'completed',
                'arguments': json.dumps({'record_id': task['requested_by_message_id'],
                    'version': None, 'cursor': None, 'item_id': None, 'json_pointer': None})}]
        return httpx.Response(200, json=response)

    client = AsyncOpenAI(api_key='local-boundary-test-only', max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        await ModelService(store, client).run('coordinator', task, run, session, config, [],
                                            '查询我的原始要求，然后继续分析。', control=control)
    finally:
        await client.close()

    assert len(requests) == 2
    expected_update = {'harness_steer_message_id': steer['id'], 'user_update': steer_text}
    for request in requests:
        assert steer_updates(request['input']) == [expected_update]
    snapshots = store.list(project, 'context_snapshot')
    assert len(snapshots) == 2
    for snapshot in snapshots:
        actual = unwrap(snapshot['input_items'], store, project)
        assert steer_updates(actual) == [expected_update]
        assert steer['id'] in snapshot['history_ids']
    assert any(item.get('type') == 'function_call_output' and item['call_id'] == 'steer_read'
               for item in requests[1]['input'])


@pytest.mark.asyncio
async def test_rejected_invocation_keeps_received_response_usage_and_archive(runtime):
    store, task, run, session, config = runtime
    project = task['project_id']
    response = provider_response()
    response['output'] = [{'id': f'fc_invalid_{index}', 'type': 'function_call',
        'call_id': 'reused_call_identity', 'name': 'read_record', 'status': 'completed',
        'arguments': json.dumps({'record_id': task['requested_by_message_id'], 'version': None,
            'cursor': str(index), 'item_id': None, 'json_pointer': None})}
        for index in range(2)]
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response)

    client = AsyncOpenAI(api_key='local-boundary-test-only', max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(ModelRunError) as rejected:
            await ModelService(store, client).run('coordinator', task, run, session, config,
                                                [], '读取原始要求。')
    finally:
        await client.close()

    assert rejected.value.code == 'ModelBehaviorError'
    assert len(requests) == 1
    calls = store.list(project, 'model_call')
    assert len(calls) == 1
    call = calls[0]
    # Provider receipt is known even though SDK semantic validation rejected it.
    assert call['state'] == 'succeeded'
    assert call['provider_response_id'] == response['id']
    assert call['usage']['input_tokens'] == response['usage']['input_tokens']
    assert call['usage']['output_tokens'] == response['usage']['output_tokens']
    assert call['response_history_ids']
    archives = [unwrap(store.get(identity, project)['content'], store, project)
                for identity in call['response_history_ids']]
    assert len(archives) == 1
    assert archives[0]['provider_response_id'] == response['id']
    assert archives[0]['output'] == response['output']
    assert archives[0]['raw_usage'] == response['usage']
    assert not store.list(project, 'tool_call')  # No invalid invocation was executed.
    assert store.projection_get(f'{project}:run_result:{run["id"]}') is None


@pytest.mark.asyncio
async def test_received_response_without_usage_keeps_unknown_counters(runtime):
    store, task, run, session, config = runtime
    project = task['project_id']
    response = provider_response()
    response.pop('usage')
    client = AsyncOpenAI(api_key='local-boundary-test-only', max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=response))))
    try:
        await ModelService(store, client).run('coordinator', task, run, session, config,
                                            [], '读取原始要求。')
    finally:
        await client.close()

    calls = store.list(project, 'model_call')
    assert len(calls) == 1
    call = calls[0]
    assert call['state'] == 'succeeded'
    assert call['provider_response_id'] == response['id']
    for key in ('input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_tokens',
                'estimated_cost', 'pricing_version'):
        assert call['usage'][key] is None
    archive = unwrap(store.get(call['response_history_ids'][0], project)['content'], store, project)
    assert archive['raw_usage'] == {}
@pytest.mark.asyncio
async def test_expired_session_writer_cannot_append_after_new_owner(runtime):
    from branch_agent.model_service import PersistentSession,ModelRunError
    store,task,run,row,_=runtime
    writer=PersistentSession(store,row,task,run)
    newer=dict(row);newer['fencing_token']+=1
    store.update(newer,row['row_version'])
    with pytest.raises(ModelRunError,match='新的运行接管'):
        await writer.add_items([{'role':'assistant','content':'过期运行的答案'}])
    assert not store.list(task['project_id'],'session_item')


@pytest.mark.asyncio
async def test_cache_write_surcharge_uses_preserved_provider_usage(runtime):
    from decimal import Decimal
    store,task,run,session,config=runtime
    response=provider_response()
    response['usage']['input_tokens_details']={'cached_tokens':30,'cache_write_tokens':20}
    client=AsyncOpenAI(api_key='mock',http_client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request:httpx.Response(200,json=response))),max_retries=0)
    try:await ModelService(store,client).run('coordinator',task,run,session,config,[],'你好')
    finally:await client.close()
    call=store.list(task['project_id'],'model_call')[0]
    assert Decimal(call['usage']['estimated_cost']['amount'])==Decimal('0.001112')


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal,reason,expected', [
    ('incomplete','max_output_tokens','output_limit_exceeded'),
    ('incomplete','content_filter','response_incomplete'),
    ('failed','server_error','response_failed'),
    ('completed','refusal','model_refusal'),
])
@pytest.mark.parametrize('streaming', [False, True])
async def test_known_terminal_response_is_archived_before_sdk_rejects(runtime, monkeypatch, terminal, reason, expected, streaming):
    from agents import Runner
    store,task,run,session,config=runtime;pid=task['project_id']
    response=provider_response();response['status']=terminal
    sensitive='sk-sensitive-fixture-only-do-not-display'
    if terminal=='incomplete':response['incomplete_details']={'reason':reason}
    elif terminal=='failed':response['error']={'code':reason,'message':sensitive}
    else:response['output'][0]['content']=[{'type':'refusal','refusal':sensitive}]
    if streaming:
        async def consume_stream(agent,input,**kwargs):
            result=Runner.run_streamed(agent,input,**kwargs)
            async for _ in result.stream_events():pass
            return result
        monkeypatch.setattr(Runner,'run',consume_stream)
    requests=[]
    def handler(request):
        requests.append(json.loads(request.content))
        if streaming:
            events=[{'type':'response.created','sequence_number':0,'response':{**response,'status':'in_progress','output':[]}},
                    {'type':'response.'+terminal,'sequence_number':1,'response':response}]
            return httpx.Response(200,headers={'content-type':'text/event-stream'},
                content=''.join('data: '+json.dumps(event)+'\n\n' for event in events))
        return httpx.Response(200,json=response)
    client=AsyncOpenAI(api_key='local-terminal-test-only',max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(ModelRunError) as caught:
            await ModelService(store,client).run('coordinator',task,run,session,config,[],'读取固定输入')
    finally:await client.close()
    assert caught.value.code==expected
    assert caught.value.details['known_outcome'] is True
    assert caught.value.details['terminal_status']==terminal
    assert caught.value.details['max_output_tokens']==config['model']['max_output_tokens']
    if reason=='max_output_tokens':
        assert str(config['model']['max_output_tokens']) in str(caught.value)
        assert '推理' in str(caught.value)
    elif reason=='content_filter':assert '内容策略' in str(caught.value)
    elif terminal=='failed':assert reason in str(caught.value)
    assert sensitive not in str(caught.value) and sensitive not in json.dumps(caught.value.details)
    assert len(requests)==1
    calls=store.list(pid,'model_call');assert len(calls)==1
    call=calls[0]
    assert call['state']=='failed' and call['error']['code']==expected
    assert call['error']['details']['known_outcome'] is True
    assert call['error']['details']['terminal_status']==terminal
    assert call['provider_response_id']==response['id']
    assert call['usage']['input_tokens']==100 and call['usage']['output_tokens']==40
    assert call['usage']['estimated_cost'] is not None
    archive=unwrap(store.get(call['response_history_ids'][0],pid)['content'],store,pid)
    assert archive['provider_response_id']==response['id']
    assert archive['provider_response']['status']==terminal
    assert archive['output']==response['output']
    # The complete SDK response retains optional null/default fields as well.
    for key,value in response['output'][0]['content'][0].items():
        assert archive['provider_response']['output'][0]['content'][0][key]==value
    assert archive['raw_usage']==response['usage']
    assert archive['terminal_event']=='response.'+terminal
    assert not store.projection_get(f'{pid}:run_result:{run["id"]}')
    assert not any(unwrap(item['sdk_item'],store,pid).get('role')=='assistant'
                   for item in store.list(pid,'session_item'))


@pytest.mark.asyncio
async def test_incomplete_without_usage_stays_known_failure_with_unknown_cost(runtime):
    store,task,run,session,config=runtime;pid=task['project_id']
    response=provider_response();response.pop('usage');response.update(status='incomplete',incomplete_details={'reason':'max_output_tokens'})
    client=AsyncOpenAI(api_key='local-test',max_retries=0,http_client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request:httpx.Response(200,json=response))))
    try:
        with pytest.raises(ModelRunError) as caught:
            await ModelService(store,client).run('coordinator',task,run,session,config,[],'读取输入')
    finally:await client.close()
    assert caught.value.code=='output_limit_exceeded'
    call=store.list(pid,'model_call')[0]
    assert call['state']=='failed' and call['error']['details']['known_outcome'] is True
    assert call['usage']['input_tokens'] is None and call['usage']['estimated_cost'] is None
    assert unwrap(store.get(call['response_history_ids'][0],pid)['content'],store,pid)['raw_usage']=={}


@pytest.mark.asyncio
async def test_transport_failure_without_terminal_remains_unknown(runtime):
    store,task,run,session,config=runtime
    def handler(request):raise httpx.ReadError('sk-sensitive-fixture-only',request=request)
    client=AsyncOpenAI(api_key='local-test',max_retries=0,http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(ModelRunError) as caught:
            await ModelService(store,client).run('coordinator',task,run,session,config,[],'读取输入')
    finally:await client.close()
    call=store.list(task['project_id'],'model_call')[0]
    assert call['state']=='unknown' and not call['response_history_ids']
    assert 'sk-sensitive' not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('status,provider_code,expected,phrase', [
    (401,'invalid_api_key','authentication_failed','凭据'),
    (403,None,'permission_denied','权限'),
    (429,'insufficient_quota','quota_exhausted','额度'),
    (429,'credit_balance_exhausted','quota_exhausted','余额'),
    (429,'rate_limit_exceeded','rate_limit_exceeded','速率'),
    (404,'model_not_found','model_not_found','模型'),
    (400,'context_length_exceeded','context_length_exceeded','上下文'),
    (400,'invalid_json_schema','invalid_output_schema','Schema'),
    (400,None,'BadRequestError','HTTP 400'),
])
async def test_http_rejection_reports_only_safe_recognized_reason(runtime,status,provider_code,expected,phrase):
    store,task,run,session,config=runtime;pid=task['project_id']
    config['retry']['max_retries']=0
    sensitive='sk-sensitive-fixture-only-do-not-display'
    payload={'error':{'type':'invalid_request_error','code':provider_code,'message':sensitive,'param':sensitive}}
    client=AsyncOpenAI(api_key='local-test',max_retries=0,http_client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request:httpx.Response(status,json=payload))))
    try:
        with pytest.raises(ModelRunError) as caught:
            await ModelService(store,client).run('coordinator',task,run,session,config,[],'读取输入')
    finally:await client.close()
    assert caught.value.code==expected
    assert phrase in str(caught.value)
    assert caught.value.details['http_status']==status
    assert sensitive not in str(caught.value) and sensitive not in json.dumps(caught.value.details)
    call=store.list(pid,'model_call')[0]
    assert call['state']=='failed' and call['error']['code']==expected
    assert sensitive not in json.dumps(call['error'])
    assert not call['response_history_ids']
