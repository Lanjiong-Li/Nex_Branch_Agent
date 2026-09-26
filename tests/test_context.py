"""Context correctness tests: real fixed records and explicit owned batch units."""
from copy import deepcopy
import json
import os
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from branch_agent.configuration import ConfigService
from branch_agent.records import new_record, scope
from branch_agent.storage import Store
from branch_agent.workflow import Workflow, ref


@pytest.fixture
def context_case(tmp_path):
    dsn = os.getenv("BRANCH_AGENT_TEST_DSN", "postgresql:///branch_agent_local")
    namespace = "context_test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
    store = Store(make_conninfo(dsn, options="-c search_path=" + namespace), tmp_path / "blobs")
    store.migrate()
    project = store.put(new_record("project", None, owner_account_id="context-test", title="Context test"))
    pid = project["id"]
    conversation = store.put(new_record("conversation", pid, title="Context"))
    message = store.put(new_record("history_record", pid, conversation_id=conversation["id"], sequence=1,
                                    role="user", content={"storage": "inline_text", "text": "阅读原作"}))
    source = "第一场：甲见乙🌍。\n第二场：乙离开。"
    workflow = Workflow(store)
    with store.transaction():
        original = workflow.save(pid, "source_text", source, origin="import", effective=True)
    def execution(stage, materials, chapter=None):
        config_record = ConfigService(store).resolve(pid, stage)
        task = store.put(new_record("task", pid, conversation_id=conversation["id"], requested_by_message_id=message["id"],
                                     intent="generate", scope=scope(stage=int(stage[4:]) if stage.startswith("step") else None, chapter_id=chapter)))
        session = store.put(new_record("work_session", pid, conversation_id=conversation["id"], session_key=str(uuid4()), scope=task["scope"]))
        run = store.put(new_record("run", pid, task_id=task["id"], session_id=session["id"], agent_key="test-context",
                                  config_version_id=config_record["id"], input_refs=[m["ref"] for m in materials]))
        return task, run, session, config_record["values"]
    try:
        yield store, pid, original, source, workflow, execution
    finally:
        store.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(namespace)))


def test_step1_prepares_fixed_full_source_and_material_audit(context_case):
    from branch_agent.context import prepare_runtime_materials, build_materials, utf16_length
    store, pid, original, source, workflow, execution = context_case
    materials = [{"schema_id": "source_text", "content": source, "ref": ref(original), "record": original}]
    task, run, session, config = execution("step1", materials)
    prepared = prepare_runtime_materials("step1", task, run, session, config, materials, store)
    packed, actual, selections = build_materials("step1", prepared, config, store, pid)
    assert actual == source
    block = next(p for p in packed if p.get("builtin") == "runtime.full_source")
    assert block["data"]["text"] == source
    assert block["data"]["end_utf16"] == utf16_length(source)
    assert selections
    assert all("*" not in item["selection"]["json_pointer"] for item in selections)
    assert any(item["source_ref"]["record_id"] == original["artifact_id"] for item in selections)


def test_step1_window_material_contains_only_fixed_utf16_slice(context_case):
    from branch_agent.context import prepare_runtime_materials, build_materials, utf16_length
    store, pid, original, source, _, execution = context_case
    materials = [{"schema_id": "source_text", "content": source, "ref": ref(original), "record": original}]
    task, run, session, config = execution("step1", materials)
    first = "第一场：甲见乙🌍。\n"
    window = {"text": source[len(first):], "start_utf16": utf16_length(first),
              "end_utf16": utf16_length(source)}
    prepared = prepare_runtime_materials("step1", task, run, session, config, materials, store,
                                         step1_window=window)
    packed, actual, _ = build_materials("step1", prepared, config, store, pid)
    assert actual == window["text"]
    assert source not in json.dumps(packed, ensure_ascii=False)
    block = next(item for item in packed if item.get("builtin") == "runtime.full_source")
    assert block["data"]["start_utf16"] == utf16_length(first)
    assert block["data"]["end_utf16"] == utf16_length(source)


def test_step10_reads_only_confirmed_chapter_source_intervals(context_case):
    from branch_agent.context import prepare_runtime_materials, build_materials, utf16_length
    store, pid, original, source, _, execution = context_case
    first = "第一场：甲见乙🌍。"
    second = "第二场：乙离开。"
    first_end = utf16_length(first)
    second_start = utf16_length(first + "\n")
    second_end = second_start + utf16_length(second)
    source_ref = ref(original)
    anchors = [{"source_ref": source_ref, "start_utf16": start, "end_utf16": end,
                "exact_quote": None, "prefix": None, "suffix": None}
               for start, end in ((0, first_end), (second_start, second_end))]
    design_content = {"payload": {"chapter_id": "chapter-one", "chapter_source_anchors": anchors}}
    artifact = store.put(new_record("artifact", pid, artifact_kind="chapter_design",
                                    scope=scope(stage=9, chapter_id="chapter-one")))
    design = store.put(new_record("artifact_version", pid, artifact_id=artifact["id"], version=1,
                                 origin="program", source_refs=[source_ref],
                                 content={"storage": "inline_json", "value": design_content}))
    state = store.put(new_record("artifact_state", pid, artifact_id=artifact["id"],
                                 artifact_version_id=design["id"], version=1,
                                 confirmation_status="confirmed", dependency_status="valid", quality_status="passed"))
    materials = [
        {"schema_id": "chapter_design", "ref": ref(design),
         "content": design_content, "state": state, "required": True},
        {"schema_id": "source_text", "ref": source_ref, "content": source, "required": True},
    ]
    task, run, session, config = execution("step10", materials, chapter="chapter-one")
    prepared = prepare_runtime_materials("step10", task, run, session, config, materials, store)
    blocks = [item["content"] for item in prepared if item.get("builtin") == "runtime.source_block"]
    assert [(item["start_utf16"], item["end_utf16"], item["text"]) for item in blocks] == [
        (0, first_end, first), (second_start, second_end, second)]
    profile = next(item for item in config["context"]["profiles"]["profiles"] if item["stage"] == "step10")
    packed, _, _ = build_materials("step10", prepared, config, store, pid)
    assert {item["source_kind"] for item in packed} == {"chapter_design", "source_text"}
    assert [item["data"]["text"] for item in packed if item.get("builtin") == "runtime.source_block"] == [first, second]


