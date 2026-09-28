"""Draft/published configuration and immutable run snapshots."""
from __future__ import annotations
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
import re
from psycopg.types.json import Jsonb
from .storage import VersionConflict
from .schemas import ROOT, SchemaCatalog, digest
from .prompts import (defaults as prompt_defaults, stage_agent, step1_agent, instruction_parts,
                      legacy_prompt_overrides)

# Capability data is explicit and editable only through registered model profiles.
MODELS = {
 'deepseek-flash': {'provider':'deepseek','context_window':1000000, 'max_output_tokens':384000,
  'reasoning_efforts':['none','low','high','max'], 'tokenizer':'o200k_base',
  'token_estimate_multiplier':1.25,
  'source':'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'},
 'deepseek-v4-pro': {'provider':'deepseek','context_window':1000000, 'max_output_tokens':384000,
  'reasoning_efforts':['none','low','high','max'], 'tokenizer':'o200k_base',
  'token_estimate_multiplier':1.25,
  'source':'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'},
 'gpt-5.6-sol': {'context_window':1050000, 'max_output_tokens':128000,
  'reasoning_efforts':['none','low','medium','high','xhigh','max'], 'tokenizer':'o200k_base',
  'source':'https://developers.openai.com/api/docs/models/gpt-5.6-sol'},
 'gpt-5.6-luna': {'context_window':1050000, 'max_output_tokens':128000,
  'reasoning_efforts':['none','low','medium','high','xhigh','max'], 'tokenizer':'o200k_base',
  'source':'https://developers.openai.com/api/docs/models/gpt-5.6-luna'},
}

# Human-facing stage input choices.  Detailed selectors remain in
# stage-materials.json; this list controls which complete, fixed artifacts are
# available to a stage before those selectors are applied.
ARTIFACT_PRODUCERS = {
    'source_text': 0,
    'source_global_events': 1,
    'source_character_events': 1,
    'source_knowledge_asset': 2,
    'adaptation_strategy': 3,
    'adaptation_plan': 4,
    'game_event_view': 5,
    'ending_routes': 7,
    'player_profiles': 8,
    'chapter_design': 9,
    'chapter_graph': 10,
    'nexo_graph': 10,
}
STAGE_INPUT_DEFAULTS = {
    'step1': ['source_text'],
    'step2': ['source_global_events', 'source_character_events'],
    'step3': ['source_knowledge_asset'],
    'step4': ['source_knowledge_asset', 'adaptation_strategy'],
    'step5': ['source_global_events', 'source_character_events', 'source_knowledge_asset'],
    'step6': ['game_event_view'],
    'step7': ['game_event_view'],
    'step8': ['game_event_view', 'ending_routes'],
    'step9': ['adaptation_plan', 'source_text'],
    'step10': ['chapter_design', 'source_text'],
    'step11': ['nexo_graph'],
}
SESSION_SHARING_DEFAULTS = {
    'step1': None,
    'step2': None,
    'step3': None,
    'step4': None,
    'step5': None,
    'step6': 'step5',
    'step7': None,
    'step8': None,
    'step9': None,
    'step10': None,
    'step11': None,
}
READ_TOOL_NAMES = ('list_records', 'read_record')
for _name in ('gpt-5-mini','gpt-5-nano'):
    MODELS[_name]={'context_window':400000,'max_output_tokens':128000,
        'reasoning_efforts':['minimal','low','medium','high'],'tokenizer':'o200k_base',
        'source':'https://developers.openai.com/api/docs/models/'+_name}


def merge(base, overlay):
    out = deepcopy(base)
    for key, value in overlay.items():
        out[key] = merge(out[key], value) if isinstance(value,dict) and isinstance(out.get(key),dict) else deepcopy(value)
    return out


