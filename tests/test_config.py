from test_sdk_integration import runtime
from copy import deepcopy
import pytest

from branch_agent.configuration import ConfigService, SESSION_SHARING_DEFAULTS, initial_values
from branch_agent.prompts import instructions, instruction_parts, stage_agent
from branch_agent.records import new_record
from branch_agent.workflow import session_key


def make_project(store, account, title):
    return store.put(new_record('project', None, owner_account_id=account, title=title))


def test_auxiliary_overrides_are_frozen_with_parent_snapshot(runtime):
    store,task,run,_,old=runtime;pid=task['project_id'];service=ConfigService(store)
    draft=service.draft(pid,{'model':{'name':'gpt-5-nano','reasoning_effort':'low'},
        'context':{'input_token_cap':16000},'prompts':{'summary':'保留真实来源的特殊摘要指令'}},
        scope_kind='auxiliary',scope_key='aux.summary')
    service.publish(pid,draft['id'])
    fresh=service.resolve(pid,'coordinator')
    assert old['auxiliary_configs']['aux.summary']['model']['name']=='gpt-5.6-sol'
    configured=fresh['values']['auxiliary_configs']['aux.summary']
    assert configured['model']['name']=='gpt-5-nano' and configured['context']['input_token_cap']==16000
    assert configured['prompts']['summary']=='保留真实来源的特殊摘要指令'
    assert store.get(run['config_version_id'],pid)['values']==old
    assert draft['id'] in fresh['resolved_from_ids']
    assert any(v['id']==draft['id'] for v in service.view(pid)['versions'])


def test_config_version_assignment_uses_database_max_in_scope(runtime):
    store,task,_,_,_=runtime;pid=task['project_id'];service=ConfigService(store)
    account=store.get(pid,pid)['owner_account_id'];service.values(pid)
    store.put(new_record('config_version',None,config_key='harness:'+account,owner_account_id=account,
        scope_kind='global',scope_key='stage:step2',version=50000,state='retired',values={'model':{'reasoning_effort':'low'}}))
    draft=service.draft(pid,{},scope_kind='stage',scope_key='step2')
    assert draft['version']==50001
    other=service.draft(pid,{},scope_kind='stage',scope_key='step3')
    assert other['version']==1


def test_account_configuration_applies_to_all_projects_but_not_other_accounts(runtime):
    store,task,_,_,_=runtime
    first=store.get(task['project_id'],task['project_id'])
    account=first['owner_account_id']
    second=make_project(store,account,'同账号项目')
    other=make_project(store,'another-account','其他账号项目')
    service=ConfigService(store)

    frozen=service.resolve(first['id'],'step1')
    draft=service.draft(first['id'],{'model':{'reasoning_effort':'low'}})
    service.publish(first['id'],draft['id'])

    assert service.resolve(first['id'],'step1')['values']['model']['reasoning_effort']=='low'
    assert service.resolve(second['id'],'step1')['values']['model']['reasoning_effort']=='low'
    assert service.resolve(other['id'],'step1')['values']['model']['reasoning_effort']=='medium'
    assert store.get(frozen['id'],first['id'])['values']['model']['reasoning_effort']=='medium'


def test_agent_debugger_fields_reach_the_frozen_harness_snapshot(runtime):
    """Every editable debugger group must alter a future Run snapshot."""
    store, task, _, _, _ = runtime
    pid = task['project_id']
    service = ConfigService(store)
    override = {
        'prompts': {'agent': '调试角色指令'},
        'model': {'name': 'gpt-5.6-luna', 'reasoning_effort': 'high', 'max_output_tokens': 64000},
        'run': {'max_turns': 7},
        'retry': {'max_retries': 1},
        'repair': {'max_rounds': 4},
        'tools': {'enabled': ['read_record']},
        'context': {'input_token_cap': 900000, 'safety_margin_tokens': 3000,
                    'recent_turns': 4, 'history_token_cap': 5000},
        'compaction': {'trigger_ratio': 0.8, 'target_ratio': 0.6},
        'output': {'structured': {'step5': False}},
    }
    draft = service.draft(pid, override, scope_kind='agent', scope_key='interaction_architect')
    service.publish(pid, draft['id'])
    frozen = service.resolve(pid, 'step5')['values']

    for group, expected in override.items():
        for key, value in expected.items():
            if group == 'output':
                assert frozen[group][key]['step5'] is False
            else:
                assert frozen[group][key] == value
    assert '调试角色指令' in instructions('step5', frozen)