def test_model_visible_runtime_projection_pointers_resolve_to_exact_values(context_case):
    from branch_agent.context import prepare_runtime_materials, build_materials, pointer_values
    store, pid, original, source, workflow, execution = context_case
    observed = set()
    for stage in ('step1', 'coordinator', 'aux.summary'):
        materials = [{'schema_id': 'source_text', 'content': source, 'ref': ref(original)}] if stage == 'step1' else []
        task, run, session, config = execution(stage, materials)
        if stage == 'coordinator':
            content = {'tasks': [task], 'queue_gate': {'holds': []}, 'pending_user_items': [],
                       'presented_at_submission': [], 'chapter_plans': []}
            event = store.put(new_record('runtime_event', pid, sequence=100,
                event_name='runtime.status_prepared', task_id=task['id'], run_id=run['id'], payload=content))
            materials.append({'builtin': 'runtime.status', 'schema_id': 'runtime.status',
                'ref': ref(event), 'content': content, 'projection_pointer': '/payload', 'required': True})
        if stage == 'aux.summary':
            message = store.get(task['requested_by_message_id'], pid)
            materials.append({'builtin': 'runtime.archive_window', 'required': True,
                'content': {'history_refs': [ref(message)], 'decision_refs': [], 'artifact_refs': [], 'tool_call_refs': []}})
        prepared = prepare_runtime_materials(stage, task, run, session, config, materials, store)
        packed, _, selections = build_materials(stage, prepared, config, store, pid)
        for material in packed:
            if not material.get('builtin'):
                continue
            record = store.resolve_ref(material['source_ref'], pid)
            if record['record_type'] != 'runtime_event':
                continue  # Full source uses its explicit immutable text interval contract.
            for field in material['data']:
                target = {**material['source_ref'], 'json_pointer': field['json_pointer'], 'item_id': field['item_id']}
                workflow.validate_evidence(pid, {'evidence_ref': target})
                assert pointer_values(record, target['json_pointer'], required=True)[0][1] == field['value']
                assert any(s['source_ref'] == material['source_ref'] and s['inclusion'] == 'included'
                           and s['selection']['json_pointer'] == target['json_pointer'] for s in selections)
                observed.add(target['json_pointer'])
    assert '/payload/content/goal' in observed
    assert '/payload' in observed  # Native runtime.status is delivered as one full payload.
    assert '/payload/content/history_refs/0' in observed


def test_missing_required_material_cannot_be_silently_skipped(context_case):
    from branch_agent.context import prepare_runtime_materials, build_materials, MaterialError
    store, pid, _, _, _, execution = context_case
    task, run, session, config = execution("step2", [])
    prepared = prepare_runtime_materials("step2", task, run, session, config, [], store)
    with pytest.raises(MaterialError, match="source_global_events"):
        build_materials("step2", prepared, config, store, pid)


def test_stage_input_switch_omits_model_payload_but_keeps_fixed_reference(context_case):
    from branch_agent.context import prepare_runtime_materials, build_materials
    store, pid, original, source, workflow, execution = context_case
    anchor = {'source_ref': ref(original), 'start_utf16': 0,
              'end_utf16': len(source.encode('utf-16-le')) // 2,
              'exact_quote': source, 'prefix': None, 'suffix': None}
    content = {'result_kind': 'ready', 'payload': {'source_ref': ref(original),
        'global_events': [{'event_id': 'event-one', 'title': '相遇', 'summary': source,
                           'narrative_order': 0, 'story_time': None, 'character_ids': [],
                           'source_anchors': [anchor]}],
        'covered_source_anchors': [anchor],
        'remaining_source_anchors': []}, 'questions': [],
        'evidence_refs': [ref(original)], 'notes': []}
    with store.transaction():
        views = workflow.save(pid, 'source_global_events', content, stage=1, origin='program',
                              inputs=[ref(original)], effective=True)
    materials = [{'schema_id': 'source_global_events', 'content': content, 'ref': ref(views),
                  'state': workflow.state(views), 'required': True}]
    task, run, session, config = execution('step2', materials)
    config['context']['stage_inputs']['step2'] = []
    prepared = prepare_runtime_materials('step2', task, run, session, config, materials, store)
    packed, _, audit = build_materials('step2', prepared, config, store, pid)
    assert not any(item['source_ref'] == ref(views) for item in packed)
    assert any(item['source_ref'] == ref(views) and item['inclusion'] == 'omitted'
               and item['reason'] == 'disabled by stage input configuration' for item in audit)


