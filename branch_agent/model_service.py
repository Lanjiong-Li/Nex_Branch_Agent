"""Agents SDK execution with persistent sessions, read tools and per-call audit."""
from __future__ import annotations
import asyncio
from copy import deepcopy
from decimal import Decimal
import json
import os
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from dotenv import load_dotenv, dotenv_values
from agents import Agent, Runner, RunConfig, ModelSettings, ModelRetrySettings, function_tool
from agents.lifecycle import RunHooksBase
from agents.models.openai_responses import OpenAIResponsesModel, _mark_transport_request_without_usage
from agents.run import ModelInputData
from openai import AsyncOpenAI
from openai.types.shared import Reasoning
from .context import BudgetExceeded, PackedMaterials, build_materials, prepare_runtime_materials, fit_input, tokens, unwrap, input_budget, prune_optional_materials
from .schemas import ROOT, SchemaCatalog, digest
from .prompts import instructions, stage_agent
from .records import new_record, now_utc, usage
from .model_errors import terminal_failure, http_failure


class ModelRunError(RuntimeError):
    def __init__(self, code, message, retryable=False, details=None):
        self.code,self.retryable,self.details=code,retryable,details or {}
        super().__init__(message)


def ref(row):
    return {'record_id':row.get('artifact_id',row.get('decision_id',row['id'])),
        'version':str(row['version']) if row['record_type'] in ('artifact_version','decision') else None,
        'item_id':None,'json_pointer':None}


def all_records(store, project_id, record_type=None, filters=None):
    result=[]; offset=0
    while True:
        page=store.list(project_id,record_type,limit=1000,offset=offset,filters=filters)
        result.extend(page)
        if len(page)<1000:return result
        offset+=len(page)


def plain(value):
    if hasattr(value,'model_dump'): return value.model_dump(mode='json',exclude_none=True)
    if isinstance(value,list): return [plain(v) for v in value]
    if isinstance(value,dict): return {k:plain(v) for k,v in value.items()}
    return value


def omit_history_summary_copy(packed, selections, history, summary_ref, summary_content):
    """Keep an exact working summary once; its archive and audit reference survive."""
    if not summary_ref or not isinstance(packed,PackedMaterials) or len(packed)!=len(packed.policies):return packed,selections
    present=False
    for item in history:
        if item.get('role')!='user' or not isinstance(item.get('content'),str):continue
        try:value=json.loads(item['content'])
        except ValueError:continue
        if isinstance(value,dict) and 'working_summary' in value and value['working_summary']==summary_content:
            present=True;break
    if not present:return packed,selections
    result=PackedMaterials();audit=deepcopy(selections)
    for block,policies in zip(packed,packed.policies):
        target=block.get('source_ref',{})
        same=block.get('source_kind')=='work_summary' and all(target.get(k)==summary_ref.get(k) for k in ('record_id','version'))
        if not same:result.add(block,policies);continue
        for policy in policies:
            for index in policy.get('also_covers',policy['selection_indices']):
                audit[index].update(inclusion='omitted',estimated_tokens=0,
                    reason=audit[index]['reason']+'; exact current summary already included in working history')
    return result,audit


def append_history(store, project_id, conversation_id, content, *, role='assistant',kind='model_output',
                   visibility='internal',session_id=None,task_id=None,run_id=None,operation_id=None,provider_item_id=None):
    with store.transaction():
        store.advisory_lock('history:'+conversation_id)
        conversation=store.get(conversation_id,project_id)
        sequence=conversation['last_message_seq']+1
        conversation['last_message_seq']=sequence
        store.update(conversation,conversation['row_version'])
        return store.put(new_record('history_record',project_id,conversation_id=conversation_id,sequence=sequence,
            role=role,visibility=visibility,kind=kind,
            content={'storage':'inline_text','text':content} if isinstance(content,str) else {'storage':'inline_json','value':plain(content)},
            session_id=session_id,task_id=task_id,run_id=run_id,operation_id=operation_id,provider_item_id=provider_item_id))