def test_new_agent_can_be_registered_and_assigned_to_a_stage(runtime):
    store, task, _, _, _ = runtime
    pid = task['project_id']
    service = ConfigService(store)
    values, _ = service.values(pid, 'step5')
    agents = deepcopy(values['prompts']['agents'])
    names = deepcopy(values['prompts']['agent_names'])
    agents['relationship_designer'] = '负责关系互动结构。'
    names['relationship_designer'] = '关系设计 Agent'
    library = service.draft(pid, {'prompts': {'agents': agents, 'agent_names': names}})
    service.publish(pid, library['id'])
    assignment = service.draft(pid, {'prompts': {'stage_agents': {
        'step5': 'relationship_designer'}}}, scope_kind='stage', scope_key='step5')
    service.publish(pid, assignment['id'])
    profile = service.draft(pid, {
        'prompts': {'agent': '关系设计 Agent 的覆盖指令。'},
        'model': {'reasoning_effort': 'high'},
        'tools': {'enabled': ['read_record']},
    }, scope_kind='agent', scope_key='relationship_designer')
    service.publish(pid, profile['id'])

    resolved = service.resolve(pid, 'step5')['values']
    assert stage_agent('step5', resolved) == 'relationship_designer'
    assert resolved['model']['reasoning_effort'] == 'high'
    assert resolved['tools']['enabled'] == ['read_record']
    assert '关系设计 Agent 的覆盖指令。' in instructions('step5', resolved)
    view = service.view(pid, 'step5')
    assert view['registry']['agents']['relationship_designer']['name'] == '关系设计 Agent'
    assert view['selected_agent'] == 'relationship_designer'
    assert '\n\n'.join(v for v in view['instruction_parts'].values() if v) == instructions('step5', resolved)


def test_configuration_page_sections_resolve_at_their_runtime_scope(runtime):
    """The four UI sections must change the same values frozen by future Runs."""
    store, task, _, _, _ = runtime
    pid = task['project_id']
    service = ConfigService(store)

    global_draft = service.draft(pid, {
        'prompts': {'base': '全局调试约束'},
        'context': {'recent_turns': 3},
    }, scope_kind='project')
    service.publish(pid, global_draft['id'])

    agent_draft = service.draft(pid, {
        'prompts': {'agent': '互动结构 Agent 调试指令'},
        'tools': {'enabled': ['get_artifact']},
        'model': {'reasoning_effort': 'high'},
    }, scope_kind='agent', scope_key='interaction_architect')
    service.publish(pid, agent_draft['id'])

    profiles = deepcopy(service.values(pid, 'step7')[0]['context']['profiles'])
    step7 = next(profile for profile in profiles['profiles'] if profile['stage'] == 'step7')
    step7['materials'][0]['enabled'] = False
    stage_draft = service.draft(pid, {
        'prompts': {'stage': 'Step7 调试指令'},
        'output': {'structured': {'step7': False}},
        'context': {'profiles': profiles,
                    'stage_inputs': {'step7': ['source_views', 'game_event_view']},
                    'session_sharing': {'step7': 'step5'}},
        'repair': {'max_rounds': 5},
    }, scope_kind='stage', scope_key='step7')
    service.publish(pid, stage_draft['id'])

    validation_draft = service.draft(pid, {
        'prompts': {'validation': '检查角色动机连续性'},
        'tools': {'enabled': ['read_record']},
    }, scope_kind='agent', scope_key='validation_agent')
    service.publish(pid, validation_draft['id'])

    resolved = service.resolve(pid, 'step7')['values']
    assert resolved['context']['recent_turns'] == 3
    assert resolved['model']['reasoning_effort'] == 'high'
    assert resolved['tools']['enabled'] == ['get_artifact']
    assert resolved['output']['structured']['step7'] is False
    assert resolved['repair']['max_rounds'] == 5
    assert next(profile for profile in resolved['context']['profiles']['profiles']
                if profile['stage'] == 'step7')['materials'][0]['enabled'] is False
    assert resolved['context']['stage_inputs']['step7'] == ['source_views', 'game_event_view']
    assert resolved['context']['session_sharing']['step7'] == 'step5'
    prompt = instructions('step7', resolved)
    assert '全局调试约束' in prompt
    assert '互动结构 Agent 调试指令' in prompt
    assert 'Step7 调试指令' in prompt

    validation = service.resolve(pid, 'step11')['values']
    assert validation['prompts']['validation'] == '检查角色动机连续性'
    assert validation['tools']['enabled'] == ['read_record']
    assert '检查角色动机连续性' in instructions('step11', validation)


def test_session_sharing_configuration_controls_real_session_keys():
    sharing = deepcopy(SESSION_SHARING_DEFAULTS)
    assert session_key(3, sharing=sharing) == session_key(2, sharing=sharing)
    sharing['step3'] = None
    assert session_key(3, sharing=sharing) != session_key(2, sharing=sharing)
    sharing['step10'] = 'step9'
    assert session_key(10, 'chapter-one', sharing) == session_key(9, 'chapter-one', sharing)


def test_stage_cannot_share_a_future_session(runtime):
    store, task, _, _, _ = runtime
    service = ConfigService(store)
    draft = service.draft(task['project_id'], {'context': {'session_sharing': {'step4': 'step5'}}},
                          scope_kind='stage', scope_key='step4')
    with pytest.raises(ValueError, match='只能共享此前阶段'):
        service.publish(task['project_id'], draft['id'])