def test_only_current_session_summary_is_automatically_loaded_and_refreshes(context_case):
    from branch_agent.context import prepare_runtime_materials, build_materials
    store, pid, _, _, workflow, execution = context_case
    task, run, session, config = execution('coordinator', [])
    def save_summary(text, artifact_id=None):
        with store.transaction():
            if artifact_id is None:
                artifact = store.put(new_record('artifact', pid, artifact_kind='work_summary'))
                artifact_id = artifact['id']
            else: artifact=store.get(artifact_id,pid)
            version=artifact['latest_version']+1
            saved=store.put(new_record('artifact_version',pid,artifact_id=artifact_id,version=version,
                parent_version=artifact['latest_version'] or None,content={'storage':'inline_text','text':text},
                output_schema=None,origin='model'))
            store.put(new_record('artifact_state',pid,artifact_id=artifact_id,artifact_version_id=saved['id'],version=version,
                confirmation_status='not_required',dependency_status='valid',quality_status='passed'))
            artifact.update(latest_version=version,current_effective_version=version);store.update(artifact,artifact['row_version'])
            return saved
    old = save_summary('Current session old summary')
    other = save_summary('Another session summary')
    session['latest_summary_ref'] = ref(old)
    session = store.update(session, session['row_version'])
    # Simulate an old coordinator snapshot containing every Session's summary.
    offered = [{'ref': ref(old), 'required': False}, {'ref': ref(other), 'required': False}]
    prepared = prepare_runtime_materials('coordinator', task, run, session, config, offered, store)
    assert [m['ref'] for m in prepared if m.get('schema_id') == 'work_summary'] == [ref(old)]
    latest = save_summary('Current session freshly compacted summary', old['artifact_id'])
    session['latest_summary_ref'] = ref(latest)
    session = store.update(session, session['row_version'])
    refreshed = prepare_runtime_materials('coordinator', task, run, session, config, offered, store)
    assert [m['ref'] for m in refreshed if m.get('schema_id') == 'work_summary'] == [ref(latest)]
    packed, _, selections = build_materials('coordinator', refreshed, config, store, pid)
    assert [m['source_ref'] for m in packed if m['source_kind'] == 'work_summary'] == [ref(latest)]
    assert not any(s['source_ref'] in (ref(old), ref(other)) for s in selections)
    # An explicit history query can still load another Session's immutable summary.
    explicit = prepare_runtime_materials('coordinator', task, run, session, config,
        [{'ref': ref(other), 'required': False, 'expanded': True}], store)
    assert {m['ref']['record_id'] for m in explicit if m.get('schema_id') == 'work_summary'} == {latest['artifact_id'], other['artifact_id']}


def test_provided_content_must_match_its_frozen_version(context_case):
    from branch_agent.context import prepare_runtime_materials, MaterialError
    store, _, original, source, _, execution = context_case
    materials = [{"schema_id": "source_text", "content": source + "伪造结局", "ref": ref(original)}]
    task, run, session, config = execution("step1", materials)
    with pytest.raises(MaterialError, match="fixed"):
        prepare_runtime_materials("step1", task, run, session, config, materials, store)


def test_historical_provenance_does_not_reintroduce_a_pinned_artifacts_old_body(context_case):
    from branch_agent.context import prepare_runtime_materials, build_materials
    store,pid,original,source,workflow,execution=context_case
    anchor={'source_ref':ref(original),'start_utf16':0,'end_utf16':len(source.encode('utf-16-le'))//2,
            'exact_quote':source,'prefix':None,'suffix':None}
    content={'result_kind':'ready','payload':{'source_ref':ref(original),'global_events':[
        {'event_id':'one','title':'相遇','summary':'Historical wording','narrative_order':0,
         'story_time':None,'character_ids':[],'source_anchors':[anchor]}],
        'covered_source_anchors':[anchor],'remaining_source_anchors':[]},
        'questions':[],'evidence_refs':[ref(original)],'notes':[]}
    character={'result_kind':'ready','payload':{'source_ref':ref(original),'character_views':[],
        'covered_source_anchors':[anchor],'remaining_source_anchors':[]},
        'questions':[],'evidence_refs':[ref(original)],'notes':[]}
    with store.transaction():
        old=workflow.save(pid,'source_global_events',content,stage=1,origin='program',inputs=[ref(original)],effective=True)
        other_artifact=store.put(new_record('artifact',pid,artifact_kind='source_global_events'))
        other=workflow.save(pid,'source_global_events',content,stage=1,origin='program',inputs=[ref(original)],
                            artifact_id=other_artifact['id'],effective=True)
        revised=deepcopy(content);revised['payload']['global_events'][0]['summary']='Current wording'
        revised['evidence_refs'].extend([ref(other),ref(old)])
        current=workflow.save(pid,'source_global_events',revised,stage=1,origin='program',inputs=[ref(original)],
                              artifact_id=old['artifact_id'],effective=True)
        character_version=workflow.save(pid,'source_character_events',character,stage=1,
            origin='program',inputs=[ref(original)],effective=True)
        analysis=workflow.save(pid,'source_global_analysis','甲见乙。',stage=1,
            inputs=[ref(original),ref(current)],effective=True)
    materials=[{'ref':ref(current),'required':True},{'ref':ref(analysis),'required':True},
               {'ref':ref(character_version),'required':True}]
    task,run,session,config=execution('step2',materials)
    prepared=prepare_runtime_materials('step2',task,run,session,config,materials,store)
    packed,_,audit=build_materials('step2',prepared,config,store,pid)
    assert any(m['source_ref']==ref(current) for m in packed)
    assert not any(m['source_ref']==ref(old) for m in packed)
    assert any(m['source_ref']==ref(other) for m in packed)  # Same schema, independent artifact.
    assert any(s['source_ref']==ref(old) and s['inclusion']=='omitted' and 'historical provenance' in s['reason'] for s in audit)
    # Citations keep their original version; nothing is rebound to the current one.
    assert store.resolve_ref(revised['evidence_refs'][-1],pid)['id']==old['id']
    explicit=prepare_runtime_materials('step2',task,run,session,config,
        materials+[{'ref':ref(old),'expanded':True}],store)
    assert not any(m.get('_provenance_only') for m in explicit if m['ref'] in (ref(old),ref(current)))
    explicit_packed,_,_=build_materials('step2',explicit,config,store,pid)
    assert any(m['source_ref']==ref(old) for m in explicit_packed)


