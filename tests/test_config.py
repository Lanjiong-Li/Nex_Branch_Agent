from test_sdk_integration import runtime
from copy import deepcopy
import pytest

from branch_agent.configuration import ConfigService, READ_TOOL_NAMES, SESSION_SHARING_DEFAULTS, initial_values
from branch_agent.model_service import ReadTools
from branch_agent.prompts import (instructions, instruction_parts, instructions_preview,
                                  legacy_prompt_overrides, stage_agent, step1_agent,
                                  step1_run_appendix, LEGACY_HARNESS_RUNTIME,
                                  LEGACY_HARNESS_STAGES, _PREVIOUS_DEFAULT_CLAUSES,
                                  _AUTO_SCHEDULER_CLAUSES,
                                  defaults as prompt_defaults)
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
    assert old['auxiliary_configs']['aux.summary']['model']['name']=='deepseek-flash'
    configured=fresh['values']['auxiliary_configs']['aux.summary']
    assert configured['model']['name']=='gpt-5-nano' and configured['context']['input_token_cap']==16000
    assert configured['prompts']['summary']=='保留真实来源的特殊摘要指令'
    assert store.get(run['config_version_id'],pid)['values']==old
    assert draft['id'] in fresh['resolved_from_ids']
    assert any(v['id']==draft['id'] for v in service.view(pid)['versions'])


def test_summary_uses_its_own_agent_and_auxiliary_assignment_selects_profile(runtime):
    store,task,_,_,_=runtime;pid=task['project_id'];service=ConfigService(store)
    baseline=service.resolve(pid,'coordinator')
    original=baseline['values']['auxiliary_configs']['aux.summary']
    assert stage_agent('aux.summary',original)=='context_summarizer'
    assert '整理便于原 Session 继续工作的简洁摘要' in instruction_parts('aux.summary',original)['creative_agent']
    assert '与用户讨论改编目标' not in instructions('aux.summary',original)

    profile=service.draft(pid,{'model':{'reasoning_effort':'low'},
        'prompts':{'harness':{'agents':{'source_global_parser':'摘要专用归档规则'}}}},
        scope_kind='agent',scope_key='source_global_parser')
    service.publish(pid,profile['id'])
    assignment=service.draft(pid,{'prompts':{'layout_version':3,
        'stage_agents':{'aux.summary':'source_global_parser'}}},
        scope_kind='auxiliary',scope_key='aux.summary')
    service.publish(pid,assignment['id'])

    fresh=service.resolve(pid,'coordinator')['values']['auxiliary_configs']['aux.summary']
    assert stage_agent('aux.summary',fresh)=='source_global_parser'
    assert fresh['model']['reasoning_effort']=='low'
    assert '摘要专用归档规则' in instructions('aux.summary',fresh)
    assert service.view(pid,'aux.summary')['selected_agent']=='source_global_parser'
    assert baseline['values']['auxiliary_configs']['aux.summary']==original