def initial_values():
    raw = json.loads((ROOT/'docs/context/defaults.json').read_text())
    values = {k:v for k,v in raw.items() if k not in ('contract_version','status','stage_overrides')}
    values = merge(values, {
        'prompts':prompt_defaults(), 'run':{'max_turns':10}, 'retry':{'max_retries':2},
        'repair':{'max_rounds':2,'content_max_rounds':5,'format_max_rounds':1},
        'task':{'max_active_seconds':3600,'max_cost':{'amount':'20.00','currency':'USD'},'disabled_limits':[]},
        'recovery':{'max_attempts':3,'backoff_seconds':[5,30,120]},
        'runtime':{'lease_seconds':60,'heartbeat_seconds':15,'stop_grace_seconds':30},
        'tools':{'enabled':list(READ_TOOL_NAMES),'ask_user_enabled':True,
                 'read_token_cap':4000,'descriptions':{}},
        'retrieval':{'top_k':8,'snippet_tokens':200,'neighbor_messages':2},
        'compaction':{'trigger_ratio':0.75,'target_ratio':0.5},
        'summary':{'target_tokens':1500,'checkpoint_events':['confirmation','stage.completed']},
        'context':{'recent_turns':6,'history_token_cap':500000,
           'stage_inputs':deepcopy(STAGE_INPUT_DEFAULTS),
           'session_sharing':deepcopy(SESSION_SHARING_DEFAULTS),
           'profiles':json.loads((ROOT/'docs/context/stage-materials.json').read_text())},
        # `structured` controls whether a stage is bound to its registered
        # Agents SDK output_type.  A disabled structured stage is a diagnostic
        # run: the raw text is kept and the workflow pauses before any business
        # artifact is committed or a downstream stage is started.
        'output':{'bindings':SchemaCatalog().registry['bindings'],
                  'structured':{'coordinator':True,
                      **{f'step{i}': True for i in range(2,12)},
                      'step1.global':True,'step1.character':True,
                      'aux.summary':False,'aux.format_repair':False,
                      'aux.history_answer':True,'aux.subtask':True}},
        'model':{'temperature':None}, 'pricing':{'version':'multi-provider-2026-09-25-peak-usd',
            'source':'https://api-docs.deepseek.com/quick_start/pricing/',
            'models':{'deepseek-flash':{'input_per_million':'0.30','cached_input_per_million':'0.006',
                'output_per_million':'1.20','long_input_threshold':1000000,
                'long_input_multiplier':'1','long_output_multiplier':'1','cache_write_multiplier':'1',
                'source':'https://api-docs.deepseek.com/quick_start/pricing/'},
              'deepseek-v4-pro':{'input_per_million':'1.32','cached_input_per_million':'0.044',
                'output_per_million':'3.96','long_input_threshold':1000000,
                'long_input_multiplier':'1','long_output_multiplier':'1','cache_write_multiplier':'1',
                'source':'https://api-docs.deepseek.com/quick_start/pricing/'},
              'gpt-5.6-sol':{'input_per_million':'4.00','cached_input_per_million':'0.40',
                'output_per_million':'20.00','long_input_threshold':272000,
                'long_input_multiplier':'2','long_output_multiplier':'1.5','cache_write_multiplier':'1.25'},
              'gpt-5.6-luna':{'input_per_million':'0.20','cached_input_per_million':'0.02',
                'output_per_million':'1.20','long_input_threshold':272000,
                'long_input_multiplier':'2','long_output_multiplier':'1.5','cache_write_multiplier':'1.25',
                'source':'https://developers.openai.com/api/docs/models/gpt-5.6-luna'}}},
    })
    for name,inp,cached,out in [('gpt-5-mini','0.25','0.025','2.00'),('gpt-5-nano','0.05','0.005','0.40')]:
        values['pricing']['models'][name]={'input_per_million':inp,'cached_input_per_million':cached,
            'output_per_million':out,'long_input_threshold':400000,'long_input_multiplier':'1',
            'long_output_multiplier':'1','cache_write_multiplier':'1','source':MODELS[name]['source']}
    return values, raw['stage_overrides']


