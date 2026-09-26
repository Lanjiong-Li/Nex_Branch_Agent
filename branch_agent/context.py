"""Token accounting, source anchors and field-bound context assembly."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import math
from importlib.metadata import version
import re
import tiktoken
from .schemas import schema_path
from .records import canonical_bytes, new_record


class MaterialError(ValueError):
    def __init__(self, code, message, details=None):
        self.code = self.reason = code
        self.details = details or {}
        super().__init__(message)

class BudgetExceeded(RuntimeError):
    def __init__(self, code='input_budget_exceeded', message='输入超过预算', details=None):
        self.code,self.details=code,details or {}
        super().__init__(message)


def tokens(value, model='deepseek-flash'):
    from .configuration import MODELS
    cap=MODELS.get(model)
    if not cap: raise ValueError('未知模型的token估算器')
    text=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,separators=(',',':'))
    estimate=len(tiktoken.get_encoding(cap['tokenizer']).encode(text,disallowed_special=()))
    # DeepSeek uses a different tokenizer. Until an exact, versioned tokenizer
    # is bundled, reserve extra space for every preflight and batch decision.
    return math.ceil(estimate*cap.get('token_estimate_multiplier',1))


def token_estimator_version(model):
    from .configuration import MODELS
    cap=MODELS[model]
    return f"tiktoken/{version('tiktoken')}:{cap['tokenizer']}:x{cap.get('token_estimate_multiplier',1)}"


def input_budget(config):
    from .configuration import MODELS
    cap=MODELS[config['model']['name']]
    return min(config['context']['input_token_cap'],cap['context_window']-config['model']['max_output_tokens']-config['context']['safety_margin_tokens'])


def unwrap(content, store=None, project_id=None):
    if isinstance(content,dict):
        if content.get('storage')=='inline_text': return content['text']
        if content.get('storage')=='inline_json': return content['value']
        if content.get('storage')=='blob':
            raw=store.blob_read(content['blob_id'],project_id)
            try: return json.loads(raw)
            except (ValueError,UnicodeDecodeError): return raw.decode('utf-8')
    return content


def pointer_values(value,path,required=False):
    """Expand registered wildcards, retaining concrete JSON pointers."""
    if path and not path.startswith('/'):
        raise MaterialError('invalid_selector','JSON selector must start with /')
    nodes=[('',value)]; empty=[]
    if path=='': return nodes
    for raw in path.lstrip('/').split('/'):
        key=raw.replace('~1','/').replace('~0','~'); out=[]
        for prefix, item in nodes:
            if key=='*' and isinstance(item,list):
                if not item: empty.append((prefix,[]))
                out.extend((prefix+'/'+str(i),v) for i,v in enumerate(item))
            elif isinstance(item,dict) and key in item: out.append((prefix+'/'+raw,item[key]))
            elif isinstance(item,list) and key.isdigit() and int(key)<len(item): out.append((prefix+'/'+key,item[int(key)]))
            elif required: raise MaterialError('missing_material_field',f'Required field missing: {prefix}/{raw}')
        nodes=out
    return nodes+empty


def validate_profiles(profiles,schemas):
    from .schemas import ROOT
    base=json.loads((ROOT/'docs/context/stage-materials.json').read_text())
    registered=set(base['scope_filters']); builtin=base['builtin_sources']
    by_stage={p['stage']:p for p in profiles['profiles']}
    if len(by_stage)!=len(profiles['profiles']): raise ValueError('阶段材料配置不可重复')
    if set(by_stage)!={p['stage'] for p in base['profiles']}: raise ValueError('阶段材料配置必须保留全部阶段')
    common={m['id']:m for m in profiles['common_materials']}
    for m in base['common_materials']:
        if m['required'] and (m['id'] not in common or not common[m['id']]['required'] or common[m['id']]['source']!=m['source'] or not set(m['selectors'])<=set(common[m['id']]['selectors'])):
            raise ValueError('公共必需材料与字段不可移除: '+m['id'])
    for p in base['profiles']:
        edited=by_stage[p['stage']]
        fixed={m['id']:m for m in p['materials'] if m['required']}
        chosen={m['id']:m for m in edited['materials']}
        for key,m in fixed.items():
            if key not in chosen or not chosen[key]['required'] or chosen[key]['source']!=m['source']:
                raise ValueError(f'{p["stage"]}必需材料不可移除: {key}')
            for field in m['selectors']:
                if not any(field==selected or field.startswith(selected.rstrip('/')+'/') or selected=='' for selected in chosen[key]['selectors']):
                    raise ValueError(f'{p["stage"]}必需字段不可移除: {key}{field}')
        if p['stage']=='step1':
            m=next((m for m in edited['materials'] if m['source'].get('builtin')=='runtime.full_source'),None)
            if not m or m.get('enabled',True) is not True or m['load']!='auto' or m['detail']!='full' or m['scope_filter']!='full_source' or set(m['selectors'])!={'/source_ref','/text','/start_utf16','/end_utf16'}:
                raise ValueError('Step1必须完整自动加载原文')
    for m in profiles['common_materials']+[m for p in profiles['profiles'] for m in p['materials']]:
        if type(m['priority']) is not int or type(m['required']) is not bool:raise ValueError('材料priority必须为整数，required必须为布尔值')
        if 'enabled' in m and type(m['enabled']) is not bool:raise ValueError('材料enabled必须为布尔值')
        if m['scope_filter'] not in registered: raise ValueError('未注册材料范围过滤器')
        if m['load'] not in ('auto','on_demand') or m['detail'] not in ('full','index','ref'): raise ValueError('无效材料加载模式')
        name=m['source'].get('schema_id')
        for path in m['selectors']:
            if name in ('work_summary','source_global_analysis') and path=='':
                continue  # Internal plain-text artifacts.
            if name and not schema_path(schemas[name],path): raise ValueError(f'材料字段不存在: {name}{path}')
            if not name and path[1:] not in builtin[m['source']['builtin']]['fields']: raise ValueError('投影字段不存在')


def utf16_length(text): return len(text.encode('utf-16-le'))//2


def validate_anchors(source,result):
    """Check all returned source intervals and exact quotations without inventing coverage."""
    raw=source.encode('utf-16-le'); problems=[]
    def visit(node):
        if isinstance(node,dict):
            start=node.get('start_utf16'); end=node.get('end_utf16')
            if start is not None and end is not None:
                try:
                    if type(start) is not int or type(end) is not int or not 0<=start<end<=len(raw)//2: raise ValueError()
                    excerpt=raw[start*2:end*2].decode('utf-16-le')
                    quote=node.get('exact_quote',node.get('quote',node.get('excerpt')))
                    if quote is not None and quote!=excerpt: problems.append('来源摘录与UTF-16范围不一致')
                except (ValueError,UnicodeError): problems.append('来源范围越界或拆开Unicode字符')
            elif (start is None) != (end is None): problems.append('来源范围必须同时提供起止偏移')
            for v in node.values(): visit(v)
        elif isinstance(node,list):
            for v in node: visit(v)
    visit(result)
    return problems


def _ref(record, pointer=None):
    return {'record_id':record.get('artifact_id',record.get('decision_id',record['id'])),
            'version':str(record['version']) if record['record_type'] in ('artifact_version','decision') else None,
            'item_id':None,'json_pointer':pointer}


def _all(store, project, kind, **filters):
    rows=[]; offset=0
    while True:
        part=store.list(project,kind,limit=200,offset=offset,filters=filters or None)
        rows.extend(part)
        if len(part)<200:return rows
        offset+=len(part)


def _refs(value):
    if isinstance(value,dict):
        if set(value)=={'record_id','version','item_id','json_pointer'}:
            yield value
        else:
            for item in value.values():yield from _refs(item)
    elif isinstance(value,list):
        for item in value:yield from _refs(item)


def _fixed(material,store,project):
    reference=material.get('ref',material.get('source_ref'))
    if not reference:
        raise MaterialError('unbound_material','Material requires a persisted fixed source reference')
    try:record=store.resolve_ref(reference,project)
    except ValueError as error:raise MaterialError('missing_fixed_material',str(error)) from error
    output=deepcopy(material);output.pop('_aggregation_ranges',None);output.pop('_historical_baseline_verified',None);output.pop('_provenance_only',None);output['record']=record;output['ref']=deepcopy(reference)
    if material.get('continuity') and record['record_type']!='artifact_version':
        output.update(content=record,schema_id=record['record_type']);output.pop('projection_pointer',None);return output
    if record['record_type']=='artifact_version':
        content=unwrap(record['content'],store,project)
        if 'content' in material and canonical_bytes(unwrap(material['content'],store,project))!=canonical_bytes(content):
            raise MaterialError('fixed_content_mismatch','Provided content does not match its fixed version')
        artifact=store.get(record['artifact_id'],project)
        output.update(content=content,schema_id=artifact['artifact_kind'],scope=artifact['scope'],
                      state=next(iter(_all(store,project,'artifact_state',artifact_version_id=record['id'])),None))
    elif record['record_type']=='runtime_event':
        payload=record['payload']
        if record['event_name']=='context.projection_prepared':
            content=payload['content']
            if hashlib.sha256(canonical_bytes(content)).hexdigest()!=payload['sha256']:
                raise MaterialError('projection_hash_mismatch','Saved projection content hash is invalid')
            output.update(content=content,builtin=payload['builtin'],projection_pointer='/payload/content')
        else:
            if 'content' in output and canonical_bytes(output['content'])!=canonical_bytes(payload):
                raise MaterialError('fixed_content_mismatch','Provided projection does not match persisted event payload')
            output.update(content=payload,projection_pointer='/payload')
    elif 'content' not in output:
        output['content']=record
    return output


def _projection(store,task,run,builtin,content,sources):
    project=task['project_id']
    with store.transaction():
        store.advisory_lock(project+':event_sequence')
        key=project+':sequence:events'
        counter=store.projection_get(key) or {'project_id':project,'value':0}
        existing=_all(store,project,'runtime_event')
        counter['value']=max(counter['value'],max((r['sequence'] for r in existing),default=0))+1
        store.projection_put(key,counter)
        event=store.put(new_record('runtime_event',project,sequence=counter['value'],conversation_id=task['conversation_id'],
            task_id=task['id'],run_id=run['id'],event_name='context.projection_prepared',subject_ref=_ref(task),
            payload={'handler_version':'context.projection.v1','builtin':builtin,'content':deepcopy(content),
                     'source_refs':deepcopy(sources),'sha256':hashlib.sha256(canonical_bytes(content)).hexdigest()}))
    return {'builtin':builtin,'schema_id':builtin,'ref':_ref(event),'record':event,'content':deepcopy(content),
            'projection_pointer':'/payload/content','scope':task['scope'],'required':False}


def _validate_batch_state(store,task,state,stage,config):
    from .context_batching import _manifest, _hash, batch_coverage, validate_manifest
    project=task['project_id'];manifest=_manifest(store,project,state['manifest_ref'])
    if manifest['stage']=='step1' or manifest['task_id'] not in (task['id'],task['parent_task_id']):
        raise MaterialError('batch_owner_mismatch','Batch scope belongs to another Task')
    plan_config=store.get(manifest['config_version_id'],project)
    if not plan_config:raise MaterialError('batch_config_missing','Fixed planner configuration is missing')
    expected=plan_config['values'].get('auxiliary_configs',{}).get('aux.subtask',plan_config['values']) if stage=='aux.subtask' else plan_config['values']
    if canonical_bytes(config)!=canonical_bytes(expected):
        raise MaterialError('batch_config_changed','Batch execution differs from its frozen auxiliary configuration')
    inputs=[]
    for reference in manifest['input_refs']:
        record=store.resolve_ref(reference,project)
        actual=pointer_values(unwrap(record['content'],store,project),reference.get('json_pointer') or '',required=True)[0][1]
        source={'source_ref':reference,'content':actual}
        if isinstance(actual,str):
            intervals=[u for u in manifest['units'] if u['source_ref']==reference and u['start_utf16'] is not None]
            if not intervals:raise MaterialError('batch_scope_changed','Text input has no fixed source intervals')
            start=min(u['start_utf16'] for u in intervals);end=max(u['end_utf16'] for u in intervals)
            if start or end!=utf16_length(actual):
                source.update(content=actual.encode('utf-16-le')[start*2:end*2].decode('utf-16-le'),start_utf16=start,end_utf16=end)
        inputs.append(source)
    validate_manifest(manifest,inputs,plan_config['values'])
    batch=next((b for b in manifest['batches'] if b['batch_id']==state['current_batch_id']),None)
    if batch is None:raise MaterialError('unknown_batch','Batch not in frozen manifest')
    units={u['unit_id']:u for u in manifest['units']}
    completed=set(batch_coverage(store,project,state['manifest_ref'])['processed_unit_ids'])
    for field,allowed in [('owned_source_ranges',set(batch['owned_unit_ids'])-completed),('neighbor_source_ranges',set(batch['context_unit_ids']))]:
        seen=set()
        for item in state[field]:
            identity=item.get('unit_id')
            if identity in seen or identity not in allowed or {k:item.get(k) for k in units[identity]}!=units[identity]:
                raise MaterialError('batch_scope_changed','Batch range differs from fixed owned/neighbor units')
            seen.add(identity);reference=item['source_ref'];record=store.resolve_ref(reference,project)
            actual=pointer_values(unwrap(record['content'],store,project),reference.get('json_pointer') or '',required=True)[0][1]
            if item['start_utf16'] is not None:
                actual=actual.encode('utf-16-le')[item['start_utf16']*2:item['end_utf16']*2].decode('utf-16-le')
            if _hash(actual)!=item['content_sha256'] or ('content' in item and canonical_bytes(actual)!=canonical_bytes(item['content'])):
                raise MaterialError('batch_source_changed','Batch content differs from persisted source')
    return manifest


def prepare_runtime_materials(stage,task,run,session,config,materials,store,step1_window=None):
    """Freeze real inputs and derive auditable runtime projections; never resolve latest."""
    project=task['project_id']
    if run['task_id']!=task['id'] or run['project_id']!=project or session['id']!=run['session_id']:
        raise MaterialError('context_owner_mismatch','Task, Run and Session must share a verified context')
    selected=[]
    registered=config['context']['profiles']['builtin_sources']
    for material in materials:
        if material.get('ref') or material.get('source_ref'):
            selected.append(_fixed(material,store,project));continue
        builtin=material.get('builtin');content=material.get('content')
        if builtin not in registered or not isinstance(content,dict) or set(content)!=set(registered[builtin]['fields']):
            raise MaterialError('unbound_material','Only registered internal projections may omit a fixed reference')
        references=list(_refs(content))
        if not references:raise MaterialError('unbound_material','Internal projection requires persisted evidence')
        for reference in references:store.resolve_ref(reference,project)
        selected.append({**_projection(store,task,run,builtin,content,references),'required':material.get('required',False)})
    for material in selected:
        if material.get('material_role')=='historical_baseline':
            baseline=next((m for m in selected if m.get('builtin')=='runtime.graph_write_context'),None)
            event=baseline.get('record',{}) if baseline else {}
            if stage!='step10' or material.get('schema_id')!='nexo_graph' or not baseline or event.get('record_type')!='runtime_event' or event.get('actor_kind')!='program' or event.get('event_name') not in ('graph.write_context_created','context.projection_prepared') or any(
                baseline['content']['baseline_ref'][k]!=material['ref'][k] for k in ('record_id','version')):
                raise MaterialError('historical_baseline_unproven','Historical baseline requires the exact persisted graph write context')
            material['_historical_baseline_verified']=True
    batch=next((m for m in selected if m.get('builtin')=='runtime.batch_state'),None)
    if batch:
        _validate_batch_state(store,task,batch['content'],stage,config)
        from .context_batching import select_continuity, _manifest
        manifest=_manifest(store,project,batch['content']['manifest_ref'])
        parent_config=store.get(manifest['config_version_id'],project)['values']
        offered=[m for m in selected if m.get('continuity')]
        supplied_cap=min((m.get('continuity_token_cap',parent_config['context']['batching']['carryover_token_cap']) for m in offered),default=None)
        if supplied_cap is not None and (type(supplied_cap) is not int or supplied_cap<0):raise MaterialError('invalid_carryover_cap','Invalid prepared carryover cap')
        continuity=select_continuity(store,project,batch['content'].get('continuity_refs',[]),parent_config,available_tokens=supplied_cap)
        selected=[m for m in selected if not m.get('continuity')]+[_fixed(m,store,project) for m in continuity['materials']]
    if stage=='aux.subtask' and not batch:raise MaterialError('missing_batch_scope','aux.subtask requires a fixed registered batch scope')
    aggregation_refs={json.dumps(m['aggregation_manifest_ref'],sort_keys=True):m['aggregation_manifest_ref'] for m in selected if m.get('aggregation_manifest_ref')}
    if len(aggregation_refs)>1:raise MaterialError('multiple_aggregation_plans','A Run can aggregate one fixed manifest at a time')
    if aggregation_refs:
        from .context_batching import aggregation_materials, _manifest
        manifest_ref=next(iter(aggregation_refs.values()))
        manifest=_manifest(store,project,manifest_ref)
        if manifest['stage']!=stage:raise MaterialError('batch_stage_mismatch','Aggregation stage differs from manifest')
        original=[m for m in selected if not m.get('aggregation_result')]
        selected=aggregation_materials(store,task,run,config,original,manifest_ref)
        selected=[_fixed(m,store,project) for m in selected]
        for material in selected:
            if material.get('aggregation_manifest_ref'):
                material['_aggregation_ranges']=[r.get('json_pointer') or '' for r in manifest['input_refs']
                    if all(r[k]==material['ref'][k] for k in ('record_id','version'))]
    def explicit_summary(material):
        return any(material.get(flag) for flag in ('required','expanded','aggregation_result','continuity'))
    # Working summaries belong to one Session. Other Sessions and stale versions
    # remain retrievable, but may not enter automatically through artifact indexes.
    selected=[m for m in selected if m.get('schema_id')!='work_summary' or explicit_summary(m)]
    seen={(m['ref']['record_id'],m['ref']['version']) for m in selected}
    pinned_versions={m['record']['artifact_id'] for m in selected if m['record']['record_type']=='artifact_version'}
    # Follow exact provenance, not current artifact pointers. Auxiliary summaries only use their archive window.
    if not stage.startswith('aux.'):
        index=0
        while index<len(selected):
            material=selected[index];index+=1
            if material.get('_provenance_only'):continue
            record=material['record']
            if material.get('schema_id')=='nexo_graph':continue
            references=list(_refs(material['content']))+record.get('source_refs',[])
            for dependency_id in record.get('dependency_ids',[]):
                dependency=store.get(dependency_id,project)
                if dependency:references.append(dependency['producer_ref'])
            for reference in references:
                key=(reference['record_id'],reference['version'])
                if key in seen or reference['version'] is None:continue
                resolved=store.resolve_ref(reference,project)
                seen.add(key)
                if resolved['record_type']=='artifact_version':
                    ancestor=_fixed({'ref':_ref(resolved),'required':False},store,project)
                    # An exact older citation is still valid provenance. It is
                    # not another current required input of the same artifact.
                    if resolved['artifact_id'] in pinned_versions:
                        ancestor['_provenance_only']=True
                    selected.append(ancestor)
    selected=[m for m in selected if m.get('schema_id')!='work_summary' or explicit_summary(m)]
    request=store.get(task['requested_by_message_id'],project)
    config_record=store.get(run['config_version_id'],project)
    if not request or not config_record:raise MaterialError('missing_runtime_record','Missing Task request or fixed configuration')
    frame={'request_refs':[_ref(request)],'goal':unwrap(request['content'],store,project),
           'scope':task['scope'],'task_ref':_ref(task),'config_ref':_ref(config_record)}
    additions=[('runtime.task_frame',frame,[_ref(task),_ref(request),_ref(config_record)])]
    chapters=set(task['scope']['chapter_ids'])
    decisions=[d for d in _all(store,project,'decision') if d['status']=='confirmed' and
               (not chapters or not d['scope']['chapter_ids'] or chapters.intersection(d['scope']['chapter_ids']))]
    latest={}
    for decision in _all(store,project,'decision'):
        if decision['decision_id'] not in latest or decision['version']>latest[decision['decision_id']]['version']:latest[decision['decision_id']]=decision
    decisions=[d for d in decisions if latest[d['decision_id']]['id']==d['id']]
    confirmations=_all(store,project,'confirmation')
    revoked={identity for c in confirmations if c['action']=='revoke' for identity in c['revokes_confirmation_ids']}
    confirmations=[c for c in confirmations if c['action']=='confirm' and c['id'] not in revoked]
    controls={'decision_refs':[_ref(d) for d in decisions],'confirmation_refs':[_ref(c) for c in confirmations],
              'constraint_items':[{'decision_ref':_ref(d),'scope':d['scope'],'value':unwrap(d['value'],store,project)} for d in decisions]}
    additions.append(('runtime.applicable_controls',controls,controls['decision_refs']+controls['confirmation_refs']))
    histories=sorted(_all(store,project,'history_record',conversation_id=task['conversation_id']),key=lambda h:h['sequence'])
    recent=histories[-max(1,config['context'].get('recent_turns',6)*2):]
    exchange=[h for h in histories if h['run_id']==run['id']]
    tools=[t for t in _all(store,project,'tool_call',run_id=run['id']) if t['state'] in ('pending','running','unknown')]
    history={'recent_message_refs':[_ref(h) for h in recent],'current_exchange_refs':[_ref(h) for h in exchange],
             'pending_tool_call_refs':[_ref(t) for t in tools]}
    additions.append(('runtime.work_history',history,history['recent_message_refs']+history['current_exchange_refs']+history['pending_tool_call_refs']))
    artifacts=_all(store,project,'artifact');tasks=_all(store,project,'task')
    pending=[]
    for current in tasks:
        data=store.projection_get(f"{project}:task_runtime:{current['id']}") or {}
        pending.extend((current,item) for item in data.get('pending_user_items',[]) if item.get('state')=='open')
    state={'artifact_refs':[{'record_id':a['id'],'version':str(a['current_effective_version'] or a['latest_version']),
                            'item_id':None,'json_pointer':None} for a in artifacts if a['latest_version']],
           'active_task_refs':[_ref(t) for t in tasks if t['state'] not in ('succeeded','failed','stopped')],
           'pending_question_refs':[_ref(t) for t,item in pending if item['kind']!='confirmation'],
           'pending_confirmation_refs':[_ref(t) for t,item in pending if item['kind']=='confirmation']}
    additions.append(('runtime.project_state',state,state['artifact_refs']+state['active_task_refs']))
    if session.get('latest_summary_ref') and not any(m.get('ref')==session['latest_summary_ref'] for m in selected):
        summary=_fixed({'ref':session['latest_summary_ref'],'required':False},store,project)
        if summary.get('schema_id')!='work_summary':
            raise MaterialError('invalid_session_summary','Session summary must reference a fixed work_summary')
        selected.append(summary)
    sources=[m for m in selected if m.get('schema_id')=='source_text']
    if stage=='step1' and len(sources)!=1:
        raise MaterialError('missing_full_source','Step1 requires exactly one frozen complete original')
    for material in sources:
        text=material['content']
        if not isinstance(text,str) or not text:raise MaterialError('empty_source','Original text is empty or not text')
        data={'source_ref':material['ref'],'text':text,'start_utf16':0,'end_utf16':utf16_length(text)}
        if stage=='step1':
            if step1_window is not None:
                start,end=step1_window['start_utf16'],step1_window['end_utf16']
                excerpt=text.encode('utf-16-le')[start*2:end*2].decode('utf-16-le')
                if excerpt!=step1_window['text']:
                    raise MaterialError('source_interval_invalid','Step1 window does not match frozen original')
                data={'source_ref':material['ref'],'text':excerpt,'start_utf16':start,'end_utf16':end}
            selected.append({**material,'builtin':'runtime.full_source','content':data,'required':True,
                             'window_mode':step1_window is not None})
            sections=[];position=data['start_utf16']
            for line in data['text'].splitlines(keepends=True):
                end=position+utf16_length(line)
                sections.append({'section_id':hashlib.sha256(canonical_bytes([material['ref'],position,end])).hexdigest()[:24],
                                 'title':None,'start_utf16':position,'end_utf16':end});position=end
            additions.append(('runtime.source_index',{'source_ref':material['ref'],'sections':sections},[material['ref']]))
        elif stage in ('step5','step9') and not any(m.get('builtin')=='runtime.source_block' for m in selected):
            # Step5 creates independent source anchors; Step9 fixes chapter spans.
            selected.append({**material,'builtin':'runtime.source_block','content':data,'required':True})
        elif stage=='step10' and material.get('required'):
            design=next((m for m in selected if m.get('schema_id')=='chapter_design' and m.get('required')),None)
            if design is None:
                raise MaterialError('missing_required_material','Step10 requires a fixed chapter_design')
            for anchor in design['content']['payload']['chapter_source_anchors']:
                if anchor['source_ref'] != material['ref']:
                    raise MaterialError('source_reference_mismatch','Chapter source range uses a different original')
                start,end=anchor['start_utf16'],anchor['end_utf16']
                if type(start) is not int or type(end) is not int or not 0<=start<end<=utf16_length(text):
                    raise MaterialError('source_interval_invalid','Chapter source range is invalid')
                try:excerpt=text.encode('utf-16-le')[start*2:end*2].decode('utf-16-le')
                except UnicodeError as error:raise MaterialError('source_interval_invalid','Chapter range splits a Unicode character') from error
                selected.append({**material,'builtin':'runtime.source_block',
                                 'content':{'source_ref':material['ref'],'text':excerpt,
                                            'start_utf16':start,'end_utf16':end},'required':True})
    if stage in ('step9','step11'):
        from .schemas import SchemaCatalog
        catalog=SchemaCatalog();schema=(config.get('schemas') or catalog.schemas)['nexo_graph']
        binding=next(item for item in catalog.registry['schemas'] if item['schema_id']=='nexo_graph')
        additions.append(('runtime.target_contract',{'schema_ref':{'schema_id':'nexo_graph','version':binding['version'],
                         'sha256':hashlib.sha256(canonical_bytes(schema)).hexdigest()},
                         'capability_ref':_ref(config_record,'/values/schemas/nexo_graph')},[_ref(config_record)]))
    existing={m.get('builtin') for m in selected}
    for name,content,sources in additions:
        if name not in existing:selected.append(_projection(store,task,run,name,content,sources))
    return selected


IDENTITY_FIELDS=('change_id','finding_id','issue_id','metric_id','check_id','conflict_id',
    'event_id','game_event_id','character_id','chapter_id','node_id','ending_id','route_id','profile_id',
    'interaction_id','annotation_id','constraint_id','entity_id','segment_id','section_id','scene_id',
    'option_id','outcome_id','contract_id','question_id','item_id','id')


def _identity(value):
    """Registered own identity, never an ID merely mentioned by a reference object."""
    if not isinstance(value,dict):return None
    fields=set(value)
    # These shapes are references in the output contracts, despite ID-like keys.
    references=(
        {'record_id','version','item_id','json_pointer'},  # EvidenceRef
        {'object_kind','object_id','chapter_id','node_id','json_pointer'},  # GraphTarget
        {'from_event_id','to_event_id','relation','condition','rationale'},  # Event link
        {'from_segment_id','interaction_id','outcome_id','target','condition_requirement'},  # Flow link
        {'kind','segment_id','chapter_id'},  # Flow target
        {'kind','id'},  # Nexo JumpTarget
        {'event_id','involvement'},  # Character-event participation
        {'profile_id','related_refs','requirement'},  # Profile design implication
        {'intent','stage','chapter_id','target_ref','request','source_message_ids','requested_confirmation_paths'},
    )
    if any(fields==shape for shape in references):return None
    # Objects with both an own identity and another object's ID need explicit ownership.
    owners=(('contract_id',{'segment_id','other_chapter_id','required_state','resulting_state'}),
            ('interaction_id',{'anchor_segment_id','placement','options','outcomes'}),
            ('route_id',{'ending_ids','event_refs'}),
            ('annotation_id',{'event_id'}),('route_id',{'ending_id'}))
    for field,markers in owners:
        if markers<=fields and isinstance(value.get(field),str):return value[field]
    candidates=[value[k] for k in IDENTITY_FIELDS if isinstance(value.get(k),str) and
                (k!='item_id' or {'category','content','suggested_retention','rationale','evidence_refs'}<=fields)]
    return candidates[0] if len(candidates)==1 else None


def _item_identity(content,path):
    current=content;identity=_identity(current)
    for part in path.lstrip('/').split('/') if path else []:
        part=part.replace('~1','/').replace('~0','~')
        current=current[int(part)] if isinstance(current,list) else current[part]
        identity=_identity(current) or identity
    return identity


def _scope_allowed(name,path,value,material,frame,materials):
    scope=frame.get('scope',{});chapters=set(scope.get('chapter_ids',[]))
    if name in ('all_pinned','full_source','source_evidence','history_query','summary_window','referenced_entities'):
        return True
    if name not in ('task_scope','current_batch','selected_player','target_chapters','chapter_dependencies','review_scope'):
        raise MaterialError('unknown_scope_filter','Unregistered scope filter: '+name)
    if name=='selected_player':
        player=None;found=False
        for candidate in materials:
            payload=candidate.get('content',{}).get('payload',{}) if isinstance(candidate.get('content'),dict) else {}
            role=payload.get('player_role',payload.get('player_identity'))
            if role is not None:player=role.get('character_ref');found=True;break
        if not found:raise MaterialError('missing_player_identity','selected_player requires fixed confirmed player identity')
        if player is None:return False
        if any(player[key]!=material['ref'][key] for key in ('record_id','version')):
            raise MaterialError('selected_player_reference_mismatch','Player identity refers to another fixed source version')
        identity=player.get('item_id')
        if identity is None and player.get('json_pointer'):
            matches=pointer_values(material['content'],player['json_pointer'],required=True)
            identity=_identity(matches[0][1]) if len(matches)==1 else None
        if identity is None:raise MaterialError('missing_player_identity','Player reference must identify exactly one original character')
        return bool(isinstance(value,dict) and value.get('character_id')==identity)
    if name=='current_batch':
        batch=next((m['content'] for m in materials if m.get('builtin')=='runtime.batch_state'),None)
        if batch:
            if _item_identity(material['content'],path) is None:return True
            candidates=batch.get('processed_item_refs',[])+batch.get('pending_item_refs',[])
            source=material['ref']
            return any(r['record_id']==source['record_id'] and r['version']==source['version'] and
                       (r.get('item_id')==_item_identity(material['content'],path) or r.get('json_pointer')==path) for r in candidates)
    if chapters and name in ('target_chapters','chapter_dependencies','review_scope','task_scope'):
        if material.get('schema_id')=='chapter_design':return material['content'].get('payload',{}).get('chapter_id') in chapters
        if path.startswith('/chapters/'):
            index=path.split('/')[2]
            if index.isdigit():return material['content']['chapters'][int(index)].get('id') in chapters
    return True  # Unknown entity/chapter dependencies retain all pinned context rather than silently dropping it.


def _batch_values(content,selector,material,materials,required):
    """Expand only exact owned/neighbor units; global indexes use their own rules."""
    batch=next((m['content'] for m in materials if m.get('builtin')=='runtime.batch_state'),None)
    if not batch:return pointer_values(content,selector,required=required)
    reference=material['ref'];units=batch.get('owned_source_ranges',[])+batch.get('neighbor_source_ranges',[])
    relevant=[u for u in units if all(u['source_ref'][k]==reference[k] for k in ('record_id','version'))]
    if not relevant:return []
    selected=pointer_values(content,selector,required=required);out=[]
    for unit in relevant:
        path=unit['source_ref'].get('json_pointer') or ''
        for selected_path,value in selected:
            if selected_path==path or selected_path=='' or path.startswith(selected_path.rstrip('/')+'/'):
                actual=pointer_values(content,path,required=True)[0][1]
                start,end=unit.get('start_utf16'),unit.get('end_utf16')
                if start is not None:
                    if not isinstance(actual,str) or type(start) is not int or type(end) is not int or not 0<=start<end<=utf16_length(actual):
                        raise MaterialError('batch_source_changed','Invalid batch source range')
                    text=actual.encode('utf-16-le')[start*2:end*2].decode('utf-16-le')
                    actual={'text':text,'start_utf16':start,'end_utf16':end}
                out.append((path,actual))
            elif path=='' or selected_path.startswith(path.rstrip('/')+'/'):
                out.append((selected_path,value))
    return out


class PackedMaterials(list):
    """JSON-compatible material list with private, non-serialized pruning policy."""
    def __init__(self):
        super().__init__();self.policies=[]
    def add(self,block,policies):
        self.append(block);self.policies.append(deepcopy(policies))


def prune_optional_materials(packed,selections,available_tokens,config):
    """Drop lowest-priority optional automatic fields; retain immutable audit refs.

    available_tokens is R after reserving the non-material input. Required input
    may still exceed R; the caller must perform fit_input before any model call.
    Plain lists have no trusted policy and are conservatively left untouched.
    """
    if type(available_tokens) is not int or available_tokens<0:raise ValueError('available_tokens must be nonnegative')
    result=deepcopy(packed);audit=deepcopy(selections)
    if not isinstance(result,PackedMaterials) or len(result)!=len(result.policies):return result,audit
    if any(len(policies)!=(len(block['data']) if isinstance(block['data'],list) else 1) for block,policies in zip(result,result.policies)):
        return result,audit
    model=config['model']['name'];candidates=[];removed=set()
    for bi,(block,policies) in enumerate(zip(result,result.policies)):
        for fi,policy in enumerate(policies):
            if policy['required'] or not policy['auto'] or block.get('builtin')=='runtime.full_source':continue
            value=block['data'][fi] if isinstance(block['data'],list) else block['data']
            candidates.append((policy['priority'],-tokens(value,model),bi,fi))
    def rebuild():
        output=PackedMaterials()
        for bi,(block,policies) in enumerate(zip(result,result.policies)):
            if isinstance(block['data'],list):
                kept=[i for i in range(len(policies)) if (bi,i) not in removed]
                if kept:output.add({**block,'data':[block['data'][i] for i in kept]},[policies[i] for i in kept])
            elif (bi,0) not in removed:output.add(block,policies)
        return output
    output=rebuild()
    for _,_,bi,fi in sorted(candidates):
        if tokens(output,model)<=available_tokens:break
        removed.add((bi,fi));output=rebuild()
    still_included={i for policies in output.policies for policy in policies for i in policy.get('also_covers',policy['selection_indices'])}
    for bi,fi in removed:
        for index in result.policies[bi][fi]['selection_indices']:
            if index not in still_included:
                audit[index].update(inclusion='omitted',estimated_tokens=0,
                    reason=audit[index]['reason']+'; optional auto material removed by priority/input budget')
    return output,audit


def build_materials(stage,materials,config,store,project_id):
    profiles=config['context']['profiles'];profile=next((x for x in profiles['profiles'] if x['stage']==stage),None)
    profile_rules=deepcopy(profile['materials'] if profile else [])
    selected_inputs=config['context'].get('stage_inputs',{}).get(stage)
    if selected_inputs is not None:
        from .configuration import ARTIFACT_PRODUCERS
        selected_inputs=set(selected_inputs)
        def logical_kind(rule):
            source=rule.get('source',{})
            builtin=source.get('builtin')
            if builtin in ('runtime.full_source','runtime.source_index','runtime.source_block'):
                return 'source_text'
            return source.get('schema_id')
        # The simple stage-input checklist is authoritative.  Technical
        # selectors remain fixed implementation detail for selected artifacts.
        selected_rules=[]
        for rule in profile_rules:
            kind=logical_kind(rule)
            if kind in ARTIFACT_PRODUCERS:
                if kind not in selected_inputs:
                    continue
                # The business-artifact checklist is authoritative.  Hidden
                # legacy selector flags cannot silently disable a checked item.
                rule['enabled']=True
            selected_rules.append(rule)
        profile_rules=selected_rules
    all_rules=(profiles['common_materials'] if not profile or profile.get('include_common_materials') else [])+profile_rules
    if stage=='aux.subtask':
        all_rules=all_rules+[{'id':'batch_scope','source':{'builtin':'runtime.batch_state'},'selectors':['/'+f for f in profiles['builtin_sources']['runtime.batch_state']['fields']],
                     'required':True,'load':'auto','detail':'full','scope_filter':'all_pinned','coverage':'context_only'}]
    rules=[rule for rule in all_rules if rule.get('enabled',True)]
    for rule in rules:
        if rule['required'] and not any(not m.get('_provenance_only') and (m.get('builtin')==rule['source'].get('builtin') if 'builtin' in rule['source'] else
                                        not m.get('builtin') and m.get('schema_id',m.get('kind'))==rule['source']['schema_id']) for m in materials):
            raise MaterialError('missing_required_material','Missing required material: '+str(rule['source']))
    frame=next((m['content'] for m in materials if m.get('builtin')=='runtime.task_frame'),{})
    packed=PackedMaterials();selections=[];source=None;seen=set()
    for material in materials:
        content=unwrap(material.get('content'),store,project_id);reference=material.get('ref')
        if not reference:raise MaterialError('unbound_material','Material has no fixed source reference')
        if material.get('_provenance_only'):
            selections.append({'source_ref':deepcopy(reference),'selection':{'item_id':None,'json_pointer':''},
                'inclusion':'omitted','reason':'historical provenance only; current input is independently pinned; exact historical ref remains retrievable',
                'estimated_tokens':0})
            continue
        if material.get('program_only'):
            selections.append({'source_ref':deepcopy(reference),'selection':{'item_id':None,'json_pointer':''},
                'inclusion':'omitted','reason':'program-only fixed input; Step6 receives the complete Step5 shared Session instead of duplicating this payload',
                'estimated_tokens':0})
            continue
        name=material.get('schema_id',material.get('kind',''));builtin=material.get('builtin')
        logical_name='source_text' if builtin in ('runtime.full_source','runtime.source_index','runtime.source_block') else name
        if (selected_inputs is not None and logical_name in ARTIFACT_PRODUCERS
                and material.get('material_role')!='current_stage_baseline'
                and logical_name not in selected_inputs):
            selections.append({'source_ref':deepcopy(reference),'selection':{'item_id':None,'json_pointer':''},
                'inclusion':'omitted','reason':'disabled by stage input configuration','estimated_tokens':0})
            continue
        if name=='source_text' and not builtin and (stage=='step10' or any(m.get('builtin') in ('runtime.full_source','runtime.source_block') and m.get('ref')==reference for m in materials)):
            continue  # The exact original is represented by its source projection once.
        configured=[] if material.get('aggregation_result') else [r for r in all_rules if (r['source'].get('builtin')==builtin if builtin else r['source'].get('schema_id')==name)]
        matching=[r for r in configured if r.get('enabled',True)]
        if stage=='aux.subtask' and not builtin and not material.get('continuity'):
            matching=[{'id':'owned_batch_fields','selectors':[''],'required':True,'load':'auto','detail':'full','scope_filter':'current_batch','coverage':'complete_scope'}]
        if material.get('continuity'):
            matching=[{'id':'batch_continuity','selectors':[material['continuity_pointer']],'required':False,'load':'auto',
                'detail':'full','scope_filter':'all_pinned','coverage':'context_only','priority':max((r.get('priority',30) for r in matching),default=30)}]
        if configured and not matching:
            selections.append({'source_ref':deepcopy(reference),'selection':{'item_id':None,'json_pointer':''},
                'inclusion':'omitted','reason':'disabled by stage input configuration','estimated_tokens':0})
            continue
        if not matching and not material.get('required'):continue
        if builtin in ('runtime.full_source','runtime.source_block'):
            original=store.resolve_ref(content['source_ref'],project_id)
            text=unwrap(original['content'],store,project_id);start,end=content['start_utf16'],content['end_utf16']
            try:actual=text.encode('utf-16-le')[start*2:end*2].decode('utf-16-le')
            except (TypeError,UnicodeError):raise MaterialError('source_interval_invalid','Invalid original text range')
            if type(start) is not int or type(end) is not int or not 0<=start<end<=utf16_length(text) or actual!=content['text']:
                raise MaterialError('source_interval_invalid','Original text does not match fixed UTF-16 range')
            if builtin=='runtime.full_source':
                if not material.get('window_mode') and (start!=0 or end!=utf16_length(text)):
                    raise MaterialError('missing_full_source','Step1 requires the full original')
                source=content['text']
            policy={'required':builtin=='runtime.full_source' or material.get('required',False) or any(r['required'] for r in matching),
                    'auto':all(r['load']=='auto' for r in matching),'priority':max((r.get('priority',100) for r in matching),default=100),
                    'selection_indices':[len(selections)]}
            include=policy['required'] or policy['auto'] or bool(material.get('expanded'))
            if include:packed.add({'project_id':project_id,'source_ref':reference,'source_kind':name,'builtin':builtin,'data':deepcopy(content)},[policy])
            selections.append({'source_ref':reference,'selection':{'item_id':None,'json_pointer':''},'inclusion':'included' if include else 'omitted',
                               'reason':f'{builtin}: exact UTF-16 [{start},{end})'+('' if include else '; optional on-demand evidence not requested'),
                               'estimated_tokens':tokens(content['text'],config['model']['name']) if include else 0})
            continue
        if isinstance(content,dict) and content.get('result_kind')=='needs_input':
            raise MaterialError('incomplete_material','needs_input cannot be consumed as a completed stage artifact')
        fields=[]
        if not matching:matching=[{'id':'explicit_dependency','selectors':[''],'required':True,'load':'auto','detail':'full','scope_filter':'all_pinned','coverage':'complete_scope'}]
        for rule in matching:
            if rule['required'] and rule['coverage']=='complete_scope' and rule['detail']!='full':
                raise MaterialError('material_not_expanded','Required complete-scope material must be fully expanded: '+rule['id'])
            for selector in rule['selectors']:
                selected_values=(_batch_values(content,selector,material,materials,rule['required']) if rule['scope_filter']=='current_batch' else pointer_values(content,selector,required=rule['required']))
                for path,value in selected_values:
                    entries=[(path+'/'+str(i),v) for i,v in enumerate(value)] if isinstance(value,list) and value else [(path,value)]
                    for concrete,value in entries:
                        identity=_item_identity(content,concrete)
                        included=not (rule['load']=='on_demand' and not rule['required'] and not material.get('expanded'))
                        included=included and (rule['scope_filter']=='current_batch' or _scope_allowed(rule['scope_filter'],concrete,value,material,frame,materials))
                        delegated=bool(rule['detail']=='full' and rule['coverage']=='complete_scope' and
                            any(root=='' or concrete==root or concrete.startswith(root.rstrip('/')+'/') for root in material.get('_aggregation_ranges',[])))
                        if delegated or (material.get('continuity') and not material.get('continuity_included')):included=False
                        actual_path=material.get('projection_pointer','')+concrete
                        selection={'source_ref':deepcopy(reference),'selection':{'item_id':identity,'json_pointer':actual_path},
                                   'inclusion':'included' if included else 'omitted','reason':rule['id']+': '+('verified complete batch coverage; original retained by fixed ref' if delegated else 'selected fixed field' if included else 'not requested or outside exact scope'),
                                   'estimated_tokens':tokens(value,config['model']['name']) if included else 0}
                        if material.get('continuity') and not included:
                            selection['reason']+='; continuity body exceeds carryover_token_cap; fixed reference retained'
                        if isinstance(value,dict) and set(value)=={'text','start_utf16','end_utf16'}:
                            selection['reason']+=f" UTF-16 [{value['start_utf16']},{value['end_utf16']})"
                        key=(reference['record_id'],reference['version'],actual_path,selection['inclusion'],selection['reason'])
                        if key in seen:continue
                        seen.add(key);selections.append(selection)
                        if included:fields.append({'json_pointer':actual_path,'item_id':identity,'value':deepcopy(value),
                            '_policy':{'required':bool(material.get('required') or rule['required']),'auto':rule['load']=='auto',
                                       'priority':rule.get('priority',100),'selection_indices':[len(selections)-1]}})
        if fields:
            all_fields=fields
            fields=[field for field in fields if not any(other is not field and other['json_pointer']!=field['json_pointer'] and
                    (other['json_pointer']=='' or field['json_pointer'].startswith(other['json_pointer'].rstrip('/')+'/')) and
                    (other['_policy']['required'] or (not field['_policy']['required'] and (not other['_policy']['auto'] or field['_policy']['auto']))) for other in fields)]
            retained_ids={id(f) for f in fields}
            unique=[];known=set();policies=[]
            for field in fields:
                key=(field['json_pointer'],json.dumps(field['value'],sort_keys=True,ensure_ascii=False))
                if key in known:continue
                known.add(key);prefix=field['json_pointer']
                descendants=[f for f in all_fields if (f['json_pointer']==prefix and f['value']==field['value']) or
                         (f['json_pointer']!=prefix and (prefix=='' or f['json_pointer'].startswith(prefix.rstrip('/')+'/')))]
                covered=[f['_policy'] for f in descendants if f['json_pointer']==prefix or id(f) not in retained_ids]
                policies.append({'required':any(p['required'] for p in covered),'auto':all(p['auto'] for p in covered),
                    'priority':max(p['priority'] for p in covered),'selection_indices':sorted({i for p in covered for i in p['selection_indices']}),
                    'also_covers':sorted({i for f in descendants for i in f['_policy']['selection_indices']})})
                unique.append({k:v for k,v in field.items() if k!='_policy'})
            fields=unique
            state=material.get('state')
            if not builtin and material.get('record',{}).get('record_type')=='artifact_version' and state is None and not material.get('aggregation_result') and stage!='coordinator' and not stage.startswith('aux.'):
                raise MaterialError('missing_material_state','Required artifact state is missing')
            if not builtin and state and not material.get('aggregation_result') and not material.get('_historical_baseline_verified') and not stage.startswith('aux.') and stage!='coordinator':
                review_candidate=stage=='step11' and name=='nexo_graph'
                if state['dependency_status']!='valid':raise MaterialError('dependency_changed','Material dependencies are not valid')
                if not review_candidate and state['confirmation_status'] not in ('confirmed','not_required'):
                    confirmed=state.get('effective_selections',[])
                    if not all(any(s['json_pointer']=='' or f['json_pointer']==s['json_pointer'] or f['json_pointer'].startswith(s['json_pointer'].rstrip('/')+'/') for s in confirmed) for f in fields):
                        raise MaterialError('confirmation_required','Selected fields are not fully confirmed')
            packed.add({'project_id':project_id,'source_ref':reference,'source_kind':name,'builtin':builtin,
                           'record_id':material.get('record',{}).get('id'),'schema':material.get('record',{}).get('output_schema'),
                           'scope':material.get('scope'),'state':state,'material_role':material.get('material_role'),'data':fields},policies)
    if stage=='step1' and source is None:raise MaterialError('missing_full_source','Step1缺少完整原文')
    return packed,source,selections


def fit_input(items,instructions,tool_definitions,output_schema,config,full_source=None):
    cap=input_budget(config); model=config['model']['name']
    count=tokens({'instructions':instructions,'input':items,'tools':tool_definitions,'output_schema':output_schema},model)
    if full_source:
        def contains(value):
            if isinstance(value,str):
                if full_source in value:return True
                try: decoded=json.loads(value)
                except (ValueError,TypeError):return False
                return contains(decoded) if decoded!=value else False
            if isinstance(value,dict):return any(contains(v) for v in value.values())
            if isinstance(value,list):return any(contains(v) for v in value)
            return False
        if not contains(items):
            raise BudgetExceeded('missing_full_source','本次调用没有完整原文，拒绝切分')
    if count>cap: raise BudgetExceeded(details={'input_tokens':count,'input_budget':cap,'source_tokens':tokens(full_source,model) if full_source else None})
    return count,cap