def test_format_repair_agent_and_limits_are_frozen_separately(runtime):
    store,task,run,_,old=runtime;pid=task['project_id'];service=ConfigService(store)
    baseline,_=initial_values()
    assert baseline['context']['history_token_cap']==500000
    assert baseline['repair']=={'max_rounds':2,'content_max_rounds':5,'format_max_rounds':1}
    assert old['auxiliary_configs']['aux.format_repair']['prompts']['stage_agents']['aux.format_repair']=='format_repairer'
    assert old['auxiliary_configs']['aux.format_repair']['output']['structured']['aux.format_repair'] is False
    assert 'candidate_sha256' in instructions('aux.format_repair',old['auxiliary_configs']['aux.format_repair'])

    profile=service.draft(pid,{'model':{'reasoning_effort':'low'},
        'prompts':{'harness':{'agents':{'source_global_parser':'格式校验专用执行规则'}}}},
        scope_kind='agent',scope_key='source_global_parser')
    service.publish(pid,profile['id'])
    configured=service.draft(pid,{'model':{'name':'gpt-5-nano'},
        'prompts':{'layout_version':3,'stage_agents':{'aux.format_repair':'source_global_parser'},
                   'stage':'只做指定路径上的局部格式修复'}},
        scope_kind='auxiliary',scope_key='aux.format_repair')
    service.publish(pid,configured['id'])
    limits=service.draft(pid,{'repair':{'content_max_rounds':4,'format_max_rounds':2}},
        scope_kind='stage',scope_key='step1')
    service.publish(pid,limits['id'])

    fresh=service.resolve(pid,'step1')['values']
    repair=fresh['auxiliary_configs']['aux.format_repair']
    assert fresh['repair']=={'max_rounds':2,'content_max_rounds':4,'format_max_rounds':2}
    assert stage_agent('aux.format_repair',repair)=='source_global_parser'
    assert repair['model']['name']=='gpt-5-nano' and repair['model']['reasoning_effort']=='low'
    assert '只做指定路径上的局部格式修复' in instructions('aux.format_repair',repair)
    assert '格式校验专用执行规则' in instructions('aux.format_repair',repair)
    assert service.view(pid,'aux.format_repair')['selected_agent']=='source_global_parser'
    assert store.get(run['config_version_id'],pid)['values']==old


@pytest.mark.parametrize('field,value',[
    ('content_max_rounds',-1),('format_max_rounds',-1),
    ('content_max_rounds',True),('format_max_rounds','1'),
])
def test_format_and_content_repair_limits_require_nonnegative_integers(runtime,field,value):
    store,task,_,_,_=runtime;service=ConfigService(store)
    values,_=service.values(task['project_id'],'step1')
    schemas=values.pop('schemas')
    values['repair'][field]=value
    with pytest.raises(ValueError,match=f'repair.{field}'):
        service.validate_candidate(values,schemas,'step1.global')


def test_format_repair_agent_cannot_bind_stage_output_type(runtime):
    store,task,_,_,_=runtime;service=ConfigService(store)
    values,_=service.values(task['project_id'],'aux.format_repair')
    schemas=values.pop('schemas')
    assert service.validate_candidate(values,schemas,'aux.format_repair')['enabled'] is False
    values['output']['structured']['aux.format_repair']=True
    with pytest.raises(ValueError,match='内部纯文本产物'):
        service.validate_candidate(values,schemas,'aux.format_repair')


def test_summary_layout_upgrade_changes_only_the_old_default_binding(runtime):
    store,task,_,_,_=runtime;pid=task['project_id'];service=ConfigService(store)
    old={'prompts':{'layout_version':2,'stage_agents':{
        'aux.summary':'conversation_coordinator','step5':'interaction_architect'}}}
    upgraded=legacy_prompt_overrides(old)
    assert old['prompts']['stage_agents']['aux.summary']=='conversation_coordinator'
    assert upgraded['prompts']['layout_version']==3
    assert upgraded['prompts']['stage_agents']=={
        'aux.summary':'context_summarizer','step5':'interaction_architect'}
    legacy=service.draft(pid,old)
    service.publish(pid,legacy['id'])
    assert stage_agent('aux.summary',service.resolve(pid,'aux.summary')['values'])=='context_summarizer'

    custom=service.draft(pid,{'prompts':{'layout_version':2,
        'stage_agents':{'aux.summary':'source_global_parser'}}},
        scope_kind='auxiliary',scope_key='aux.summary')
    service.publish(pid,custom['id'])
    assert stage_agent('aux.summary',service.resolve(pid,'aux.summary')['values'])=='source_global_parser'

    modern=service.draft(pid,{'prompts':{'layout_version':3,
        'stage_agents':{'aux.summary':'conversation_coordinator'}}},
        scope_kind='auxiliary',scope_key='aux.summary')
    service.publish(pid,modern['id'])
    assert stage_agent('aux.summary',service.resolve(pid,'aux.summary')['values'])=='conversation_coordinator'


