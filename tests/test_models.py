from copy import deepcopy
import json
import uuid
import pytest
from branch_agent.schemas import SchemaCatalog
from branch_agent.configuration import initial_values, merge, validate_values, ConfigService
from branch_agent.context import build_materials, fit_input, input_budget, BudgetExceeded, utf16_length, validate_anchors


def test_active_schemas_and_strict_json_adapter():
    catalog=SchemaCatalog();assert len(catalog.schemas)==15
    assert catalog.schema_for('step1.global')=='source_global_events'
    assert catalog.schema_for('step1.character')=='source_character_events'
    assert catalog.registry['bindings']['step6']=='game_event_narrative_patch'
    assert 'event_function_map' not in catalog.schemas
    assert 'work_summary' not in catalog.schemas
    assert 'source_analysis' not in catalog.schemas
    assert 'aux.summary' not in catalog.registry['bindings']
    assert 'step2' not in catalog.registry['bindings']
    graph=json.loads((catalog.path/'examples/nexo_graph.example.json').read_text())
    catalog.output_type('nexo_graph').validate_json(json.dumps(graph))
    broken=deepcopy(graph);broken['chapters']='invalid'
    with pytest.raises(Exception): catalog.output_type('nexo_graph').validate_json(json.dumps(broken))


def test_saved_editable_schemas_upgrade_to_direct_source_index_contract():
    catalog = SchemaCatalog()
    service = ConfigService.__new__(ConfigService)
    service.catalog = catalog
    saved = deepcopy(catalog.schemas)
    character_event = saved['source_views']['properties']['payload']['anyOf'][0]['properties']['character_views']['items']['properties']['events']['items']
    character_event['properties'] = {'event_id': {'type': 'string'}, 'involvement': {'type': 'string'}}
    character_event['required'] = ['event_id', 'involvement']
    game_event = saved['game_event_view']['properties']['payload']['anyOf'][0]['properties']['events']['items']
    game_event['properties'].pop('source_anchors')
    game_event['required'].remove('source_anchors')
    game_event['properties']['source_event_refs'] = {'type': 'array', 'items': {'$ref': '#/$defs/EvidenceRef'}}
    game_event['required'].append('source_event_refs')
    coverage = saved['game_event_view']['$defs']['CoverageItem']
    coverage['properties'].pop('source_anchors')
    coverage['required'].remove('source_anchors')
    coverage['properties']['source_ref'] = {'$ref': '#/$defs/EvidenceRef'}
    coverage['required'].append('source_ref')
    chapter = saved['chapter_design']['properties']['payload']['anyOf'][0]
    chapter['properties'].pop('chapter_source_anchors')
    chapter['required'].remove('chapter_source_anchors')
    upgraded = service._upgrade_schema_overrides(saved)
    assert 'character_event_id' in upgraded['source_views']['properties']['payload']['anyOf'][0]['properties']['character_views']['items']['properties']['events']['items']['required']
    assert 'source_anchors' in upgraded['game_event_view']['properties']['payload']['anyOf'][0]['properties']['events']['items']['required']
    assert 'source_event_refs' not in upgraded['game_event_view']['properties']['payload']['anyOf'][0]['properties']['events']['items']['properties']
    assert 'source_anchors' in upgraded['game_event_view']['$defs']['CoverageItem']['required']
    assert 'chapter_source_anchors' in upgraded['chapter_design']['properties']['payload']['anyOf'][0]['required']
    catalog.validate_publication(upgraded)


def test_step7_saved_configuration_upgrades_to_event_only_input_and_output():
    catalog=SchemaCatalog()
    service=ConfigService.__new__(ConfigService)
    service.catalog=catalog
    saved=deepcopy(catalog.schemas['ending_routes'])
    payload=saved['properties']['payload']['anyOf'][0]
    reference={'$ref':'#/$defs/EvidenceRef'}
    for field in ('plan_ref','game_event_view_ref'):
        payload['properties'][field]=deepcopy(reference);payload['required'].append(field)
    ending=payload['properties']['endings']['items']
    ending['properties']['evidence_refs']={'type':'array','items':deepcopy(reference)}
    ending['required'].append('evidence_refs')
    route=payload['properties']['routes']['items']
    route['properties'].pop('game_event_ids');route['required'].remove('game_event_ids')
    route['properties']['event_refs']={'type':'array','items':deepcopy(reference)}
    route['required'].append('event_refs')
    requirement=saved['$defs']['StateRequirement']
    requirement['properties']['used_by_refs']={'type':'array','items':deepcopy(reference)}
    requirement['required'].append('used_by_refs')
    upgraded=service._upgrade_schema_overrides({'ending_routes':saved})['ending_routes']
    upgraded_payload=upgraded['properties']['payload']['anyOf'][0]
    assert set(upgraded_payload['properties'])==set(payload['properties'])-{'plan_ref','game_event_view_ref'}
    assert 'game_event_ids' in upgraded_payload['properties']['routes']['items']['required']
    assert 'event_refs' not in upgraded_payload['properties']['routes']['items']['properties']
    assert 'used_by_refs' not in upgraded['$defs']['StateRequirement']['properties']
    values,_=initial_values()
    profile=next(p for p in values['context']['profiles']['profiles'] if p['stage']=='step7')
    profile['include_common_materials']=True
    profile['materials'].append({'id':'old_plan','source':{'schema_id':'adaptation_plan'},'selectors':['/payload']})
    migrated=service._upgrade_context_overrides(values)
    current=next(p for p in migrated['context']['profiles']['profiles'] if p['stage']=='step7')
    assert not current['include_common_materials']
    assert current['materials']==next(p for p in initial_values()[0]['context']['profiles']['profiles'] if p['stage']=='step7')['materials']