def test_exact_quote_is_checked_against_utf16_interval():
    from branch_agent.context import validate_anchors
    assert validate_anchors("a🌍b", {"start_utf16": 1, "end_utf16": 3, "exact_quote": "🌍"}) == []
    assert validate_anchors("a🌍b", {"start_utf16": 1, "end_utf16": 3, "exact_quote": "wrong"})
    assert validate_anchors("abc", {"start_utf16": True, "end_utf16": 2, "exact_quote": "b"})


def test_required_schema_field_cannot_be_removed_from_profile():
    from branch_agent.configuration import initial_values
    from branch_agent.context import validate_profiles
    from branch_agent.schemas import SchemaCatalog
    config, _ = initial_values()
    profiles = deepcopy(config["context"]["profiles"])
    profile = next(p for p in profiles["profiles"] if p["stage"] == "step10")
    design = next(m for m in profile["materials"] if m["id"] == "chapter_design")
    design["selectors"] = ["/payload/chapter_id"]
    with pytest.raises(ValueError):
        validate_profiles(profiles, SchemaCatalog().schemas)


def test_registered_pointer_expansion_distinguishes_empty_from_missing():
    from branch_agent.context import pointer_values, MaterialError
    assert pointer_values({"items": []}, "/items/*/id") == [("/items", [])]
    with pytest.raises(MaterialError):
        pointer_values({"items": [{"id": "one"}, {"name": "missing-id"}]}, "/items/*/id", required=True)


def test_schema_identity_uses_own_ids_and_rejects_reference_shapes():
    from branch_agent.context import _identity
    for field in ('change_id', 'finding_id', 'issue_id', 'metric_id', 'check_id', 'conflict_id',
                  'game_event_id', 'outcome_id'):
        assert _identity({field: 'actual-item', 'description': 'test item'}) == 'actual-item'
    assert _identity({'change_id': 'change-1', 'subject_ref': {'record_id': 'source'},
                      'original': 'before', 'adapted': 'after', 'rationale': 'why', 'evidence_refs': []}) == 'change-1'
    assert _identity({'contract_id': 'contract', 'kind': 'entry', 'segment_id': 'foreign-segment',
                      'other_chapter_id': 'foreign-chapter', 'required_state': [], 'resulting_state': [], 'rationale': ''}) == 'contract'
    assert _identity({'annotation_id': 'annotation', 'event_id': 'foreign-event'}) == 'annotation'
    assert _identity({'route_id': 'route', 'ending_id': 'foreign-ending'}) == 'route'
    assert _identity({'item_id': 'keep-1', 'category': 'fact', 'content': 'preserve',
                      'suggested_retention': 'preserve', 'rationale': 'why', 'evidence_refs': []}) == 'keep-1'
    for foreign in (
        {'record_id': 'record', 'version': '1', 'item_id': 'change-1', 'json_pointer': '/payload/items/0'},
        {'object_kind': 'node', 'object_id': 'node', 'chapter_id': 'chapter', 'node_id': 'node', 'json_pointer': ''},
        {'kind': 'node', 'id': 'foreign-node'},
        {'from_segment_id': 'segment', 'interaction_id': 'choice', 'outcome_id': 'outcome', 'target': {}, 'condition_requirement': None},
        {'kind': 'chapter', 'segment_id': None, 'chapter_id': 'foreign-chapter'},
        {'event_id': 'foreign-event', 'involvement': 'observer'},
        {'profile_id': 'foreign-profile', 'related_refs': [], 'requirement': 'design'},
        {'unregistered_id': 'not-an-identity'},
    ):
        assert _identity(foreign) is None


def test_step1_cannot_create_batch_plan():
    from branch_agent.context_batching import plan_batches
    from branch_agent.configuration import initial_values
    config, _ = initial_values()
    with pytest.raises(ValueError, match="Step1"):
        plan_batches("step1", str(uuid4()), str(uuid4()), [], config)


def test_text_batches_preserve_unicode_and_exact_ownership():
    from branch_agent.context_batching import plan_batches, validate_manifest
    from branch_agent.configuration import initial_values
    from branch_agent.context import utf16_length
    config, _ = initial_values()
    config["context"]["batching"].update(target_tokens=12, hard_max_tokens=20, neighbor_tokens_each_side=0, max_items=2)
    text = ("甲遇到🌍。乙离开。\n" * 25)
    original_ref = {"record_id": str(uuid4()), "version": "1", "item_id": None, "json_pointer": ""}
    inputs = [{"source_ref": original_ref, "content": text}]
    manifest = plan_batches("step2", str(uuid4()), str(uuid4()), inputs, config)
    assert len(manifest["batches"]) > 1
    validate_manifest(manifest, inputs, config)
    ranges = sorted((u["start_utf16"], u["end_utf16"]) for u in manifest["units"])
    assert ranges[0][0] == 0 and ranges[-1][1] == utf16_length(text)
    assert all(a[1] == b[0] for a, b in zip(ranges, ranges[1:]))
    units = [unit for batch in manifest["batches"] for unit in batch["owned_unit_ids"]]
    assert len(units) == len(set(units)) == len(manifest["units"])