@pytest.mark.parametrize('layout_version', [2, 3])
def test_previous_default_prompts_upgrade_exactly_and_preserve_edits(layout_version):
    current = prompt_defaults()
    previous = deepcopy(current)
    previous['layout_version'] = layout_version

    def field(values, path):
        node = values
        for key in path[:-1]:
            node = node[key]
        return node, path[-1]

    # Rebuild all 11 defaults saved before the two-view contract changed.
    for path, clauses in _PREVIOUS_DEFAULT_CLAUSES.items():
        current_parent, key = field(current, path)
        previous_parent, _ = field(previous, path)
        old_text = current_parent[key]
        for new_clause, old_clause in clauses:
            assert old_text.count(new_clause) == 1, path
            old_text = old_text.replace(new_clause, old_clause, 1)
        assert old_text != current_parent[key], path
        previous_parent[key] = old_text

    original = {'prompts': previous}
    upgraded = legacy_prompt_overrides(original)
    assert original['prompts'] == previous
    assert upgraded['prompts']['layout_version'] == 3
    for path in _PREVIOUS_DEFAULT_CLAUSES:
        parent, key = field(upgraded['prompts'], path)
        current_parent, _ = field(current, path)
        assert parent[key] == current_parent[key], path
    assert legacy_prompt_overrides(upgraded) == upgraded

    customized = deepcopy(previous)
    for path in _PREVIOUS_DEFAULT_CLAUSES:
        parent, key = field(customized, path)
        parent[key] += '\n用户自定义补充：保留原文。'
    customized_result = legacy_prompt_overrides({'prompts': customized})
    for path in _PREVIOUS_DEFAULT_CLAUSES:
        parent, key = field(customized_result['prompts'], path)
        original_parent, _ = field(customized, path)
        assert parent[key] == original_parent[key], path
    assert legacy_prompt_overrides(customized_result) == customized_result


def test_coordinator_default_authorizes_then_harness_dispatches_step1():
    values = {'prompts': prompt_defaults(), 'output': {'structured': {'coordinator': True}}}
    final = instructions_preview('coordinator', values)['final']
    assert 'begin_adaptation 后由 Harness 根据真实流程状态自动调度' in final
    assert '不依赖你逐阶段调用 run_stage' in final
    assert 'Step1–10 候选保存后由 Harness 创建固定版本确认卡' in final
    assert '不得再为这些候选调用 ask_user' in final
    assert 'run_stage 只返回真实状态，不执行或重跑阶段' in final
    assert '修订与恢复走 Harness 待办和任务恢复入口' in final
    assert 'confirmation_task_id 请求确认' not in final
    assert '实际创作必须调用 begin_adaptation 和 run_stage' not in final


def test_old_coordinator_defaults_upgrade_without_overwriting_user_edits():
    current = prompt_defaults()
    old = deepcopy(current)

    def at(tree, path):
        node = tree
        for key in path[:-1]:
            node = node[key]
        return node, path[-1]

    for path, clauses in _AUTO_SCHEDULER_CLAUSES.items():
        if path == ('harness', 'runtime', 'coordinator'):
            continue  # Runtime overrides are optional and absent from defaults.
        node, key = at(old, path)
        for previous, replacement in clauses:
            assert node[key].count(replacement) == 1
            node[key] = node[key].replace(replacement, previous, 1)
    upgraded = legacy_prompt_overrides({'prompts': old})['prompts']
    for path in _AUTO_SCHEDULER_CLAUSES:
        if path == ('harness', 'runtime', 'coordinator'):
            continue
        node, key = at(upgraded, path)
        expected, _ = at(current, path)
        assert node[key] == expected[key]

    edited = deepcopy(old)
    edited['harness']['agents']['conversation_coordinator'] += '\n用户自定义：先解释剧情因果。'
    edited_result = legacy_prompt_overrides({'prompts': edited})['prompts']
    assert edited_result['harness']['agents']['conversation_coordinator'] == edited['harness']['agents']['conversation_coordinator']