def test_step8_saved_configuration_upgrades_to_two_fixed_inputs():
    values,_=initial_values()
    profile=next(p for p in values['context']['profiles']['profiles'] if p['stage']=='step8')
    profile['include_common_materials']=True
    profile['materials'].append({'id':'old_plan','source':{'schema_id':'adaptation_plan'},'selectors':['/payload']})
    migrated=ConfigService._upgrade_context_overrides(values)
    current=next(p for p in migrated['context']['profiles']['profiles'] if p['stage']=='step8')
    canonical=next(p for p in initial_values()[0]['context']['profiles']['profiles'] if p['stage']=='step8')
    assert not current['include_common_materials']
    assert current['materials']==canonical['materials']
    assert [m['source']['schema_id'] for m in current['materials']]==['game_event_view','ending_routes']


def test_step9_saved_configuration_upgrades_to_plan_refs_and_original():
    values,_=initial_values()
    profile=next(p for p in values['context']['profiles']['profiles'] if p['stage']=='step9')
    profile['include_common_materials']=True
    profile['materials'].append({'id':'adjacent_design_contracts','source':{'schema_id':'chapter_design'},'selectors':['/payload']})
    migrated=ConfigService._upgrade_context_overrides(values)
    current=next(p for p in migrated['context']['profiles']['profiles'] if p['stage']=='step9')
    canonical=next(p for p in initial_values()[0]['context']['profiles']['profiles'] if p['stage']=='step9')
    assert current==canonical
    assert [m['source'] for m in current['materials']]==[
        {'schema_id':'adaptation_plan'}, {'schema_id':'game_event_view'},
        {'schema_id':'ending_routes'}, {'schema_id':'player_profiles'},
        {'builtin':'runtime.source_block'},
    ]


def test_step10_saved_configuration_upgrades_to_design_and_original_only():
    values,_=initial_values()
    profile=next(p for p in values['context']['profiles']['profiles'] if p['stage']=='step10')
    profile['include_common_materials']=True
    profile['materials'].append({'id':'old_plan','source':{'schema_id':'adaptation_plan'},'selectors':['/payload']})
    migrated=ConfigService._upgrade_context_overrides(values)
    current=next(p for p in migrated['context']['profiles']['profiles'] if p['stage']=='step10')
    canonical=next(p for p in initial_values()[0]['context']['profiles']['profiles'] if p['stage']=='step10')
    assert current['include_common_materials'] is False
    assert current['materials']==canonical['materials']
    assert [m['source'] for m in current['materials']]==[
        {'schema_id':'chapter_design'}, {'builtin':'runtime.source_block'}]


def test_default_stage_budgets_use_full_model_window():
    base,stages=initial_values();step1=merge(base,stages['step1'])
    assert base['context']['input_token_cap']==1050000
    assert step1['context']['input_token_cap']==1050000
    assert input_budget(base)==990000
    assert input_budget(step1)==966000
    assert step1['model']['max_output_tokens']==32000
    assert validate_values(step1,SchemaCatalog().schemas)==966000


def test_luna_matches_sol_runtime_capabilities_and_has_pricing():
    config,stages=initial_values();config=merge(config,stages['step1']);config['model']['name']='gpt-5.6-luna'
    assert validate_values(config,SchemaCatalog().schemas)==1016000
    assert config['pricing']['models']['gpt-5.6-luna']=={
        'input_per_million':'0.20','cached_input_per_million':'0.02','output_per_million':'1.20',
        'long_input_threshold':272000,'long_input_multiplier':'2','long_output_multiplier':'1.5',
        'cache_write_multiplier':'1.25','source':'https://developers.openai.com/api/docs/models/gpt-5.6-luna'}