class PersistentSession:
    session_settings=None
    def __init__(self, store, session, task, run):
        self.store,self.row,self.task,self.run=store,session,task,run
        self.session_id=session['id']; self.history_ids=[]
    def assert_writer(self,row):
        live=self.store.get(self.run['id'],self.row['project_id'])
        if row['fencing_token']!=self.run['fencing_token'] or live['fencing_token']!=self.run['fencing_token']:
            raise ModelRunError('lease_lost','Session已由新的运行接管，旧运行不能覆盖工作历史')
    async def get_items(self,limit=None):
        row=self.store.get(self.session_id,self.row['project_id'])
        items=all_records(self.store,self.row['project_id'],'session_item',{'session_id':self.session_id,'generation':row['generation']})
        items.sort(key=lambda x:x['sequence'])
        if limit is not None: items=items[-limit:] if limit else []
        self.history_ids=[i for r in items for i in r['history_ids']]
        result=[]
        for r in items:
            item=unwrap(r['sdk_item'],self.store,r['project_id'])
            # Re-load materials from this Run's fixed versions, not previous copies in chat history.
            if item.get('role')=='user' and isinstance(item.get('content'),str):
                try:
                    old=json.loads(item['content'])
                    if isinstance(old,dict) and '_harness_materials' in old: item={**item,'content':old['request']}
                except (ValueError,KeyError): pass
            result.append(item)
        return result
    async def add_items(self,items):
        with self.store.transaction():
            self.store.advisory_lock('session:'+self.session_id)
            row=self.store.get(self.session_id,self.row['project_id'])
            self.assert_writer(row)
            for item in items:
                item=plain(item)
                archive=append_history(self.store,row['project_id'],row['conversation_id'],item,
                   role='user' if item.get('role')=='user' else 'assistant',
                   kind='model_input' if item.get('role')=='user' else 'model_output',
                   session_id=row['id'],task_id=self.task['id'],run_id=self.run['id'])
                row['last_item_seq']+=1
                self.store.put(new_record('session_item',row['project_id'],session_id=row['id'],generation=row['generation'],
                   sequence=row['last_item_seq'],sdk_item={'storage':'inline_json','value':item},history_ids=[archive['id']]))
            self.row=self.store.update(row,row['row_version'])
    async def pop_item(self):
        with self.store.transaction():
            self.store.advisory_lock('session:'+self.session_id)
            row=self.store.get(self.session_id,self.row['project_id']);self.assert_writer(row)
            items=all_records(self.store,self.row['project_id'],'session_item',{'session_id':self.session_id,'generation':row['generation']})
            if not items:return None
            item=max(items,key=lambda x:x['sequence']); self.store.delete(item['id'],self.row['project_id'])
            self.row=self.store.update(row,row['row_version'])
            return unwrap(item['sdk_item'],self.store,self.row['project_id'])
    async def clear_session(self):
        with self.store.transaction():
            self.store.advisory_lock('session:'+self.session_id)
            row=self.store.get(self.session_id,self.row['project_id']); row['generation']+=1
            self.assert_writer(row)
            self.row=self.store.update(row,row['row_version'])


