"""Same-origin local web application and persistent harness API."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
import json
import os
import logging
import hashlib
import re
from datetime import datetime, timezone
from zipfile import ZipFile
from urllib.parse import quote
from pathlib import Path
import uuid
from fastapi import FastAPI, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from .schemas import ROOT, digest
from .storage import Store, StorageError, VersionConflict, TABLES
from .records import new_record, canonical_bytes
from .auth import LocalAuth
from .configuration import ConfigService
from .context import unwrap, tokens, input_budget, BudgetExceeded
from .prompts import step1_agent
from .model_service import ModelService, ReadTools
from .workflow import WorkflowBlocked
from .artifact_workspace import ArtifactWorkspace
from .presentation import stage_result_text

BASE='/api/branch-agent/v1'
MAX_SOURCE_BYTES=10*1024*1024
LOG=logging.getLogger(__name__)
LEGACY_STAGE_NOTICE=re.compile(r'^Step(\d+) 已生成草稿 v(\d+)')


def public(value):
    """Return a labelled display projection; stored evidence remains unchanged."""
    hidden={'lease_owner','fencing_token','signing_key','password_hash','encrypted_content',
        'authorization','api_key','openai_api_key','access_token','refresh_token','password',
        'cookie','set_cookie','csrf_token','hidden_reasoning','chain_of_thought','reasoning_content'}
    if isinstance(value,list):return [public(v) for v in value]
    if isinstance(value,dict):
        if value.get('type')=='reasoning':
            return {'type':'reasoning','summary':public(value.get('summary',[])),
                'opaque_content':'[已遮蔽：不可公开的推理载荷]'}
        return {k:('[已遮蔽：内部凭据或不可公开内容]' if k.lower().replace('-','_') in hidden else public(v))
            for k,v in value.items()}
    return value


def create_app(store=None,engine=None,model_service=None,data_dir=None):
    load_dotenv(ROOT/'.env',override=False)
    data=Path(data_dir or os.environ.get('BRANCH_DATA_DIR',ROOT/'.data'))
    db=store or Store(os.environ.get('DATABASE_URL','postgresql:///branch_agent_local'),data/'blobs')
    db.migrate();auth=LocalAuth(data);config=ConfigService(db);artifact_workspace=ArtifactWorkspace(db,config)
    if engine is None:
        from .engine import Engine
        engine=Engine(db,model_service or ModelService(db),config)
    @asynccontextmanager
    async def lifespan(app):
        if os.environ.get('BRANCH_EMBEDDED_WORKER','1')=='1': await engine.start()
        yield
        await engine.stop()
        if store is None: db.close()
    app=FastAPI(title='Nexo 分支 Agent',version='0.1.0',lifespan=lifespan)
    app.state.store=db;app.state.engine=engine;app.state.config=config;app.state.auth=auth
    app.state.artifact_workspace=artifact_workspace
    release_hash=hashlib.sha256()
    for path in sorted((ROOT/'branch_agent').rglob('*')):
        if path.is_file() and (path.suffix=='.py' or path.parent.name=='static'):
            release_hash.update(str(path.relative_to(ROOT)).encode('utf-8'))
            release_hash.update(path.read_bytes())
    release_notes=json.loads((ROOT/'branch_agent'/'static'/'release-notes.json').read_text(encoding='utf-8'))
    release_info={'id':release_hash.hexdigest()[:16],**release_notes}
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request,exc):
        codes={400:'invalid_request',401:'unauthenticated',403:'forbidden',404:'not_found',409:'invalid_state',413:'payload_too_large',422:'validation_failed'}
        return JSONResponse({'error':{'code':codes.get(exc.status_code,str(exc.status_code)),'message':str(exc.detail)}},status_code=exc.status_code)
    @app.exception_handler(RequestValidationError)
    async def validation_error(request,exc):
        fields=[{'field':'.'.join(map(str,e['loc'])),'message':e['msg']} for e in exc.errors()]
        return JSONResponse({'error':{'code':'invalid_request','message':'请求参数不符合接口要求','details':fields}},status_code=422)
    @app.exception_handler(WorkflowBlocked)
    async def blocked(request,exc):
        labels={'invalid_state':'当前状态不支持此操作','target_run_required':'调整当前任务需要指定正在运行的 Run',
            'task_not_found':'任务不存在或不属于当前项目','conversation_not_found':'会话不存在',
            'confirmation_required':'当前阶段需要先确认指定版本与范围','confirmation_not_presented':'请先查看待确认的固定版本',
            'dependency_changed':'上游依赖已经改变，需要先完成复核','idempotency_conflict':'同一幂等键用于不同请求',
            'source_index_migration_required':'旧版阶段产物缺少独立原文索引，需重新生成并确认对应阶段',
            'operation_uncertain':'上一次操作结果尚未确认，请先核对保存结果','queue_hold':'队列已暂停，需要明确继续',
            'cost_limit':'任务费用已达到限额，请明确追加额度后继续','active_time_limit':'任务活动耗时已达到限额',
            'turn_limit':'任务模型轮次已达到限额','usage_uncertain':'用量尚未核验，暂不能开始新的调用',
            'action_stale':'待处理事项已发生变化，请查看更新后的选项再提交',
            'action_not_found':'待处理事项或操作不存在，请刷新后查看当前事项',
            'action_value_required':'请填写必需的信息或明确勾选后再提交',
            'invalid_action_values':'所填内容无效，请检查选项、数值和必填内容',
            'action_unavailable':'当前条件尚不支持此操作，请查看待处理卡片中的原因'}
        return JSONResponse({'error':{'code':exc.reason,'message':labels.get(exc.reason,'工作流暂不能继续：'+exc.reason),'details':public(exc.details)}},status_code=409)
    @app.exception_handler(ValueError)
    async def invalid(request,exc):
        return JSONResponse({'error':{'code':type(exc).__name__,'message':str(exc)}},status_code=409 if isinstance(exc,VersionConflict) else 400)
    @app.exception_handler(BudgetExceeded)
    async def exceeded(request,exc):
        return JSONResponse({'error':{'code':exc.code,'message':str(exc),'details':public(exc.details)}},status_code=422)
    @app.exception_handler(Exception)
    async def unexpected(request,exc):
        LOG.exception('Unhandled API failure on %s',request.url.path)
        return JSONResponse({'error':{'code':'internal_error','message':'服务暂时无法完成请求，请稍后重试。'}},status_code=500)
    async def json_body(request):
        try: body=await request.json()
        except (ValueError,UnicodeDecodeError): raise HTTPException(400,'请求正文必须是有效 JSON') from None
        if not isinstance(body,dict): raise HTTPException(400,'请求正文必须是 JSON 对象')
        return body
    def text_field(body,key,default='',required=False):
        value=body.get(key,default)
        if not isinstance(value,str): raise HTTPException(400,f'{key} 必须是文本')
        if required and not value.strip(): raise HTTPException(400,f'{key} 不能为空')
        return value
    def idempotency_key(request):
        key=request.headers.get('Idempotency-Key','')
        if not 1<=len(key)<=128: raise HTTPException(400,'Idempotency-Key 长度须为1～128字符')
        return key
    def records_iter(project_id,kind=None,filters=None):
        offset=0
        while True:
            batch=db.list(project_id,kind,limit=1000,offset=offset,filters=filters)
            yield from batch
            if len(batch)<1000:return
            offset+=len(batch)
    def page_window(limit,cursor):
        if not 0<=cursor<=9223372036854775807:raise HTTPException(400,'分页游标超出有效范围')
        return min(max(limit,1),100),cursor
    def event_rows(project_id,after,limit=1000):
        return [r['data'] for r in db._connection().execute(
            'SELECT data FROM runtime_events WHERE project_id=%s AND sequence>%s ORDER BY sequence LIMIT %s',
            (project_id,after,limit)).fetchall()]
    app.state.event_rows=event_rows
    def access(request,p,write=False):
        user=auth.identity(request,write)
        try:uuid.UUID(p)
        except (ValueError,TypeError):raise HTTPException(404,'项目不存在') from None
        project=db.get(p,p)
        if not project or project['record_type']!='project' or project['owner_account_id']!=user['account_id']: raise HTTPException(404,'项目不存在')
        return user
    def conversation(p,c):
        row=db.get(c,p)
        if not row or row['record_type']!='conversation':raise HTTPException(404,'会话不存在')
        return row
    def once(request,p,operation,body,work):
        user=access(request,p,True);key=idempotency_key(request)
        identity=f'{p}:api:{user["account_id"]}:{operation}:{key}'
        with db.transaction():
            db.advisory_lock(identity);old=db.projection_get(identity);sha=digest(body)
            if old:
                if old['sha256']!=sha:raise HTTPException(409,'同一幂等键用于不同请求')
                return old['result']
            result=work();db.projection_put(identity,{'project_id':p,'sha256':sha,'result':result})
            return result
    def account_once(request,operation,body,work):
        user=auth.identity(request,True);key=idempotency_key(request);sha=digest(body)
        identity=f'account:api:{user["account_id"]}:{operation}:{key}'
        with db.transaction():
            db.advisory_lock(identity);old=db.account_idempotency_get(user['account_id'],operation,key)
            if old:
                if old['sha256']!=sha:raise HTTPException(409,'同一幂等键用于不同请求')
                return old['result']
            result=work();db.account_idempotency_put(user['account_id'],operation,key,sha,result)
            return result
    @app.get('/health')
    async def health(): return {'status':'ok','database':db.now(),'worker':os.environ.get('BRANCH_EMBEDDED_WORKER','1')=='1'}
    @app.get(BASE+'/release')
    async def release(): return JSONResponse(release_info,headers={'Cache-Control':'no-store'})
    @app.get(BASE+'/auth/me')
    async def me(request:Request):return auth.identity(request)
    @app.post(BASE+'/auth/login')
    async def login(request:Request):
        origin=request.headers.get('origin')
        if origin and origin.rstrip('/')!=str(request.base_url).rstrip('/'):raise HTTPException(403,'来源不匹配')
        body=await json_body(request);identity,cookie=auth.login(body.get('username',''),body.get('password',''))
        response=JSONResponse(identity);response.set_cookie('branch_session',cookie,httponly=True,samesite='strict',secure=request.url.scheme=='https',max_age=86400*7)
        return response
    @app.post(BASE+'/auth/logout')
    async def logout(request:Request):
        auth.identity(request,True);response=JSONResponse({'ok':True});response.delete_cookie('branch_session');return response
    @app.get(BASE+'/projects')
    async def projects(request:Request,limit:int=50,cursor:int=0):
        user=auth.identity(request);limit,cursor=page_window(limit,cursor)
        items=db.list_projects(user['account_id'],limit=limit+1,offset=cursor);more=len(items)>limit
        return {'items':public(items[:limit]),'next_cursor':str(cursor+limit) if more else None,'has_more':more}
    @app.post(BASE+'/projects')
    async def create_project(request:Request):
        user=auth.identity(request,True);body=await json_body(request);title=text_field(body,'title',required=True).strip()
        if not title:raise HTTPException(400,'项目名称不能为空')
        key=idempotency_key(request)
        project_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'branch-agent:'+user['account_id']+':'+key))
        identity=f'{project_id}:create:{user["account_id"]}:{key}'
        with db.transaction():
            db.advisory_lock(identity);old=db.projection_get(identity)
            if old:
                if old['sha256']!=digest(body):raise HTTPException(409,'幂等键冲突')
                return old['result']
            row=db.put(new_record('project',None,id=project_id,title=title,owner_account_id=user['account_id']))
            artifact_workspace.seed_project(row['id'],user['account_id'])
            db.projection_put(identity,{'project_id':row['id'],'sha256':digest(body),'result':row})
        return row
    @app.get(BASE+'/projects/{p}')
    async def project(request:Request,p:str):access(request,p);return db.get(p,p)
    @app.delete(BASE+'/projects/{p}')
    async def delete_project(request:Request,p:str):
        user=auth.identity(request,True);body=await json_body(request)
        if body.get('confirm') is not True or set(body)-{'confirm'}:raise HTTPException(400,'请通过确认按钮确认永久删除项目')
        key=idempotency_key(request);sha=digest(body);operation='project.delete:'+p
        old=db.account_idempotency_get(user['account_id'],operation,key)
        if old:
            if old['sha256']!=sha:raise HTTPException(409,'同一幂等键用于不同请求')
            return old['result']
        access(request,p,True)
        deleted=await engine.delete_project(p,user['account_id']) if hasattr(engine,'delete_project') else db.delete_project(p,user['account_id'])
        if not deleted:raise HTTPException(404,'项目不存在')
        result={'deleted':True,'project_id':p}
        identity=f'account:api:{user["account_id"]}:{operation}:{key}'
        with db.transaction():
            db.advisory_lock(identity)
            old=db.account_idempotency_get(user['account_id'],operation,key)
            if old:
                if old['sha256']!=sha:raise HTTPException(409,'同一幂等键用于不同请求')
                return old['result']
            db.account_idempotency_put(user['account_id'],operation,key,sha,result)
        return result
    @app.get(BASE+'/projects/{p}/conversations')
    async def conversations(request:Request,p:str):access(request,p);return {'items':list(records_iter(p,'conversation'))}
    @app.post(BASE+'/projects/{p}/conversations')
    async def create_conversation(request:Request,p:str):
        body=await json_body(request);return once(request,p,'conversation.create',body,lambda:db.put(new_record('conversation',p,title=text_field(body,'title','新的改编',required=True))))
    @app.get(BASE+'/projects/{p}/conversations/{c}/history')
    async def history(request:Request,p:str,c:str,limit:int=100,cursor:int=0,tail:bool=False,after_sequence:int|None=None):
        access(request,p);conversation(p,c)
        limit,cursor=page_window(limit,cursor)
        if after_sequence is not None and not 0<=after_sequence<=9223372036854775807:raise HTTPException(400,'消息序号超出有效范围')
        connection=db._connection();lower=after_sequence or 0
        upper=connection.execute("SELECT COALESCE(MAX(sequence),0) AS last FROM history_records WHERE project_id=%s AND conversation_id=%s AND visibility='conversation'",(p,c)).fetchone()['last']
        count=connection.execute("SELECT COUNT(*) AS n FROM history_records WHERE project_id=%s AND conversation_id=%s AND visibility='conversation' AND sequence>%s AND sequence<=%s",(p,c,lower,upper)).fetchone()['n']
        if tail:cursor=max(0,count-limit)
        page=[r['data'] for r in connection.execute("SELECT data FROM history_records WHERE project_id=%s AND conversation_id=%s AND visibility='conversation' AND sequence>%s AND sequence<=%s ORDER BY sequence LIMIT %s OFFSET %s",(p,c,lower,upper,limit,cursor)).fetchall()]
        legacy={row['id']:LEGACY_STAGE_NOTICE.match(row.get('content',{}).get('text','')) for row in page
                if row.get('role')=='assistant' and row.get('content',{}).get('storage')=='inline_text'}
        legacy={identity:match for identity,match in legacy.items() if match and 2<=int(match.group(1))<=10}
        if legacy:
            events=connection.execute("SELECT data FROM runtime_events WHERE project_id=%s AND event_name='artifact.presented' AND data->'payload'->>'message_id'=ANY(%s)",
                (p,list(legacy))).fetchall()
            by_message={row['data']['payload']['message_id']:row['data']['payload'].get('artifact_ref') for row in events}
            reader=ReadTools(db,p,config.values(p)[0])
            for row in page:
                match=legacy.get(row['id']);target=by_message.get(row['id'])
                if not match or not target or str(target.get('version'))!=match.group(2):continue
                try:
                    _,content=reader.resolve(target)
                    row['display_text']=stage_result_text(int(match.group(1)),content,
                        version=int(match.group(2)),confirmation=True)
                except (KeyError,TypeError,ValueError):
                    LOG.warning('Could not project legacy stage presentation for message %s',row['id'],exc_info=True)
        more=count>cursor+limit
        return {'items':public(page),'next_cursor':str(cursor+limit) if more else None,'has_more':more,
            'start_cursor':cursor,'previous_cursor':str(max(0,cursor-limit)) if cursor>0 else None}
    @app.post(BASE+'/projects/{p}/conversations/{c}/messages')
    async def message(request:Request,p:str,c:str):
        access(request,p,True);conversation(p,c);body=await json_body(request)
        text=text_field(body,'text',required=True).strip()
        if body.get('mode','queue') not in ('queue','steer','start'):raise HTTPException(400,'消息 mode 必须为 queue、steer 或 start')
        return once(request,p,'message:'+c,body,lambda:engine.submit_message(p,c,text,mode=body.get('mode','queue'),target_run_id=body.get('target_run_id'),idempotency_key=request.headers['Idempotency-Key']))
    @app.get(BASE+'/projects/{p}/conversations/{c}/actions')
    async def user_actions(request:Request,p:str,c:str):
        from .actions import ActionService
        access(request,p);conversation(p,c)
        return public(ActionService(engine).list_cards(p,c))
    @app.post(BASE+'/projects/{p}/conversations/{c}/actions')
    async def submit_user_action(request:Request,p:str,c:str):
        from .actions import ActionService
        access(request,p,True);conversation(p,c);body=await json_body(request)
        card_id=text_field(body,'card_id',required=True)
        action_id=text_field(body,'action_id',required=True)
        revision=text_field(body,'expected_revision',required=True)
        values=body.get('values',{})
        if not isinstance(values,dict):raise HTTPException(400,'values 必须是 JSON 对象')
        if set(body)-{'card_id','action_id','expected_revision','values'}:
            raise HTTPException(400,'动作请求包含未支持的字段')
        return once(request,p,'user-action:'+c,body,
            lambda:public(ActionService(engine).submit(p,c,card_id,action_id,revision,values)))
    @app.post(BASE+'/projects/{p}/source')
    async def source(request:Request,p:str):
        access(request,p,True);idempotency_key(request)
        if request.headers.get('content-type','').startswith('multipart/'):
            form=await request.form();upload=form.get('file')
            if not upload or not callable(getattr(upload,'read',None)):
                raise HTTPException(400,'请选择一个原作文件')
            c=str(form.get('conversation_id',''));name=getattr(upload,'filename',None) or '原作.txt'
            raw=await upload.read(MAX_SOURCE_BYTES+1)
            if len(raw)>MAX_SOURCE_BYTES:raise HTTPException(413,'文件超过10MB')
            if name.lower().endswith('.docx'):
                from docx import Document
                from docx.oxml.ns import qn
                from docx.table import Table
                from docx.text.paragraph import Paragraph
                try:
                    with ZipFile(BytesIO(raw)) as archive:
                        entries=archive.infolist()
                        if len(entries)>10000 or sum(entry.file_size for entry in entries)>100*1024*1024:
                            raise HTTPException(413,'DOCX 展开内容超过限制')
                    doc=Document(BytesIO(raw))
                    if doc.element.body.xpath('.//w:drawing | .//w:object | .//w:pict | .//w:footnoteReference | .//w:endnoteReference'):
                        raise HTTPException(400,'DOCX 含图片、嵌入对象或注释内容，当前无法完整读取；请补充完整纯文本原作')
                    def blocks(container,parent):
                        parts=[]
                        for child in container.iterchildren():
                            if child.tag==qn('w:p'):parts.append(Paragraph(child,parent).text)
                            elif child.tag==qn('w:tbl'):
                                table=Table(child,parent)
                                for row in table.rows:
                                    parts.append('\t'.join('\n'.join(blocks(cell._tc,cell)) for cell in row.cells))
                        return parts
                    text='\n'.join(blocks(doc.element.body,doc))
                except HTTPException:raise
                except Exception as exc:
                    raise HTTPException(400,'DOCX 无法解析，请检查文件或改用 UTF-8 文本') from exc
                if len(text.encode('utf-8'))>MAX_SOURCE_BYTES:
                    raise HTTPException(413,'DOCX 展开正文超过10MB')
                media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
            elif name.lower().endswith(('.txt','.md','.markdown')):
                try:
                    text=raw.decode('utf-16') if raw.startswith(b'\xff\xfe') else raw.decode('utf-8-sig')
                except UnicodeDecodeError:
                    raise HTTPException(400,'文本文件请使用 UTF-8 或带 BOM 的 UTF-16 LE 编码') from None
                media_type='text/plain'
            else:raise HTTPException(400,'首版支持TXT、Markdown和DOCX')
        else:
            body=await json_body(request)
            text=text_field(body,'text',required=True);name=text_field(body,'name','原作.txt')
            c=text_field(body,'conversation_id',required=True);raw=text.encode('utf-8');media_type='text/plain'
            if len(raw)>MAX_SOURCE_BYTES:raise HTTPException(413,'原作正文超过10MB')
        conversation(p,c)
        if not text.strip():raise HTTPException(400,'原作没有可读取正文')
        # Hash original bytes as well as parsed text: different attachments are distinct submissions.
        body={'conversation_id':c,'name':name,'source_hash':digest(text),'file_hash':hashlib.sha256(raw).hexdigest()}
        def work():
            blob=db.blob_put(p,raw,media_type,name)
            result=engine.import_source(p,c,text,name)
            base,_=config.values(p,'step1')
            branch_preflight={}
            for view in ('global','character'):
                agent_key=step1_agent(view,base)
                values,_=config.values(p,'step1',agent_key=agent_key)
                model=values['model']['name']
                source_tokens=tokens(text,model)
                settings=values['context']['step1_source']
                budget=input_budget(values)
                safe_full_budget=max(0,budget-min(8192,budget//10))
                window_mode=(source_tokens>settings['trigger_tokens'] or
                             source_tokens>safe_full_budget)
                branch_preflight[view]={
                    'agent_key':agent_key,
                    'agent_name':values['prompts'].get('agent_names',{}).get(agent_key,agent_key),
                    'model':model,
                    'source_tokens':source_tokens,
                    'source_mode':'sliding_window' if window_mode else 'full_text',
                    'source_window_threshold':settings['trigger_tokens'],
                    'source_window_tokens':settings['window_tokens'],
                    'input_budget':budget,
                    'safe_full_budget':safe_full_budget,
                    'admitted':settings['window_tokens']<budget if window_mode else source_tokens<=safe_full_budget,
                }
            global_branch=branch_preflight['global']
            modes={item['source_mode'] for item in branch_preflight.values()}
            return public({'result':result,'attachment':blob,'source_tokens':global_branch['source_tokens'],
                'source_mode':modes.pop() if len(modes)==1 else 'mixed',
                'source_window_threshold':global_branch['source_window_threshold'],
                'source_window_tokens':global_branch['source_window_tokens'],
                'input_budget':global_branch['input_budget'],
                'step1_branches':branch_preflight,
                'admitted':all(item['admitted'] for item in branch_preflight.values())})
        return once(request,p,'source.import',body,work)
    @app.get(BASE+'/projects/{p}/status')
    async def status(request:Request,p:str):access(request,p);return public(engine.status(p))
    @app.post(BASE+'/projects/{p}/tasks/{task_id}/{action}')
    async def control(request:Request,p:str,task_id:str,action:str):
        if action not in ('stop','resume'):raise HTTPException(404,'未知操作')
        body=await json_body(request);return once(request,p,'task:'+task_id+':'+action,body,lambda:engine.control_task(p,task_id,action,body.get('additional_seconds'),body.get('additional_cost')))
    @app.post(BASE+'/projects/{p}/queue/resume')
    async def resume(request:Request,p:str):
        body=await json_body(request);return once(request,p,'queue.resume',body,lambda:engine.resume_queue(p,body.get('conversation_id')))
    @app.get(BASE+'/projects/{p}/run-debug')
    async def run_debug_list(request:Request,p:str,limit:int=30,cursor:int=0):
        access(request,p);limit,cursor=page_window(limit,cursor)
        agent_names={}
        def run_agent_name(run):
            version_id=run.get('config_version_id')
            if version_id not in agent_names:
                snapshot=db.get(version_id,p) if version_id else None
                agent_names[version_id]=(snapshot or {}).get('values',{}).get('prompts',{}).get('agent_names',{})
            return agent_names[version_id].get(run['agent_key']) or run['agent_key']
        rows=db._connection().execute(
            'SELECT data FROM runs WHERE project_id=%s ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s',
            (p,limit+1,cursor)).fetchall()
        result=[]
        for entry in rows[:limit]:
            run=entry['data'];task=db.get(run['task_id'],p)
            calls=db.list(p,'model_call',limit=1000,filters={'run_id':run['id']})
            snapshots=db.list(p,'context_snapshot',limit=1,filters={'run_id':run['id']})
            result.append({'run':run,'task':task,'agent_name':run_agent_name(run),
                           'model':snapshots[0]['model'] if snapshots else None,
                           'calls':len(calls),'input_tokens':sum(c.get('usage',{}).get('input_tokens') or 0 for c in calls),
                           'output_tokens':sum(c.get('usage',{}).get('output_tokens') or 0 for c in calls)})
        return {'items':public(result),'next_cursor':str(cursor+limit) if len(rows)>limit else None}
    @app.get(BASE+'/projects/{p}/run-debug/{run_id}')
    async def run_debug_detail(request:Request,p:str,run_id:str):
        access(request,p);run=db.get(run_id,p)
        if not run or run['record_type']!='run':raise HTTPException(404,'Run 不存在')
        task=db.get(run['task_id'],p)
        snapshot=db.get(run['config_version_id'],p) if run.get('config_version_id') else None
        agent_name=(snapshot or {}).get('values',{}).get('prompts',{}).get('agent_names',{}).get(run['agent_key']) or run['agent_key']
        calls=sorted(db.list(p,'model_call',limit=1000,filters={'run_id':run_id}),
                     key=lambda row:(row['turn_index'],row['attempt'],row['created_at']))
        snapshots={};outputs=[]
        for call in calls:
            snapshot=db.get(call['context_snapshot_id'],p)
            if snapshot and snapshot['record_type']=='context_snapshot':snapshots[snapshot['id']]=snapshot
            for history_id in call.get('response_history_ids',[]):
                history=db.get(history_id,p)
                if history and history['record_type']=='history_record':outputs.append({'model_call_id':call['id'],'history':history})
        tools=sorted(db.list(p,'tool_call',limit=1000,filters={'run_id':run_id}),key=lambda row:row['created_at'])
        versions=db.list(p,'artifact_version',limit=1000,filters={'producer_run_id':run_id})
        trace_spans=db._connection().execute('''
            SELECT trace_id,span_id,parent_id,kind,name,started_at,ended_at,error_code,metadata
            FROM sdk_trace_spans WHERE project_id=%s AND run_id=%s
            ORDER BY started_at NULLS LAST,span_id LIMIT 2000''',(p,run_id)).fetchall()
        visible_events={'run.transitioned','task.transitioned','checkpoint.saved',
                        'artifact.presented','graph.checked','project.delivered',
                        'repair.scheduled','confirmation.requested_by_agent',
                        'session.compacted','source.window_completed'}
        events=sorted((row for row in db.list(p,'runtime_event',limit=1000,filters={'run_id':run_id})
                       if row['event_name'] in visible_events),
                      key=lambda row:(row['created_at'],row['sequence']))
        artifacts=[]
        for version in versions:
            artifact=db.get(version['artifact_id'],p)
            _,content=ReadTools(db,p,config.values(p)[0]).resolve(
                {'record_id':version['artifact_id'],'version':str(version['version'])})
            artifacts.append({'artifact':artifact,'version':version,'content':content})
        return public({'run':run,'task':task,'agent_name':agent_name,
                       'calls':calls,'snapshots':list(snapshots.values()),
                       'outputs':outputs,'tools':tools,'artifacts':artifacts,
                       'trace_spans':trace_spans,'events':events})
    @app.get(BASE+'/projects/{p}/records')
    async def records(request:Request,p:str,record_type:str|None=None,limit:int=50,cursor:int=0,state:str|None=None,
                      stage:int|None=None,chapter_id:str|None=None,session_id:str|None=None,task_id:str|None=None,
                      run_id:str|None=None,agent_key:str|None=None,created_from:str|None=None,created_to:str|None=None):
        access(request,p);limit,cursor=page_window(limit,cursor)
        if record_type is not None and record_type not in TABLES:raise HTTPException(400,'未知记录类型')
        if stage is not None and not 1<=stage<=11:raise HTTPException(400,'阶段应在1～11之间')
        filters={'state':state} if state else None
        dates={}
        for key,value in [('created_from',created_from),('created_to',created_to)]:
            if value:
                try:
                    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
                    if parsed.tzinfo is None:raise ValueError()
                    dates[key]=parsed.astimezone(timezone.utc)
                except ValueError:raise HTTPException(400,'时间筛选必须为带时区的 RFC3339 时间') from None
        def matches(row):
            for field,value in [('session_id',session_id),('task_id',task_id),('run_id',run_id)]:
                if value is not None and row.get(field)!=value:return False
            scope=row.get('scope',{})
            if row.get('task_id'):
                task=db.get(row['task_id'],p)
                if task:scope=task.get('scope',scope)
            if stage is not None and scope.get('stage')!=stage:return False
            if chapter_id is not None and chapter_id not in scope.get('chapter_ids',[]):return False
            if agent_key is not None:
                run=row if row['record_type']=='run' else db.get(row['run_id'],p) if row.get('run_id') else None
                if not run or run.get('agent_key')!=agent_key:return False
            at=datetime.fromisoformat(row['created_at'].replace('Z','+00:00'))
            if 'created_from' in dates and at<dates['created_from']:return False
            if 'created_to' in dates and at>dates['created_to']:return False
            return True
        found=[];skipped=0
        for row in records_iter(p,record_type,filters):
            if not matches(row):continue
            if skipped<cursor:skipped+=1;continue
            found.append(row)
            if len(found)>limit:break
        more=len(found)>limit
        return {'items':public(found[:limit]),'next_cursor':str(cursor+limit) if more else None,'has_more':more}
    @app.get(BASE+'/projects/{p}/blobs/{blob_id}/content')
    async def blob_content(request:Request,p:str,blob_id:str,download:bool=False):
        access(request,p);row=db.get(blob_id,p)
        if not row or row['record_type']!='blob':raise HTTPException(404,'内容不存在')
        raw=db.blob_read(blob_id,p);media=row.get('media_type','application/octet-stream')
        headers={'X-Content-Type-Options':'nosniff'}
        if 'json' in media:
            try:raw=canonical_bytes(public(json.loads(raw)))
            except (ValueError,UnicodeDecodeError):raise HTTPException(400,'保存的 JSON 内容无法读取') from None
            headers['X-Content-Projection']='public-redacted';media='application/json'
        elif media.startswith('text/'):
            media='text/plain'
        else:
            # Raw documents always download; uploaded active content cannot execute under this origin.
            media='application/octet-stream';download=True
        if download:
            name=row.get('name') or row.get('filename') or ('attachment-'+blob_id)
            headers['Content-Disposition']="attachment; filename*=UTF-8''"+quote(str(name),safe='')
        return Response(raw,media_type=media,headers=headers)
    @app.get(BASE+'/projects/{p}/records/{record_id}')
    async def record(request:Request,p:str,record_id:str):
        access(request,p);row=db.get(record_id,p)
        if not row:raise HTTPException(404,'记录不存在')
        return public(row)
    @app.get(BASE+'/projects/{p}/records/{record_id}/relations')
    async def relations(request:Request,p:str,record_id:str,direction:str='both',limit:int=50,cursor:int=0):
        access(request,p);row=db.get(record_id,p)
        if not row:raise HTTPException(404,'记录不存在')
        if direction not in ('in','out','both'):raise HTTPException(400,'关系方向须为 in、out 或 both')
        limit,cursor=page_window(min(limit,50),cursor)
        # Relations come from validated, project-bound storage projections, never UUID text searches.
        raw=db._connection().execute("""
            SELECT from_id,to_id,relation,selector FROM record_links
             WHERE project_id=%s AND ((%s IN ('out','both') AND from_id=%s) OR (%s IN ('in','both') AND to_id=%s))
            UNION ALL
            SELECT consumer_id AS from_id,config_version_id AS to_id,relation,NULL::jsonb AS selector FROM config_links
             WHERE consumer_project_id=%s AND ((%s IN ('out','both') AND consumer_id=%s) OR (%s IN ('in','both') AND config_version_id=%s))
            ORDER BY from_id,to_id,relation LIMIT %s OFFSET %s
        """,(p,direction,record_id,direction,record_id,p,direction,record_id,direction,record_id,limit+1,cursor)).fetchall()
        related=[]
        for link in raw[:limit]:
            incoming=str(link['to_id'])==record_id
            neighbor=str(link['from_id'] if incoming else link['to_id'])
            candidate=db.get(neighbor,p)
            if candidate is None and not incoming:
                global_row=db.get(neighbor,None)
                if global_row and global_row['record_type']=='config_version':candidate=global_row
            if candidate is None:continue
            item={'record':public(candidate),'direction':'in' if incoming else 'out',
                'source':str(link['from_id']),'target':str(link['to_id']),'relation':link['relation']}
            if link['selector'] is not None:item['target_ref']=public(link['selector'])
            related.append(item)
        more=len(raw)>limit
        return {'items':related,'has_more':more,'next_cursor':str(cursor+limit) if more else None}
    @app.post(BASE+'/projects/{p}/references/resolve')
    async def resolve(request:Request,p:str):
        access(request,p);body=await json_body(request)
        target=body.get('ref')
        if not isinstance(target,dict) or not isinstance(target.get('record_id'),str):raise HTTPException(400,'缺少有效固定引用')
        version=target.get('version')
        if version is not None and (not isinstance(version,str) or not version.isdigit() or int(version)<1 or str(int(version))!=version):raise HTTPException(400,'引用版本必须是正整数十进制字符串')
        reader=ReadTools(db,p,config.values(p)[0]);row,content=reader.resolve(body['ref'])
        if not row:raise HTTPException(404,'固定引用不存在')
        return public({'ref':body['ref'],'resolved_record_id':row['id'],'record':row,'content':content})
    @app.get(BASE+'/artifact-defaults')
    async def get_artifact_defaults(request:Request):
        user=auth.identity(request)
        return public(artifact_workspace.defaults_view(user['account_id']))
    @app.put(BASE+'/artifact-defaults')
    async def put_artifact_defaults(request:Request):
        user=auth.identity(request,True);body=await json_body(request)
        return public(artifact_workspace.save_defaults(user['account_id'],body))
    @app.get(BASE+'/projects/{p}/artifacts')
    async def list_project_artifacts(request:Request,p:str,kinds:str=''):
        access(request,p)
        selected=tuple(x.strip() for x in kinds.split(',') if x.strip())
        return public(artifact_workspace.items(p,selected))
    @app.put(BASE+'/projects/{p}/artifacts/{kind}')
    async def put_project_artifact(request:Request,p:str,kind:str):
        access(request,p,True);body=await json_body(request)
        if set(body)!={'content','expected_version'}:
            raise HTTPException(400,'请提交 content 和 expected_version')
        return public(artifact_workspace.save_project(p,kind,body['content'],body['expected_version']))
    @app.get(BASE+'/projects/{p}/artifacts/{artifact_id}/versions')
    async def versions(request:Request,p:str,artifact_id:str):
        access(request,p);return {'items':public(list(records_iter(p,'artifact_version',filters={'artifact_id':artifact_id})))}
    def version_content(p,a,v):
        row,content=ReadTools(db,p,config.values(p)[0]).resolve({'record_id':a,'version':str(v)})
        if not row:raise HTTPException(404,'产物版本不存在')
        states=db.list(p,'artifact_state',limit=1,filters={'artifact_version_id':row['id']})
        return {'version':row,'state':states[0] if states else None,'content':content}
    @app.get(BASE+'/projects/{p}/artifacts/{artifact_id}/versions/{version}/download')
    async def download(request:Request,p:str,artifact_id:str,version:int):
        access(request,p);value=version_content(p,artifact_id,version)
        return Response(canonical_bytes(value['content']),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="nexo-{artifact_id}-v{version}.json"'})
    @app.get(BASE+'/projects/{p}/artifacts/{artifact_id}/versions/{version}')
    async def artifact_version(request:Request,p:str,artifact_id:str,version:int):access(request,p);return public(version_content(p,artifact_id,version))
    @app.get(BASE+'/projects/{p}/config')
    async def get_config(request:Request,p:str,stage:str='coordinator',agent_key:str|None=None):access(request,p);return public(config.view(p,stage,agent_key))
    @app.post(BASE+'/projects/{p}/config')
    async def save_config(request:Request,p:str):
        body=await json_body(request)
        if not isinstance(body.get('values'),dict) or (body.get('schemas') is not None and not isinstance(body['schemas'],dict)):raise HTTPException(400,'values 和 schemas 必须是 JSON 对象')
        return once(request,p,'config.draft',body,lambda:config.draft(p,body['values'],body.get('schemas'),body.get('scope_kind','project'),body.get('scope_key')))
    @app.post(BASE+'/projects/{p}/config/{config_id}/publish')
    async def publish(request:Request,p:str,config_id:str):return once(request,p,'config.publish:'+config_id,{},lambda:config.publish(p,config_id))
    @app.get(BASE+'/account/config')
    async def get_account_config(request:Request,stage:str='coordinator',agent_key:str|None=None):
        user=auth.identity(request);return public(config.view_for_account(user['account_id'],stage,agent_key))
    @app.post(BASE+'/account/config/instructions-preview')
    async def preview_account_instructions(request:Request):
        auth.identity(request);body=await json_body(request)
        values,stage=body.get('values'),body.get('stage')
        step1_mode=body.get('step1_mode','full')
        step1_view=body.get('step1_view')
        if not isinstance(values,dict) or not isinstance(stage,str) or stage not in (
                'coordinator',*[f'step{i}' for i in range(1,12)],
                'aux.summary','aux.history_answer','aux.subtask') or step1_mode not in ('full','window') or (
                step1_view is not None and (stage!='step1' or step1_view not in ('global','character'))):
            raise HTTPException(400,'预览需要有效的配置与阶段')
        from .prompts import instructions_preview
        return public(instructions_preview(stage,values,step1_mode=step1_mode,step1_view=step1_view))
    @app.get(BASE+'/account/config/editor')
    async def get_account_config_editor(request:Request,scope_kind:str='project',scope_key:str|None=None):
        user=auth.identity(request)
        return config.editor_draft_for_account(user['account_id'],scope_kind,scope_key)
    @app.put(BASE+'/account/config/editor')
    async def save_account_config_editor(request:Request):
        user=auth.identity(request,True);body=await json_body(request)
        return config.save_editor_draft_for_account(user['account_id'],body.get('scope_kind','project'),
          body.get('scope_key'),body.get('payload'),body.get('expected_revision'))
    @app.delete(BASE+'/account/config/editor')
    async def clear_account_config_editor(request:Request):
        user=auth.identity(request,True);body=await json_body(request)
        return config.clear_editor_draft_for_account(user['account_id'],body.get('scope_kind','project'),
          body.get('scope_key'),body.get('expected_revision'))
    @app.post(BASE+'/account/config/validate')
    async def validate_account_config(request:Request):
        auth.identity(request,True);body=await json_body(request)
        values,schemas=body.get('values'),body.get('schemas')
        if not isinstance(values,dict) or not isinstance(schemas,dict):
            raise HTTPException(400,'values 和 schemas 必须是 JSON 对象')
        stage=body.get('stage','coordinator')
        if not isinstance(stage,str):raise HTTPException(400,'stage 必须是文本')
        return config.validate_candidate(values,schemas,stage)
    @app.post(BASE+'/account/config')
    async def save_account_config(request:Request):
        user=auth.identity(request,True);body=await json_body(request)
        if not isinstance(body.get('values'),dict) or (body.get('schemas') is not None and not isinstance(body['schemas'],dict)):raise HTTPException(400,'values 和 schemas 必须是 JSON 对象')
        return account_once(request,'config.draft',body,lambda:config.draft_for_account(user['account_id'],body['values'],body.get('schemas'),body.get('scope_kind','project'),body.get('scope_key')))
    @app.post(BASE+'/account/config/{config_id}/publish')
    async def publish_account_config(request:Request,config_id:str):
        user=auth.identity(request,True)
        return account_once(request,'config.publish:'+config_id,{},lambda:config.publish_for_account(user['account_id'],config_id))
    @app.get(BASE+'/projects/{p}/events')
    async def events(request:Request,p:str,after:int=0):
        access(request,p)
        if not 0<=after<=9223372036854775807:raise HTTPException(400,'事件游标超出有效范围')
        last=request.headers.get('Last-Event-ID')
        if last:
            if not last.isdigit() or len(last)>19 or int(last)>9223372036854775807:raise HTTPException(400,'Last-Event-ID 必须是有效非负整数')
            after=max(after,int(last))
        async def stream():
            position=after
            while not await request.is_disconnected():
                try:access(request,p)
                except HTTPException:
                    yield 'event: error\ndata: {"code":"unauthenticated","message":"请重新登录"}\n\n'
                    return
                rows=event_rows(p,position)
                for row in rows:
                    position=row['sequence']
                    yield f'id: {position}\ndata: {json.dumps(public(row),ensure_ascii=False)}\n\n'
                if len(rows)==1000:continue
                yield ': heartbeat\n\n';await asyncio.sleep(2)
        return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
    @app.get('/')
    async def index():return FileResponse(ROOT/'branch_agent/static/index.html')
    app.mount('/static',StaticFiles(directory=ROOT/'branch_agent/static'),name='static')
    return app