def test_auxiliary_archive_window_is_preserved_without_expanding_source(context_case):
    from branch_agent.context import prepare_runtime_materials, build_materials
    store,pid,_,_,_,execution=context_case
    task,run,session,config=execution('aux.summary',[])
    request=store.get(task['requested_by_message_id'],pid)
    window={'history_refs':[ref(request)],'decision_refs':[],'artifact_refs':[],'tool_call_refs':[]}
    materials=[{'builtin':'runtime.archive_window','content':window,'required':True}]
    prepared=prepare_runtime_materials('aux.summary',task,run,session,config,materials,store)
    actual=next(m for m in prepared if m.get('builtin')=='runtime.archive_window')
    assert actual['content']==window
    assert all(m.get('schema_id')!='source_text' for m in prepared)
    packed,source,selections=build_materials('aux.summary',prepared,config,store,pid)
    assert source is None
    assert any(s['selection']['json_pointer'].startswith('/payload/content/history_refs') for s in selections)


def test_tampered_batch_plan_or_input_is_rejected():
    from branch_agent.context_batching import plan_batches, validate_manifest
    from branch_agent.context import MaterialError
    from branch_agent.configuration import initial_values
    config,_=initial_values()
    reference={'record_id':str(uuid4()),'version':'1','item_id':None,'json_pointer':''}
    inputs=[{'source_ref':reference,'content':'固定输入内容'}]
    plan=plan_batches('step2',str(uuid4()),str(uuid4()),inputs,config)
    changed=deepcopy(plan);changed['units'][0]['end_utf16']-=1
    with pytest.raises(MaterialError):validate_manifest(changed,inputs,config)
    with pytest.raises(MaterialError):validate_manifest(plan,[{'source_ref':reference,'content':'输入改版'}],config)


def test_exact_batch_scope_does_not_include_other_fields():
    from branch_agent.context import _batch_values
    reference={'record_id':str(uuid4()),'version':'1','item_id':None,'json_pointer':None}
    content={'payload':{'events':[{'event_id':'one','text':'甲'},{'event_id':'two','text':'乙'}]}}
    unit={'source_ref':{**reference,'item_id':'one','json_pointer':'/payload/events/0'},'start_utf16':None,'end_utf16':None}
    materials=[{'builtin':'runtime.batch_state','content':{'owned_source_ranges':[unit],'neighbor_source_ranges':[]}}]
    selected=_batch_values(content,'/payload/events',{'ref':reference},materials,True)
    assert selected==[('/payload/events/0',content['payload']['events'][0])]


def test_batch_completion_requires_child_results_and_complete_ownership(context_case):
    from branch_agent.context_batching import plan_batches,persist_manifest,prepare_batch,complete_batch,batch_coverage
    from branch_agent.context import MaterialError
    store,pid,original,source,_,execution=context_case
    task,run,session,config=execution('step2',[{'ref':ref(original)}])
    config['context']['batching'].update(target_tokens=4,hard_max_tokens=10,max_items=1,neighbor_tokens_each_side=0)
    snapshot=store.put(new_record('config_version',pid,config_key='harness',scope_kind='run_snapshot',scope_key=str(uuid4()),state='snapshot',values=config))
    run=store.put(new_record('run',pid,task_id=task['id'],session_id=session['id'],agent_key='test',config_version_id=snapshot['id']))
    inputs=[{'source_ref':ref(original),'content':source}]
    plan=plan_batches('step2',task['id'],run['config_version_id'],inputs,config)
    saved=persist_manifest(store,task,run,plan,inputs,config);manifest_ref=ref(saved)
    assert not batch_coverage(store,pid,manifest_ref)['complete']
    batch=plan['batches'][0]
    prepared=prepare_batch(plan,batch['batch_id'],inputs,config,manifest_ref=manifest_ref)
    assert len(prepared['owned'])==1 and not prepared['complete']
    child=store.put(new_record('task',pid,conversation_id=task['conversation_id'],requested_by_message_id=task['requested_by_message_id'],
        intent='generate',scope=task['scope'],parent_task_id=task['id'],budget_root_task_id=task['budget_root_task_id']))
    child_run=store.put(new_record('run',pid,task_id=child['id'],session_id=session['id'],agent_key='test',config_version_id=run['config_version_id']))
    artifact=store.put(new_record('artifact',pid,artifact_kind='batch_result',scope=task['scope']))
    result=store.put(new_record('artifact_version',pid,artifact_id=artifact['id'],version=1,origin='model',
        producer_run_id=child_run['id'],content={'storage':'inline_json','value':{'observed':'test-only committed result'}}))
    with pytest.raises(MaterialError,match='actual batch child'):
        complete_batch(store,pid,manifest_ref,batch['batch_id'],child['id'],[ref(original)],batch['owned_unit_ids'])
    complete_batch(store,pid,manifest_ref,batch['batch_id'],child['id'],[ref(result)],batch['owned_unit_ids'])
    status=batch_coverage(store,pid,manifest_ref)
    assert not status['complete'] and status['processed_unit_ids']==batch['owned_unit_ids']
    resumed=prepare_batch(plan,batch['batch_id'],inputs,config,manifest_ref=manifest_ref,processed_unit_ids=status['processed_unit_ids'])
    assert resumed['complete'] and resumed['owned']==[]
    with pytest.raises(MaterialError,match='reused'):
        complete_batch(store,pid,manifest_ref,batch['batch_id'],child['id'],[ref(result)],batch['owned_unit_ids'])


