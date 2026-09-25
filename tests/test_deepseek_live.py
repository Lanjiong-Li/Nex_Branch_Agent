"""Opt-in DeepSeek smoke test; normal tests never contact a paid provider."""
from copy import deepcopy
import os

import pytest
from dotenv import load_dotenv

from branch_agent.configuration import ConfigService
from branch_agent.model_service import ModelService
from branch_agent.records import new_record
from test_sdk_integration import runtime


@pytest.mark.asyncio
@pytest.mark.parametrize('model', ['deepseek-flash', 'deepseek-v4-pro'])
@pytest.mark.parametrize('effort', ['none', 'high'])
async def test_deepseek_responses_through_harness(runtime, model, effort):
    load_dotenv('.env', override=False)
    if os.getenv('DEEPSEEK_LIVE_TEST') != '1' or not os.getenv('DEEPSEEK_API_KEY'):
        pytest.skip('set DEEPSEEK_LIVE_TEST=1 and DEEPSEEK_API_KEY for paid live tests')
    store, task, _, session, _ = runtime
    project = task['project_id']
    config_service = ConfigService(store)
    draft = config_service.draft(project, {
        'model': {'name': model, 'reasoning_effort': effort, 'max_output_tokens': 2400},
        'tools': {'enabled': [], 'ask_user_enabled': False},
    }, scope_kind='stage', scope_key='coordinator')
    config_service.publish(project, draft['id'])
    snapshot = config_service.resolve(project, 'coordinator')
    run = store.put(new_record('run', project, task_id=task['id'],
        agent_key='conversation_coordinator', session_id=session['id'],
        config_version_id=snapshot['id'], state='running'))
    service = ModelService(store)
    result = await service.run('coordinator', task, run, session, deepcopy(snapshot['values']), [],
        '只回答你好，不启动改编。',
        instructions_override='请以绑定的 JSON Schema 输出。result_kind 为 ready，payload.reply 为简短中文回答，'
            'payload.source_message_kind 为 request，payload.task_requests、questions、evidence_refs、notes 都为空数组。')
    assert result['result_kind'] == 'ready'
    assert isinstance(result['payload']['reply'], str) and result['payload']['reply']
    calls = [row for row in store.list(project, 'model_call') if row['run_id'] == run['id']]
    assert calls and calls[-1]['state'] == 'succeeded'
    assert calls[-1]['usage']['input_tokens'] > 0
    assert calls[-1]['usage']['output_tokens'] > 0
    assert float(calls[-1]['usage']['estimated_cost']['amount']) > 0
    assert store.list(project, 'context_snapshot')[-1]['model'] == model


@pytest.mark.asyncio
@pytest.mark.parametrize('model', ['deepseek-flash', 'deepseek-v4-pro'])
async def test_deepseek_ask_user_tool_through_harness(runtime, model):
    load_dotenv('.env', override=False)
    if os.getenv('DEEPSEEK_LIVE_TEST') != '1' or not os.getenv('DEEPSEEK_API_KEY'):
        pytest.skip('set DEEPSEEK_LIVE_TEST=1 and DEEPSEEK_API_KEY for paid live tests')
    store, task, _, session, _ = runtime
    project = task['project_id']
    config_service = ConfigService(store)
    draft = config_service.draft(project, {
        'model': {'name': model, 'reasoning_effort': 'none', 'max_output_tokens': 1200},
        'tools': {'enabled': [], 'ask_user_enabled': True},
        'output': {'structured': {'coordinator': False}},
    }, scope_kind='stage', scope_key='coordinator')
    config_service.publish(project, draft['id'])
    snapshot = config_service.resolve(project, 'coordinator')
    run = store.put(new_record('run', project, task_id=task['id'],
        agent_key='conversation_coordinator', session_id=session['id'],
        config_version_id=snapshot['id'], state='running'))
    result = await ModelService(store).run('coordinator', task, run, session,
        deepcopy(snapshot['values']), [], '请问我想要哪种故事氛围。',
        instructions_override='本次必须调用 ask_user 工具，向用户询问故事氛围。'
            '只提一个问题，提供“悬疑”和“轻松”两个 suggested_answers；'
            'confirmation_task_id 设为 null。不要直接输出文本。')
    assert result['__ask_user__'][0]['suggested_answers']
    calls = [row for row in store.list(project, 'tool_call') if row['run_id'] == run['id']]
    assert len(calls) == 1 and calls[0]['tool_name'] == 'ask_user'
    assert calls[0]['state'] == 'succeeded'