class ReadTools:
    def __init__(self,store,project_id,config): self.store,self.project_id,self.config=store,project_id,config
    def _record_ref(self,row):
        target=ref(row)
        if row['record_type'] not in ('artifact_version','decision'):target['record_id']=row['id']
        return target
    def resolve(self,target):
        record_id=target['record_id']; version=target.get('version')
        if version is not None:
            rows=self.store.list(self.project_id,'artifact_version',limit=10000,filters={'artifact_id':record_id,'version':int(version)})
            if not rows: rows=self.store.list(self.project_id,'decision',limit=10000,filters={'decision_id':record_id,'version':int(version)})
            record=rows[0] if rows else None
        else: record=self.store.get(record_id,self.project_id)
        if not record:return None,None
        content=unwrap(record.get('content',record),self.store,self.project_id)
        root=content
        pointer=target.get('json_pointer')
        if pointer is not None:
            from .context import pointer_values
            paths=pointer_values(root,pointer,required=True)
            if len(paths)!=1:raise ValueError('字段路径必须定位一个确定值')
            content=paths[0][1]
        if target.get('item_id'):
            from .context import _identity
            matches=[]
            def visit(value,path=''):
                if isinstance(value,dict):
                    if _identity(value)==target['item_id']:matches.append((path,value))
                    for key,child in value.items():visit(child,path+'/'+str(key).replace('~','~0').replace('/','~1'))
                elif isinstance(value,list):
                    for index,child in enumerate(value):visit(child,path+'/'+str(index))
            visit(root)
            if pointer is None:
                if len(matches)!=1:raise ValueError('条目ID不存在或不唯一，请提供准确字段路径')
                content=matches[0][1]
            elif not any(pointer==path or pointer.startswith(path+'/') or path.startswith(pointer+'/') for path,_ in matches):
                raise ValueError('字段路径与条目ID不对应')
        return record,content
    def page(self,content,target,offset=0,extra=None):
        text=content if isinstance(content,str) else json.dumps(content,ensure_ascii=False)
        if offset<0 or offset>len(text):raise ValueError('分页位置超出该固定内容范围')
        cap=self.config['tools']['read_token_cap'];model=self.config['model']['name']
        def result(end):
            return {'status':'ok','data':text[offset:end],'source_ref':target,
                    'range':{'unit':'unicode_codepoints','start':offset,'end':end},
                    'truncated':end<len(text),'next_cursor':str(end) if end<len(text) else None,**(extra or {})}
        def size(end):return tokens(json.dumps(result(end),ensure_ascii=False),model)
        # Include state, refs, cursors and JSON escaping; every non-final page must advance.
        low=min(len(text),offset+1);high=min(len(text),offset+max(64,cap*12))
        if size(low)>cap:
            raise BudgetExceeded('tool_output_budget_exceeded','工具读取预算不足以容纳来源引用与状态')
        while low<high:
            end=(low+high+1)//2
            if size(end)<=cap:low=end
            else:high=end-1
        return result(low)
    def _search_text(self,row):
        content=unwrap(row.get('content',row),self.store,self.project_id)
        return content if isinstance(content,str) else json.dumps(content,ensure_ascii=False)
    def _snippet(self,text,position,limit):
        # Slice actual content at Unicode boundaries; measure the configured model's tokens.
        start=max(0,position-limit//4)
        low=start;high=min(len(text),start+max(64,limit*12))
        while low<high:
            end=(low+high+1)//2
            if tokens(text[start:end],self.config['model']['name'])<=limit:low=end
            else:high=end-1
        return {'snippet':text[start:low],
                'range':{'unit':'unicode_codepoints','start':start,'end':low},
                'truncated':start>0 or low<len(text)}
    def search(self,query,record_type=None,cursor=None):
        import re
        if not query.strip():raise ValueError('检索关键词不能为空')
        if cursor is not None and (not cursor.isascii() or not cursor.isdecimal()):
            raise ValueError('分页游标必须为非负整数')
        offset=int(cursor or 0)
        rows=all_records(self.store,self.project_id,record_type)
        texts={row['id']:self._search_text(row) for row in rows}
        pattern=re.compile(re.escape(query),re.IGNORECASE)
        matches={identity:match.start() for identity,text in texts.items() if (match:=pattern.search(text))}
        history={};positions={}
        neighbors=self.config['retrieval']['neighbor_messages']
        if neighbors:
            for row in rows:
                if row['record_type']=='history_record':history.setdefault(row['conversation_id'],[]).append(row)
            for messages in history.values():
                messages.sort(key=lambda row:row['sequence'])
                positions.update((row['id'],index) for index,row in enumerate(messages))
        entries=[];seen=set()
        def add(row,neighbor_of=None):
            if row['id'] in seen:return
            seen.add(row['id'])
            item={'record_type':row['record_type'],'record_ref':self._record_ref(row),'created_at':row['created_at'],
                  'match':row['id'] in matches}
            if row['record_type']=='history_record':
                item.update({key:row.get(key) for key in ('conversation_id','sequence','role','kind','session_id','operation_id','provider_item_id')})
            if neighbor_of:item['neighbor_of']=self._record_ref(neighbor_of)
            entries.append((item,texts[row['id']],matches.get(row['id'],0)))
        for row in rows:
            if row['id'] not in matches:continue
            add(row)
            if neighbors and row['record_type']=='history_record':
                messages=history[row['conversation_id']];index=positions[row['id']]
                for neighbor in messages[max(0,index-neighbors):index+neighbors+1]:
                    if neighbor['id']!=row['id']:add(neighbor,row)
        if offset>len(entries):raise ValueError('分页游标超出当前检索结果范围')
        selected=[];hit_count=0;next_offset=offset
        top_k=self.config['retrieval']['top_k'];cap=self.config['tools']['read_token_cap']
        model=self.config['model']['name']
        def result(items,end):
            return {'status':'ok' if entries else 'no_matches','items':items,
                    'next_cursor':str(end) if end<len(entries) else None}
        for item,text,position in entries[offset:]:
            if item['match'] and hit_count>=top_k:break
            limit=self.config['retrieval']['snippet_tokens']
            candidate={**item,**self._snippet(text,position,limit)}
            # Include references, metadata and JSON encoding in the actual tool-output cap.
            while tokens(json.dumps(result(selected+[candidate],next_offset+1),ensure_ascii=False),model)>cap:
                if selected:break
                if limit==0:raise ValueError('工具读取预算不足以容纳检索引用与分页信息')
                limit//=2
                candidate={**item,**self._snippet(text,position,limit)}
            else:
                selected.append(candidate);next_offset+=1;hit_count+=int(item['match'])
                continue
            break
        return result(selected,next_offset)
    def functions(self):
        @function_tool
        async def read_record(record_id:str, version:str|None=None, cursor:str|None=None, item_id:str|None=None, json_pointer:str|None=None)->str:
            """读取当前项目的固定记录或原作；后续页须沿用record_id和version。"""
            target={'record_id':record_id,'version':version,'item_id':item_id,'json_pointer':json_pointer}
            row,value=self.resolve(target)
            if row is None: return json.dumps({'status':'not_found'},ensure_ascii=False)
            return json.dumps(self.page(value,target,int(cursor or 0)),ensure_ascii=False)
        @function_tool
        async def get_artifact(artifact_kind:str, artifact_id:str|None=None, version:str='current_effective', chapter_id:str|None=None, cursor:str|None=None)->str:
            """读取产物，默认有效版；续页用source_ref.version固定数字。部分确认不代表整份生效；状态范围未展开时先read_record读取effective_selections_ref。"""
            if cursor is not None and not version.isdigit():raise ValueError('续页必须使用上一页source_ref.version，不能重新取current_effective或latest_draft')
            rows=all_records(self.store,self.project_id,'artifact',{'artifact_kind':artifact_kind})
            if artifact_id: rows=[r for r in rows if r['id']==artifact_id]
            if chapter_id: rows=[r for r in rows if chapter_id in r['scope']['chapter_ids']]
            if len(rows)!=1: return json.dumps({'status':'ambiguous_target' if rows else 'not_found','candidates':[r['id'] for r in rows]})
            a=rows[0]; v=a['current_effective_version'] if version=='current_effective' else a['latest_version'] if version=='latest_draft' else int(version)
            if v is None: return json.dumps({'status':'no_effective_version'})
            target={'record_id':a['id'],'version':str(v),'item_id':None,'json_pointer':None}; record,content=self.resolve(target)
            if not record:return json.dumps({'status':'not_found'})
            states=self.store.list(self.project_id,'artifact_state',limit=10000,filters={'artifact_version_id':record['id']})
            state=states[0] if states else None
            extra={'state':None,'state_ref':self._record_ref(state) if state else None}
            if state:
                extra['state']={key:state[key] for key in ('confirmation_status','dependency_status','quality_status','row_version','effective_selections')}
                extra['state'].update(effective_selections_complete=True,effective_selection_count=len(state['effective_selections']))
            try:page=self.page(content,target,int(cursor or 0),extra)
            except BudgetExceeded as exc:
                if exc.code!='tool_output_budget_exceeded' or not state:raise
                # Unknown scope is explicit, never an empty list or a whole-artifact confirmation.
                extra['state'].update(effective_selections=None,effective_selections_complete=False)
                extra['effective_selections_ref']={**extra['state_ref'],'json_pointer':'/effective_selections'}
                page=self.page(content,target,int(cursor or 0),extra)
            return json.dumps(page,ensure_ascii=False)
        @function_tool
        async def search_records(query:str, record_type:str|None=None, cursor:str|None=None)->str:
            """检索项目归档；match=false为同会话邻接片段。续页保持query/record_type不变，详情用read_record读取固定引用。"""
            return json.dumps(self.search(query,record_type,cursor),ensure_ascii=False)
        available=[get_artifact,search_records,read_record]
        for tool in available:
            if tool.name in self.config['tools'].get('descriptions',{}): tool.description=self.config['tools']['descriptions'][tool.name]
        return [t for t in available if t.name in self.config['tools']['enabled']]


class AuditHooks(RunHooksBase):
    def __init__(self,service,stage,task,run,session,config,control,tools,output_type,materials,source):
        self.service,self.store=service,service.store
        self.stage,self.task,self.run,self.session,self.config=stage,task,run,session,config
        self.control,self.tools,self.output_type,self.materials,self.source=control,tools,output_type,materials,source
        self.current=None; self.started=0; self.tool_calls={}; self.steer_messages={}; self.seen_steer=set(); self.response_details={}
    async def boundary(self):
        if self.control:
            command=await self.control()
            if command:
                if command.get('stop'): raise asyncio.CancelledError(command.get('reason','stopped'))
                for message in command.get('steer_messages',[]):
                    if message['id'] not in self.seen_steer:
                        self.steer_messages[message['id']]=message;self.seen_steer.add(message['id'])
    async def input_filter(self,data):
        await self.boundary()
        items=list(data.model_data.input)
        for msg in self.steer_messages.values():
            content=json.dumps({'harness_steer_message_id':msg['id'],
                'user_update':unwrap(msg['content'],self.store,self.task['project_id'])},ensure_ascii=False)
            if not any(item.get('role')=='user' and item.get('content')==content for item in items):
                items.append({'role':'user','content':content})
        schema=self.output_type.json_schema() if self.output_type else None
        fit_input(items,data.model_data.instructions or '',self.tools,schema,self.config,self.source)
        return ModelInputData(input=items,instructions=data.model_data.instructions)
    async def on_llm_start(self,context,agent,system_prompt,input_items):
        from .records import content_hash
        schema=self.output_type.json_schema() if self.output_type else None
        count,cap=fit_input(input_items,system_prompt or '',self.tools,schema,self.config,self.source)
        project=self.task['project_id']; live=self.store.get(self.run['id'],project)
        if live['model_turns_used']>=live['max_turns']: raise BudgetExceeded('max_turns','达到本次运行模型轮次上限')
        with self.store.transaction():
            inst={'storage':'inline_text','text':system_prompt or ''}; inp={'storage':'inline_json','value':plain(input_items)}; tools={'storage':'inline_json','value':self.tools}
            snapshot=self.store.put(new_record('context_snapshot',project,task_id=self.task['id'],run_id=self.run['id'],session_id=self.session.session_id,
                config_version_id=live['config_version_id'],model=self.config['model']['name'],reasoning_effort=self.config['model']['reasoning_effort'],
                output_schema=self.service.catalog.binding(self.output_type.schema_id,self.config.get('schemas')) if self.output_type else None,
                summary_ref=self.session.row.get('latest_summary_ref'),
                instructions=inst,input_items=inp,tool_definitions=tools,materials=self.materials,history_ids=list(dict.fromkeys(self.session.history_ids+list(self.seen_steer))),
                input_token_estimate=count,input_token_budget=cap,content_sha256=digest({'instructions':inst,'input_items':inp,'tool_definitions':tools})))
            operation=str(uuid.uuid4())
            key=f'{project}:task_runtime:{self.task["id"]}'
            runtime=self.store.projection_get(key)
            if runtime and runtime.get('allowance'):
                allowance=runtime['allowance']
                if allowance['used']>=allowance['limit']:raise BudgetExceeded('max_turns','达到任务模型轮次上限')
                allowance['used']+=1;allowance['reserved_operation_ids'].append(operation)
                self.store.projection_put(key,runtime)
                from .compaction import event
                event(self.store,project,'turn.reserved',{'operation_id':operation,'allowance_id':allowance['allowance_id'],
                    'used':allowance['used'],'limit':allowance['limit']},task=self.task['id'],run=self.run['id'])
            live['model_turns_used']+=1; self.store.update(live,live['row_version'])
            self.current=self.store.put(new_record('model_call',project,task_id=self.task['id'],run_id=self.run['id'],operation_id=operation,
                attempt=1,turn_index=live['model_turns_used'],context_snapshot_id=snapshot['id'],state='running',started_at=now_utc()))
        self.started=time.monotonic(); self.response_details={}

    def rejected_attempt(self, exc):
        row=self.store.get(self.current['id'],self.task['project_id'])
        row.update(state='failed',finished_at=now_utc(),error=http_failure(exc))
        self.store.update(row,row['row_version'])

    def retry_attempt(self):
        previous=self.current
        self.current=self.store.put(new_record('model_call',self.task['project_id'],task_id=self.task['id'],run_id=self.run['id'],
            operation_id=previous['operation_id'],attempt=previous['attempt']+1,turn_index=previous['turn_index'],
            context_snapshot_id=previous['context_snapshot_id'],state='running',started_at=now_utc()))
        self.started=time.monotonic(); self.response_details={}
    def record_provider_response(self,response,event_type=None):
        raw=response.model_dump(mode='json')
        status=raw.get('status')
        failure=terminal_failure(raw,event_type)
        details={'known_outcome':status in ('completed','incomplete','failed'),
                 'terminal_status':status if status in ('completed','incomplete','failed') else None,
                 'terminal_event':event_type or ('response.'+status if status in ('completed','incomplete','failed') else None),
                 'model_call_id':self.current['id']}
        # Provider IDs belong in the internal archive; only standard response IDs
        # are copied into the user-visible classification.
        identity=raw.get('id')
        if isinstance(identity,str) and identity.startswith('resp_') and len(identity)<=200 and all(c.isalnum() or c in '_-' for c in identity):
            details['provider_response_id']=identity
        if failure:
            failure['details'].update(details)
            limit=self.config['model']['max_output_tokens']
            failure['details']['max_output_tokens']=limit
            if failure['code']=='output_limit_exceeded':
                failure['message']=f'模型达到本次冻结配置的输出 token 上限 {limit}（包含推理 token）而截断；该响应未作为有效产物。'
        self.response_details=deepcopy(failure['details'] if failure else details)
        self.record_response(SimpleNamespace(output=getattr(response,'output',None) or [],
            response_id=identity,raw_usage=plain(getattr(response,'usage',None)) or {}),
            provider_response=raw,terminal_event=details['terminal_event'],failure=failure)
        return failure

    def record_response(self,response,*,provider_response=None,terminal_event=None,failure=None):
        # The SDK may reject the received tool protocol before on_llm_end. Save
        # the physical response first so a known response never becomes unknown.
        existing=self.store.get(self.current['id'],self.task['project_id'])
        if existing['response_history_ids']:
            self.current=existing
            return
        output=plain(response.output); raw=response.raw_usage or {}
        # Normalized SDK counters are used only when provider actually returned usage.
        u=usage(); u['active_ms']=round((time.monotonic()-self.started)*1000)
        if raw:
            u.update(input_tokens=raw.get('input_tokens'),output_tokens=raw.get('output_tokens'),
                cached_input_tokens=raw.get('input_tokens_details',{}).get('cached_tokens'),
                reasoning_tokens=raw.get('output_tokens_details',{}).get('reasoning_tokens'))
        price=self.config.get('pricing',{}).get('models',{}).get(self.config['model']['name'])
        if price and u['input_tokens'] is not None and u['output_tokens'] is not None:
            cached=u['cached_input_tokens'] or 0; inp=u['input_tokens']; out=u['output_tokens']
            writes=raw.get('input_tokens_details',{}).get('cache_write_tokens',0)
            im=Decimal(price['long_input_multiplier']) if inp>price['long_input_threshold'] else Decimal(1)
            om=Decimal(price['long_output_multiplier']) if inp>price['long_input_threshold'] else Decimal(1)
            if type(writes) is int and 0<=writes<=inp-cached:
                cost=((inp-cached-writes)*Decimal(price['input_per_million'])*im+
                    writes*Decimal(price['input_per_million'])*im*Decimal(price['cache_write_multiplier'])+
                    cached*Decimal(price['cached_input_per_million'])*im+out*Decimal(price['output_per_million'])*om)/Decimal(1000000)
                u['estimated_cost']={'amount':str(cost),'currency':'USD'};u['pricing_version']=self.config['pricing']['version']
        archived={'provider_response_id':response.response_id,'output':output,'raw_usage':plain(raw)}
        if provider_response is not None:
            archived.update(provider_response=provider_response,terminal_event=terminal_event,terminal_status=provider_response.get('status'))
        archive=append_history(self.store,self.task['project_id'],self.task['conversation_id'],
            archived,task_id=self.task['id'],run_id=self.run['id'],
            session_id=self.session.session_id,operation_id=self.current['operation_id'])
        state=('failed' if failure['details']['known_outcome'] else 'unknown') if failure else 'succeeded'
        row=self.store.get(self.current['id'],self.task['project_id']); row.update(state=state,finished_at=now_utc(),usage=u,provider_response_id=response.response_id,response_history_ids=[archive['id']])
        if failure:row['error']=deepcopy(failure)
        self.current=self.store.update(row,row['row_version'])
        if not failure and not any(item.get('type')=='function_call' for item in output):
            texts=[part.get('text','') for item in output if item.get('type')=='message'
                   for part in item.get('content',[]) if part.get('type')=='output_text']
            candidate=''.join(texts)
            if self.output_type:
                try:
                    candidate=self.service.catalog.validate(self.output_type.schema_id,json.loads(candidate),self.config.get('schemas'))
                except (ValueError,TypeError):return
            pid=self.task['project_id']
            self.store.projection_put(f'{pid}:run_result:{self.run["id"]}',{'project_id':pid,'output':candidate,
                'run_id':self.run['id'],'context_snapshot_id':row['context_snapshot_id'],'model_call_id':row['id']})
    async def on_llm_end(self,context,agent,response):
        self.record_response(response)
        await self.boundary()
    async def on_tool_start(self,context,agent,tool):
        await self.boundary()
        call_id=getattr(context,'tool_call_id',None) or str(uuid.uuid4())
        args=getattr(context,'tool_arguments','{}')
        try: args=json.loads(args) if isinstance(args,str) else args
        except ValueError: args={'unparsed':str(args)}
        row=self.store.put(new_record('tool_call',self.task['project_id'],task_id=self.task['id'],run_id=self.run['id'],model_call_id=self.current['id'],
            operation_id=str(uuid.uuid4()),provider_tool_call_id=call_id,tool_name=tool.name,arguments={'storage':'inline_json','value':args},state='running',attempt=1,started_at=now_utc()))
        self.tool_calls[call_id]=row
    async def on_tool_end(self,context,agent,tool,result):
        call_id=getattr(context,'tool_call_id',None); row=self.tool_calls.get(call_id)
        if not row:return
        archive=append_history(self.store,self.task['project_id'],self.task['conversation_id'],plain(result),role='tool',kind='tool_result',
            task_id=self.task['id'],run_id=self.run['id'],session_id=self.session.session_id,operation_id=row['operation_id'],provider_item_id=call_id)
        row.update(state='succeeded',finished_at=now_utc(),result={'storage':'inline_json','value':plain(result)},history_ids=[archive['id']])
        self.store.update(row,row['row_version'])


class AuditedResponsesModel(OpenAIResponsesModel):
    """Retries preserve one logical turn and expose every physical request attempt."""
    def __init__(self,model,openai_client,audit,max_retries):
        super().__init__(model=model,openai_client=openai_client)
        self.audit,self.max_retries=audit,max_retries
    async def _fetch_response(self,system_instructions,input,model_settings,tools,output_schema,handoffs,
                              previous_response_id=None,conversation_id=None,stream=False,prompt=None):
        kwargs=dict(system_instructions=system_instructions,input=input,model_settings=model_settings,tools=tools,
            output_schema=output_schema,handoffs=handoffs,previous_response_id=previous_response_id,
            conversation_id=conversation_id,stream=stream,prompt=prompt)
        if not stream:
            # agents==0.22.2 raises on failed/incomplete inside this method without
            # retaining Response on the exception. Capture before that boundary.
            response=await self._get_client().responses.create(**self._build_response_create_kwargs(**kwargs))
            with self.audit.store.transaction():failure=self.audit.record_provider_response(response)
            if failure:raise ModelRunError(**failure)
            _mark_transport_request_without_usage(response)
            return response
        source=await super()._fetch_response(**kwargs)
        async def observed():
            terminal=False
            try:
                async for event in source:
                    if getattr(event,'type',None) in ('response.completed','response.failed','response.incomplete'):
                        terminal=True
                        with self.audit.store.transaction():failure=self.audit.record_provider_response(event.response,event.type)
                        if failure:raise ModelRunError(**failure)
                    yield event
            finally:
                try:await self._maybe_aclose_async_iterator(source)
                except Exception:
                    if not terminal:raise
        return observed()

    async def get_response(self,*args,**kwargs):
        for attempt in range(self.max_retries+1):
            try:
                response=await super().get_response(*args,**kwargs)
                with self.audit.store.transaction():self.audit.record_response(response)
                return response
            except Exception as exc:
                failure=http_failure(exc)
                if not failure or not failure['retryable'] or attempt>=self.max_retries:raise
                self.audit.rejected_attempt(exc)
                await asyncio.sleep(min(2**attempt,8))
                await self.audit.boundary()
                self.audit.retry_attempt()


class ModelService:
    def __init__(self,store,client=None):
        self.store=store;self.catalog=SchemaCatalog();self.client=client
    async def run(self,stage,task,run,session,config,materials,message,control=None):
        load_dotenv(ROOT/'.env',override=False)
        if self.client is None:
            api_key=dotenv_values(ROOT/'.env').get('OPENAI_API_KEY') or os.environ.get('OPENAI_API_KEY')
            if not api_key: raise ModelRunError('missing_api_key','请在服务端 .env 配置 OPENAI_API_KEY')
            self.client=AsyncOpenAI(api_key=api_key,max_retries=0,timeout=600)
        original_materials=materials
        materials=prepare_runtime_materials(stage,task,run,session,config,materials,self.store)
        packed,source,selections=build_materials(stage,materials,config,self.store,task['project_id'])
        structured=config.get('output',{}).get('structured',{}).get(
            stage, stage not in ('aux.summary','step2'))
        plain_text=not structured
        schema_id=None if plain_text else self.catalog.schema_for(stage,config)
        output=None if plain_text else self.catalog.output_type(schema_id,config.get('schemas'))
        # Tool availability follows the selected Agent's resolved profile.
        # Internal compaction remains tool-free because it is not a user
        # configurable business stage.
        tools=[] if stage=='aux.summary' else ReadTools(self.store,task['project_id'],config).functions()
        tool_defs=[{'type':'function','name':t.name,'description':t.description,'parameters':t.params_json_schema} for t in tools]
        persistent=PersistentSession(self.store,session,task,run)
        audit=AuditHooks(self,stage,task,run,persistent,config,control,tool_defs,output,selections,source if stage=='step1' else None)
        settings=ModelSettings(max_tokens=config['model']['max_output_tokens'],reasoning=Reasoning(effort=config['model']['reasoning_effort']),
            temperature=config['model'].get('temperature'),truncation='disabled',store=False,preserve_raw_usage=True,
            retry=ModelRetrySettings(max_retries=0))
        agent=Agent(name=stage_agent(stage,config),instructions=instructions(stage,config),
            model=AuditedResponsesModel(model=config['model']['name'],openai_client=self.client,audit=audit,max_retries=config['retry']['max_retries']),model_settings=settings,
            output_type=output,tools=tools)
        async def deduplicate_summary(packed,selections):
            summary_ref=persistent.row.get('latest_summary_ref')
            if not summary_ref:return packed,selections
            saved=self.store.resolve_ref(summary_ref,task['project_id'])
            return omit_history_summary_copy(packed,selections,await persistent.get_items(),summary_ref,
                unwrap(saved['content'],self.store,task['project_id']))
        packed,selections=await deduplicate_summary(packed,selections)
        audit.materials=selections
        request=json.dumps({'request':message,'task_id':task['id'],'source_message_id':task['requested_by_message_id'],
           '_harness_materials':packed,'task_scope':task['scope']},ensure_ascii=False)
        try:
            from .compaction import compact_session
            await compact_session(self,persistent,stage,config,agent.instructions,tool_defs,output,request,control)
            if persistent.row['generation']!=session['generation']:
                materials=prepare_runtime_materials(stage,task,run,persistent.row,config,original_materials,self.store)
                packed,source,selections=build_materials(stage,materials,config,self.store,task['project_id'])
                packed,selections=await deduplicate_summary(packed,selections)
                audit.materials=selections
                request=json.dumps({'request':message,'task_id':task['id'],'source_message_id':task['requested_by_message_id'],
                    '_harness_materials':packed,'task_scope':task['scope']},ensure_ascii=False)
            # Reserve the complete request envelope and working history before
            # removing optional automatic fields. Required fields stay intact.
            history=await persistent.get_items()
            envelope={'request':message,'task_id':task['id'],'source_message_id':task['requested_by_message_id'],
                '_harness_materials':[],'task_scope':task['scope']}
            output_schema=output.json_schema() if output else None
            fixed_tokens=tokens({'instructions':agent.instructions,
                'input':history+[{'role':'user','content':json.dumps(envelope,ensure_ascii=False)}],
                'tools':tool_defs,'output_schema':output_schema},config['model']['name'])
            remaining=max(0,input_budget(config)-fixed_tokens)
            packed,selections=prune_optional_materials(packed,selections,remaining,config)
            audit.materials=selections
            request=json.dumps({**envelope,'_harness_materials':packed},ensure_ascii=False)
            while True:
                try:
                    fit_input(history+[{'role':'user','content':request}],agent.instructions,tool_defs,
                              output_schema,config,source if stage=='step1' else None)
                    break
                except BudgetExceeded as exc:
                    if exc.code!='input_budget_exceeded':raise
                    extra=exc.details['input_tokens']-exc.details['input_budget']
                    reduced,trimmed=prune_optional_materials(packed,selections,
                        max(0,tokens(packed,config['model']['name'])-extra-32),config)
                    if reduced==packed:raise
                    packed,selections=reduced,trimmed;audit.materials=selections
                    request=json.dumps({**envelope,'_harness_materials':packed},ensure_ascii=False)
            result=await Runner.run(agent,request,session=persistent,max_turns=run['max_turns']-run['model_turns_used'],hooks=audit,
                run_config=RunConfig(tracing_disabled=True,call_model_input_filter=audit.input_filter))
            return result.final_output if plain_text else output.catalog.validate(schema_id,result.final_output,config.get('schemas'))
        except BaseException as exc:
            http_error=http_failure(exc)
            if audit.current:
                row=self.store.get(audit.current['id'],task['project_id'])
                if row['state']=='running':
                    # A transport failure does not prove the provider did no work.
                    known=getattr(exc,'status_code',None)
                    row.update(state='failed' if known else 'unknown',finished_at=now_utc(),
                        error=http_error or {'code':type(exc).__name__,'message':'模型调用失败，详见受控错误分类','retryable':False,'details':{'http_status':known}})
                    self.store.update(row,row['row_version'])
            if isinstance(exc,(BudgetExceeded,asyncio.CancelledError,ModelRunError)) or getattr(exc,'reason',None): raise
            if http_error:raise ModelRunError(**http_error) from None
            code=type(exc).__name__; status=getattr(exc,'status_code',None)
            details=deepcopy(audit.response_details)
            if status:details['http_status']=status
            safe='模型调用失败，请检查服务端凭据、模型权限和运行记录'
            if code=='ModelBehaviorError':safe='模型输出不符合结构或工具调用协议；请按当前输出 Schema 和工具定义修正。'
            elif code=='ModelRefusalError':code='model_refusal';safe='模型已明确拒绝本次请求；拒答未作为有效产物。'
            elif code in ('APIConnectionError','APITimeoutError'):safe='模型请求连接中断，尚不能确认供应商是否完成；请查看运行记录后处理。'
            if status:safe+=f'（HTTP {status}）'
            raise ModelRunError(code,safe,retryable=status in (429,500,502,503,504),details=details) from None
