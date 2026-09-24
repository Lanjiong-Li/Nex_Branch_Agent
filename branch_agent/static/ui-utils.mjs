export const recordTypes = {project:'项目',conversation:'会话',history_record:'历史记录',work_session:'工作 Session',session_item:'Session 条目',artifact:'产物',artifact_version:'产物版本',artifact_state:'产物状态',decision:'决策',confirmation:'确认',task:'任务',run:'运行',queued_request:'排队请求',checkpoint:'检查点',config_version:'配置版本',context_snapshot:'实际上下文',model_call:'模型调用',tool_call:'工具调用',dependency:'依赖',blob:'内容与附件',runtime_event:'运行事件',idempotency_record:'幂等操作'};
export const stateLabels = {running:'运行中',queued:'排队中',waiting_user:'等待你回复',paused:'已暂停',stopping:'停止请求已提交',stopped:'已停止',succeeded:'已完成',failed:'失败',interrupted:'已中断',created:'已创建',pending:'待执行',unknown:'结果待核对',draft:'草稿',published:'已发布',snapshot:'运行快照',retired:'历史配置',unconfirmed:'待确认',partial:'部分确认',confirmed:'已确认',not_required:'无需确认',valid:'依赖有效',review_required:'依赖待复核',invalid:'依赖失效',unchecked:'未检查',passed:'检查通过',inconclusive:'尚无结论',ready:'已保存'};
export const stageNames=['原作切分','原作分析','玩家与策略','改编方案','全剧规划','章节事件','线性正文','互动设计','章节分支','整剧组装','最终审核'];
export function selectActiveTask(tasks,conversationId=null){
  const byId=new Map(tasks.map(task=>[task.id,task]));
  const scoped=tasks.filter(task=>{
    if(conversationId&&task.conversation_id!==conversationId)return false;
    if(task.parent_task_id&&task.intent==='summarize')return false;
    if(task.state==='failed'&&byId.get(task.parent_task_id)?.state==='succeeded')return false;
    return true;
  });
  for(const states of [['running','stopping'],['paused','failed','stopped'],['waiting_user'],['queued']]){
    const candidates=scoped.filter(task=>states.includes(task.state));
    if(candidates.length)return candidates.sort((a,b)=>String(a.updated_at??a.created_at??'').localeCompare(String(b.updated_at??b.created_at??''))).at(-1);
  }
}
const failureMessages={
  output_limit_exceeded:'模型输出达到本次上限，结果未完整生成。',
  response_incomplete:'供应商已返回未完成的响应，该响应未作为有效产物。',
  response_failed:'供应商已明确返回失败结果，请查看对应错误分类。',
  response_not_terminal:'尚未确认模型响应已完成，该响应未作为有效产物。',
  model_refusal:'模型已明确拒绝本次请求，拒答未作为有效产物。',
  ModelBehaviorError:'模型输出不符合当前结构或工具调用协议。',
  operation_uncertain:'模型请求的完成状态无法确认，需要先核对调用记录。',
  input_budget_exceeded:'本次请求的输入超出上下文预算。',
  source_index_migration_required:'旧版阶段产物缺少独立原文索引，需重新生成并确认对应阶段。',
  tool_output_budget_exceeded:'工具读取结果超出本次读取预算。',
  cost_limit:'任务费用已达到预算上限。',active_time_limit:'任务活动耗时已达到预算上限。',
  turn_limit:'本次运行已达到模型轮次上限。',usage_uncertain:'调用用量尚未核验，暂不能开始新的请求。',
  child_blocked:'子任务尚未完成，当前任务暂时无法继续。',summary_failed:'上下文摘要未完成，当前任务暂停。',
  dependency_changed:'上游依赖已改变，需要先复核相关产物。',repair_exhausted:'已达到本次修复轮数上限。',
  user_stop:'任务已按停止请求暂停。',checkpoint_invalid:'保存的检查点无法用于本次恢复。',
  configuration_error:'本次运行的配置无法通过检查。',RateLimitError:'供应商返回请求额度或频率限制。',
  insufficient_quota:'供应商账户额度不足。',credit_balance_exhausted:'供应商账户余额不足。',
  AuthenticationError:'模型服务身份验证失败。',PermissionDeniedError:'模型服务拒绝了此请求的权限。',
  authentication_failed:'模型服务身份验证失败，请检查服务端凭据。',permission_denied:'当前模型服务凭据没有请求权限。',
  quota_exhausted:'模型服务账户的可用额度或余额不足。',rate_limit_exceeded:'模型服务请求频率已达到限制。',
  model_not_found:'所选模型不存在或当前账户无法访问。',context_length_exceeded:'本次请求超过模型的上下文长度上限。',
  invalid_output_schema:'输出或工具参数 Schema 未被模型服务接受。',
  BadRequestError:'模型服务拒绝了本次请求参数。',NotFoundError:'请求的模型或服务资源不存在。',
  UnprocessableEntityError:'模型服务无法处理当前请求内容。',ConflictError:'本次请求与模型服务当前状态冲突。',
  InternalServerError:'模型服务发生内部错误。',APIStatusError:'模型服务返回错误状态，请查看 HTTP 状态及错误码。',
  APIConnectionError:'模型服务连接中断，尚不能确认请求是否完成。',APITimeoutError:'模型请求超时，尚不能确认供应商是否完成。',
  missing_api_key:'服务端尚未配置模型 API 凭据。',
  content_filter:'模型输出受到内容策略限制，结果未完整生成。',task_failed:'任务执行失败，请查看对应运行记录。'
};
function diagnosticCode(value){return typeof value==='string'&&/^[a-zA-Z][a-zA-Z0-9_.:-]{0,95}$/.test(value)&&!/^sk-|bearer|authorization/i.test(value)?value:null;}
export function describeFailure(code,error={}){
  const safeCode=diagnosticCode(code)||'unclassified_error',details={};
  for(const key of ['provider_status','terminal_status','terminal_event','incomplete_reason','error_code','error_type','provider_error_code']){
    const value=diagnosticCode(error?.details?.[key]);if(value)details[key]=value;
  }
  for(const key of ['max_output_tokens','http_status']){
    const value=error?.details?.[key];if(Number.isSafeInteger(value)&&value>=0)details[key]=value;
  }
  for(const key of ['provider_response_id','model_call_id']){
    const value=error?.details?.[key];if(typeof value==='string'&&/^[a-zA-Z0-9_-]{1,160}$/.test(value)&&!/^sk-|bearer|authorization/i.test(value))details[key]=value;
  }
  if(typeof error?.details?.known_outcome==='boolean')details.known_outcome=error.details.known_outcome;
  const message=safeCode==='response_incomplete'&&details.incomplete_reason==='content_filter'?'供应商因内容策略限制返回未完成的响应，该响应未作为有效产物。':failureMessages[safeCode]??'任务暂时无法继续，请查看对应运行记录中的错误码。';
  return {code:safeCode,message,details};
}
function newestRun(task,runs){
  const own=runs.filter(run=>run.task_id===task.id);
  return own.find(run=>run.id===task.current_run_id)??own.sort((a,b)=>String(a.started_at??a.created_at??'').localeCompare(String(b.started_at??b.created_at??''))).at(-1);
}
export function taskIssue(task,{tasks=[],runs=[]}={}){
  if(!task||!['paused','failed','stopped'].includes(task.state))return null;
  const controlId=task.id,visited=new Set();let source=task;
  while(source.pause_reason==='child_blocked'&&!visited.has(source.id)){
    visited.add(source.id);const parentRun=newestRun(source,runs),since=parentRun?.started_at??parentRun?.created_at;
    const explicit=parentRun?.error?.details?.child_task_id??parentRun?.error?.details?.task_id;
    const candidates=tasks.filter(child=>child.parent_task_id===source.id&&['paused','failed','stopped'].includes(child.state)&&child.intent!=='summarize'&&!visited.has(child.id))
      .filter(child=>!(child.state==='failed'&&newestRun(child,runs)?.state==='succeeded'))
      .filter(child=>child.id===explicit||child.state!=='failed'||!since||!(child.updated_at??child.created_at)||(child.updated_at??child.created_at)>=since);
    const child=candidates.find(row=>row.id===explicit)??candidates.sort((a,b)=>String(a.updated_at??a.created_at??'').localeCompare(String(b.updated_at??b.created_at??''))).at(-1);
    if(!child)break;source=child;
  }
  const run=newestRun(source,runs),pause=diagnosticCode(source.pause_reason);
  let error=['paused','failed','stopped','interrupted'].includes(run?.state)?run?.error:null;
  // A fresh unknown outcome must not be relabelled using an earlier failed attempt.
  const uncertainTransport=pause==='operation_uncertain'&&['APIConnectionError','APITimeoutError'].includes(error?.code)&&error?.details?.known_outcome!==true;
  if(pause==='operation_uncertain'&&error?.code!==pause&&!uncertainTransport)error=null;
  const generic=new Set(['child_blocked','task_failed','summary_failed','configuration_error']);
  const code=error&&(!pause||generic.has(pause))?error.code:pause??error?.code??'task_failed';
  if(error&&diagnosticCode(error.code)!==diagnosticCode(code)&&!uncertainTransport)error=null;
  return {...describeFailure(code,error),control_task_id:controlId,task_id:source.id,run_id:error?run.id:null};
}
export function escapeHTML(x) { return String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
const secretKeys=/^(api[_-]?key|authorization|cookie|set-cookie|password|secret|access_token|refresh_token|csrf_token|fencing_token|lease_owner|provider_payload|encrypted_content|reasoning_content|chain_of_thought|hidden_reasoning|raw_reasoning|recovery_token|raw_response|response_body|raw_body|request_headers|response_headers)$/i;
export function publicView(value) { if(Array.isArray(value)) return value.map(publicView); if(value && typeof value==='object') return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,secretKeys.test(k)?'[已遮蔽：内部凭据或不可公开内容]':publicView(v)])); return value; }
export function textContent(value) { if(value == null)return ''; if(typeof value==='string')return value; if(value.storage==='inline_text')return value.text; if(value.storage==='inline_json')return textContent(value.value); if(value.storage==='blob')return `外置内容 · ${value.blob_id}`; return JSON.stringify(publicView(value),null,2); }
export function getPath(obj,path) { return path.split('.').reduce((x,k)=>x?.[k],obj); }
export function setPath(obj,path,value) { const keys=path.split('.'); let at=obj; for(const key of keys.slice(0,-1)){if(!at[key]||typeof at[key]!=='object')at[key]={};at=at[key];} at[keys.at(-1)]=value; }
export function pointerGet(obj,path) { return path.split('/').slice(1).reduce((x,k)=>x?.[k.replace(/~1/g,'/').replace(/~0/g,'~')],obj); }
export function pointerSet(obj,path,value) { const keys=path.split('/').slice(1).map(k=>k.replace(/~1/g,'/').replace(/~0/g,'~')); let at=obj; keys.slice(0,-1).forEach(k=>at=at[k]); at[keys.at(-1)]=value; }
export function number(value) { return value == null?'未提供':new Intl.NumberFormat('zh-CN').format(value); }
export function inputBudget(values,model={}) { const cap=Number(getPath(values,'context.input_token_cap'));const output=Number(getPath(values,'model.max_output_tokens'));const margin=Number(getPath(values,'context.safety_margin_tokens')??0);const window=Number(model.context_window??model.context_window_tokens);return Number.isFinite(cap)&&cap>0&&window>0 ? Math.min(cap,window-output-margin):null; }
export function objectDiff(a,b,path='') { if(JSON.stringify(a)===JSON.stringify(b))return []; if(a&&b&&typeof a==='object'&&typeof b==='object'&&!Array.isArray(a)&&!Array.isArray(b)){return [...new Set([...Object.keys(a),...Object.keys(b)])].flatMap(k=>objectDiff(a[k],b[k],`${path}/${k.replace(/~/g,'~0').replace(/\//g,'~1')}`));} if(Array.isArray(a)&&Array.isArray(b)&&a.every(x=>x&&typeof x==='object'&&x.id)&&b.every(x=>x&&typeof x==='object'&&x.id)){const am=new Map(a.map(x=>[x.id,x])),bm=new Map(b.map(x=>[x.id,x]));return [...new Set([...am.keys(),...bm.keys()])].flatMap(k=>objectDiff(am.get(k),bm.get(k),`${path}/[id=${k}]`));}return [{path:path||'/',kind:a===undefined?'新增':b===undefined?'删除':'修改',before:a,after:b}]; }
export function schemaFields(schema,path='',depth=0) { if(!schema||typeof schema!=='object'||depth>20)return [];let out=[];for(const[k,v]of Object.entries(schema.properties||{})){const p=path+'/properties/'+k.replace(/~/g,'~0').replace(/\//g,'~1');out.push({name:k,path:p,depth,node:v,required:(schema.required||[]).includes(k)});out.push(...schemaFields(v,p,depth+1));if(v.items)out.push(...schemaFields(v.items,p+'/items',depth+1));}for(const[k,v]of Object.entries(schema.$defs||{})){out.push({name:'定义 · '+k,path:path+'/$defs/'+k,depth,node:v,required:false});out.push(...schemaFields(v,path+'/$defs/'+k,depth+1));}return out; }
export function validateConfig(values,schemas,registry={}) {const errors=[];const model=registry.models?.[values.model?.name]??registry[values.model?.name];for(const key of ['context.input_token_cap','context.step1_source.max_source_tokens','model.max_output_tokens','run.max_turns']){const x=getPath(values,key);if(x!==undefined&&(!Number.isInteger(x)||x<=0))errors.push(`${key} 须为正整数`);}if(registry.models&&Object.keys(registry.models).length&&!model)errors.push('所选模型未注册，无法校验其能力');if(model){if(values.model?.max_output_tokens>model.max_output_tokens)errors.push('输出上限超出所选模型能力');const b=inputBudget(values,model);if(b!==null&&b<=0)errors.push('输出预留和安全余量没有留下有效输入空间');const sourceLimit=values.context?.step1_source?.max_source_tokens;if(Number.isInteger(sourceLimit)&&b!==null&&sourceLimit>=b)errors.push('原作全文 token 上限须小于有效输入预算');const efforts=model.reasoning_efforts??model.reasoning_effort;if(Array.isArray(efforts)&&!efforts.includes(values.model?.reasoning_effort))errors.push('推理强度不在模型支持范围');}if(values.context?.step1_source?.mode&&values.context.step1_source.mode!=='full_text')errors.push('Step1 必须全文导入');if(values.context?.step1_source?.overflow_behavior&&values.context.step1_source.overflow_behavior!=='reject')errors.push('Step1 超限处理必须为拒绝');for(const[k,v]of Object.entries(schemas||{})){if(!v||typeof v!=='object'||Array.isArray(v))errors.push(`${k} 必须是 JSON Schema 对象`);}return errors; }
export function lineDiff(before,after){const a=String(before??'').split('\n'),b=String(after??'').split('\n');if(a.length*b.length>1_000_000){let i=0;while(i<Math.min(a.length,b.length)&&a[i]===b[i])i++;let j=0;while(j<Math.min(a.length,b.length)-i&&a[a.length-j-1]===b[b.length-j-1])j++;return [...a.slice(0,i).map(text=>({kind:'same',text})),...a.slice(i,a.length-j).map(text=>({kind:'delete',text})),...b.slice(i,b.length-j).map(text=>({kind:'add',text})),...a.slice(a.length-j).map(text=>({kind:'same',text}))];}const grid=Array.from({length:a.length+1},()=>new Uint32Array(b.length+1));for(let i=a.length-1;i>=0;i--)for(let j=b.length-1;j>=0;j--)grid[i][j]=a[i]===b[j]?grid[i+1][j+1]+1:Math.max(grid[i+1][j],grid[i][j+1]);const out=[];let i=0,j=0;while(i<a.length||j<b.length){if(i<a.length&&j<b.length&&a[i]===b[j]){out.push({kind:'same',text:a[i]});i++;j++;}else if(i<a.length&&(j===b.length||grid[i+1][j]>=grid[i][j+1]))out.push({kind:'delete',text:a[i++]});else out.push({kind:'add',text:b[j++]});}return out;}
export function configDelta(baseline,edited){const out={};for(const[k,v]of Object.entries(edited||{})){if(JSON.stringify(v)===JSON.stringify(baseline?.[k]))continue;if(v&&typeof v==='object'&&!Array.isArray(v)&&baseline?.[k]&&typeof baseline[k]==='object'&&!Array.isArray(baseline[k])){const nested=configDelta(baseline[k],v);if(Object.keys(nested).length)out[k]=nested;}else out[k]=structuredClone(v);}return out;}
export function deepMerge(base,overlay){const out=structuredClone(base??{});for(const[k,v]of Object.entries(overlay??{})){out[k]=v&&typeof v==='object'&&!Array.isArray(v)&&out[k]&&typeof out[k]==='object'&&!Array.isArray(out[k])?deepMerge(out[k],v):structuredClone(v);}return out;}
export function deletePath(obj,path){const keys=path.split('.');let at=obj;for(const key of keys.slice(0,-1)){if(!at||typeof at!=='object')return;at=at[key];}if(at&&typeof at==='object')delete at[keys.at(-1)];}
export function decimalSum(values){const safe=values.filter(v=>/^\d+(\.\d+)?$/.test(String(v)));if(!safe.length)return null;const scale=Math.max(...safe.map(v=>(String(v).split('.')[1]??'').length));const sum=safe.reduce((total,v)=>{const [whole,fraction='']=String(v).split('.');return total+BigInt(whole+fraction.padEnd(scale,'0'));},0n);const s=sum.toString().padStart(scale+1,'0');return scale?s.slice(0,-scale)+'.'+s.slice(-scale):s;}

// A draft is pinned to the revision the person actually saw. Background updates
// preserve entered values but never silently approve a changed target/config.
export function actionDraft(card,previous=null,{acceptRevision=false}={}) {
  const actions=card.actions??[],values={};
  for(const action of actions){values[action.id]={};for(const field of action.fields??[]){
    const old=previous?.values?.[action.id];
    // A changed recovery/configuration needs a fresh affirmative acknowledgement.
    values[action.id][field.name]=field.type==='checkbox'&&acceptRevision&&previous?.revision!==card.revision?false:old&&Object.hasOwn(old,field.name)?old[field.name]:field.type==='checkbox'?false:field.type==='select'&&field.required?'':field.default??'';
  }}
  const revision=!previous||acceptRevision?card.revision:previous.revision;
  const selected=actions.some(a=>a.id===previous?.selected)?previous.selected:actions.length===1?actions[0].id:'';
  return {...previous,revision,selected,values,stale:revision!==card.revision||(!acceptRevision&&previous?.stale===true),inFlight:previous?.inFlight??false,error:acceptRevision?'':previous?.error??''};
}
export function actionSubmission(card,draft) {
  if(draft.inFlight)return {error:'正在提交，请等待结果。'};
  if(draft.stale||draft.revision!==card.revision)return {error:'处理对象已更新，请先刷新并核对最新选项。'};
  const action=card.actions?.find(a=>a.id===draft.selected);
  if(!action)return {error:'请选择要执行的操作。'};
  if(action.disabled_reason)return {error:action.disabled_reason};
  const values={},customChoice=(action.fields??[]).some(field=>field.name==='answer'&&field.type==='select'&&field.options?.some(option=>option.value==='__custom__'));
  for(const field of action.fields??[]){
    if(customChoice&&field.name==='text'&&draft.values[action.id]?.answer!=='__custom__')continue;
    let value=draft.values?.[action.id]?.[field.name];
    const label=field.label??field.name;
    if(field.type==='checkbox'){value=value===true;if(field.required&&!value)return {error:`请主动勾选“${label}”。`};}
    else {
      value=String(value??'').trim();
      if(field.required&&!value)return {error:`请填写或选择“${label}”。`};
      if(field.type==='select'&&value&&!field.options?.some(option=>String(option.value)===value))return {error:`请为“${label}”选择有效选项。`};
      if(field.type==='number'&&value){value=Number(value);if(!Number.isFinite(value))return {error:`“${label}”须为有效数字。`};}
    }
    if(value!==''||field.type!=='number')values[field.name]=value;
  }
  if(values.answer==='__custom__'&&!String(values.text??'').trim())return {error:'请填写自定义回答。'};
  return {body:{card_id:card.id,action_id:action.id,expected_revision:draft.revision,values}};
}
export function artifactStatusLabel(artifact,states=[]) {
  const state=states.find(row=>row.artifact_id===artifact.id&&String(row.version)===String(artifact.latest_version));
  if(!state||!state.dependency_status)return '已保存 · 状态未提供';
  if(state.dependency_status==='review_required')return '依赖待复核';
  if(state.dependency_status==='invalid')return '依赖已失效';
  if(state.dependency_status!=='valid')return '已保存 · 状态未提供';
  if(state.quality_status==='failed')return '质量检查未通过';
  if(state.quality_status==='inconclusive')return '质量待复核';
  if(state.confirmation_status==='partial')return '部分范围已确认';
  if(state.confirmation_status==='unconfirmed')return '待确认';
  if(!['confirmed','not_required'].includes(state.confirmation_status))return '已保存 · 状态未提供';
  return String(artifact.current_effective_version)===String(artifact.latest_version)?'当前有效版本':'最新草稿';
}
export function pendingQueueCount(queue,conversationId) {
  return conversationId?(queue??[]).filter(row=>row.conversation_id===conversationId&&['pending','blocked'].includes(row.state)).length:0;
}