def test_known_old_published_coordinator_route_migrates_only_unchanged_sections():
    old_a = '''### A. 原文导入后承接首轮 Step1

触发条件：原文解析保存且导入预检通过。该次导入包含首轮 Step1 执行授权，由程序直接创建 Step1 任务并排队执行。

- `step1_started=true`：沿真实 `step1_task_id` 核对任务和原文版本，承接该任务。排队时说明“原文已保存，Step1 已排队，将分别整理作品事件和人物经历”；实际运行时依据两路状态说明哪位 Agent 正在处理什么。
- `step1_started=false`：读取 `step1_reason` 与具体回执。输入预算不足时说明对应配置及范围；在途流程冲突时定位该流程。先处理实际原因，再使用当前可用的恢复或重新运行入口。
- 相同原文版本已有排队、运行或等待用户的任务时，继续承接该任务及真实待办。协调器用于状态判断的 `get_workflow_state().workflow` 与自动导入任务分别核对。'''
    custom_b = '### B. 自定义检查\n\n用户自定义：核对来源。'
    text = ('# 对话协调阶段 Harness：导入、Step1 衔接与 Step2 审阅\n\n'
            '用户自定义开头。\n\n' + old_a + '\n\n' + custom_b + '\n\n### F. 用户自定义结尾')
    values = {'prompts': {'layout_version': 3, 'harness': {'stages': {'coordinator': text}}}}
    migrated = legacy_prompt_overrides(values)['prompts']['harness']['stages']['coordinator']
    assert '### A. 授权后由 Harness 启动 Step1' in migrated
    assert '该次导入包含首轮 Step1 执行授权' not in migrated
    assert custom_b in migrated and '用户自定义结尾' in migrated
    assert legacy_prompt_overrides({'prompts': {'layout_version': 3, 'harness': {'stages': {'coordinator': migrated}}}})['prompts']['harness']['stages']['coordinator'] == migrated

    edited = text.replace('触发条件：', '用户自定义触发条件：')
    untouched = legacy_prompt_overrides({'prompts': {'layout_version': 3, 'harness': {'stages': {'coordinator': edited}}}})['prompts']['harness']['stages']['coordinator']
    assert untouched == edited


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
    assert service.resolve(other['id'],'step1')['values']['model']['reasoning_effort']=='high'
    assert store.get(frozen['id'],first['id'])['values']['model']['reasoning_effort']=='high'


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
        'tools': {'enabled': ['read_record'], 'ask_user_enabled': False},
        'context': {'input_token_cap': 900000, 'safety_margin_tokens': 3000,
                    'recent_turns': 4, 'history_token_cap': 5000},
        'compaction': {'trigger_ratio': 0.8, 'target_ratio': 0.6},
        'output': {'structured': {'step5': False}},
    }
    draft = service.draft(pid, override, scope_kind='agent', scope_key='interaction_architect')
    service.publish(pid, draft['id'])
    frozen = service.resolve(pid, 'step5')['values']

    for group, expected in override.items():
        if group == 'tools':
            continue  # Legacy permission switches no longer affect new Runs.
        for key, value in expected.items():
            if group == 'output':
                assert frozen[group][key]['step5'] is False
            elif group == 'prompts':
                assert frozen['prompts']['harness']['legacy_agent'] == value
            else:
                assert frozen[group][key] == value
    assert frozen['tools']['enabled'] == list(READ_TOOL_NAMES)
    assert frozen['tools']['ask_user_enabled'] is True
    assert {tool.name for tool in ReadTools(store, pid, frozen).functions()} == set(READ_TOOL_NAMES)
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
    assert resolved['tools']['enabled'] == list(READ_TOOL_NAMES)
    assert '关系设计 Agent 的覆盖指令。' in instructions('step5', resolved)
    view = service.view(pid, 'step5')
    assert view['registry']['agents']['relationship_designer']['name'] == '关系设计 Agent'
    assert view['selected_agent'] == 'relationship_designer'
    assert '\n\n'.join(v for v in view['instruction_parts'].values() if v) == instructions('step5', resolved)


