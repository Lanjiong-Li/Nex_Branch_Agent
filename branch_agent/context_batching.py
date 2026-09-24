"""Fixed-input batch plans and a persisted completion ledger.

Planning/preview never marks coverage. The runtime must execute each child Task,
commit its real result versions and call complete_batch in the same transaction.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
from importlib.metadata import version
import re
from uuid import UUID

from .context import MaterialError, BudgetExceeded, _all, _identity, _ref, pointer_values, tokens, unwrap, utf16_length, input_budget
from .records import canonical_bytes, new_record


def _hash(value):
    return hashlib.sha256(value.encode('utf-8') if isinstance(value,str) else canonical_bytes(value)).hexdigest()


def _key(value):return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _check_ref(ref):
    if not isinstance(ref,dict) or set(ref)!={'record_id','version','item_id','json_pointer'}:
        raise MaterialError('batch_reference_invalid','A complete fixed EvidenceRef is required')
    UUID(ref['record_id'])
    if ref['version'] is None or not str(ref['version']).isdigit():
        raise MaterialError('batch_reference_invalid','Batch input must be a fixed artifact version')


def _text_chunks(text, target, hard, model, boundary_order):
    offset=0
    while offset<len(text):
        low=offset+1;high=len(text);best=offset
        while low<=high:
            mid=(low+high)//2
            if tokens(text[offset:mid],model)<=target:best=mid;low=mid+1
            else:high=mid-1
        if best==offset:
            if tokens(text[offset:offset+1],model)>hard:raise BudgetExceeded('batch_unit_too_large','Single Unicode character exceeds batch budget')
            best=offset+1
        end=best;forced=False
        if best<len(text):
            candidate=text[offset:best]
            patterns={'chapter':r'(?m)\n(?=(?:第[^\n]{1,24}[章节卷]|Chapter\s+\d))',
                      'scene':r'(?m)\n(?=(?:第[^\n]{1,24}场|Scene\s+\d|INT[.]|EXT[.]))',
                      'paragraph':r'\n','sentence':r'[。！？!?；;.]'}
            found=False
            for boundary in boundary_order:
                if boundary not in patterns:raise ValueError('Unregistered batch boundary')
                breaks=[m.end() for m in re.finditer(patterns[boundary],candidate)]
                valid=[i for i in breaks if i>=max(1,len(candidate)//2)]
                if valid:end=offset+valid[-1];found=True;break
            forced=not found
        piece=text[offset:end]
        if tokens(piece,model)>hard:raise BudgetExceeded('batch_unit_too_large','Text unit exceeds hard batch budget')
        yield offset,end,piece,forced
        offset=end


def batch_main_limits(config,*,available_tokens=None,neighbor_tokens=0,carryover_tokens=0):
    """Main body <= output reserve and min(hard, R - actual auxiliaries)."""
    settings=config['context']['batching'];ratio=settings['output_headroom_ratio']
    if isinstance(ratio,bool) or not isinstance(ratio,(int,float)) or not .1<=ratio<=.5:
        raise ValueError('output_headroom_ratio must be between 0.1 and 0.5')
    output_body=int(config['model']['max_output_tokens']*(1-ratio))
    hard=min(settings['hard_max_tokens'],output_body)
    if available_tokens is not None:
        if type(available_tokens) is not int or available_tokens<0:raise ValueError('available_tokens must be nonnegative')
        hard=min(hard,available_tokens-neighbor_tokens-carryover_tokens)
    if hard<1:raise BudgetExceeded('batch_material_budget_exhausted','No budget remains for owned batch material')
    return min(settings['target_tokens'],hard),hard


def select_continuity(store,project_id,references,config,*,available_tokens=None):
    """Read exact fixed values up to the cap; retain every unread reference."""
    cap=config['context']['batching']['carryover_token_cap']
    if type(cap) is not int or cap<0:raise ValueError('carryover_token_cap must be nonnegative')
    if available_tokens is not None:cap=min(cap,max(0,available_tokens))
    selected=[];omitted=[];materials=[];used=0;seen=set()
    from .workflow import Workflow
    validator=Workflow(store)
    for reference in references:
        key=_key(reference)
        if key in seen:continue
        seen.add(key)
        validator.validate_evidence(project_id,reference)
        record=store.resolve_ref(reference,project_id)
        if record['record_type']=='artifact_version':value=unwrap(record['content'],store,project_id);prefix=''
        else:value=record;prefix=''
        path=reference.get('json_pointer') or ''
        if reference.get('json_pointer') is None and reference.get('item_id'):
            from .context import IDENTITY_FIELDS
            matches=[]
            def visit(node,prefix=''):
                if isinstance(node,dict):
                    if any(node.get(field)==reference['item_id'] for field in IDENTITY_FIELDS):matches.append(prefix)
                    for name,child in node.items():visit(child,prefix+'/'+str(name).replace('~','~0').replace('/','~1'))
                elif isinstance(node,list):
                    for index,child in enumerate(node):visit(child,prefix+'/'+str(index))
            visit(value)
            if len(matches)!=1:raise MaterialError('continuity_reference_ambiguous','Continuity item must be unique')
            path=matches[0]
        values=pointer_values(value,path,required=True)
        if len(values)!=1:raise MaterialError('continuity_reference_ambiguous','Continuity must identify exactly one original value')
        value=values[0][1];size=tokens(value,config['model']['name'])
        include=size<=cap-used
        if include:used+=size;selected.append(deepcopy(reference))
        else:omitted.append(deepcopy(reference))
        materials.append({'ref':deepcopy(reference),'record':record,'required':False,'continuity':True,
            'continuity_pointer':path,'continuity_included':include,'continuity_token_cap':cap})
    return {'materials':materials,'included_refs':selected,'omitted_refs':omitted,'tokens':used,'token_cap':cap}


def _units(inputs,config):
    settings=config['context']['batching'];model=config['model']['name']
    target,hard=batch_main_limits(config);result=[];seen=set()
    if type(target) is not int or type(hard) is not int or not 0<target<=hard:
        raise ValueError('Batch token limits must satisfy 0 < target <= hard')
    for item in inputs:
        reference=deepcopy(item['source_ref']);_check_ref(reference)
        if _key(reference) in seen:raise MaterialError('duplicate_batch_source','Batch input source duplicated')
        seen.add(_key(reference));content=item['content']
        def emit(value,ref,start=None,end=None,forced=False):
            unit={'source_ref':deepcopy(ref),'start_utf16':start,'end_utf16':end,'content_sha256':_hash(value)}
            unit['unit_id']='unit_'+_key(unit)[:32]
            result.append((unit,value,forced))
        def structured(value,ref):
            if tokens(value,model)<=target:
                emit(value,ref);return
            if isinstance(value,(dict,list)) and value:
                entries=value.items() if isinstance(value,dict) else enumerate(value)
                for key,child in entries:
                    escaped=str(key).replace('~','~0').replace('/','~1')
                    childref={**ref,'json_pointer':(ref.get('json_pointer') or '')+'/'+escaped,
                              'item_id':_identity(child) or ref.get('item_id')}
                    structured(child,childref)
            elif isinstance(value,str):
                for start,end,piece,forced in _text_chunks(value,target,hard,model,settings['boundary_order']):
                    emit(piece,ref,utf16_length(value[:start]),utf16_length(value[:end]),forced)
            elif tokens(value,model)<=hard:emit(value,ref)
            else:raise BudgetExceeded('batch_unit_too_large','Structured atomic value exceeds batch budget')
        if isinstance(content,str):
            if not content:raise MaterialError('empty_batch_source','Empty original cannot create a batch plan')
            base=item.get('start_utf16',0)
            if type(base) is not int or base<0:raise ValueError('Invalid text base offset')
            if item.get('end_utf16',base+utf16_length(content))!=base+utf16_length(content):raise ValueError('Text range length mismatch')
            for start,end,piece,forced in _text_chunks(content,target,hard,model,settings['boundary_order']):
                emit(piece,reference,base+utf16_length(content[:start]),base+utf16_length(content[:end]),forced)
        else:structured(content,reference)
    if not result:raise MaterialError('empty_batch_plan','No actual material to process')
    return result


def plan_batches(stage,task_id,config_version_id,inputs,config,*,generation=1):
    if stage=='step1':raise ValueError('Step1 must always read the complete original; batching is forbidden')
    allowed={p['stage'] for p in config['context']['profiles']['profiles']}
    if stage not in allowed:raise ValueError('Unknown batch stage')
    UUID(task_id);UUID(config_version_id)
    if type(generation) is not int or generation<1:raise ValueError('Invalid plan generation')
    values=_units(inputs,config);settings=config['context']['batching'];model=config['model']['name']
    max_items=settings['max_items'];target,hard=batch_main_limits(config)
    if type(max_items) is not int or max_items<1:raise ValueError('Batch max_items must be positive')
    groups=[];current=[];count=0
    for entry in values:
        size=tokens(entry[1],model)
        if current and (len(current)>=max_items or count+size>target):groups.append(current);current=[];count=0
        current.append(entry);count+=size
        if count>hard:raise BudgetExceeded('batch_unit_too_large','Owned materials exceed batch hard limit')
    if current:groups.append(current)
    batches=[];positions={entry[0]['unit_id']:i for i,entry in enumerate(values)}
    neighbor=settings.get('neighbor_tokens_each_side',0)
    for group in groups:
        owned=[u['unit_id'] for u,_,_ in group];context=[]
        for direction,start in ((-1,positions[owned[0]]-1),(1,positions[owned[-1]]+1)):
            budget=neighbor;index=start
            while 0<=index<len(values):
                entry=values[index];size=tokens(entry[1],model)
                if size>budget:break
                context.append(entry[0]['unit_id']);budget-=size;index+=direction
        batch_id='batch_'+_key([task_id,config_version_id,generation,owned])[:32]
        batches.append({'batch_id':batch_id,'sequence':len(batches)+1,'owned_unit_ids':owned,
                        'context_unit_ids':context,'boundary_split':any(e[2] for e in group)})
    return {'schema_version':'1.0.0','task_id':task_id,'config_version_id':config_version_id,'stage':stage,
            'generation':generation,'input_refs':[deepcopy(i['source_ref']) for i in inputs],
            'units':[u for u,_,_ in values],'batches':batches,
            'estimator_version':'tiktoken/'+version('tiktoken')+':o200k_base'}


def validate_manifest(manifest,inputs,config):
    """Registered planner output is deterministic; reject edits, gaps and hash drift."""
    expected=plan_batches(manifest['stage'],manifest['task_id'],manifest['config_version_id'],inputs,config,
                          generation=manifest['generation'])
    if canonical_bytes(expected)!=canonical_bytes(manifest):
        raise MaterialError('batch_manifest_mismatch','Manifest differs from fixed input, coverage or registered plan')
    return True


def prepare_batch(manifest,batch_id,inputs,config,*,manifest_ref=None,processed_unit_ids=(),continuity_refs=(),
                  available_tokens=None,store=None,project_id=None):
    """Fit pending owned units into R; omitted units stay pending in the same batch.

    R excludes instructions/protocol/current user input and required controls.
    No model output or completion is inferred from this preparation operation.
    """
    validate_manifest(manifest,inputs,config)
    batch=next((b for b in manifest['batches'] if b['batch_id']==batch_id),None)
    if batch is None:raise MaterialError('unknown_batch','Batch not found in fixed manifest')
    unitmap={u['unit_id']:(u,value) for u,value,_ in _units(inputs,config)}
    completed=set(processed_unit_ids)
    if not completed<=set(unitmap):raise MaterialError('batch_coverage_invalid','Processed unit is not in this plan')
    def pieces(ids):return [{**deepcopy(unitmap[i][0]),'content':deepcopy(unitmap[i][1])} for i in ids]
    pending=[i for i in batch['owned_unit_ids'] if i not in completed]
    if continuity_refs and (store is None or project_id is None):
        raise MaterialError('continuity_store_required','Continuity refs require real project storage for budgeted reading')
    continuity=select_continuity(store,project_id,continuity_refs,config) if continuity_refs else {'materials':[],'tokens':0,'omitted_refs':[],'included_refs':[]}
    remaining=input_budget(config) if available_tokens is None else available_tokens
    # Mandatory first unit wins over optional adjacent/carryover content.
    minimum=tokens(unitmap[pending[0]][1],config['model']['name']) if pending else 0
    if pending and minimum>remaining:
        raise BudgetExceeded('batch_unit_exceeds_remaining_budget','Fixed owned unit does not fit remaining R; create a revised smaller plan',
                             {'remaining_tokens':remaining,'unit_tokens':minimum,'unit_id':pending[0]})
    if continuity['tokens']>max(0,remaining-minimum):
        continuity=select_continuity(store,project_id,continuity_refs,config,available_tokens=max(0,remaining-minimum))
    neighbors=[];neighbor_tokens=0
    for identity in batch['context_unit_ids']:
        size=tokens(unitmap[identity][1],config['model']['name'])
        if size+neighbor_tokens+continuity['tokens']+minimum<=remaining:
            neighbors.append(identity);neighbor_tokens+=size
    _,hard=batch_main_limits(config,available_tokens=remaining,neighbor_tokens=neighbor_tokens,carryover_tokens=continuity['tokens']) if pending else (0,0)
    owned=[];owned_tokens=0
    for identity in pending:
        size=tokens(unitmap[identity][1],config['model']['name'])
        if size+owned_tokens>hard:break
        owned.append(identity);owned_tokens+=size
    # State holds exact ranges, not another full text copy. Source fields are
    # expanded once by context.build_materials, preserving their own selectors.
    ranges=lambda ids:[deepcopy(unitmap[i][0]) for i in ids]
    state={'manifest_ref':deepcopy(manifest_ref),'current_batch_id':batch_id,
           'owned_source_ranges':ranges(owned),'neighbor_source_ranges':ranges(neighbors),
           'processed_item_refs':[deepcopy(unitmap[i][0]['source_ref']) for i in batch['owned_unit_ids'] if i in completed],
           'pending_item_refs':[deepcopy(unitmap[i][0]['source_ref']) for i in pending],
           'continuity_refs':list(deepcopy(continuity_refs))}
    return {'owned':pieces(owned),'neighbors':pieces(neighbors),'runtime_batch_state':state,
            'continuity_materials':continuity['materials'],'continuity_omitted_refs':continuity['omitted_refs'],
            'budget':{'remaining_tokens':remaining,'main_hard_tokens':hard,'owned_tokens':owned_tokens,
                      'neighbor_tokens':neighbor_tokens,'carryover_tokens':continuity['tokens'],
                      'output_headroom_ratio':config['context']['batching']['output_headroom_ratio']},
            'deferred_unit_ids':[i for i in pending if i not in owned],'complete':not pending}


def persist_manifest(store,task,run,manifest,inputs,config):
    validate_manifest(manifest,inputs,config);project=task['project_id']
    if manifest['task_id']!=task['id'] or manifest['config_version_id']!=run['config_version_id'] or run['task_id']!=task['id']:
        raise MaterialError('batch_owner_mismatch','Manifest Task/Run/config mismatch')
    fixedconfig=store.get(run['config_version_id'],project)
    if not fixedconfig or canonical_bytes(fixedconfig['values'])!=canonical_bytes(config):
        raise MaterialError('batch_config_changed','Planner settings differ from fixed Run configuration')
    for item in inputs:
        record=store.resolve_ref(item['source_ref'],project);full=unwrap(record['content'],store,project)
        path=item['source_ref']['json_pointer'] or ''
        selected=pointer_values(full,path,required=True)
        if len(selected)!=1:raise MaterialError('batch_source_invalid','Input must name exactly one fixed value')
        actual=selected[0][1]
        if 'start_utf16' in item:
            actual=actual.encode('utf-16-le')[item['start_utf16']*2:item['end_utf16']*2].decode('utf-16-le')
        if canonical_bytes(actual)!=canonical_bytes(item['content']):raise MaterialError('batch_source_changed','Input differs from persisted fixed content')
    with store.transaction():
        artifact=store.put(new_record('artifact',project,artifact_kind='batch_manifest',scope=task['scope']))
        saved=store.put(new_record('artifact_version',project,artifact_id=artifact['id'],version=1,
            content={'storage':'inline_json','value':manifest},output_schema=None,source_refs=manifest['input_refs'],
            origin='program',config_version_id=run['config_version_id']))
        store.put(new_record('artifact_state',project,artifact_id=artifact['id'],artifact_version_id=saved['id'],version=1,
            confirmation_status='not_required',dependency_status='valid',quality_status='passed'))
        artifact.update(latest_version=1,current_effective_version=1);store.update(artifact,artifact['row_version'])
    return saved


def _manifest(store,project,reference):
    saved=store.resolve_ref(reference,project);artifact=store.get(saved.get('artifact_id'),project)
    if not artifact or artifact['artifact_kind']!='batch_manifest':raise MaterialError('invalid_manifest_ref','Expected fixed batch_manifest artifact')
    return unwrap(saved['content'],store,project)


def batch_coverage(store,project,manifest_ref):
    manifest=_manifest(store,project,manifest_ref);units={u['unit_id'] for u in manifest['units']};processed=set();results=[]
    for event in _all(store,project,'runtime_event',event_name='context.batch_completed'):
        payload=event['payload']
        if payload['manifest_ref']==manifest_ref:
            processed.update(payload['processed_unit_ids']);results.extend(payload['result_refs'])
    if not processed<=units:raise MaterialError('invalid_batch_ledger','Completion ledger names unknown units')
    return {'complete':processed==units,'processed_unit_ids':sorted(processed),'pending_unit_ids':sorted(units-processed),
            'result_refs':results,'batches':[{'batch_id':b['batch_id'],'complete':set(b['owned_unit_ids'])<=processed} for b in manifest['batches']]}


def complete_batch(store,project,manifest_ref,batch_id,child_task_id,result_refs,processed_unit_ids,unresolved_refs=()):
    """Commit only proven result identities; unresolved work never grants coverage."""
    with store.transaction():
        store.advisory_lock(f'{project}:batch:{manifest_ref["record_id"]}:{manifest_ref["version"]}')
        manifest=_manifest(store,project,manifest_ref)
        batch=next((b for b in manifest['batches'] if b['batch_id']==batch_id),None)
        child=store.get(child_task_id,project)
        if not child or child['record_type']!='task' or child['parent_task_id']!=manifest['task_id']:
            raise MaterialError('batch_owner_mismatch','Completion requires an actual child Task of the manifest Task')
        if not batch or not set(processed_unit_ids)<=set(batch['owned_unit_ids']) or len(set(processed_unit_ids))!=len(processed_unit_ids):
            raise MaterialError('batch_coverage_invalid','Only unique owned units can be completed')
        if unresolved_refs and processed_unit_ids:raise MaterialError('batch_unresolved','Separate unresolved work before granting completion')
        if not result_refs:raise MaterialError('batch_result_missing','Persisted result references are required')
        for reference in result_refs:
            result=store.resolve_ref(reference,project)
            producer=store.get(result.get('producer_run_id'),project) if result.get('producer_run_id') else None
            if result['record_type']!='artifact_version' or not producer or producer['task_id']!=child_task_id:
                raise MaterialError('batch_result_owner_mismatch','Result must be produced by the actual batch child Task')
        for reference in unresolved_refs:store.resolve_ref(reference,project)
        previous=batch_coverage(store,project,manifest_ref)
        if set(processed_unit_ids)&set(previous['processed_unit_ids']):
            raise MaterialError('batch_already_completed','Already committed units must be reused, not completed again')
        store.advisory_lock(f'{project}:event_sequence');key=f'{project}:sequence:events'
        counter=store.projection_get(key) or {'project_id':project,'value':0}
        counter['value']=max(counter['value'],max((e['sequence'] for e in _all(store,project,'runtime_event')),default=0))+1
        store.projection_put(key,counter)
        return store.put(new_record('runtime_event',project,sequence=counter['value'],event_name='context.batch_completed',
            conversation_id=child['conversation_id'],task_id=child_task_id,run_id=child.get('current_run_id'),
            payload={'manifest_ref':deepcopy(manifest_ref),'batch_id':batch_id,'task_id':child_task_id,
                     'result_refs':deepcopy(result_refs),'processed_unit_ids':list(processed_unit_ids),'unresolved_refs':list(unresolved_refs)}))


def aggregation_materials(store,task,run,config,materials,manifest_ref):
    """Authorize complete batch synthesis for Steps 2–8, preserving all source refs.

    Pass the returned list through prepare_runtime_materials/build_materials.
    Source fields remain audited as delegated, and real child outputs are loaded.
    """
    manifest=_manifest(store,task['project_id'],manifest_ref)
    stage=manifest['stage']
    if stage not in {f'step{i}' for i in range(2,9)}:
        raise MaterialError('aggregation_forbidden','Step1/9/10/11 require original full-scope reading; summary substitution is forbidden')
    project=task['project_id']
    if manifest['task_id']!=task['id'] or run['task_id']!=task['id']:
        raise MaterialError('batch_owner_mismatch','Aggregation must belong to the original manifest Task')
    fixedconfig=store.get(manifest['config_version_id'],project)
    if not fixedconfig or canonical_bytes(fixedconfig['values'])!=canonical_bytes(config):
        raise MaterialError('batch_config_changed','Aggregation must retain the manifest fixed configuration')
    inputs=[]
    for reference in manifest['input_refs']:
        record=store.resolve_ref(reference,project)
        actual=pointer_values(unwrap(record['content'],store,project),reference['json_pointer'] or '',required=True)
        if len(actual)!=1:raise MaterialError('batch_input_ambiguous','Manifest input must name exactly one fixed value')
        states=_all(store,project,'artifact_state',artifact_version_id=record['id'])
        if not states or states[0]['dependency_status']!='valid':
            raise MaterialError('dependency_changed','Batch source dependencies are no longer valid')
        state=states[0];path=reference['json_pointer'] or ''
        if state['confirmation_status'] not in ('confirmed','not_required') and not any(
            s['json_pointer']=='' or path==s['json_pointer'] or path.startswith(s['json_pointer'].rstrip('/')+'/') for s in state['effective_selections']):
            raise MaterialError('confirmation_required','Batch source scope is no longer confirmed')
        inputs.append({'source_ref':reference,'content':actual[0][1]})
    validate_manifest(manifest,inputs,config)
    coverage=batch_coverage(store,project,manifest_ref)
    if not coverage['complete']:raise MaterialError('batch_coverage_incomplete','All owned units must be completed before aggregation')
    results=[];seen=set()
    for event in _all(store,project,'runtime_event',event_name='context.batch_completed'):
        payload=event['payload']
        if payload['manifest_ref']!=manifest_ref:continue
        child=store.get(payload['task_id'],project)
        if not child or child['parent_task_id']!=task['id']:
            raise MaterialError('batch_owner_mismatch','Completion ledger child does not belong to the manifest Task')
        for reference in payload['result_refs']:
            record=store.resolve_ref(reference,project)
            producer=store.get(record.get('producer_run_id'),project) if record.get('producer_run_id') else None
            if record['record_type']!='artifact_version' or not producer or producer['task_id']!=child['id']:
                raise MaterialError('batch_result_owner_mismatch','Aggregation result producer does not match the actual batch Task')
            content=unwrap(record['content'],store,project)
            artifact=store.get(record['artifact_id'],project)
            if artifact['artifact_kind'] not in ('subtask_result','work_summary') or not record['output_schema']:
                raise MaterialError('invalid_batch_result_type','Aggregation accepts registered subtask_result/work_summary outputs')
            from .schemas import SchemaCatalog
            SchemaCatalog().validate(artifact['artifact_kind'],content,config.get('schemas'))
            if isinstance(content,dict) and content.get('result_kind')=='needs_input':
                raise MaterialError('batch_result_incomplete','Incomplete child output cannot satisfy batch coverage')
            if _key(reference) not in seen:
                seen.add(_key(reference));results.append({'ref':deepcopy(reference),'content':content,
                    'record':record,'required':True,'aggregation_result':True})
    bound={(m['ref']['record_id'],m['ref']['version']) for m in materials if m.get('ref')}
    if any((r['record_id'],r['version']) not in bound for r in manifest['input_refs']):
        raise MaterialError('batch_input_unbound','Manifest input is absent from current fixed stage materials')
    result=deepcopy(materials)
    for material in result:
        if material.get('ref') and any(all(material['ref'][k]==r[k] for k in ('record_id','version')) for r in manifest['input_refs']):
            material['aggregation_manifest_ref']=deepcopy(manifest_ref)
    return result+results