def validate_values(values, schemas):
    prompts=values.get('prompts',{})
    harness=prompts.get('harness',{})
    if not isinstance(harness,dict) or any(not isinstance(harness.get(key),str)
        for key in ('base','manager','manager_structured','manager_plain',
                    'ask_user','no_ask_user','window')):
        raise ValueError('Harness 协议指令必须是文本')
    if type(prompts.get('validation_enabled')) is not bool:
        raise ValueError('校验 Agent 启用开关必须为布尔值')
    if any(not isinstance(harness.get(key),dict) or
           any(not isinstance(value,str) for value in harness[key].values())
           for key in ('agents','stages','runtime','legacy_agents','legacy_stages')):
        raise ValueError('Harness Agent 与阶段协议必须是文本映射')
    if any(not isinstance(harness.get(key,''),str) for key in
           ('legacy_base','legacy_agent','legacy_stage')):
        raise ValueError('旧版指令必须是文本')
    agents=prompts.get('agents')
    names=prompts.get('agent_names')
    assignments=prompts.get('stage_agents')
    executable={'coordinator',*[f'step{i}' for i in range(1,12)],
                'aux.summary','aux.format_repair','aux.history_answer','aux.subtask'}
    if not isinstance(agents,dict) or not agents:
        raise ValueError('Agent 库不能为空')
    if any(not isinstance(key,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',key)
           for key in agents):
        raise ValueError('agent_key 必须以小写字母开头，只能包含小写字母、数字和下划线')
    if any(not isinstance(value,str) for value in agents.values()):
        raise ValueError('Agent instructions 必须是文本')
    if not isinstance(names,dict) or set(names)!=set(agents) or any(
            not isinstance(value,str) or not value.strip() for value in names.values()):
        raise ValueError('每个 Agent 都必须有非空显示名称')
    if not isinstance(assignments,dict) or set(assignments)!=executable:
        raise ValueError('阶段 Agent 映射必须覆盖全部可执行阶段')
    if any(agent_key not in agents for agent_key in assignments.values()):
        raise ValueError('阶段 Agent 映射引用了不存在的 Agent')
    step1_assignments=prompts.get('step1_view_agents')
    if not isinstance(step1_assignments,dict) or set(step1_assignments)!={'global','character'}:
        raise ValueError('Step1 必须分别配置全局事件与主要人物事件 Agent')
    if any(not isinstance(agent_key,str) or agent_key not in agents
           for agent_key in step1_assignments.values()):
        raise ValueError('Step1 事件视图引用了不存在的 Agent')
    unsupported_output=set(values.get('output',{}))-{'bindings','structured'}
    if unsupported_output:
        raise ValueError('尚未注册这些输出消费端配置：'+', '.join(sorted(unsupported_output))+'；请使用现有Schema编辑入口')
    structured=values.get('output',{}).get('structured',{})
    if not isinstance(structured,dict) or any(type(value) is not bool for value in structured.values()):
        raise ValueError('output.structured 必须是阶段到布尔值的映射')
    step1_outputs={'step1.global','step1.character'}
    if not step1_outputs <= set(structured):
        raise ValueError('Step1 两路必须分别配置 output_type 开关')
    allowed=executable|step1_outputs
    if set(structured)-set(allowed):
        raise ValueError('存在未注册的 output_type 阶段开关')
    for auxiliary in ('aux.summary','aux.format_repair'):
        if structured.get(auxiliary) is True:
            raise ValueError(f'{auxiliary} 是内部纯文本产物，不绑定 output_type')
    if values['summary']['checkpoint_events']!=['confirmation','stage.completed']:
        raise ValueError('当前运行适配器固定在关键确认和阶段完成保存检查点，尚未注册其他检查点事件配置')
    if values['context']['version_mapping']!={'field_mappings':[],'dependency_scope':'selected_with_guards','extra_guard_paths':[],'transforms':[]}:
        raise ValueError('当前版本采用整产物的保守依赖失效；字段映射、语义保护和跨版本确认继承尚未注册运行适配器')
    stage_inputs=values['context'].get('stage_inputs')
    if not isinstance(stage_inputs,dict) or set(stage_inputs)!=set(STAGE_INPUT_DEFAULTS):
        raise ValueError('阶段输入配置必须保留 Step1–Step11')
    for stage, selected in stage_inputs.items():
        if not isinstance(selected,list) or len(selected)!=len(set(selected)) or any(
                not isinstance(kind,str) or kind not in ARTIFACT_PRODUCERS for kind in selected):
            raise ValueError(f'{stage} 的阶段输入包含未注册或重复的产物')
        number=int(stage[4:])
        if any(ARTIFACT_PRODUCERS[kind] >= number and not (stage=='step11' and kind=='nexo_graph') for kind in selected):
            raise ValueError(f'{stage} 只能选择此前已经产生的产物')
    if stage_inputs['step1']!=['source_text']:
        raise ValueError('Step1 必须且只能读取原作全文')
    sharing=values['context'].get('session_sharing')
    if not isinstance(sharing,dict) or set(sharing)!=set(SESSION_SHARING_DEFAULTS):
        raise ValueError('Session 配置必须保留 Step1–Step11')
    for stage,target in sharing.items():
        if target is None:
            continue
        if not isinstance(target,str) or target not in sharing or int(target[4:])>=int(stage[4:]):
            raise ValueError(f'{stage} 只能共享此前阶段的 Session')
    model = values['model']; limits = MODELS.get(model['name'])
    if not limits: raise ValueError('模型能力未注册，请先由服务端添加准确的模型适配配置')
    if model['reasoning_effort'] not in limits['reasoning_efforts']: raise ValueError('不支持该推理强度')
    for value in (model['max_output_tokens'],values['context']['input_token_cap'],values['run']['max_turns']):
        if type(value) is not int or value <= 0: raise ValueError('输入、输出和轮次数必须为正整数')
    margin = values['context']['safety_margin_tokens']
    if type(margin) is not int or margin < 0: raise ValueError('安全余量必须是非负整数')
    if model['max_output_tokens'] > limits['max_output_tokens']: raise ValueError('输出预算超过模型能力')
    available = min(values['context']['input_token_cap'],limits['context_window']-model['max_output_tokens']-margin)
    if available <= 0: raise ValueError('配置未留下有效输入空间')
    source = values['context']['step1_source']
    for field in ('trigger_tokens', 'window_tokens'):
        if type(source.get(field)) is not int or source[field] <= 0:
            raise ValueError('滑动窗口机制参数必须为正整数')
    ratios = values['compaction']
    if not 0 < ratios['target_ratio'] < ratios['trigger_ratio'] < 1: raise ValueError('压缩比例应满足0<目标<触发<1')
    rt = values['runtime']
    if rt['lease_seconds'] <= 2*rt['heartbeat_seconds']: raise ValueError('租约必须大于心跳两倍')
    try:cost=Decimal(values['task']['max_cost']['amount'])
    except (InvalidOperation,TypeError,ValueError):raise ValueError('费用必须是有限数字') from None
    if not cost.is_finite() or cost <= 0 or values['task']['max_cost']['currency'] != 'USD': raise ValueError('当前计费适配使用正数USD预算')
    if values['task']['max_active_seconds'] <= 0: raise ValueError('任务执行耗时必须为正数')
    for group,fields in {'retry':['max_retries'],
                         'repair':['max_rounds','content_max_rounds','format_max_rounds'],
                         'recovery':['max_attempts'],
                         'context':['recent_turns','history_token_cap'], 'summary':['target_tokens'],
                         'tools':['read_token_cap'],'retrieval':['top_k','snippet_tokens','neighbor_messages']}.items():
        for field in fields:
            value=values[group][field]
            minimum=0 if field in ('max_retries','max_rounds','content_max_rounds',
                                    'format_max_rounds','neighbor_messages') else 1
            if type(value) is not int or value<minimum:raise ValueError(f'{group}.{field}必须是至少{minimum}的整数')
    if values['tools']['read_token_cap']<600:raise ValueError('工具读取预算至少600 tokens，需为引用及分页元数据留空间')
    for key in ('target_tokens','hard_max_tokens','max_items'):
        if type(values['context']['batching'][key]) is not int or values['context']['batching'][key]<1:
            raise ValueError('分批参数必须是正整数')
    batching=values['context']['batching']
    if batching['target_tokens']>batching['hard_max_tokens']:raise ValueError('目标批次大小不可超过硬上限')
    for key in ('neighbor_tokens_each_side','carryover_token_cap'):
        if type(batching[key]) is not int or batching[key]<0:raise ValueError('邻接和继承预算必须是非负整数')
    if not 0.1<=batching['output_headroom_ratio']<=0.5:raise ValueError('批次输出余量比例需在0.1到0.5之间')
    if not batching['boundary_order'] or set(batching['boundary_order'])-{'chapter','scene','paragraph','sentence'}:
        raise ValueError('批次边界类型未注册')
    if any(type(value) is not int or value<0 for value in values['recovery']['backoff_seconds']):raise ValueError('恢复间隔必须为非负整数')
    if rt['heartbeat_seconds']<=0 or rt['stop_grace_seconds']<0:raise ValueError('心跳或停止等待时间无效')
    if not Decimal(values['task']['max_cost']['amount']).is_finite():raise ValueError('费用必须是有限数字')
    if model.get('temperature') is not None:raise ValueError('当前模型适配未启用temperature，必须设为null')
    for name,price in values['pricing']['models'].items():
        for key in ('input_per_million','cached_input_per_million','output_per_million','long_input_multiplier','long_output_multiplier','cache_write_multiplier'):
            try:amount=Decimal(price[key])
            except (InvalidOperation,TypeError,ValueError):raise ValueError(f'{name}计价字段{key}无效') from None
            if not amount.is_finite() or amount<0:raise ValueError(f'{name}计价字段{key}无效')
        if type(price['long_input_threshold']) is not int or price['long_input_threshold']<1:raise ValueError('长输入计价阈值无效')
    if set(values['tools']['enabled']) - set(READ_TOOL_NAMES): raise ValueError('存在未注册工具')
    if type(values['tools'].get('ask_user_enabled', True)) is not bool: raise ValueError('主动提问工具开关必须为布尔值')
    catalog = SchemaCatalog(); catalog.validate_publication(schemas,values['context']['profiles'])
    for name in values['output']['bindings'].values():
        if name not in schemas: raise ValueError('输出类型绑定不存在')
    if values['output']['bindings']!=catalog.registry['bindings']:
        raise ValueError('阶段与output_type的语义绑定不能互换；请编辑对应Schema，或新增消费端适配器')
    from .context import validate_profiles
    validate_profiles(values['context']['profiles'], schemas)
    return available


class ConfigService:
    def __init__(self, store):
        self.store=store; self.catalog=SchemaCatalog(); self.base,self.stages=initial_values()

    def _upgrade_schema_overrides(self, schemas):
        """Add required runtime fields introduced after a saved editable schema."""
        upgraded=deepcopy(schemas)
        upgraded.pop('work_summary',None)
        # Step1's global output now is its saved event view. Old published
        # overrides must not restore the removed top-level analysis contract.
        upgraded.pop('source_global_step1_result',None)
        global_events=upgraded.get('source_global_events')
        if global_events:
            try:
                global_events['properties'].pop('analysis',None)
                global_events['required']=[field for field in global_events['required'] if field!='analysis']
                event=global_events['properties']['payload']['anyOf'][0]['properties']['global_events']['items']
                current=self.catalog.schemas['source_global_events']['properties']['payload']['anyOf'][0]['properties']['global_events']['items']
                event['properties'].setdefault('analysis',deepcopy(current['properties']['analysis']))
                if 'analysis' not in event['required']:
                    event['required'].append('analysis')
            except (KeyError,IndexError,TypeError):
                pass
        knowledge=upgraded.get('source_knowledge_asset')
        if knowledge:
            try:
                payload=knowledge['properties']['payload']['anyOf'][0]
                payload['properties'].pop('source_global_analysis_ref',None)
                payload['required']=[field for field in payload['required'] if field!='source_global_analysis_ref']
            except (KeyError,IndexError,TypeError):
                pass
        # Character Step1 still has a separate model output Schema.
        for view,result in (('source_character_events','source_character_step1_result'),):
            if view in upgraded and result not in upgraded:
                combined=deepcopy(upgraded[view])
                combined['title']=self.catalog.schemas[result]['title']
                upgraded[result]=combined
        # Step6 now returns a small patch; the enriched game_event_view is
        # assembled by the Harness. The old standalone event_function_map
        # remains readable only as a historical artifact.
        upgraded.pop('event_function_map',None)
        game_events=upgraded.get('game_event_view')
        if game_events:
            try:
                payload=game_events['properties']['payload']['anyOf'][0]
                current_payload=self.catalog.schemas['game_event_view']['properties']['payload']['anyOf'][0]
                current_event=current_payload['properties']['events']['items']
                event_items=payload['properties']['events']['items']
                event_items['properties']['narrative_function']=deepcopy(current_event['properties']['narrative_function'])
                if 'narrative_function' not in event_items['required']:
                    event_items['required'].append('narrative_function')
                event_items['properties'].pop('source_event_refs',None)
                event_items['required']=[field for field in event_items['required'] if field!='source_event_refs']
                event_items['properties']['source_anchors']=deepcopy(current_event['properties']['source_anchors'])
                if 'source_anchors' not in event_items['required']:
                    event_items['required'].append('source_anchors')
                game_events['$defs']['SourceAnchor']=deepcopy(self.catalog.schemas['game_event_view']['$defs']['SourceAnchor'])
                coverage=game_events['$defs']['CoverageItem']
                coverage['properties'].pop('source_ref',None)
                coverage['required']=[field for field in coverage['required'] if field!='source_ref']
                coverage['properties']['source_anchors']=deepcopy(self.catalog.schemas['game_event_view']['$defs']['CoverageItem']['properties']['source_anchors'])
                if 'source_anchors' not in coverage['required']:
                    coverage['required'].append('source_anchors')
                payload['properties'].pop('plan_ref',None)
                payload['required']=[field for field in payload['required'] if field!='plan_ref']
            except (KeyError,IndexError,TypeError):
                pass
        chapter_design=upgraded.get('chapter_design')
        if chapter_design:
            try:
                payload=chapter_design['properties']['payload']['anyOf'][0]
                current=self.catalog.schemas['chapter_design']
                chapter_design['$defs']['SourceAnchor']=deepcopy(current['$defs']['SourceAnchor'])
                payload['properties']['chapter_source_anchors']=deepcopy(current['properties']['payload']['anyOf'][0]['properties']['chapter_source_anchors'])
                if 'chapter_source_anchors' not in payload['required']:
                    payload['required'].append('chapter_source_anchors')
            except (KeyError,IndexError,TypeError):
                pass
        ending_routes=upgraded.get('ending_routes')
        if ending_routes:
            try:
                payload=ending_routes['properties']['payload']['anyOf'][0]
                current=self.catalog.schemas['ending_routes']['properties']['payload']['anyOf'][0]
                for field in ('plan_ref','game_event_view_ref'):
                    payload['properties'].pop(field,None)
                    payload['required']=[name for name in payload['required'] if name!=field]
                ending=payload['properties']['endings']['items']
                ending['properties'].pop('evidence_refs',None)
                ending['required']=[name for name in ending['required'] if name!='evidence_refs']
                route=payload['properties']['routes']['items']
                for field in ('event_refs','graph_compatibility'):
                    route['properties'].pop(field,None)
                    route['required']=[name for name in route['required'] if name!=field]
                route['properties']['game_event_ids']=deepcopy(current['properties']['routes']['items']['properties']['game_event_ids'])
                if 'game_event_ids' not in route['required']:
                    route['required'].append('game_event_ids')
                requirement=ending_routes['$defs']['StateRequirement']
                requirement['properties'].pop('used_by_refs',None)
                requirement['required']=[name for name in requirement['required'] if name!='used_by_refs']
                ending_routes['$defs'].pop('GraphCompatibility',None)
            except (KeyError,IndexError,TypeError):
                pass
        for name in ('player_profiles',):
            contract=upgraded.get(name)
            if not contract:
                continue
            try:
                payload=contract['properties']['payload']['anyOf'][0]
                current=self.catalog.schemas[name]['properties']['payload']['anyOf'][0]
                props=payload['properties']; required=payload['required']
                if 'event_function_map_ref' in props:
                    props.pop('event_function_map_ref',None)
                props['game_event_view_ref']=deepcopy(current['properties']['game_event_view_ref'])
                payload['required']=['game_event_view_ref' if f=='event_function_map_ref' else f for f in required]
                if 'game_event_view_ref' not in payload['required']:
                    payload['required'].append('game_event_view_ref')
            except (KeyError,IndexError,TypeError):
                pass
        coordinator=upgraded.get('coordinator_response')
        if not coordinator:return upgraded
        try:
            payload=coordinator['properties']['payload']['anyOf'][0]
            current=self.catalog.schemas['coordinator_response']['properties']['payload']['anyOf'][0]
            if 'source_message_kind' not in payload['properties']:
                payload['properties']['source_message_kind']=deepcopy(current['properties']['source_message_kind'])
            if 'source_message_kind' not in payload['required']:
                payload['required'].append('source_message_kind')
        except (KeyError,IndexError,TypeError):
            # Invalid custom structures are rejected by normal publication
            # validation with their precise schema path.
            return upgraded
        return upgraded

    @staticmethod
    def _upgrade_context_overrides(values):
        """Upgrade saved material profiles for the supported later stages."""
        upgraded=deepcopy(values)
        bindings=upgraded.get('output',{}).get('bindings',{})
        if isinstance(bindings,dict) and bindings.get('step1.global')=='source_global_step1_result':
            bindings['step1.global']='source_global_events'
        if isinstance(bindings,dict) and bindings.get('step6') in ('event_function_map', 'game_event_view'):
            bindings['step6']='game_event_narrative_patch'
        context=upgraded.get('context',{})
        stage_inputs=context.get('stage_inputs')
        if isinstance(stage_inputs,dict):
            for selected in stage_inputs.values():
                if isinstance(selected,list):
                    selected[:]=[kind for kind in selected if kind!='source_global_analysis']
        profile_config=context.get('profiles',{})
        common_materials=profile_config.get('common_materials')
        if isinstance(common_materials,list):
            common_materials[:]=[material for material in common_materials
                if material.get('source',{}).get('schema_id')!='source_global_analysis']
        profiles=profile_config.get('profiles')
        if not isinstance(profiles,list):
            return upgraded
        for profile in profiles:
            materials=profile.get('materials')
            if not isinstance(materials,list):
                continue
            stage=profile.get('stage')
            canonical=json.loads((ROOT/'docs/context/stage-materials.json').read_text())
            current=next(p for p in canonical['profiles'] if p['stage']==stage)
            canonical_ids={m.get('id') for m in current['materials']}
            material_ids={m.get('id') for m in materials}
            legacy_fixed_scope=(stage in ('step7','step8','step10','step11') and material_ids!=canonical_ids)
            if legacy_fixed_scope or (stage == 'step9' and
                any(m.get('id') in ('adjacent_design_contracts','target_contract') for m in materials)):
                profile['include_common_materials']=False
                profile['materials']=deepcopy(current['materials'])
                continue
            if profile.get('stage')=='step6':
                # Step6's model input is the shared Step5 Session plus the
                # exact fixed Step5 inputs/output supplied by Workflow.  No
                # stale per-profile selectors should hide those full records.
                profile['materials']=[]
                continue
            expanded=[]
            for material in materials:
                if material.get('source',{}).get('schema_id')=='source_global_analysis':
                    continue
                if material.get('source',{}).get('builtin')=='runtime.default_strategy':
                    continue
                if profile.get('stage')=='step5' and material.get('source',{}).get('schema_id')=='adaptation_plan':
                    continue
                if profile.get('stage') in ('step7','step8') and material.get('source',{}).get('schema_id')=='event_function_map':
                    continue
                selectors=material.get('selectors')
                if profile.get('stage')=='step6' and material.get('source',{}).get('schema_id')=='game_event_view' \
                        and isinstance(selectors,list):
                    selectors=[selector for selector in selectors if selector!='/payload/plan_ref']
                    material['selectors']=selectors
                expanded.append(material)
            profile['materials']=expanded
        return upgraded

    def _account(self, project_id):
        project=self.store.get(project_id,project_id)
        if not project or project['record_type']!='project': raise ValueError('项目不存在')
        return project['owner_account_id']

    @staticmethod
    def _account_key(account_id):
        return 'harness:'+account_id

    @staticmethod
    def _editor_scope(scope_kind,scope_key):
        if scope_kind not in ('project','agent','stage','auxiliary'):
            raise ValueError('不允许编辑该配置范围')
        if scope_kind=='project': return ''
        if not isinstance(scope_key,str) or not scope_key:
            raise ValueError('缺少配置范围标识')
        return scope_key

    def editor_draft_for_account(self,account_id,scope_kind,scope_key=None):
        key=self._editor_scope(scope_kind,scope_key)
        row=self.store._connection().execute('''SELECT revision,payload,updated_at
          FROM account_config_editor_drafts WHERE account_id=%s AND scope_kind=%s AND scope_key=%s''',
          (account_id,scope_kind,key)).fetchone()
        return {'revision':row['revision'],'payload':row['payload'],'updated_at':row['updated_at'].isoformat()} if row else {'revision':0,'payload':None}

    def save_editor_draft_for_account(self,account_id,scope_kind,scope_key,payload,expected_revision):
        key=self._editor_scope(scope_kind,scope_key)
        if not isinstance(payload,dict) or not isinstance(payload.get('values'),dict) or not isinstance(payload.get('schemas'),dict):
            raise ValueError('编辑草稿需包含 values 和 schemas 对象')
        if not isinstance(payload.get('schema_texts',{}),dict) or not isinstance(payload.get('raw_fields',{}),dict):
            raise ValueError('编辑草稿的原始文本字段必须是对象')
        if not isinstance(expected_revision,int) or expected_revision<0:
            raise ValueError('草稿版本无效')
        if len(json.dumps(payload,ensure_ascii=False).encode('utf-8'))>8*1024*1024:
            raise ValueError('编辑草稿超过 8 MB')
        with self.store.transaction():
            self.store.advisory_lock('config:editor:'+account_id+':'+scope_kind+':'+key)
            current=self.editor_draft_for_account(account_id,scope_kind,key)
            if current['revision']!=expected_revision:
                raise VersionConflict('草稿已在其他页面更新；请先刷新配置页再继续')
            if expected_revision:
                row=self.store._connection().execute('''UPDATE account_config_editor_drafts
                  SET payload=%s,revision=revision+1,updated_at=clock_timestamp()
                  WHERE account_id=%s AND scope_kind=%s AND scope_key=%s
                  RETURNING revision,updated_at''',(Jsonb(payload),account_id,scope_kind,key)).fetchone()
            else:
                row=self.store._connection().execute('''INSERT INTO account_config_editor_drafts
                  (account_id,scope_kind,scope_key,payload) VALUES(%s,%s,%s,%s)
                  RETURNING revision,updated_at''',(account_id,scope_kind,key,Jsonb(payload))).fetchone()
            return {'revision':row['revision'],'updated_at':row['updated_at'].isoformat()}

    def clear_editor_draft_for_account(self,account_id,scope_kind,scope_key,expected_revision):
        key=self._editor_scope(scope_kind,scope_key)
        with self.store.transaction():
            self.store.advisory_lock('config:editor:'+account_id+':'+scope_kind+':'+key)
            current=self.editor_draft_for_account(account_id,scope_kind,key)
            if current['revision']!=expected_revision:
                raise VersionConflict('草稿已在其他页面更新；请先刷新配置页再继续')
            self.store._connection().execute('''DELETE FROM account_config_editor_drafts
              WHERE account_id=%s AND scope_kind=%s AND scope_key=%s''',(account_id,scope_kind,key))
            return {'revision':0,'payload':None}

    @staticmethod
    def _scope_key(scope_kind,scope_key):
        return scope_kind+':'+(scope_key or '')

    @staticmethod
    def _logical(row):
        row=deepcopy(row)
        if row.get('scope_kind')!='global': return row
        kind,key=(row.get('scope_key') or 'project:').split(':',1)
        row['scope_kind'],row['scope_key']=kind,key or None
        return row

    def _ensure_account(self,account_id):
        """One-time adoption of the newest legacy project's published configuration."""
        from .records import new_record
        with self.store.transaction():
            self.store.advisory_lock('config:migrate:'+account_id)
            connection=self.store._connection()
            if connection.execute('SELECT 1 FROM account_config_migrations WHERE account_id=%s',(account_id,)).fetchone(): return
            existing=connection.execute("SELECT 1 FROM config_versions WHERE project_id IS NULL AND data->>'owner_account_id'=%s LIMIT 1",(account_id,)).fetchone()
            source=None
            if not existing:
                found=connection.execute("""SELECT c.project_id FROM config_versions c JOIN projects p ON p.id=c.project_id
                  WHERE p.owner_account_id=%s AND c.state='published' AND c.scope_kind<>'run_snapshot'
                  ORDER BY COALESCE(NULLIF(c.data->>'published_at','')::timestamptz,c.created_at) DESC,c.id DESC LIMIT 1""",(account_id,)).fetchone()
                source=str(found['project_id']) if found else None
                if source:
                    rows=[r['data'] for r in connection.execute("""SELECT data FROM config_versions
                      WHERE project_id=%s AND state='published' AND scope_kind<>'run_snapshot'
                      ORDER BY version,id""",(source,)).fetchall()]
                    latest={}
                    for row in rows: latest[(row['scope_kind'],row['scope_key'])]=row
                    for (kind,key),row in latest.items():
                        self.store.put(new_record('config_version',None,config_key=self._account_key(account_id),
                          owner_account_id=account_id,scope_kind='global',scope_key=self._scope_key(kind,key),
                          state='published',version=row['version'],values=row['values'],published_at=row['published_at']))
            connection.execute('INSERT INTO account_config_migrations(account_id,source_project_id) VALUES(%s,%s)',(account_id,source))

    def _physical(self,account_id,state=None):
        self._ensure_account(account_id)
        filters={'owner_account_id':account_id}
        if state: filters['state']=state
        return [r for r in self.store.list(None,'config_version',limit=10000,filters=filters)
                if r['config_key']==self._account_key(account_id)]

    def published_for_account(self,account_id):
        rows=[self._logical(r) for r in self._physical(account_id,'published')]
        for row in rows:
            row['values']=legacy_prompt_overrides(row['values'])
        return rows

    def published(self, project_id):
        return self.published_for_account(self._account(project_id))

    def next_version(self,project_id,scope_kind,scope_key):
        if scope_kind=='run_snapshot':
            row=self.store._connection().execute("""SELECT COALESCE(MAX((data->>'version')::integer),0)+1 AS version
              FROM config_versions WHERE project_id=%s AND data->>'scope_kind'=%s
              AND data->>'scope_key' IS NOT DISTINCT FROM %s""",(project_id,scope_kind,scope_key)).fetchone()
            return row['version']
        return self.next_account_version(self._account(project_id),scope_kind,scope_key)

    def next_account_version(self,account_id,scope_kind,scope_key):
        self._ensure_account(account_id)
        row=self.store._connection().execute("""SELECT COALESCE(MAX((data->>'version')::integer),0)+1 AS version
          FROM config_versions WHERE project_id IS NULL AND config_key=%s AND data->>'owner_account_id'=%s
          AND data->>'scope_key'=%s""",(self._account_key(account_id),account_id,self._scope_key(scope_kind,scope_key))).fetchone()
        return row['version']

    def values(self, project_id, stage='coordinator', additional=None, agent_key=None):
        return self.values_for_account(self._account(project_id),stage,additional,agent_key)

    def values_for_account(self,account_id,stage='coordinator',additional=None,agent_key=None):
        data=deepcopy(self.base); schemas=deepcopy(self.catalog.schemas); ids=[]
        data=merge(data,self.stages.get(stage,{}))
        rows=self.published_for_account(account_id)
        if additional: additional=self._logical(additional)
        if additional: rows=[r for r in rows if not (r['scope_kind']==additional['scope_kind'] and r['scope_key']==additional['scope_key'])]+[additional]
        def latest(kind,key):
            matching=[r for r in rows if r['scope_kind']==kind and r['scope_key']==key]
            return max(matching,key=lambda r:r['version']) if matching else None
        project_row=latest('project',None)
        stage_row=latest('stage',stage)
        auxiliary_row=latest('auxiliary',stage)
        # The selected Agent is data, not a hard-coded workflow property.  A
        # stage or auxiliary override participates in selection before its
        # ordinary values are applied after the Agent profile.
        selector=deepcopy(data)
        for row in (project_row,stage_row,auxiliary_row):
            if row:
                value=legacy_prompt_overrides(self._upgrade_context_overrides(row['values']))
                value.pop('schemas',None)
                selector=merge(selector,value)
        selected_agent=agent_key or (step1_agent('global',selector) if stage=='step1' else stage_agent(stage,selector))
        if selected_agent not in selector.get('prompts',{}).get('agents',{}):
            raise ValueError('选择的 Agent 不存在')
        for chosen in (project_row,latest('agent',selected_agent),stage_row,auxiliary_row):
            if chosen:
                ids.append(chosen['id'])
                v=legacy_prompt_overrides(self._upgrade_context_overrides(chosen['values']))
                schemas=merge(schemas,self._upgrade_schema_overrides(v.pop('schemas',{})))
                data=merge(data,v)
        # Internal plain-text artifacts are not Agent structured outputs.
        # Remove legacy published schemas and bindings.
        schemas.pop('work_summary',None)
        data.get('output',{}).get('bindings',{}).pop('aux.summary',None)
        data.get('output',{}).get('bindings',{}).pop('aux.format_repair',None)
        data.get('summary',{}).pop('extra_fields',None)
        # Tool permissions are no longer configurable. Keep the legacy fields
        # in resolved snapshots for compatibility, but override old per-Agent
        # switches so every new Run receives the registered tools.
        data['tools']['enabled']=list(READ_TOOL_NAMES)
        data['tools']['ask_user_enabled']=True
        if stage in ('step1','aux.summary','aux.format_repair'):
            # Each branch snapshot must resolve its own Agent profile and
            # instructions. A summary Run must likewise use the Agent profile
            # selected before the auxiliary override's ordinary values merge.
            data['prompts']['stage_agents'][stage]=selected_agent
        data['schemas']=schemas
        return data,list(dict.fromkeys(ids))

    def resolve(self, project_id, stage='coordinator', agent_key=None):
        from .records import new_record
        with self.store.transaction():
            self.store.advisory_lock('config:publish:'+project_id)
            values,ids=self.values(project_id,stage,agent_key=agent_key); validate_values(values,values['schemas'])
            if not stage.startswith('aux.'):
                values['auxiliary_configs']={}
                for auxiliary in ('aux.summary','aux.format_repair','aux.subtask'):
                    data,source_ids=self.values(project_id,auxiliary);validate_values(data,data['schemas'])
                    values['auxiliary_configs'][auxiliary]=data
                    ids=list(dict.fromkeys(ids+source_ids))
            self.store.advisory_lock('config:snapshot:'+project_id+':'+stage)
            version=self.next_version(project_id,'run_snapshot',stage)
            return self.store.put(new_record('config_version',project_id,config_key='harness',scope_kind='run_snapshot',
                scope_key=stage,state='snapshot',version=version,values=values,sha256=digest(values),resolved_from_ids=ids))

    def view(self, project_id, stage='coordinator', agent_key=None):
        return self.view_for_account(self._account(project_id),stage,agent_key)

    def view_for_account(self,account_id,stage='coordinator',agent_key=None):
        values,ids=self.values_for_account(account_id,stage,agent_key=agent_key)
        schemas=values.pop('schemas')
        registry=deepcopy(self.catalog.registry); registry['models']=MODELS
        registry['agents']={key:{'name':values['prompts']['agent_names'][key]}
                            for key in values['prompts']['agents']}
        selected=agent_key or (step1_agent('global',values) if stage=='step1' else stage_agent(stage,values))
        preview_values=deepcopy(values)
        preview_values['prompts']['stage_agents'][stage]=selected
        parts=instruction_parts(stage,preview_values)
        versions=self.published_for_account(account_id)
        recent=self.store._connection().execute("""SELECT data FROM config_versions WHERE project_id IS NULL
          AND config_key=%s AND data->>'owner_account_id'=%s AND data->>'state' IN ('draft','retired')
          ORDER BY created_at DESC,id DESC LIMIT 200""",(self._account_key(account_id),account_id)).fetchall()
        return {'values':values,'schemas':schemas,'registry':registry,
          'selected_agent':selected,'instruction_parts':parts,
          'versions':versions+[self._logical(r['data']) for r in recent],
          'origins':{'resolved_from_ids':ids,'stage':stage,'stage_defaults':self.stages},
          'stage_defaults':self.stages,'models':MODELS}

    def validate_candidate(self, values, schemas, stage='coordinator'):
        """Validate an editor candidate without saving or publishing it."""
        allowed={'coordinator',*[f'step{i}' for i in range(1,12)],
                 'aux.summary','aux.format_repair','aux.history_answer','aux.subtask',
                 'step1.global','step1.character'}
        if stage not in allowed:
            raise ValueError('未知的 output_type 阶段')
        candidate=deepcopy(values)
        candidate['schemas']=deepcopy(schemas)
        available=validate_values(candidate,candidate['schemas'])
        enabled=candidate.get('output',{}).get('structured',{}).get(stage,False)
        schema_id=self.catalog.schema_for(stage,candidate) if enabled else None
        return {'valid':True,'stage':stage,'enabled':enabled,'schema_id':schema_id,
                'input_token_budget':available,
                'schema_sha256':digest(candidate['schemas'][schema_id]) if schema_id else None}

    def draft(self, project_id, values, schemas=None, scope_kind='project',scope_key=None):
        return self.draft_for_account(self._account(project_id),values,schemas,scope_kind,scope_key)

    def draft_for_account(self,account_id,values,schemas=None,scope_kind='project',scope_key=None):
        from .records import new_record
        if scope_kind not in ('project','agent','stage','auxiliary'): raise ValueError('不允许编辑该配置范围')
        if scope_kind=='project': scope_key=None
        elif not scope_key: raise ValueError('缺少配置范围标识')
        with self.store.transaction():
            self.store.advisory_lock('config:'+account_id+':'+scope_kind+':'+str(scope_key))
            version=self.next_account_version(account_id,scope_kind,scope_key)
            v=deepcopy(values)
            if schemas is not None: v['schemas']=schemas
            row=self.store.put(new_record('config_version',None,config_key=self._account_key(account_id),version=version,
              owner_account_id=account_id,scope_kind='global',scope_key=self._scope_key(scope_kind,scope_key),
              state='draft',values=v,sha256=digest(v)))
            return self._logical(row)

    def publish(self, project_id, config_id):
        return self.publish_for_account(self._account(project_id),config_id)

    def publish_for_account(self,account_id,config_id):
        from .records import now_utc
        with self.store.transaction():
            self.store.advisory_lock('config:publish:'+account_id)
            row=self.store.get(config_id,None)
            if (not row or row['record_type']!='config_version' or row['state']!='draft'
                    or row.get('owner_account_id')!=account_id): raise ValueError('配置草稿不存在')
            for stage in ['coordinator',*[f'step{i}' for i in range(1,12)],
                          'aux.summary','aux.format_repair','aux.history_answer','aux.subtask']:
                data,_=self.values_for_account(account_id,stage,row); available=validate_values(data,data['schemas'])
                if stage=='step1':
                    for view in ('global','character'):
                        branch,_=self.values_for_account(account_id,stage,row,agent_key=step1_agent(view,data))
                        branch_available=validate_values(branch,branch['schemas'])
                        if branch['context']['step1_source']['window_tokens'] >= branch_available:
                            raise ValueError('滑动窗口大小须小于两个 Step1 Agent 各自的输入预算')
            for old in self._physical(account_id,'published'):
                if old['scope_key']==row['scope_key']:
                    old['state']='retired'; self.store.update(old,old['row_version'])
            row['state']='published'; row['published_at']=now_utc()
            return self._logical(self.store.update(row,row['row_version']))