def test_step1_branches_resolve_distinct_agent_profiles_and_instructions(runtime):
    store, task, _, _, _ = runtime
    pid=task['project_id']
    service=ConfigService(store)
    base,_=service.values(pid,'step1')
    assert step1_agent('global',base)=='source_global_parser'
    assert step1_agent('character',base)=='source_character_parser'
    profile=service.draft(pid,{'model':{'reasoning_effort':'low'},
        'prompts':{'harness':{'agents':{'source_character_parser':'人物分支专属协议'}}}},
        scope_kind='agent',scope_key='source_character_parser')
    service.publish(pid,profile['id'])
    global_snapshot=service.resolve(pid,'step1',agent_key=step1_agent('global',base))['values']
    character_snapshot=service.resolve(pid,'step1',agent_key=step1_agent('character',base))['values']
    assert stage_agent('step1',global_snapshot)=='source_global_parser'
    assert stage_agent('step1',character_snapshot)=='source_character_parser'
    assert global_snapshot['model']['reasoning_effort']=='high'
    assert character_snapshot['model']['reasoning_effort']=='low'
    assert '人物分支专属协议' in instructions('step1',character_snapshot)
    preview=instructions_preview('step1',base)
    assert '作品事件视图' in preview['views']['global']['final']
    assert '逐事件' in preview['views']['global']['final'] and 'analysis' in preview['views']['global']['final']
    assert '不生成逐人物事件原文锚点' in preview['views']['character']['final']
    assert '不输出顶层 analysis' in preview['views']['character']['final']


def test_step1_requires_two_existing_agents(runtime):
    store,task,_,_,_=runtime
    service=ConfigService(store)
    values,_=service.values(task['project_id'],'step1')
    schemas=values.pop('schemas')
    values['prompts']['step1_view_agents']={'global':'source_global_parser'}
    with pytest.raises(ValueError,match='分别配置'):
        service.validate_candidate(values,schemas,'step1.global')
    values['prompts']['step1_view_agents']['character']='missing_agent'
    with pytest.raises(ValueError,match='不存在的 Agent'):
        service.validate_candidate(values,schemas,'step1.character')


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
                    'stage_inputs': {'step7': ['source_global_events', 'game_event_view']},
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
    assert resolved['tools']['enabled'] == list(READ_TOOL_NAMES)
    assert resolved['output']['structured']['step7'] is False
    assert resolved['repair']['max_rounds'] == 5
    assert next(profile for profile in resolved['context']['profiles']['profiles']
                if profile['stage'] == 'step7')['materials'][0]['enabled'] is False
    assert resolved['context']['stage_inputs']['step7'] == [
        'source_global_events', 'game_event_view']
    assert resolved['context']['session_sharing']['step7'] == 'step5'
    prompt = instructions('step7', resolved)
    assert '全局调试约束' in prompt
    assert '互动结构 Agent 调试指令' in prompt
    assert 'Step7 调试指令' in prompt

    validation = service.resolve(pid, 'step11')['values']
    assert validation['prompts']['validation'] == '检查角色动机连续性'
    assert validation['tools']['enabled'] == list(READ_TOOL_NAMES)
    assert '检查角色动机连续性' in instructions('step11', validation)


def test_session_sharing_configuration_controls_real_session_keys():
    sharing = deepcopy(SESSION_SHARING_DEFAULTS)
    assert sharing['step3'] is None
    assert sharing['step4'] is None
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
    assert '不得把Step2中的可选建议、叙事预示或其他待定创作事项扩展成Step3问题' in prompt
    assert 'adaptation_strategy' in prompt
    assert 'runtime.default_strategy' not in prompt


def test_step2_has_dedicated_knowledge_asset_agent():
    values, _ = initial_values()
    prompt = instructions('step2', values)
    assert values['prompts']['stage_agents']['step2']=='source_knowledge_analyst'
    assert values['output']['bindings']['step2']=='source_knowledge_asset'
    assert values['output']['structured']['step2'] is True
    assert values['context']['stage_inputs']['step2']==[
        'source_global_events','source_character_events']
    assert '结构化原作知识资产' in prompt