def test_aux_subtask_reads_only_validated_owned_units(context_case):
    from branch_agent.context_batching import plan_batches,persist_manifest,prepare_batch
    from branch_agent.context import prepare_runtime_materials,build_materials,MaterialError
    store,pid,original,source,_,execution=context_case
    parent,run,session,config=execution('step2',[{'ref':ref(original)}])
    config['context']['batching'].update(target_tokens=4,hard_max_tokens=10,max_items=1,neighbor_tokens_each_side=0)
    snapshot=store.put(new_record('config_version',pid,config_key='harness',scope_kind='run_snapshot',scope_key=str(uuid4()),state='snapshot',values=config))
    run=store.put(new_record('run',pid,task_id=parent['id'],session_id=session['id'],agent_key='test',config_version_id=snapshot['id']))
    inputs=[{'source_ref':ref(original),'content':source}]
    plan=plan_batches('step2',parent['id'],run['config_version_id'],inputs,config)
    manifest=persist_manifest(store,parent,run,plan,inputs,config)
    batch=prepare_batch(plan,plan['batches'][0]['batch_id'],inputs,config,manifest_ref=ref(manifest))
    child=store.put(new_record('task',pid,conversation_id=parent['conversation_id'],requested_by_message_id=parent['requested_by_message_id'],
        intent='generate',scope=parent['scope'],parent_task_id=parent['id'],budget_root_task_id=parent['budget_root_task_id']))
    child_config=config.get('auxiliary_configs',{}).get('aux.subtask',config)
    child_snapshot=store.put(new_record('config_version',pid,config_key='harness',scope_kind='run_snapshot',scope_key=str(uuid4()),state='snapshot',values=child_config))
    child_run=store.put(new_record('run',pid,task_id=child['id'],session_id=session['id'],agent_key='test',config_version_id=child_snapshot['id']))
    materials=[{'ref':ref(original),'required':True},{'builtin':'runtime.batch_state','content':batch['runtime_batch_state'],'required':True}]
    prepared=prepare_runtime_materials('aux.subtask',child,child_run,session,child_config,materials,store)
    packed,full_source,selections=build_materials('aux.subtask',prepared,child_config,store,pid)
    actual=next(m for m in packed if m['source_kind']=='source_text')['data']
    assert actual[0]['value']['text']==batch['owned'][0]['content']
    assert len(actual[0]['value']['text'])<len(source) and full_source is None
    continuity=[]
    for text in ('上一批已核对身份','补充证据'*5000):
        artifact=store.put(new_record('artifact',pid,artifact_kind='continuity_fixture',scope=scope()))
        version=store.put(new_record('artifact_version',pid,artifact_id=artifact['id'],origin='program',
                                     content={'storage':'inline_text','text':text}))
        continuity.append(ref(version))
    with_carryover=deepcopy(materials)
    with_carryover[1]['content']['continuity_refs']=continuity
    ready=prepare_runtime_materials('aux.subtask',child,child_run,session,child_config,with_carryover,store)
    packed,_,audit=build_materials('aux.subtask',ready,child_config,store,pid)
    loaded=[m for m in packed if m['source_kind']=='continuity_fixture']
    assert len(loaded)==1 and loaded[0]['data'][0]['value']=='上一批已核对身份'
    assert any(a['source_ref']==continuity[1] and a['inclusion']=='omitted' and 'carryover_token_cap' in a['reason'] for a in audit)
    broken=deepcopy(materials);broken[1]['content']['owned_source_ranges'][0]['end_utf16']+=1
    with pytest.raises(MaterialError,match='differs'):
        prepare_runtime_materials('aux.subtask',child,child_run,session,child_config,broken,store)