def test_first_account_read_migrates_latest_project_configuration(runtime):
    store,task,_,_,_=runtime
    first=store.get(task['project_id'],task['project_id'])
    account=first['owner_account_id']
    newer_project=make_project(store,account,'更新配置来源')
    store.put(new_record('config_version',first['id'],config_key='harness',scope_kind='project',
        state='published',version=91,published_at='2026-01-01T00:00:00.000000Z',
        values={'model':{'reasoning_effort':'high'}}))
    legacy=store.put(new_record('config_version',newer_project['id'],config_key='harness',scope_kind='project',
        state='published',version=7,published_at='2026-09-01T00:00:00.000000Z',
        values={'model':{'reasoning_effort':'low'}}))
    store._connection().execute('DELETE FROM account_config_migrations WHERE account_id=%s',(account,))

    service=ConfigService(store)
    values,_=service.values(first['id'],'step1')

    assert values['model']['reasoning_effort']=='low'
    assert store.get(legacy['id'],newer_project['id']) is not None
    globals_=store.list(None,'config_version',limit=100,filters={'state':'published'})
    assert any(row.get('owner_account_id')==account for row in globals_)


def test_step3_prompt_only_asks_for_player_role_and_interaction_ideas():
    values, _ = initial_values()
    prompt = instructions('step3', values)
    assert '只允许询问两件事' in prompt
    assert '玩家扮演哪个角色' in prompt
    assert '是否有其他互动设计想法' in prompt
    assert '不得询问是否采用默认改编策略' in prompt
    assert '不得把Step2中的可选建议、叙事预示或其他待定创作事项扩展成Step3问题' in prompt
    assert 'Harness静默沿用runtime.default_strategy' in prompt


def test_step2_prompt_generates_analysis_without_questions():
    values, _ = initial_values()
    prompt = instructions('step2', values)
    assert '此阶段不向用户提出任何问题' in prompt
    assert '直接输出可读文本' in prompt
    assert '不输出JSON' in prompt
    assert 'Step2 不绑定 output_type' in prompt
    assert '不得询问保留程度、改编策略、玩家身份、哪些人物需要互动' in prompt


def test_validation_agent_is_configurable_and_disabled_by_default():
    values, _ = initial_values()
    assert values['prompts']['validation'] == ''
    assert instructions('step11', values).strip().endswith('不得从会话历史猜测或编造数据库身份。')
    values['prompts']['validation'] = '检查最终 Graph 的剧情连续性。'
    prompt = instructions('step11', values)
    assert '检查最终 Graph 的剧情连续性。' in prompt
    assert '只校验本次提供的最终 Nexo Graph' in prompt


def test_legacy_published_schema_is_upgraded_with_source_message_classification(runtime):
    store, task, _, _, _ = runtime
    pid = task['project_id']
    account = store.get(pid, pid)['owner_account_id']
    service = ConfigService(store)
    service.values(pid)
    schemas = deepcopy(service.catalog.schemas)
    payload = schemas['coordinator_response']['properties']['payload']['anyOf'][0]
    payload['properties'].pop('source_message_kind')
    payload['required'].remove('source_message_kind')
    store.put(new_record('config_version', None, config_key='harness:' + account,
        owner_account_id=account, scope_kind='global', scope_key='project:', state='published',
        version=99, values={'schemas': schemas}))

    resolved = service.resolve(pid, 'coordinator')['values']['schemas']['coordinator_response']
    upgraded = resolved['properties']['payload']['anyOf'][0]
    assert 'source_message_kind' in upgraded['properties']
    assert 'source_message_kind' in upgraded['required']


def test_legacy_plain_text_output_types_are_removed_from_resolved_configuration(runtime):
    store, task, _, _, _ = runtime
    pid = task['project_id']
    account = store.get(pid, pid)['owner_account_id']
    service = ConfigService(store)
    legacy_schemas = deepcopy(service.catalog.schemas)
    legacy_schemas['work_summary'] = {
        'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False,
    }
    legacy_schemas['source_analysis'] = {
        'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False,
    }
    store.put(new_record('config_version', None, config_key='harness:' + account,
        owner_account_id=account, scope_kind='global', scope_key='project:', state='published',
        version=100, values={'schemas': legacy_schemas,
            'output': {'bindings': {**service.catalog.registry['bindings'],
                'aux.summary': 'work_summary', 'step2': 'source_analysis'}}}))

    resolved = service.resolve(pid, 'coordinator')['values']
    assert 'work_summary' not in resolved['schemas']
    assert 'source_analysis' not in resolved['schemas']
    assert 'aux.summary' not in resolved['output']['bindings']
    assert 'step2' not in resolved['output']['bindings']