def test_published_step1_and_step2_overrides_upgrade_to_per_event_analysis():
    from copy import deepcopy
    from branch_agent.schemas import SchemaCatalog

    catalog = SchemaCatalog()
    service = ConfigService.__new__(ConfigService)
    service.catalog = catalog
    global_schema = deepcopy(catalog.schemas['source_global_events'])
    event = global_schema['properties']['payload']['anyOf'][0]['properties']['global_events']['items']
    event['properties'].pop('analysis')
    event['required'].remove('analysis')
    knowledge_schema = deepcopy(catalog.schemas['source_knowledge_asset'])
    knowledge_payload = knowledge_schema['properties']['payload']['anyOf'][0]
    knowledge_payload['properties']['source_global_analysis_ref'] = deepcopy(
        knowledge_payload['properties']['source_global_events_ref'])
    knowledge_payload['required'].append('source_global_analysis_ref')
    upgraded = service._upgrade_schema_overrides({
        'source_global_events': global_schema,
        'source_global_step1_result': {'obsolete': True},
        'source_knowledge_asset': knowledge_schema,
    })
    assert 'source_global_step1_result' not in upgraded
    upgraded_event = upgraded['source_global_events']['properties']['payload']['anyOf'][0]['properties']['global_events']['items']
    assert 'analysis' in upgraded_event['properties']
    assert 'analysis' in upgraded_event['required']
    upgraded_knowledge = upgraded['source_knowledge_asset']['properties']['payload']['anyOf'][0]
    assert 'source_global_analysis_ref' not in upgraded_knowledge['properties']
    assert 'source_global_analysis_ref' not in upgraded_knowledge['required']

    values = {'output': {'bindings': {'step1.global': 'source_global_step1_result'}},
              'context': {'stage_inputs': {'step2': [
                  'source_global_events', 'source_global_analysis', 'source_character_events']},
                  'profiles': {'common_materials': [
                      {'id': 'optional_old_analysis', 'source': {'schema_id': 'source_global_analysis'},
                       'selectors': ['']},
                  ], 'profiles': [{'stage': 'step2', 'materials': [
                      {'id': 'source_global_analysis', 'source': {'schema_id': 'source_global_analysis'},
                       'selectors': ['']},
                      {'id': 'source_global_events_detail', 'source': {'schema_id': 'source_global_events'},
                       'selectors': ['/payload/global_events']},
                  ]}]}}}
    resolved = service._upgrade_context_overrides(values)
    assert resolved['output']['bindings']['step1.global'] == 'source_global_events'
    assert resolved['context']['stage_inputs']['step2'] == [
        'source_global_events', 'source_character_events']
    assert [item['id'] for item in resolved['context']['profiles']['profiles'][0]['materials']] == [
        'source_global_events_detail']
    assert resolved['context']['profiles']['common_materials'] == []


def test_validation_agent_is_configurable_and_disabled_by_default():
    values, _ = initial_values()
    assert values['prompts']['validation'] == ''
    assert instructions('step11', values).count('不得从会话历史猜测或编造数据库身份。') == 1
    values['prompts']['validation'] = '检查最终 Graph 的剧情连续性。'
    prompt = instructions('step11', values)
    assert '检查最终 Graph 的剧情连续性。' in prompt
    assert '只校验本次提供的最终 Nexo Graph' in prompt


def test_creative_and_harness_layers_preview_the_effective_run_prompt():
    values, _ = initial_values()
    p = values['prompts']
    p['base'] = '共通创作测试'
    p['agents']['interaction_architect'] = '角色创作测试'
    p['stages']['step5'] = '阶段创作测试'
    p['harness']['base'] = '共通协议测试'
    p['harness']['agents']['interaction_architect'] = '角色协议测试'
    p['harness']['stages']['step5'] = '阶段协议测试'
    p['harness']['runtime']['step5'] = '运行协议测试'
    parts = instruction_parts('step5', values)
    assert list(parts.values())[1:] == [
        '共通协议测试', '共通创作测试', '角色协议测试', '角色创作测试',
        '阶段协议测试', '阶段创作测试', '运行协议测试']
    preview = instructions_preview('step5', values)
    assert preview['final'] == instructions('step5', values) + '\n\n' + p['harness']['ask_user']
    p['harness']['ask_user'] = '新提问协议'
    assert instructions_preview('step5', values)['final'].endswith('新提问协议')