def test_aggregation_waits_for_full_ledger_then_preserves_original_refs(context_case):
    from branch_agent.context_batching import plan_batches,persist_manifest,complete_batch,aggregation_materials
    from branch_agent.context import MaterialError,prepare_runtime_materials,build_materials
    store,pid,original,source,workflow,execution=context_case
    anchor={'source_ref':ref(original),'start_utf16':0,'end_utf16':len(source.encode('utf-16-le'))//2,'exact_quote':source,'prefix':None,'suffix':None}
    content={'result_kind':'ready','payload':{'source_ref':ref(original),'global_events':[
        {'event_id':'one','title':'相遇','summary':source,'narrative_order':0,'story_time':None,'character_ids':[],'source_anchors':[anchor]}],
        'covered_source_anchors':[anchor],'remaining_source_anchors':[]},'questions':[],'evidence_refs':[ref(original)],'notes':[]}
    character={'result_kind':'ready','payload':{'source_ref':ref(original),'character_views':[],
        'covered_source_anchors':[anchor],'remaining_source_anchors':[]},
        'questions':[],'evidence_refs':[ref(original)],'notes':[]}
    with store.transaction():
        old_views=workflow.save(pid,'source_global_events',content,stage=1,origin='program',inputs=[ref(original)],effective=True)
        content['evidence_refs'].append(ref(old_views))
        views=workflow.save(pid,'source_global_events',content,stage=1,origin='program',inputs=[ref(original)],effective=True)
        character_views=workflow.save(pid,'source_character_events',character,stage=1,origin='program',inputs=[ref(original)],effective=True)
        analysis=workflow.save(pid,'source_global_analysis','甲见乙。',stage=1,
            inputs=[ref(original),ref(views)],effective=True)
    materials=[{'ref':ref(views),'required':True},{'ref':ref(analysis),'required':True},
               {'ref':ref(character_views),'required':True}]
    task,run,session,config=execution('step2',materials)
    inputs=[{'source_ref':ref(views),'content':content},
            {'source_ref':ref(character_views),'content':character}]
    plan=plan_batches('step2',task['id'],run['config_version_id'],inputs,config)
    manifest=persist_manifest(store,task,run,plan,inputs,config)
    with pytest.raises(MaterialError,match='All owned'):
        aggregation_materials(store,task,run,config,materials,ref(manifest))
    child=store.put(new_record('task',pid,conversation_id=task['conversation_id'],requested_by_message_id=task['requested_by_message_id'],
        intent='generate',scope=task['scope'],parent_task_id=task['id'],budget_root_task_id=task['budget_root_task_id']))
    child_run=store.put(new_record('run',pid,task_id=child['id'],session_id=session['id'],agent_key='test',config_version_id=run['config_version_id']))
    child_output={'result_kind':'ready','payload':{'task':'测试批次','findings':[], 'recommendations':[],
        'limitations':[],'artifact_refs':[ref(views),ref(character_views)]},
        'questions':[],'evidence_refs':[ref(views),ref(character_views)],'notes':[]}
    with store.transaction():
        result=workflow.save(pid,'subtask_result',child_output,run=child_run,
            inputs=[ref(views),ref(character_views)],effective=True)
    for batch in plan['batches']:
        complete_batch(store,pid,ref(manifest),batch['batch_id'],child['id'],[ref(result)],batch['owned_unit_ids'])
    aggregate=aggregation_materials(store,task,run,config,materials,ref(manifest))
    prepared=prepare_runtime_materials('step2',task,run,session,config,aggregate,store)
    packed,_,selections=build_materials('step2',prepared,config,store,pid)
    assert any(s['source_ref']==ref(views) and 'verified complete batch coverage' in s['reason'] for s in selections)
    assert any(p['source_ref']==ref(result) for p in packed)
    assert not any(p['source_ref']==ref(old_views) for p in packed)
    assert any(s['source_ref']==ref(old_views) and 'historical provenance' in s['reason'] for s in selections)
    assert not any(p['source_kind']=='source_text' for p in packed)
    assert any(s['source_ref']==ref(views) and s['inclusion']=='included' and 'source_global_events_index' in s['reason'] for s in selections)


def test_headroom_reduces_main_batch_units():
    from branch_agent.context_batching import plan_batches
    from branch_agent.configuration import initial_values
    from branch_agent.context import tokens
    cfg,_=initial_values();cfg['model'].update(name='gpt-5.6-sol',max_output_tokens=40)
    cfg['context']['batching'].update(target_tokens=35,hard_max_tokens=40,max_items=1,neighbor_tokens_each_side=0)
    data=[{'source_ref':{'record_id':str(uuid4()),'version':'1','item_id':None,'json_pointer':''},'content':'甲乙丙丁戊己庚辛壬癸。'*30}]
    loose=deepcopy(cfg);loose['context']['batching']['output_headroom_ratio']=.1
    tight=deepcopy(cfg);tight['context']['batching']['output_headroom_ratio']=.5
    ids=(str(uuid4()),str(uuid4()))
    a=plan_batches('step2',*ids,data,loose);b=plan_batches('step2',*ids,data,tight)
    assert len(b['units'])>len(a['units'])
    for unit in b['units']:
        raw=data[0]['content'].encode('utf-16-le')[unit['start_utf16']*2:unit['end_utf16']*2].decode('utf-16-le')
        assert tokens(raw,cfg['model']['name'])<=20


def test_optional_pruning_updates_evidence_and_keeps_required():
    from branch_agent.context import PackedMaterials,prune_optional_materials,tokens
    from branch_agent.configuration import initial_values
    cfg,_=initial_values();ref1={'record_id':str(uuid4()),'version':'1','item_id':None,'json_pointer':None}
    selections=[{'source_ref':ref1,'selection':{'item_id':None,'json_pointer':'/'+name},'inclusion':'included','reason':name,'estimated_tokens':100} for name in ('low','high','required')]
    packed=PackedMaterials()
    for i,(name,priority,required) in enumerate([('low',10,False),('high',60,False),('required',1,True)]):
        packed.add({'source_ref':ref1,'data':[{'json_pointer':'/'+name,'item_id':None,'value':name*300}]},
                   [{'required':required,'auto':True,'priority':priority,'selection_indices':[i]}])
    cap=tokens(packed[1:],cfg['model']['name'])
    after,audit=prune_optional_materials(packed,selections,cap,cfg)
    assert [m['data'][0]['json_pointer'] for m in after]==['/high','/required']
    assert audit[0]['inclusion']=='omitted' and audit[0]['estimated_tokens']==0
    assert audit[1]['inclusion']==audit[2]['inclusion']=='included'
    assert selections[0]['inclusion']=='included'  # snapshots supplied by caller are untouched
    after,audit=prune_optional_materials(packed,selections,0,cfg)
    assert [m['data'][0]['json_pointer'] for m in after]==['/required']


def test_prepare_batch_defers_owned_units_when_remaining_budget_is_small():
    from branch_agent.context_batching import plan_batches,prepare_batch
    from branch_agent.configuration import initial_values
    cfg,_=initial_values();cfg['model']['name']='gpt-5.6-sol';cfg['context']['batching'].update(target_tokens=10,hard_max_tokens=20,max_items=10,neighbor_tokens_each_side=0)
    inputs=[{'source_ref':{'record_id':str(uuid4()),'version':'1','item_id':None,'json_pointer':''},'content':'甲'} for _ in range(6)]
    plan=plan_batches('step2',str(uuid4()),str(uuid4()),inputs,cfg)
    result=prepare_batch(plan,plan['batches'][0]['batch_id'],inputs,cfg,available_tokens=3)
    assert len(result['owned'])==3 and len(result['deferred_unit_ids'])==3
    assert result['budget']['owned_tokens']<=result['budget']['main_hard_tokens']==3
    assert not result['complete']
    assert all('content' not in r for r in result['runtime_batch_state']['owned_source_ranges'])


def test_carryover_reads_original_refs_with_a_real_cap(context_case):
    from branch_agent.context_batching import select_continuity
    from branch_agent.context import prepare_runtime_materials,build_materials,prune_optional_materials
    store,pid,_,_,_,execution=context_case
    refs=[]
    for text in ('甲','乙'*1000):
        artifact=store.put(new_record('artifact',pid,artifact_kind='continuity_fixture',scope=scope()))
        value=store.put(new_record('artifact_version',pid,artifact_id=artifact['id'],origin='program',content={'storage':'inline_text','text':text}))
        refs.append(ref(value))
    _,_,_,cfg=execution('coordinator',[])
    cfg['context']['batching']['carryover_token_cap']=3
    selected=select_continuity(store,pid,refs,cfg)
    assert selected['included_refs']==refs[:1] and selected['omitted_refs']==refs[1:] and selected['tokens']<=3
    assert [m['ref'] for m in selected['materials']]==refs
    # Original record is intact; omission grants no processed coverage.
    assert store.resolve_ref(refs[1],pid)['content']['text']=='乙'*1000
    assert 'processed_unit_ids' not in selected


def test_step1_full_source_is_never_pruned(context_case):
    from branch_agent.context import prepare_runtime_materials,build_materials,prune_optional_materials
    store,pid,original,source,_,execution=context_case
    materials=[{'ref':ref(original),'content':source}]
    task,run,session,cfg=execution('step1',materials)
    ready=prepare_runtime_materials('step1',task,run,session,cfg,materials,store)
    packed,_,audit=build_materials('step1',ready,cfg,store,pid)
    kept,updated=prune_optional_materials(packed,audit,0,cfg)
    full=next(m for m in kept if m.get('builtin')=='runtime.full_source')
    assert full['data']['text']==source
    assert all(a['inclusion']=='included' for a in updated if 'runtime.full_source:' in a['reason'])


def test_build_policy_covers_mixed_required_and_optional_fields(context_case):
    from branch_agent.context import build_materials,prune_optional_materials,tokens
    store,pid,original,_,_,execution=context_case
    task,run,session,cfg=execution('coordinator',[])
    cfg['context']['profiles']['common_materials']=[]
    profile=next(p for p in cfg['context']['profiles']['profiles'] if p['stage']=='coordinator')
    profile['materials']=[{'id':name,'source':{'schema_id':'test_material'},'selectors':['/'+name],
        'scope_filter':'all_pinned','load':'auto','required':required,'detail':'full','coverage':'context_only','priority':priority}
        for name,required,priority in [('required',True,1),('optional',False,50)]]
    materials=[{'schema_id':'test_material','ref':ref(original),'content':{'required':'甲','optional':'乙'*500}}]
    packed,_,audit=build_materials('coordinator',materials,cfg,store,pid)
    kept,updated=prune_optional_materials(packed,audit,0,cfg)
    assert [f['json_pointer'] for f in kept[0]['data']]==['/required']
    assert next(a for a in updated if a['selection']['json_pointer']=='/optional')['inclusion']=='omitted'
    encoded=json.dumps(kept,ensure_ascii=False)
    assert 'selection_indices' not in encoded and '_policy' not in encoded
    # An optional parent body must not make its separately required index unprunable.
    profile['materials'][1]['selectors']=['']
    packed,_,audit=build_materials('coordinator',materials,cfg,store,pid)
    kept,updated=prune_optional_materials(packed,audit,0,cfg)
    assert [f['json_pointer'] for f in kept[0]['data']]==['/required']
    assert next(a for a in updated if a['selection']['json_pointer']=='')['inclusion']=='omitted'


def test_historical_graph_baseline_requires_program_write_context(context_case):
    from branch_agent.context import prepare_runtime_materials,MaterialError
    store,pid,original,_,_,execution=context_case
    artifact=store.put(new_record('artifact',pid,artifact_kind='nexo_graph',scope=scope(stage=10)))
    graph=store.put(new_record('artifact_version',pid,artifact_id=artifact['id'],origin='program',source_refs=[ref(original)],
                              content={'storage':'inline_json','value':{'id':'test-baseline'}}))
    store.put(new_record('artifact_state',pid,artifact_id=artifact['id'],artifact_version_id=graph['id'],version=1,
                         confirmation_status='confirmed',dependency_status='review_required',quality_status='passed'))
    materials=[{'ref':ref(graph),'required':True,'material_role':'historical_baseline'}]
    task,run,session,cfg=execution('step10',materials)
    with pytest.raises(MaterialError,match='Historical baseline'):
        prepare_runtime_materials('step10',task,run,session,cfg,materials,store)
    content={'baseline_ref':ref(graph),'target_chapter_id':None,'reserved_identity_refs':[ref(store.get(run['config_version_id'],pid))],
             'metadata_ref':ref(store.get(run['config_version_id'],pid)),'change_scope':{'chapter_ids':[]}}
    event=store.put(new_record('runtime_event',pid,sequence=1,conversation_id=task['conversation_id'],task_id=task['id'],
                               event_name='graph.write_context_created',payload=content))
    materials.append({'ref':ref(event),'builtin':'runtime.graph_write_context','content':content})
    prepared=prepare_runtime_materials('step10',task,run,session,cfg,materials,store)
    old=next(m for m in prepared if m.get('schema_id')=='nexo_graph')
    assert old['_historical_baseline_verified']
    assert not any(m.get('schema_id')=='source_text' for m in prepared)
    with pytest.raises(MaterialError,match='Historical baseline'):
        prepare_runtime_materials('step11',task,run,session,cfg,materials,store)