def test_publication_rejects_consumer_break_and_fulltext_removal():
    catalog=SchemaCatalog();schemas=deepcopy(catalog.schemas)
    del schemas['nexo_graph']['properties']['chapters']
    with pytest.raises(Exception): catalog.validate_publication(schemas)
    config,_=initial_values()
    step=next(p for p in config['context']['profiles']['profiles'] if p['stage']=='step1')
    step['materials']=[m for m in step['materials'] if m['source'].get('builtin')!='runtime.full_source']
    with pytest.raises(ValueError): validate_values(config,catalog.schemas)


def test_program_generated_chapter_edge_cannot_require_an_unimplemented_field():
    catalog=SchemaCatalog();schemas=deepcopy(catalog.schemas)
    for name in ('nexo_graph','chapter_graph'):
        definition=schemas[name]['$defs']['ChapterEdge']
        definition['properties']['review_note']={'type':'string'}
        definition['required'].append('review_note')
    with pytest.raises(ValueError,match='程序生成'):catalog.validate_publication(schemas)


@pytest.mark.parametrize('model',['gpt-5-mini','gpt-5-nano'])
def test_registered_lower_cost_models_keep_step1_fulltext_capacity(model):
    config,stages=initial_values();config=merge(config,stages['step1']);config['model']['name']=model
    assert validate_values(config,SchemaCatalog().schemas)==366000
    assert config['pricing']['models'][model]['input_per_million']


def test_source_always_complete_and_budget_never_truncates():
    config,stages=initial_values();config=merge(config,stages['step1'])
    source='第一场：你好🌍\n第十五场：这一事件终于结束。'
    packed=[{'data':{'text':source,'start_utf16':0,'end_utf16':utf16_length(source)}}]
    items=[{'role':'user','content':json.dumps(packed,ensure_ascii=False)}]
    count,budget=fit_input(items,'rules',[],{},config,source)
    assert count<budget
    with pytest.raises(BudgetExceeded):fit_input([{'role':'user','content':'只有摘要'}],'',[],{},config,source)
    config['context']['input_token_cap']=1
    with pytest.raises(BudgetExceeded):fit_input(items,'rules',[],{},config,source)


def test_utf16_anchor_rejects_half_surrogate_and_false_quote():
    assert validate_anchors('a🌍b',{'start_utf16':1,'end_utf16':3,'quote':'🌍'})==[]
    assert validate_anchors('a🌍b',{'start_utf16':1,'end_utf16':2})
    assert validate_anchors('abc',{'start_utf16':0,'end_utf16':2,'quote':'bc'})


@pytest.mark.parametrize('amount',['NaN','Infinity','not-a-number'])
def test_invalid_cost_configuration_is_rejected_as_a_user_error(amount):
    config,_=initial_values();config['task']['max_cost']['amount']=amount
    with pytest.raises(ValueError):validate_values(config,SchemaCatalog().schemas)


def test_unimplemented_mapping_configuration_cannot_publish_silently():
    config,_=initial_values();config['context']['version_mapping']['extra_guard_paths']=['/payload/premise']
    with pytest.raises(ValueError,match='尚未注册'):validate_values(config,SchemaCatalog().schemas)
    config,_=initial_values();config['output']['result_mapping']={'payload':'/other'}
    with pytest.raises(ValueError,match='尚未注册'):validate_values(config,SchemaCatalog().schemas)


def test_summary_dedup_requires_exact_current_version_in_working_history():
    from branch_agent.context import PackedMaterials
    from branch_agent.model_service import omit_history_summary_copy
    target={'record_id':'summary','version':'3'}
    summary={'result_kind':'ready','payload':{'open_questions':['尚未确认结局']}}
    packed=PackedMaterials()
    for version in ('2','3'):
        packed.add({'source_kind':'work_summary','source_ref':{**target,'version':version},
                    'data':[{'value':summary}]},[{'selection_indices':[len(packed)]}])
    selections=[{'inclusion':'included','estimated_tokens':200,'reason':'fixed summary'} for _ in range(2)]
    history=[{'role':'user','content':json.dumps({'working_summary':summary})}]
    reduced,audit=omit_history_summary_copy(packed,selections,history,target,summary)
    assert [b['source_ref']['version'] for b in reduced]==['2']
    assert len(reduced.policies)==1
    assert audit[0]['inclusion']=='included' and audit[1]['inclusion']=='omitted'
    assert 'working history' in audit[1]['reason']
    assert selections[1]['inclusion']=='included' and len(packed)==2
    # A similar summary or a different generation cannot justify removing it.
    different=deepcopy(summary);different['payload']['open_questions']=[]
    unchanged,_=omit_history_summary_copy(packed,selections,history,target,different)
    assert unchanged==packed
    unchanged,_=omit_history_summary_copy(packed,selections,[],target,summary)
    assert unchanged==packed