def test_new_stage_harness_rule_avoids_duplicate_legacy_runtime_text():
    values, _ = initial_values()
    harness = values['prompts']['harness']
    assert harness['runtime'] == {}
    assert LEGACY_HARNESS_RUNTIME['step5'] in harness['stages']['step5']
    assert instruction_parts('step5', values)['harness_runtime'] == ''
    harness['runtime']['step5'] = LEGACY_HARNESS_RUNTIME['step5']
    assert instruction_parts('step5', values)['harness_runtime'] == ''
    harness['stages']['step5'] = LEGACY_HARNESS_STAGES['step5']
    assert instruction_parts('step5', values)['harness_runtime'] == LEGACY_HARNESS_RUNTIME['step5']
    assert instructions('step5', values).count('不得从会话历史猜测或编造数据库身份。') == 1
    harness['runtime']['step5'] = '旧配置自定义的运行协议'
    assert instruction_parts('step5', values)['harness_runtime'] == '旧配置自定义的运行协议'
    assert '旧配置自定义的运行协议' in instructions('step5', values)
    harness['stages'] = deepcopy(LEGACY_HARNESS_STAGES)
    harness['runtime'] = deepcopy(LEGACY_HARNESS_RUNTIME)
    for stage, rule in LEGACY_HARNESS_RUNTIME.items():
        assert instruction_parts(stage, values)['harness_runtime'] == rule


def test_step1_preview_follows_full_or_window_runtime_and_disables_asking():
    values, _ = initial_values()
    harness = values['prompts']['harness']
    full = instructions_preview('step1', values, step1_mode='full')
    global_full = full['views']['global']
    assert global_full['parts']['tool_guidance'] == harness['no_ask_user']
    assert global_full['parts']['step1_mode'] == step1_run_appendix('global', 'full', values)
    assert harness['window'] not in global_full['final']
    assert '窗口模式' not in global_full['final']
    assert full['window_appendix'] is None
    window = instructions_preview('step1', values, step1_mode='window')['views']['character']
    assert harness['window'] in window['parts']['step1_mode']
    assert '当前独立产物：主要人物事件' in window['final']
    assert window['parts']['tool_guidance'] == harness['no_ask_user']
    assert step1_run_appendix('character','window',values,12,56).find('[12,56)') >= 0


def test_legacy_mixed_prompt_is_preserved_in_advanced_layer():
    old = {'prompts': {'base': '旧自定义共通规则',
                       'agent': '旧自定义 Agent 规则',
                       'stage': '旧自定义阶段规则'}}
    upgraded = legacy_prompt_overrides(old)
    h = upgraded['prompts']['harness']
    assert h['legacy_base'] == '旧自定义共通规则'
    assert h['legacy_agent'] == '旧自定义 Agent 规则'
    assert h['legacy_stage'] == '旧自定义阶段规则'
    assert upgraded['prompts']['validation_enabled'] is True
    values, _ = initial_values()
    from branch_agent.configuration import merge
    effective = merge(values, upgraded)
    assert '旧自定义共通规则' in instructions('step5', effective)
    assert effective['prompts']['base'] != '旧自定义共通规则'


def test_old_frozen_config_without_harness_layer_remains_readable():
    values, _ = initial_values()
    values['prompts'].pop('harness')
    values['prompts'].pop('validation_enabled')
    assert '运行时固定引用协议' in instructions('step5', values)
    assert 'ask_user' in instructions_preview('coordinator', values)['final']


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


def test_obsolete_step2_output_binding_is_rejected(runtime):
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

    with pytest.raises(ValueError, match='必须保留全部|输出类型绑定不存在|语义绑定不能互换'):
        service.resolve(pid, 'coordinator')
