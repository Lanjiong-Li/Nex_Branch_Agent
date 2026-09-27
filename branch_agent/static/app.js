import {runtimeHTML} from './runtime-view.mjs?v=20260925-6';
import {artifactMonitorHTML,editableArtifactKinds} from './artifact-view.mjs?v=20260926-4';
import {renderMarkdown} from './markdown.mjs?v=20260927-2';
import {recordTypes,stateLabels,stageNames,escapeHTML as e,publicView,textContent,getPath,setPath,pointerGet,pointerSet,number,inputBudget,objectDiff,lineDiff,decimalSum,configDelta,deepMerge,validateConfig,selectActiveTask,taskIssue,describeFailure,actionDraft,actionSubmission,pendingQueueCount} from './ui-utils.mjs?v=20260926-2';
const API='/api/branch-agent/v1';
const $=(selector,root=document)=>root.querySelector(selector);
const $$=(selector,root=document)=>[...root.querySelectorAll(selector)];
const state={account:null,projects:[],project:null,conversations:[],conversation:null,history:[],activity:[],activityTruncatedBefore:false,replyStreams:new Map(),chatEventSequences:new Set(),chatEventRows:new Map(),status:{tasks:[],runs:[],queue:[],artifacts:[]},panel:'config',panelOpen:false,recordType:'task',records:[],cursor:null,record:null,recordVersion:null,recordContent:null,config:null,models:{},configValues:null,schemas:null,configDirty:false,schemaName:null,draft:null,events:[],sequence:0,eventSource:null,refreshing:false,refreshPending:false,view:'list',historyCursor:null,historyAll:false,configSection:'global',configScope:'project',configScopeKey:'',configAgent:'source_global_parser',configStage:'coordinator',schemaJsonTexts:{},rawConfigFields:{},editorRevision:0,editorBaseValues:null,schemaValidation:null,schemaValidationRequired:false,uploadedSchemaFileName:'',runtimeData:null,runtimeDetail:null,runtimeRunId:null,runtimeProjectId:null,artifactEntries:[],artifactDefaults:null,artifactError:'',artifactDefaultsError:'',artifactProjectId:null,artifactLoading:false,artifactVisibleKinds:['adaptation_strategy','adaptation_plan'],artifactExpandedKinds:[]};
let loadedRelease=null;
let shownRelease=null;
async function checkRelease(){
  try{
    const release=await request('/release');
    if(!release?.id)return;
    if(loadedRelease===null){loadedRelease=release.id;return;}
    if(release.id===loadedRelease||shownRelease===release.id||$('#modal').open)return;
    shownRelease=release.id;
    $('#update-notes').innerHTML=`<strong>${e(release.title??'版本更新')}</strong><ul>${(release.changes??[]).map(change=>`<li>${e(change)}</li>`).join('')}</ul>`;
    $('#update-modal').showModal();
  }catch{ /* A temporary network failure must not interrupt the current task. */ }
}
const labels={id:'记录 ID',record_type:'记录类型',schema_version:'契约版本',project_id:'项目',conversation_id:'会话',created_at:'创建时间',updated_at:'状态更新时间',row_version:'并发版本',state:'状态',version:'内容版本',scope:'作用范围',stage:'阶段',chapter_ids:'章节范围',branch_ids:'分支范围',description:'说明',role:'发言方',visibility:'展示范围',kind:'种类',content:'内容',text:'正文',value:'结构化内容',storage:'存储方式',artifact_kind:'产物类型',artifact_id:'产物',artifact_version_id:'产物版本记录',current_effective_version:'当前完整有效版本',latest_version:'最新版本',confirmation_status:'确认状态',dependency_status:'依赖状态',quality_status:'质量检查',effective_selections:'已生效范围',confirmation_ids:'确认记录',source_refs:'来源引用',dependency_ids:'依赖记录',producer_run_id:'生成运行',parent_version:'上一版本',origin:'产生来源',source_message_ids:'原始用户消息',source_message_id:'来源消息',subject:'确认对象',selections:'确认范围',action:'动作',basis:'确认依据',carried_from_confirmation_ids:'继承依据',task_id:'任务',run_id:'运行',session_id:'工作 Session',agent_key:'Agent',intent:'任务意图',pause_reason:'暂停原因',budget:'预算',usage:'用量',current_run_id:'当前运行',latest_checkpoint_id:'最新检查点',repair_rounds_used:'已用修复轮数',max_turns:'模型轮次限额',model_turns_used:'已用模型轮次',execution_kind:'执行类型',started_at:'开始时间',finished_at:'结束时间',error:'错误',instructions:'实际 instructions',input_items:'实际输入',tool_definitions:'工具定义',materials:'材料清单',model:'模型',reasoning_effort:'推理强度',input_token_estimate:'估算输入 token',input_token_budget:'实际输入预算',config_version_id:'固定配置',context_snapshot_id:'实际上下文',output_schema:'输出结构',history_ids:'历史记录',summary_ref:'工作摘要',input_tokens:'输入 token',output_tokens:'输出 token',cached_input_tokens:'缓存 token（输入子集）',reasoning_tokens:'推理 token（输出子集）',active_ms:'活动耗时（ms）',estimated_cost:'估算费用',reported_cost:'已报告费用',pricing_version:'计价版本',attempt:'尝试序号',turn_index:'模型轮次',max_active_seconds:'活动耗时限额（秒）',max_cost:'费用限额',amount:'金额',currency:'币种',tool_name:'工具名',arguments:'参数',result:'结果',event_name:'事件',payload:'事件内容',sequence:'顺序',consumer_ref:'使用方',producer_ref:'依赖方',relation:'关系',parent_task_id:'父任务',budget_root_task_id:'预算根任务',scope_kind:'配置范围',scope_key:'范围键',values:'配置内容',resolved_from_ids:'继承来源',reason:'原因',status:'状态',operation_id:'操作 ID',blob_id:'外置内容',sha256:'内容校验',content_sha256:'内容校验',filename:'文件名',media_type:'媒体类型',size_bytes:'字节数',cursor:'恢复位置',handler_version:'恢复协议',requested_by_message_id:'入口消息'};
const artifactLabels={source_text:'原作全文',source_segments:'原作切分',source_global_events:'作品事件视图',source_character_events:'主要人物事件视图',event_function_map:'事件功能映射',game_event_view:'游戏事件视图',ending_routes:'结局路线',chapter_design:'章节设计',coordinator_response:'协调结果',source_global_analysis:'作品事件分析',source_knowledge_asset:'原作知识资产',adaptation_strategy:'玩家与改编策略',adaptation_plan:'改编方案',story_plan:'全剧规划',chapter_events:'章节事件',linear_script:'线性正文',interaction_design:'互动设计',chapter_graph:'章节 Graph',nexo_graph:'完整互动剧本',review_report:'审核报告',work_summary:'工作摘要',batch_manifest:'材料分批计划',scope_mapping:'版本影响映射'};
const stageArtifactCatalog=[['source_text','原作全文',0],['source_global_events','作品事件视图',1],['source_character_events','主要人物事件视图',1],['source_global_analysis','作品事件分析',1],['source_knowledge_asset','原作知识资产',2],['adaptation_strategy','玩家与互动策略',3],['adaptation_plan','互动剧本改编方案',4],['game_event_view','互动剧本事件视图',5],['ending_routes','结局路线',7],['player_profiles','目标玩家画像',8],['chapter_design','章节设计',9],['chapter_graph','章节 Graph',10],['nexo_graph','完整互动剧本',10]];
function unwrap(payload){return payload&&Object.hasOwn(payload,'data')?payload.data:payload;}
const pendingWrites=new Map();
const actionDrafts=new Map();
let activeDecisionCardId=null;
state.actionCards=[];state.actionsError="";
const actionScope=()=>`${state.project?.id??""}/${state.conversation?.id??""}`;
const actionKey=id=>`${actionScope()}/${id}`;
function clearProjectActions(projectId){
  for(const key of actionDrafts.keys())if(key.startsWith(`${projectId}/`))actionDrafts.delete(key);
  state.actionsRequestSeq=(state.actionsRequestSeq??0)+1;
  state.actionCards=[];
  state.actionsError='';
  activeDecisionCardId=null;
  renderActionCards();
}
async function request(path,{method='GET',body,...options}={}){const headers={Accept:'application/json',...(options.headers||{})};let pendingKey=null;if(method!=='GET'){if(state.account?.csrf_token)headers['X-CSRF-Token']=state.account.csrf_token;const fingerprint=body instanceof FormData?JSON.stringify([...body.entries()].map(([k,v])=>[k,typeof v==='string'?v:{name:v.name,size:v.size,lastModified:v.lastModified}])):JSON.stringify(body);pendingKey=method+path+fingerprint;if(!pendingWrites.has(pendingKey))pendingWrites.set(pendingKey,crypto.randomUUID());headers['Idempotency-Key']=pendingWrites.get(pendingKey);}if(body!==undefined&&!(body instanceof FormData)){headers['Content-Type']='application/json';body=JSON.stringify(body);}let response;try{response=await fetch(API+path,{...options,method,body,headers,credentials:'same-origin'});}catch{throw new Error(method==='GET'?'连接失败，请检查服务后重试。':'连接中断，提交结果尚未确认。重试相同内容会复用原幂等标识。');}const payload=await response.json().catch(()=>({error:{code:'invalid_response',message:'服务端未返回有效 JSON'}}));if(!response.ok||(payload.error&&!payload.record_type)){if(response.status<500&&pendingKey)pendingWrites.delete(pendingKey);const err=new Error(payload.error?.message||`请求失败 (${response.status})`);err.code=payload.error?.code;err.status=response.status;throw err;}if(pendingKey)pendingWrites.delete(pendingKey);return unwrap(payload);}

const ppath=()=>`/projects/${encodeURIComponent(state.project.id)}`;
const cpath=()=>`${ppath()}/conversations/${encodeURIComponent(state.conversation.id)}`;
const short=id=>String(id??'').slice(0,8);
const date=value=>value?new Date(value).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'未提供';
const obj=x=>x?.record??x;
const items=x=>Array.isArray(x)?x:(x?.items??[]);
const badge=value=>`<span class="badge ${['succeeded','confirmed','valid','passed'].includes(value)?'good':['failed','invalid'].includes(value)?'bad':['paused','waiting_user','review_required','partial'].includes(value)?'warn':''}">${e(stateLabels[value]??value??'未提供')}</span>`;
function toast(message,isError=false){const node=$('#toast');node.textContent=message;node.classList.toggle('error',isError);node.hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>node.hidden=true,isError?7000:3500);}
function modal(title,body){$('#modal-title').textContent=title;$('#modal-body').innerHTML=body;if(!$('#modal').open)$('#modal').showModal();}
function fail(error){console.error(error);toast(error.message||String(error),true);if(error.status===401)showLogin();}
async function busy(button,action){const old=button?.disabled;if(button)button.disabled=true;try{return await action();}catch(error){fail(error);}finally{if(button)button.disabled=old??false;}}
function requireProject(){if(!state.project){toast('先创建或选择一个改编项目');return false;}return true;}
function activeTask(){return selectActiveTask(state.status.tasks,state.conversation?.id);}
function issueText(issue){return `${e(issue.message)} <code>${e(issue.code)}</code>`;}
function issueNotice(issue,recordId=null){if(!issue)return '';const detail=Object.entries(issue.details).map(([key,value])=>`${e(key)}: ${e(value)}`).join(' · ');return `<div class="notice" data-error-code="${e(issue.code)}"><strong>${issueText(issue)}</strong>${detail?`<div class="small muted">${detail}</div>`:''}<div class="small">${issue.task_id&&issue.task_id!==recordId?`原因任务 <button data-action="record" data-id="${e(issue.task_id)}">${e(short(issue.task_id))} ↗</button>`:''}${issue.run_id&&issue.run_id!==recordId?` · 对应 Run <button data-action="record" data-id="${e(issue.run_id)}">${e(short(issue.run_id))} ↗</button>`:''}</div></div>`;}
function recordIssue(record){if(record.record_type==='task')return taskIssue(record,state.status);if(record.error&&['run','model_call'].includes(record.record_type))return {...describeFailure(record.error.code,record.error),task_id:record.task_id,run_id:record.record_type==='run'?record.id:record.run_id};return null;}

function activeRun(){const task=activeTask();return state.status.runs.find(r=>r.id===task?.current_run_id&&['created','running','stopping'].includes(r.state));}
async function refreshActions(acceptRevision=false,cardId=null){
  if(!state.project||!state.conversation)return;
  const scope=actionScope(),path=cpath(),sequence=(state.actionsRequestSeq??0)+1,previousCards=new Map(state.actionCards.map(card=>[card.id,card]));state.actionsRequestSeq=sequence;
  try{
    const data=await request(`${path}/actions`);if(scope!==actionScope()||sequence!==state.actionsRequestSeq)return;
    if(!Array.isArray(data.cards))throw new Error('服务端没有返回有效的操作卡片列表');
    state.actionCards=data.cards;state.actionsError='';
    for(const card of state.actionCards){
      const key=actionKey(card.id),previous=actionDrafts.get(key),oldCard=previousCards.get(card.id);
      const decision=card.kind==='confirmation'||card.kind==='question';
      const reviewed=decision&&!previous?.inFlight||acceptRevision&&(!cardId||cardId===card.id);
      const changed=decision&&oldCard&&JSON.stringify([oldCard.title,oldCard.description,oldCard.targets,oldCard.actions])!==JSON.stringify([card.title,card.description,card.targets,card.actions]);
      const draft=changed&&!previous?.inFlight?actionDraft(card):actionDraft(card,previous,{acceptRevision:reviewed});
      actionDrafts.set(key,draft);
    }
    renderActionCards();
  }catch(error){if(scope!==actionScope()||sequence!==state.actionsRequestSeq)return;state.actionsError=error.message;renderActionCards();}
}
function actionFieldHTML(field,action,draft,index){
  const customChoice=(action.fields??[]).some(item=>item.name==='answer'&&item.type==='select'&&item.options?.some(option=>option.value==='__custom__'));
  if(customChoice&&field.name==='text'&&draft.values[action.id]?.answer!=='__custom__')return '';
  const value=draft.values[action.id]?.[field.name],id=`action-field-${index}-${field.name}`;
  const custom=field.name==='text'&&draft.values[action.id]?.answer==='__custom__';
  const required=field.required||custom,attrs=`data-card-field="${e(field.name)}" name="${e(field.name)}" ${required?'required':''}`;
  const label=`${e(field.label??field.name)}${required?'<span class="required-mark"> *</span>':'<span class="muted">（选填）</span>'}`;
  if(field.type==='checkbox')return `<label class="action-checkbox"><input type="checkbox" ${attrs} ${value===true?'checked':''}><span>${label}</span></label>`;
  if(field.type==='select')return `<fieldset class="action-options"><legend>${label}</legend>${(field.options??[]).map((option,i)=>`<label class="action-option ${String(value)===String(option.value)?'selected':''}"><input type="radio" ${attrs} value="${e(option.value)}" ${String(value)===String(option.value)?'checked':''}><span>${e(option.label??option.value)}</span></label>`).join('')}</fieldset>`;
  const control=field.type==='textarea'?`<textarea id="${e(id)}" ${attrs} rows="3">${e(value)}</textarea>`:`<input id="${e(id)}" ${attrs} type="${field.type==='number'?'number':'text'}" ${field.type==='number'?'step="any"':''} value="${e(value)}">`;
  return `<label class="field" for="${e(id)}"><span>${label}</span>${control}</label>`;
}
function decisionNav(position,total){
  if(total<=1)return '';
  return `<nav class="plan-decision-nav" aria-label="待回答问题"><button type="button" data-action="decision-page" data-step="-1" aria-label="上一题" ${position===0?'disabled':''}>‹</button><span>${position+1} / ${total}</span><button type="button" data-action="decision-page" data-step="1" aria-label="下一题" ${position===total-1?'disabled':''}>›</button></nav>`;
}
function compactConfirmationCard(card,draft,position,total,locked){
  const confirm=card.actions?.find(action=>action.id==='confirm'),change=card.actions?.find(action=>action.id==='request_changes');
  if(!confirm||!change)return null;
  const scope=card.details?.find(detail=>detail.label==='范围')?.value?.match(/Step\s*\d+/i)?.[0]?.replace(/\s+/g,'')??'本阶段';
  const feedback=draft.values?.request_changes?.text??'';
  const disabled=locked||!!confirm.disabled_reason,changeDisabled=locked||!!change.disabled_reason;
  return `<form class="action-card plan-decision" data-card-form="${e(card.id)}" data-card-revision="${e(card.revision)}" aria-label="确认阶段产物" novalidate><div class="plan-decision-head"><h3>${e(card.agent_requested?card.description:`确认 ${scope} 产物？`)}</h3>${decisionNav(position,total)}</div>${draft.error?`<div class="action-error" role="alert">${e(draft.error)}</div>`:''}${confirm.disabled_reason?`<div class="action-error" role="alert">${e(confirm.disabled_reason)}</div>`:''}<div class="plan-decision-options"><button type="button" class="plan-decision-option" data-action="submit-card-action" data-card-action="confirm" ${disabled?'disabled':''}><span class="plan-choice-mark">1</span><span class="plan-choice-copy"><strong>确认</strong></span><span class="plan-choice-arrow">→</span></button><div class="plan-decision-option plan-feedback-row"><span class="plan-choice-mark">✎</span><label class="plan-choice-copy"><span class="sr-only">提出修改</span><textarea rows="1" data-plan-feedback data-card-field="text" data-card-action="request_changes" placeholder="提出修改：告诉 Agent 应该如何调整…" ${changeDisabled?'disabled':''}>${e(feedback)}</textarea></label><button type="button" class="plan-feedback-send" data-action="submit-card-action" data-card-action="request_changes" aria-label="提交修改要求" title="提交修改要求" ${changeDisabled?'disabled':''}>↑</button></div></div>${draft.stale?'<button type="button" class="button small" data-action="refresh-actions" data-id="'+e(card.id)+'">重试加载</button>':''}</form>`;
}
function compactQuestionCard(card,draft,position,total,locked){
  const answer=card.actions?.find(action=>action.id==='answer');if(!answer)return null;
  const choice=answer.fields?.find(field=>field.name==='answer'),hasOptions=choice?.type==='select';
  const options=hasOptions?(choice.options??[]).filter(option=>option.value!=='__custom__'):[];
  const feedback=hasOptions?draft.values?.answer?.text??'':draft.values?.answer?.answer??'';
  const disabled=locked||!!answer.disabled_reason;
  return `<form class="action-card plan-decision" data-card-form="${e(card.id)}" data-card-revision="${e(card.revision)}" aria-label="回答问题" novalidate><div class="plan-decision-head"><h3>${e(card.description)}</h3>${decisionNav(position,total)}</div>${draft.error?`<div class="action-error" role="alert">${e(draft.error)}</div>`:''}${answer.disabled_reason?`<div class="action-error" role="alert">${e(answer.disabled_reason)}</div>`:''}<div class="plan-decision-options">${options.map((option,index)=>`<button type="button" class="plan-decision-option" data-action="submit-card-answer" data-value="${e(option.value)}" ${disabled?'disabled':''}><span class="plan-choice-mark">${index+1}</span><span class="plan-choice-copy"><strong>${e(option.label??option.value)}</strong></span><span class="plan-choice-arrow">→</span></button>`).join('')}<div class="plan-decision-option plan-feedback-row"><span class="plan-choice-mark">✎</span><label class="plan-choice-copy"><span class="sr-only">自行填写回答</span><textarea rows="1" data-plan-feedback data-card-field="${hasOptions?'text':'answer'}" data-card-action="answer" placeholder="${hasOptions?'自行填写回答…':'输入你的回答…'}" ${disabled?'disabled':''}>${e(feedback)}</textarea></label><button type="button" class="plan-feedback-send" data-action="submit-custom-answer" aria-label="提交回答" title="提交回答" ${disabled?'disabled':''}>↑</button></div></div>${draft.stale?'<button type="button" class="button small" data-action="refresh-actions" data-id="'+e(card.id)+'">重试加载</button>':''}</form>`;
}
function renderActionCards(){
  const root=$('#action-cards');if(!root)return;
  const all=state.actionCards??[],decisions=all.filter(card=>card.kind==='confirmation'||card.kind==='question');
  if(!decisions.some(card=>card.id===activeDecisionCardId))activeDecisionCardId=decisions[0]?.id??null;
  const position=decisions.findIndex(card=>card.id===activeDecisionCardId);
  const cards=[...(position>=0?[decisions[position]]:[]),...all.filter(card=>!['confirmation','question','queue'].includes(card.kind))];
  const composer=$('#composer-wrap');if(composer)composer.hidden=cards.length>0;root.hidden=!cards.length&&!state.actionsError;if(root.hidden){root.innerHTML='';root.dataset.signature='';return;}
  const entries=cards.map(card=>{const key=actionKey(card.id),draft=actionDrafts.get(key)??actionDraft(card);actionDrafts.set(key,draft);return {card,draft,reason:card.kind==='recovery'?taskIssue(state.status.tasks.find(task=>'task:'+task.id===card.id),state.status):null};});
  const signature=JSON.stringify([actionScope(),state.actionsError,entries,position,decisions.length]);if(root.dataset.signature===signature)return;
  const focused=document.activeElement,form=focused?.closest('[data-card-form]');
  const focus=form?{card:form.dataset.cardForm,field:focused.dataset.cardField,choice:focused.dataset.cardChoice,value:focused.value,start:focused.selectionStart,end:focused.selectionEnd}:null;
  const scroll=root.scrollTop;
  const compactOnly=cards.length===1&&['confirmation','question'].includes(cards[0].kind);
  root.classList.toggle('compact-only',compactOnly);
  root.innerHTML=`${compactOnly?'':`<header class="actions-heading"><div><span class="action-spark">◇</span><strong>待你处理</strong><span class="action-count">${cards.length}</span></div><button type="button" class="button small subtle" data-action="refresh-actions">刷新状态</button></header>`}${state.actionsError?`<div class="action-error" role="alert">待处理事项读取失败：${e(state.actionsError)}。恢复连接并刷新后再操作。</div>`:''}<div class="action-card-list">${entries.map(({card,draft,reason},index)=>{
    const action=card.actions?.find(a=>a.id===draft.selected),locked=draft.inFlight||draft.stale||!!state.actionsError;
    const compact=card.kind==='confirmation'?compactConfirmationCard(card,draft,position,decisions.length,locked):card.kind==='question'?compactQuestionCard(card,draft,position,decisions.length,locked):null;if(compact)return compact;
    return `<form class="action-card" data-card-form="${e(card.id)}" data-card-revision="${e(card.revision)}" novalidate><div class="action-card-top"><span class="action-kind">${e(({confirmation:'阶段确认',question:'需要你的选择',task:'任务恢复',recovery:'任务恢复',queue:'队列请求'})[card.kind]??'待处理事项')}</span><span class="small muted">${index+1} / ${cards.length}</span></div><h3>${e(card.title)}</h3><p class="action-description">${e(card.description)}</p>${reason?issueNotice(reason):''}${card.details?.length?`<dl class="action-details">${card.details.map(detail=>`<div><dt>${e(detail.label)}</dt><dd>${e(detail.value)}</dd></div>`).join('')}</dl>`:''}${card.targets?.length?`<details class="action-targets"><summary>查看固定对象与版本（${card.targets.length}）</summary>${card.targets.map(ref=>`<button type="button" class="data-link" data-action="reference" data-ref="${e(JSON.stringify(ref))}">${e(ref.record_type??ref.type??'记录')} · ${e(ref.record_id??ref.artifact_id??ref.id??'')} ${ref.version!=null?`· v${e(ref.version)}`:''} ↗</button>`).join('')}</details>`:''}${draft.stale?'<div class="action-error" role="alert">状态、配置或固定版本已更新。填写内容已保留，请先核对最新内容，再刷新此卡片。</div>':''}${draft.error?`<div class="action-error" role="alert">${e(draft.error)}</div>`:''}<fieldset class="action-controls" ${locked?'disabled':''}><legend class="sr-only">选择处理方式</legend>${(card.actions??[]).length>1?`<div class="action-choices">${card.actions.map(a=>`<label class="action-choice ${draft.selected===a.id?'selected':''} ${a.disabled_reason?'unavailable':''}"><input type="radio" name="_action" data-card-choice="${e(a.id)}" value="${e(a.id)}" ${draft.selected===a.id?'checked':''} ${a.disabled_reason?'disabled':''}><span><strong>${e(a.label)}</strong>${a.description?`<small>${e(a.description)}</small>`:''}${a.disabled_reason?`<small class="action-disabled">${e(a.disabled_reason)}</small>`:''}</span></label>`).join('')}</div>`:''}${action?`<div class="action-fields">${action.description&&(card.actions?.length===1)?`<p class="small muted">${e(action.description)}</p>`:''}${(action.fields??[]).map(field=>actionFieldHTML(field,action,draft,index)).join('')}</div>`:''}<div class="action-footer"><span class="small muted">${draft.inFlight?'正在提交，请勿重复操作':'提交后按所选操作直接处理'}</span><button type="submit" class="button primary" ${!action||action.disabled_reason?'disabled':''}>${draft.inFlight?'提交中…':e(action?.label??'先选择处理方式')}</button></div>${action?.disabled_reason?`<p class="action-disabled">${e(action.disabled_reason)}</p>`:''}</fieldset>${draft.stale?`<button type="button" class="button small" data-action="refresh-actions" data-id="${e(card.id)}" ${draft.inFlight?'disabled':''}>已核对，刷新此卡片</button>`:''}</form>`;
  }).join('')}</div>`;
  root.dataset.signature=signature;root.scrollTop=scroll;
  if(focus){const current=$$('[data-card-form]',root).find(node=>node.dataset.cardForm===focus.card);const control=current&&$$('input,textarea,select',current).find(node=>focus.field?node.dataset.cardField===focus.field&&(node.type!=='radio'||node.value===focus.value):node.dataset.cardChoice===focus.choice);if(control&&!control.disabled){control.focus({preventScroll:true});if(['text','textarea'].includes(control.type)&&focus.start!=null)control.setSelectionRange(focus.start,focus.end);}}
}
function updateActionInput(input){
  const form=input.closest('[data-card-form]');if(!form)return;
  const draft=actionDrafts.get(actionKey(form.dataset.cardForm));if(!draft||draft.inFlight||draft.stale)return;
  if(input.dataset.cardChoice){draft.selected=input.dataset.cardChoice;draft.error='';renderActionCards();return;}
  const actionId=input.dataset.cardAction??draft.selected;if(!input.dataset.cardField||!actionId)return;
  draft.values[actionId][input.dataset.cardField]=input.type==='checkbox'?input.checked:input.value;draft.error='';
  if(input.type==='radio')renderActionCards();
}
async function submitCardAction(button){
  const form=button.closest('[data-card-form]'),draft=form&&actionDrafts.get(actionKey(form.dataset.cardForm));if(!form||!draft||draft.inFlight||draft.stale)return;
  draft.selected=button.dataset.cardAction;await submitActionCard(form);
}
async function submitCardAnswer(button){
  const form=button.closest('[data-card-form]'),draft=form&&actionDrafts.get(actionKey(form.dataset.cardForm));
  if(!form||!draft||draft.inFlight||draft.stale)return;
  draft.selected='answer';draft.values.answer.answer=button.dataset.value;
  await submitActionCard(form);
}
async function submitCustomAnswer(button){
  const form=button.closest('[data-card-form]'),card=form&&state.actionCards.find(item=>item.id===form.dataset.cardForm);
  const draft=form&&actionDrafts.get(actionKey(form.dataset.cardForm));
  if(!form||!card||!draft||draft.inFlight||draft.stale)return;
  draft.selected='answer';
  if(card.actions?.[0]?.fields?.find(field=>field.name==='answer')?.type==='select')draft.values.answer.answer='__custom__';
  await submitActionCard(form);
}
async function submitActionCard(form){
  const id=form.dataset.cardForm,scope=actionScope(),key=actionKey(id),card=state.actionCards.find(c=>c.id===id),draft=actionDrafts.get(key);
  if(!card||!draft||draft.inFlight)return;
  const result=actionSubmission(card,draft);if(state.actionsError||result.error){draft.error=state.actionsError?'请先刷新待处理事项。':result.error;renderActionCards();return;}
  const path=cpath();draft.inFlight=true;draft.error='';renderActionCards();
  try{const receipt=await request(`${path}/actions`,{method:'POST',body:result.body});actionDrafts.delete(key);if(scope!==actionScope())return;state.actionCards=state.actionCards.filter(c=>c.id!==id);if(form.closest('#modal'))$('#modal').close();renderActionCards();toast(receipt.message??'操作已提交，正在更新对话与状态');await Promise.all([refreshHistory(true),refreshStatus()]);}
  catch(error){const current=actionDrafts.get(key)??draft;current.error=error.message;current.inFlight=false;const changed=['action_stale','action_not_found','action_unavailable'].includes(error.code);if(changed){current.stale=true;if(scope===actionScope())await refreshActions();}if(scope===actionScope()){renderActionCards();if(!changed||!['confirmation','question'].includes(card.kind))toast(error.message,true);}}
  finally{const current=actionDrafts.get(key);if(current)current.inFlight=false;if(scope===actionScope())renderActionCards();}
}
async function focusActions(id=null){await refreshActions();const root=$('#action-cards');if(root.hidden){toast('当前没有可执行的待处理操作。请查看运行记录或等待状态更新。');return;}const card=id&&$$('[data-card-form]',root).find(node=>node.dataset.cardForm===id);(card??root).scrollIntoView({behavior:'smooth',block:'nearest'});root.focus({preventScroll:true});}
async function loadProjects(){const data=await request('/projects');state.projects=items(data).map(obj);renderProjects();}
function renderProjects(){$('#projects').innerHTML=state.projects.length?state.projects.map(p=>`<div class="project-item"><button class="project-link ${state.project?.id===p.id?'active':''}" data-action="select-project" data-id="${e(p.id)}" title="${e(p.title)}"><span>▧</span><span>${e(p.title||'未命名项目')}</span></button><button class="project-delete" data-action="delete-project" data-id="${e(p.id)}" aria-label="删除项目 ${e(p.title||'未命名项目')}" title="永久删除项目">×</button></div>`).join(''):'<p class="muted small" style="padding:10px">还没有项目<br>从一部故事开始。</p>';}
function renderConversations(){$('#conversations').innerHTML=state.conversations.map(c=>`<button class="conversation-link ${state.conversation?.id===c.id?'active':''}" data-action="select-conversation" data-id="${e(c.id)}">${e(c.title||'改编会话')}</button>`).join('');}
async function selectProject(id){const epoch=(state.projectEpoch??0)+1;state.projectEpoch=epoch;state.eventSource?.close();state.eventSource=null;clearTimeout(scheduleRefresh.timer);state.project=state.projects.find(p=>p.id===id);if(!state.project)return;sessionStorage.setItem('branch-project',id);state.conversation=null;state.actionCards=[];state.actionsError='';renderActionCards();state.history=[];state.activity=[];state.activityTruncatedBefore=false;state.replyStreams.clear();state.chatEventSequences.clear();state.chatEventRows.clear();state.status={tasks:[],runs:[],queue:[],artifacts:[]};state.sequence=0;state.events=[];state.record=null;state.view='list';state.config=null;state.configDirty=false;state.draft=null;$('#project-title').textContent=state.project.title;renderProjects();$('#thread').innerHTML='<p class="loading">正在读取历史…</p>';const cs=await request(`${ppath()}/conversations`);if(epoch!==state.projectEpoch)return;state.conversations=items(cs).map(obj);renderConversations();if(state.conversations.length)await selectConversation(state.conversations[0].id);else renderThread();if(epoch!==state.projectEpoch)return;await refreshStatus();if(epoch!==state.projectEpoch)return;startEvents();if(state.panelOpen)await renderPanel();}
async function selectConversation(id){state.conversation=state.conversations.find(c=>c.id===id);state.history=[];state.activity=[];state.activityTruncatedBefore=false;state.replyStreams.clear();state.chatEventSequences.clear();state.chatEventRows.clear();state.historyCursor=null;state.historyAll=false;state.actionCards=[];state.actionsError="";renderActionCards();renderConversations();await Promise.all([refreshHistory(true),refreshActivity(),refreshActions()]);renderStatus();}
function pruneReplyStreams(){
  for(const [id,stream] of state.replyStreams){
    if(!stream.runId)continue;
    if(state.history.some(row=>row.role==='assistant'&&row.visibility!=='internal'&&row.run_id===stream.runId&&(!stream.startedAt||!row.created_at||row.created_at>=stream.startedAt))){state.replyStreams.delete(id);continue;}
    const run=state.status.runs.find(row=>row.id===stream.runId);
    if(['failed','paused','stopped','waiting_user','interrupted'].includes(run?.state)){stream.failed=true;stream.text='';}
    else if(run?.state==='succeeded')state.replyStreams.delete(id);
  }
}
async function refreshHistory(scroll=false){if(!state.conversation)return;const convId=state.conversation.id;const data=await request(`${cpath()}/history?limit=100&tail=true`);if(state.conversation?.id!==convId)return;const fresh=items(data).map(obj);state.history=[...new Map([...state.history,...fresh].map(r=>[r.id,r])).values()];if(!state.historyAll)state.historyCursor=data.previous_cursor??(data.start_cursor>0?String(Math.max(0,data.start_cursor-100)):null);pruneReplyStreams();renderThread(scroll);}
function applyChatEvent(row){
  if(row.conversation_id!==state.conversation?.id)return false;
  const payload=row.payload??{},name=row.event_name;
  if(!['chat.activity','chat.reply.started','chat.reply.delta','chat.reply.completed','chat.reply.failed'].includes(name))return false;
  if(Number.isSafeInteger(row.sequence)){
    if(state.chatEventSequences.has(row.sequence))return false;
    state.chatEventSequences.add(row.sequence);
    if(state.chatEventSequences.size>3000)for(const old of [...state.chatEventSequences].slice(0,1000))state.chatEventSequences.delete(old);
    state.chatEventRows.set(row.sequence,row);
  }
  if(name==='chat.activity'){
    if(typeof payload.text!=='string'||!payload.text.trim())return false;
    if(!state.activity.some(item=>item.id===row.id||item.sequence===row.sequence))state.activity.push(row);
    state.activity.sort((a,b)=>(a.sequence??0)-(b.sequence??0));
    return true;
  }
  if(!name?.startsWith('chat.reply.')||typeof payload.stream_id!=='string'||!payload.stream_id)return false;
  const id=payload.stream_id;
  let stream=state.replyStreams.get(id);
  if(!stream){stream={id,text:'',runId:row.run_id,taskId:row.task_id,startedAt:row.created_at,sequence:row.sequence,partialBefore:name!=='chat.reply.started',completed:false,failed:false};state.replyStreams.set(id,stream);}
  if(name==='chat.reply.delta'&&typeof payload.delta==='string')stream.text+=payload.delta;
  if(name==='chat.reply.completed')stream.completed=true;
  if(name==='chat.reply.failed'){stream.failed=true;stream.text='';}
  pruneReplyStreams();
  return true;
}
function rebuildChatEvents(){
  const rows=[...state.chatEventRows.values()].sort((a,b)=>a.sequence-b.sequence);
  state.activity=[];state.replyStreams.clear();state.chatEventSequences.clear();
  for(const row of rows)applyChatEvent(row);
}
async function backfillReplyPrefixes(projectId,conversationId){
  // A bounded tail can begin in the middle of a long reply. Recover the
  // preceding chunks before showing it as a complete-looking assistant reply.
  for(let page=0;page<100&&state.activityTruncatedBefore&&[...state.replyStreams.values()].some(stream=>stream.partialBefore);page++){
    const earliest=Math.min(...state.chatEventRows.keys());
    const data=await request(`${cpath()}/activity?limit=500&before=${earliest}`);
    if(state.project?.id!==projectId||state.conversation?.id!==conversationId)return;
    if(!data.events?.length)break;
    for(const row of data.events)if(Number.isSafeInteger(row.sequence))state.chatEventRows.set(row.sequence,row);
    rebuildChatEvents();
    state.activityTruncatedBefore=Boolean(data.truncated_before);
  }
}
async function refreshActivity(){
  if(!state.conversation)return;
  const projectId=state.project.id,conversationId=state.conversation.id;
  const data=await request(`${cpath()}/activity?limit=200&tail=true`);
  if(state.project?.id!==projectId||state.conversation?.id!==conversationId)return;
  for(const row of (data.events??[]).sort((a,b)=>(a.sequence??0)-(b.sequence??0)))applyChatEvent(row);
  state.activityTruncatedBefore=Boolean(data.truncated_before);
  if(!state.eventSource)state.sequence=Math.max(state.sequence,Number(data.last_sequence??data.next_sequence??0));
  if(state.activityTruncatedBefore&&[...state.replyStreams.values()].some(stream=>stream.partialBefore))await backfillReplyPrefixes(projectId,conversationId);
  if(state.project?.id!==projectId||state.conversation?.id!==conversationId)return;
  renderThread();
}
async function loadOlderActivity(){
  if(!state.conversation||!state.activityTruncatedBefore||!state.chatEventRows.size)return;
  const projectId=state.project.id,conversationId=state.conversation.id;
  const earliest=Math.min(...state.chatEventRows.keys());
  const data=await request(`${cpath()}/activity?limit=200&before=${earliest}`);
  if(state.project?.id!==projectId||state.conversation?.id!==conversationId)return;
  for(const row of data.events??[])if(Number.isSafeInteger(row.sequence))state.chatEventRows.set(row.sequence,row);
  rebuildChatEvents();
  state.activityTruncatedBefore=Boolean(data.truncated_before);
  renderThread();
}
function markdownDownloadURL(row){
  const ref=row.artifact_ref;
  if(!row.markdown_download_url||!ref||typeof ref.record_id!=='string'||!/^[1-9]\d*$/.test(String(ref.version)))return null;
  const expected=`${API+ppath()}/artifacts/${encodeURIComponent(ref.record_id)}/versions/${ref.version}/download.md`;
  return row.markdown_download_url===expected?expected:null;
}
function renderThread(scroll=false){
  const thread=$('#thread');
  const entries=[
    ...state.history.filter(r=>r.visibility!=='internal').map(row=>({kind:'message',row,createdAt:row.created_at,order:row.sequence??0})),
    ...state.activity.map(row=>({kind:'activity',row,createdAt:row.created_at,order:row.sequence??0})),
    ...[...state.replyStreams.values()].map(row=>({kind:'stream',row,createdAt:row.startedAt,order:row.sequence??0})),
  ].sort((a,b)=>{
    const at=Date.parse(a.createdAt??'')||0,bt=Date.parse(b.createdAt??'')||0;
    return at-bt||a.order-b.order||a.kind.localeCompare(b.kind);
  });
  if(!entries.length){
    thread.innerHTML=`<section class="welcome"><div class="welcome-symbol">⌘</div><div class="eyebrow">${state.project?'新的故事，从这里开始':'从一条故事线，到无数种可能'}</div><h2>让故事，长出分支。</h2><p>放入你的小说或剧本，一起梳理人物、设计选择，<br>逐步完成可交付的互动剧本。</p><div class="welcome-cards"><button data-action="${state.project?'source':'new-project'}"><span>01</span><strong>${state.project?'导入原作全文':'创建改编项目'}</strong><small>保存完整原作，开始第一步分析</small></button><button data-action="focus-input"><span>02</span><strong>聊聊你的想法</strong><small>角色、风格，或希望保留的情节</small></button></div><p class="welcome-note">创作决策、修改和阶段确认，都在对话中完成。</p></section>`;
    return;
  }
  const bottom=thread.scrollHeight-thread.scrollTop-thread.clientHeight<100;
  thread.innerHTML=(state.historyCursor?'<button class="button small" data-action="older-history">加载更早的对话</button>':'')+
    (state.activityTruncatedBefore?'<button class="button small subtle" data-action="older-activity">加载更早的运行记录</button>':'')+
    entries.map(entry=>{
      if(entry.kind==='activity'){
        const row=entry.row,kind=['source','coordinator','agent','tool','artifact','error'].includes(row.payload?.kind)?row.payload.kind:'info';
        return `<div class="chat-activity ${kind}" data-event-sequence="${e(row.sequence)}"><span class="chat-activity-mark" aria-hidden="true"></span><span>${e(row.payload.text)}</span><time>${date(row.created_at)}</time></div>`;
      }
      if(entry.kind==='stream'){
        const stream=entry.row,status=stream.failed?'回复生成中断':stream.completed?'流式草稿 · 正在保存':'流式草稿 · 保存后为准';
        return `<article class="message assistant streaming" data-stream-id="${e(stream.id)}"><div class="message-label">分支 Agent<time>${date(stream.startedAt)}</time><span class="stream-status">${status}</span></div><div class="message-body markdown">${stream.failed?'<span class="stream-placeholder">本次回复未完成，后续状态会继续显示在对话中。</span>':`${stream.partialBefore?'<p class="stream-placeholder">较早的回复片段尚未加载；可加载更早的运行记录。</p>':''}${stream.text?renderMarkdown(stream.text):'<span class="stream-placeholder">正在生成回复…</span>'}`}</div></article>`;
      }
      const r=entry.row;
      const content=r.display_text??textContent(publicView(r.content??r.text));
      const markdown=r.display_format==='markdown'||r.role==='assistant';
      const download=markdownDownloadURL(r);
      return `<article class="message ${e(r.role)}"><div class="message-label">${r.role==='user'?'你':r.role==='assistant'?'分支 Agent':'系统记录'}<time>${date(r.created_at)}</time></div><div class="message-body ${markdown?'markdown':''}">${markdown?renderMarkdown(content):e(content)}</div>${download?`<a class="button small markdown-download" href="${e(download)}" download>下载 Markdown · v${e(r.artifact_ref.version)}</a>`:''}${r.content?.storage==='blob'?`<button class="button small" data-action="blob" data-id="${e(r.content.blob_id)}">展开完整内容</button>`:''}<button class="data-link" data-action="record" data-id="${e(r.id)}">查看来源与记录 ↗</button></article>`;
    }).join('');
  if(scroll||bottom)thread.scrollTop=thread.scrollHeight;
}
async function refreshStatus(){if(!state.project)return;const id=state.project.id;const data=await request(`${ppath()}/status`);if(state.project?.id!==id)return;state.status={...data,tasks:items(data.tasks).map(obj),runs:items(data.runs).map(obj),queue:items(data.queue).map(obj),artifacts:items(data.artifacts).map(obj),artifact_states:items(data.artifact_states).map(obj)};pruneReplyStreams();renderThread();renderStatus();renderArtifacts();await refreshActions();}
function renderStatus(){const task=activeTask(),run=activeRun(),issue=taskIssue(task,state.status);let html='<span class="status-dot"></span><span>准备好继续你的故事</span>';if(task){const stage=task.scope?.stage;html=`<span class="status-dot ${task.state==='running'?'active':''}"></span><span>${e(stateLabels[task.state]||task.state)}</span>${stage?`<span class="progress-stage">STEP ${String(stage).padStart(2,'0')} · ${e(stageNames[stage-1]||'')}</span>`:''}${issue?`<span class="muted" data-error-code="${e(issue.code)}">${issueText(issue)}</span><button class="button small subtle" data-action="record" data-id="${e(issue.run_id??issue.task_id)}">错误详情</button>`:''}<span class="spacer"></span><button class="button small subtle" data-action="record" data-id="${e(task.id)}">运行详情</button>${['running','stopping','queued','waiting_user'].includes(task.state)?`<button class="button small danger" data-action="stop-task" data-id="${e(task.id)}" ${task.state==='stopping'?'disabled':''}>${task.state==='stopping'?'正在停止…':'停止'}</button>`:`<button class="button small" data-action="focus-actions" data-id="task:${e(task.id)}">处理暂停</button>`}`;}const queueCount=pendingQueueCount(state.status.queue,state.conversation?.id);if(queueCount)html+=`<button class="button small subtle" data-action="queue-view">队列 ${queueCount}</button>`;$('#run-status').innerHTML=html;$('#mode-wrap').hidden=!run;if(!run)$('#message-mode').value='';$('#message-input').disabled=!state.conversation;$('#send-button').disabled=!state.conversation;}
function deliveryRef(){const value=state.status.latest_delivery;const ref=value?.artifact_ref??value?.ref??value;return ref?{id:ref.artifact_id??ref.record_id??ref.id,version:ref.version??ref.latest_version}:null;}
function renderArtifacts(){const shelf=$('#artifact-shelf'),delivery=deliveryRef();if(!shelf)return;shelf.hidden=!delivery;shelf.innerHTML=delivery?`<button class="artifact-chip" data-action="artifact" data-id="${e(delivery.id)}" data-version="${e(delivery.version)}"><strong>✓ 查看最终交付</strong><small>固定 Project JSON · v${e(delivery.version)}</small></button><a class="artifact-chip" href="${API+ppath()}/artifacts/${encodeURIComponent(delivery.id)}/versions/${encodeURIComponent(delivery.version)}/download"><strong>↓ 下载最终互动剧本</strong><small>Project JSON · 已保存 v${e(delivery.version)}</small></a>`:'';}
function scheduleRefresh(){clearTimeout(scheduleRefresh.timer);scheduleRefresh.timer=setTimeout(async()=>{if(state.refreshing){state.refreshPending=true;return;}state.refreshing=true;try{await Promise.all([refreshStatus(),refreshHistory()]);if(state.panelOpen&&state.panel==='data'&&state.view==='list'){await loadArtifactData(false,true,true);$('#new-records')?.removeAttribute('hidden');}}catch(error){toast(error.message,true);}finally{state.refreshing=false;if(state.refreshPending){state.refreshPending=false;scheduleRefresh();}}},250);}
function scheduleChatRender(){clearTimeout(scheduleChatRender.timer);scheduleChatRender.timer=setTimeout(()=>renderThread(),70);}
function startEvents(){state.eventSource?.close();if(!state.project)return;const source=new EventSource(API+ppath()+`/events?after=${state.sequence}`,{withCredentials:true});state.eventSource=source;source.onopen=()=>{$('#connection').textContent='实时同步';$('#connection').className='connection live';};const receive=event=>{let data;try{data=JSON.parse(event.data);}catch{return;}const n=Number(event.lastEventId||data.sequence||0);if(n&&n<=state.sequence)return;state.sequence=Math.max(n,state.sequence);if(data.event_name!=='chat.reply.delta'){state.events.push(data);state.events=state.events.slice(-100);}if(applyChatEvent(data))scheduleChatRender();if(!['chat.reply.started','chat.reply.delta'].includes(data.event_name))scheduleRefresh();};source.onmessage=receive;for(const name of ['runtime_event','event','update'])source.addEventListener(name,receive);source.onerror=()=>{$('#connection').textContent='连接恢复中';$('#connection').className='connection';};}
function showLogin(){state.eventSource?.close();modal('进入你的故事工作区',`<div class="login-mark">⌘</div><p class="modal-note">使用本地开发账号登录。会话、创作版本和任务进度由服务端保存。</p><form id="login-form"><label class="field"><span>账号</span><input name="username" autocomplete="username" value="developer" required></label><label class="field"><span>密码</span><input name="password" type="password" autocomplete="current-password" required></label><div id="login-error" class="notice error" hidden></div><div class="modal-footer"><button class="button primary" type="submit">登录工作区 →</button></div></form>`);}
async function init(){try{state.account=await request('/auth/me');$('#account-name').textContent=state.account.display_name||state.account.account_id;await loadProjects();const old=sessionStorage.getItem('branch-project');if(state.projects.length)await selectProject(state.projects.some(p=>p.id===old)?old:state.projects[0].id);else renderStatus();}catch(error){if(error.status===401||error.status===403)showLogin();else{showLogin();toast(error.message,true);}}}
function showCreate(kind){if(!state.account){showLogin();return;}if(kind==='conversation'&&!requireProject())return;modal(kind==='project'?'新建改编项目':'新建会话',`<p class="modal-note">${kind==='project'?'为一部作品保存独立的原作、创作决策与产物版本。':'在当前项目里开启新的对话，项目记录持续保留。'}</p><form id="create-form" data-kind="${kind}"><label class="field"><span>${kind==='project'?'项目':'会话'}名称</span><input name="title" autofocus required maxlength="150" placeholder="例如：雾港来信"></label><div class="modal-footer"><button class="button subtle" type="button" data-action="close-modal">取消</button><button class="button primary" type="submit">创建 →</button></div></form>`);}
function showDeleteProject(id){const project=state.projects.find(item=>item.id===id);if(!project)return;modal('永久删除项目',`<p class="modal-note">将永久删除项目“${e(project.title)}”的全部会话、原作、产物、运行记录和附件。此操作无法撤销。</p><form id="delete-project-form" data-id="${e(id)}"><div class="notice error">账号全局配置会保留，其他项目不受影响。</div><div class="modal-footer"><button class="button subtle" type="button" data-action="close-modal">取消</button><button class="button danger" type="submit">确认永久删除</button></div></form>`);}
function sourceModal(){if(!requireProject())return;if(!state.conversation){showCreate('conversation');return;}modal('导入原作全文',`<p class="modal-note">选择文件或粘贴一部完整作品。原作会原样保存；预算检查通过后自动启动 Step1，两路 Agent 的进展会出现在对话中。</p><form id="source-form"><label class="field"><span>作品名称</span><input name="name" placeholder="原作全文" maxlength="180"></label><div class="source-drop"><label>选择原作文件<input type="file" name="file" accept=".txt,.md,.markdown,.docx,text/plain,text/markdown,application/vnd.openxmlformats-officedocument.wordprocessingml.document"></label><small class="muted">支持 TXT、Markdown、DOCX · 最大 10 MB</small></div><label class="field"><span>或粘贴全文</span><textarea name="text" rows="8" placeholder="在这里粘贴小说或剧本正文…"></textarea></label><div id="source-result"></div><div class="modal-footer"><button class="button subtle" type="button" data-action="close-modal">取消</button><button type="submit" class="button primary">导入并开始 Step1</button></div></form>`);}
function sourceReceiptHTML(result){
 const branches=result.step1_branches??{};
 const entries=[['global','作品事件视图 Agent'],['character','主要人物事件视图 Agent']].filter(([view])=>branches[view]);
 if(!entries.length)return `<div class="notice ${result.step1_started?'':'warning'}">${result.step1_started?'原作已保存，Step1 已启动。':'原作已保存，但当前模型预算不足，Step1 未启动。请调整配置后重试。'}</div>`;
 const cards=entries.map(([view,label])=>{const branch=branches[view],mode=branch.source_mode==='sliding_window'?'独立滑动窗口':'全文输入';return `<div class="metric"><small>${e(label)} · ${e(branch.agent_name??branch.agent_key??view)}</small><strong>${mode}</strong><small>原文 ${number(branch.source_tokens)} tokens · 触发阈值 ${number(branch.source_window_threshold)} · 安全全文预算 ${number(branch.safe_full_budget)}</small>${branch.source_mode==='sliding_window'?`<small>窗口大小 ${number(branch.source_window_tokens)} tokens</small>`:''}</div>`;}).join('');
 return `<div class="notice ${result.step1_started?'':'warning'}">${result.step1_started?'原作已保存，Step1 两路已开始处理。':'原作已保存，但至少一路当前预算不足，Step1 未启动。请调整配置后重试。'}</div><div class="metric-grid">${cards}</div><details><summary class="small muted">查看提交记录</summary><pre class="code-box">${e(JSON.stringify(publicView(result),null,2))}</pre></details>`;
}
async function togglePanel(open){const next=open??!state.panelOpen;if(state.panelOpen&&!next&&state.panel==='config')await flushWorkingDraft();state.panelOpen=next;$('#tools-page').hidden=!state.panelOpen;$('#chat-sidebar').hidden=state.panelOpen;$('#chat-main').hidden=state.panelOpen;$('#app').classList.toggle('tools-open',state.panelOpen);$('#panel-toggle').setAttribute('aria-expanded',String(state.panelOpen));if(state.panelOpen)await renderPanel();}
async function renderPanel(){$$('.panel-tabs button').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.tab===state.panel)));if(state.panel==='config'){if(!state.config)await loadConfig();renderConfig();return;}if(!state.project){await loadArtifactData(true,false);$('#panel-content').innerHTML=artifactMonitor()+ '<p class="empty">创建项目后可查看 Agent Run。</p>';return;}if(state.view==='detail'&&state.record)renderDetail();else await loadRuntimeData();}
function runtimeProjectControl(){return `<label class="runtime-project field"><span>运行数据项目</span><select id="runtime-project-select" aria-label="运行数据项目">${state.projects.map(p=>`<option value="${e(p.id)}" ${p.id===state.project?.id?'selected':''}>${e(p.title)}</option>`).join('')}</select></label>`;}
function artifactMonitor(){return artifactMonitorHTML({entries:state.artifactEntries,selectedKinds:state.artifactVisibleKinds,defaults:state.artifactDefaults,expandedKinds:state.artifactExpandedKinds,error:state.artifactError,defaultsError:state.artifactDefaultsError,project:state.project,loading:state.artifactLoading});}
function renderArtifactMonitor(){const root=$('#artifact-monitor');if(root)root.outerHTML=artifactMonitor();}
const artifactEntryKey=entry=>entry.artifact_id??`${entry.kind}:${entry.chapter_id??''}`;
const artifactListPath=kinds=>`${ppath()}/artifacts${kinds.length?`?kinds=${encodeURIComponent(kinds.join(','))}`:''}`;
async function loadProjectArtifacts(metadataFirst){
  const kinds=state.artifactVisibleKinds;
  if(!metadataFirst)return request(artifactListPath(kinds));
  const metadata=await request(artifactListPath([]));
  const previous=new Map(state.artifactEntries.map(entry=>[artifactEntryKey(entry),entry]));
  const changed=new Set((metadata.artifacts??[]).filter(entry=>{
    if(!kinds.includes(entry.kind))return false;
    const old=previous.get(artifactEntryKey(entry));
    return !old||old.version!==entry.version||(entry.version!=null&&old.content==null&&entry.has_content!==false);
  }).map(entry=>entry.kind));
  const fetched=changed.size?await request(artifactListPath([...changed])):null;
  const fresh=new Map((fetched?.artifacts??[]).map(entry=>[artifactEntryKey(entry),entry]));
  return {artifacts:(metadata.artifacts??[]).map(entry=>({...entry,content:changed.has(entry.kind)?fresh.get(artifactEntryKey(entry))?.content??null:previous.get(artifactEntryKey(entry))?.content??null}))};
}
async function loadArtifactData(refreshDefaults=false,render=true,metadataFirst=false){
  const projectId=state.project?.id??null;
  const sequence=(state.artifactRequestSeq??0)+1;state.artifactRequestSeq=sequence;
  if(state.artifactProjectId!==projectId){state.artifactProjectId=projectId;state.artifactEntries=[];state.artifactError='';state.artifactExpandedKinds=[];}
  state.artifactLoading=true;
  const [artifacts,defaults]=await Promise.allSettled([
    projectId?loadProjectArtifacts(metadataFirst):Promise.resolve({artifacts:[]}),
    refreshDefaults||state.artifactDefaults===null?request('/artifact-defaults'):Promise.resolve(state.artifactDefaults)
  ]);
  if(projectId!==(state.project?.id??null)||sequence!==state.artifactRequestSeq)return;
  if(artifacts.status==='fulfilled'){state.artifactEntries=artifacts.value.artifacts??[];state.artifactError='';}
  else state.artifactError=`中间产物读取失败：${artifacts.reason?.message??'请重试'}`;
  if(defaults.status==='fulfilled'){state.artifactDefaults=defaults.value;state.artifactDefaultsError='';}
  else state.artifactDefaultsError=`新项目默认值读取失败：${defaults.reason?.message??'请重试'}`;
  state.artifactLoading=false;
  if(render)renderArtifactMonitor();
}
function editArtifact(kind,scope){
  if(!editableArtifactKinds.includes(kind))return;
  const entry=state.artifactEntries.find(item=>item.kind===kind);
  if(scope==='project'&&!state.project)return;
  const value=scope==='default'?state.artifactDefaults?.[kind]:entry?.content;
  const starter=state.artifactDefaults?.starters?.[kind]??{result_kind:'ready',payload:{},questions:[],evidence_refs:[],notes:[]};
  const initial=JSON.stringify(value??starter,null,2);
  const title=artifactLabels[kind]??kind;
  modal(scope==='default'?`编辑新项目默认值 · ${title}`:`编辑当前项目产物 · ${title}`,`<p class="modal-note">${scope==='default'?'此内容将复制到以后新建项目的同名中间产物，已有项目不受影响。':`保存后成为当前项目 ${e(kind)} 的新版本，后续阶段读取这份产物。`}请输入完整 JSON 产物内容。</p><form id="artifact-editor-form" data-kind="${e(kind)}" data-scope="${e(scope)}" data-version="${e(entry?.version??'')}"><label class="field"><span>产物内容 · JSON</span><textarea class="code-editor artifact-editor" name="content" rows="20" spellcheck="false" required>${e(initial)}</textarea></label><div class="modal-footer"><button type="button" class="button subtle" data-action="close-modal">取消</button><button type="submit" class="button primary">保存${scope==='default'?'默认值':'新版本'}</button></div></form>`);
}
async function saveArtifactEditor(form){
  const kind=form.dataset.kind,scope=form.dataset.scope;
  if(!editableArtifactKinds.includes(kind))throw new Error('此类型不支持手工编辑');
  let content;
  try{content=JSON.parse(form.elements.content.value);}catch{throw new Error('请输入有效的 JSON 内容');}
  if(!content||typeof content!=='object'||Array.isArray(content))throw new Error('产物内容必须是 JSON 对象');
  try{
    if(scope==='default')await request('/artifact-defaults',{method:'PUT',body:{[kind]:content}});
    else await request(`${ppath()}/artifacts/${encodeURIComponent(kind)}`,{method:'PUT',body:{content,expected_version:form.dataset.version===''?null:Number(form.dataset.version)}});
  }catch(error){if(error.status===409){await loadArtifactData(scope==='default',true);if(scope==='project'){form.dataset.version=String(state.artifactEntries.find(item=>item.kind===kind)?.version??'');let notice=$('.action-error',form);if(!notice){notice=document.createElement('p');notice.className='action-error';notice.setAttribute('role','alert');form.prepend(notice);}notice.textContent='产物已出现新版本。编辑内容已保留；请核对最新版本后再次保存。';}throw new Error('产物已更新，请核对后再次保存。');}throw error;}
  $('#modal').close();await loadArtifactData(scope==='default',true);toast(scope==='default'?'新项目默认值已保存':'中间产物的新版本已保存');
}
async function clearArtifactDefault(kind){
  if(!editableArtifactKinds.includes(kind))return;
  await request('/artifact-defaults',{method:'PUT',body:{[kind]:null}});
  await loadArtifactData(true,true);toast('新项目默认值已清空');
}
async function loadRuntimeData(append=false){
  if(!state.project)return;
  const projectId=state.project.id;
  if(state.runtimeProjectId!==projectId){state.runtimeProjectId=projectId;state.runtimeData=null;state.runtimeDetail=null;state.runtimeRunId=null;}
  if(!append)$('#panel-content').innerHTML=runtimeProjectControl()+'<p class="loading">正在读取 Agent Run…</p>';
  const cursor=append?state.runtimeData?.next_cursor:null;
  const [data]=await Promise.all([request(`${ppath()}/run-debug?limit=30${cursor?`&cursor=${encodeURIComponent(cursor)}`:''}`),loadArtifactData(false,false)]);
  if(projectId!==state.project?.id)return;
  state.runtimeData={items:append?[...(state.runtimeData?.items??[]),...items(data)]:items(data),next_cursor:data.next_cursor??null};
  if(state.runtimeData.items.length&&(!state.runtimeRunId||(!append&&!state.runtimeData.items.some(item=>item.run.id===state.runtimeRunId)))){
    state.runtimeRunId=state.runtimeData.items[0].run.id;state.runtimeDetail=null;
  }
  renderRuntimeData();
  if(state.runtimeRunId&&!state.runtimeDetail)await openRunDebug(state.runtimeRunId);
}
function renderRuntimeData(){
  $('#panel-content').innerHTML=runtimeProjectControl()+artifactMonitor()+runtimeHTML(state.runtimeData,state.runtimeRunId,state.runtimeDetail);
}
async function openRunDebug(id){
  if(!state.project)return;
  const projectId=state.project.id;
  state.runtimeRunId=id;state.runtimeDetail=null;renderRuntimeData();
  const detail=await request(`${ppath()}/run-debug/${encodeURIComponent(id)}`);
  if(projectId!==state.project?.id||state.runtimeRunId!==id)return;
  state.runtimeDetail=detail;renderRuntimeData();
}

function filtersHTML(){return `${runtimeProjectControl()}<p class="panel-intro">查看所选项目已保存的运行依据。浏览记录不会触发生成或改变确认。</p><form id="record-filters"><div class="filter-grid"><select class="wide" name="record_type" aria-label="记录类型">${Object.entries(recordTypes).map(([k,v])=>`<option value="${k}" ${state.recordType===k?'selected':''}>${v} · ${k}</option>`).join('')}</select><input name="stage" type="number" min="1" max="11" placeholder="阶段 1–11" aria-label="阶段"><input name="chapter_id" placeholder="章节 ID" aria-label="章节 ID"><input name="task_id" placeholder="任务 ID" aria-label="任务 ID"><input name="run_id" placeholder="运行 ID" aria-label="运行 ID"><input name="agent_key" placeholder="Agent" aria-label="Agent"><input name="state" placeholder="状态（英文值）" aria-label="状态"><label class="small">起始时间<input type="datetime-local" name="created_from" aria-label="起始时间"></label><label class="small">截止时间<input type="datetime-local" name="created_to" aria-label="截止时间"></label><input class="wide" name="record_id" placeholder="精确记录 ID，可直接跳转" aria-label="精确记录 ID"></div><div class="inline-actions"><button class="button small primary" type="submit">筛选记录</button><button class="button small" type="button" data-action="refresh-records">重置 / 刷新</button><button id="new-records" class="button small" type="button" data-action="refresh-records" hidden>有新记录 ↻</button></div></form><div id="usage-summary"></div><div id="records-list"></div><div class="inline-actions" style="margin-top:15px"><button class="button small" id="load-more" data-action="more-records" hidden>加载下一页</button></div>`;}
async function loadRecords(query=null,append=false){if(!state.project){$('#panel-content').innerHTML=runtimeProjectControl()+'<p class="empty">选择一个项目后查看运行数据。</p>';return;}const projectId=state.project.id;const epoch=(state.recordEpoch??0)+1;state.recordEpoch=epoch;state.view='list';if(!append&&!query){$('#panel-content').innerHTML=filtersHTML();}if(!append){state.records=[];state.cursor=null;}const params=query??new URLSearchParams({record_type:state.recordType,limit:'50'});params.set('record_type',state.recordType);params.set('limit','50');if(append&&state.cursor)params.set('cursor',state.cursor);state.recordQuery=new URLSearchParams(params);const data=await request(`${ppath()}/records?${params}`);if(projectId!==state.project?.id||epoch!==state.recordEpoch)return;state.records.push(...items(data).map(obj));state.cursor=data.next_cursor??null;renderRecords();}
function renderRecords(){if(!$('#records-list'))return;renderUsageSummary();$('#records-list').innerHTML=state.records.length?state.records.map(r=>`<button class="record-row" data-action="record" data-id="${e(r.id)}"><div class="row-top"><strong>${e(recordTitle(r))}</strong>${badge(r.state??r.confirmation_status??r.dependency_status??r.kind??'已保存')}</div><div class="row-bottom"><span>${e(short(r.id))}${r.version?` · v${e(r.version)}`:''}${r.scope?.stage?` · Step${r.scope.stage}`:''}</span><time>${date(r.created_at)}</time></div></button>`).join(''):'<p class="empty">当前筛选下暂无记录。<br>执行任务后，真实记录会出现在这里。</p>';$('#load-more').hidden=!state.cursor;}
function renderUsageSummary(){const target=$('#usage-summary');if(!target)return;if(state.recordType!=='model_call'){target.innerHTML='';return;}const records=[...new Map(state.records.map(r=>[r.id,r])).values()];if(!records.length){target.innerHTML='';return;}const mode=$('#usage-group')?.value??state.usageGroup??'task';state.usageGroup=mode;const groups=new Map();for(const r of records){const run=state.status.runs.find(x=>x.id===r.run_id),task=state.status.tasks.find(x=>x.id===r.task_id);const key=mode==='run'?r.run_id:mode==='agent'?run?.agent_key:mode==='stage'?task?.scope?.stage:r.task_id;const label=key==null?'未提供':mode==='stage'?'Step '+key:mode==='agent'?key:short(key);if(!groups.has(label))groups.set(label,[]);groups.get(label).push(r);}const tokenTotal=key=>{const known=records.filter(r=>r.usage?.[key]!=null);return known.length?number(known.reduce((n,r)=>n+r.usage[key],0))+(known.length<records.length?' + 未知':''):'未提供';};const rows=[...groups.entries()].map(([key,rows])=>{const currencies=new Map();let unknown=0,estimated=0;for(const r of rows){const cost=r.usage?.reported_cost??r.usage?.estimated_cost;if(!cost){unknown++;continue;}if(!r.usage.reported_cost)estimated++;if(!currencies.has(cost.currency))currencies.set(cost.currency,[]);currencies.get(cost.currency).push(cost.amount);}const amount=[...currencies].map(([currency,values])=>currency+' '+decimalSum(values)).join(' / ');return `<tr><td>${e(key)}</td><td>${rows.length}</td><td>${e(amount||'未提供')}${estimated?`<br>含 ${estimated} 次估算`:''}${unknown?`<br>${unknown} 次未知`:''}</td></tr>`;}).join('');target.innerHTML=`<details class="panel-section" open><summary>已加载调用用量 · ${records.length} 次</summary><p class="small muted">仅汇总当前筛选已加载的记录，按调用 ID 去重；不代表完整项目总量。</p><div class="metric-grid"><div class="metric"><small>输入 token</small><strong>${tokenTotal('input_tokens')}</strong></div><div class="metric"><small>输出 token（含推理）</small><strong>${tokenTotal('output_tokens')}</strong></div></div><select id="usage-group" style="margin-top:12px" aria-label="用量分组">${Object.entries({task:'按任务',run:'按运行',agent:'按 Agent',stage:'按阶段'}).map(([k,v])=>`<option value="${k}" ${mode===k?'selected':''}>${v}</option>`).join('')}</select><table class="relation-table"><thead><tr><th>范围</th><th>调用</th><th>已知费用</th></tr></thead><tbody>${rows}</tbody></table></details>`;}
function recordTitle(r){return r.title??r.event_name??r.tool_name??artifactLabels[r.artifact_kind]??r.artifact_kind??r.scope?.description??r.agent_key??recordTypes[r.record_type]??r.record_type??'记录';}
async function openRecord(id){if(!requireProject())return;state.panel='data';state.view='detail';state.record=null;await togglePanel(true);$('#panel-content').innerHTML='<p class="loading">读取固定记录…</p>';const data=await request(`${ppath()}/records/${encodeURIComponent(id)}`);state.record=obj(data);state.recordContent=data.content_resolved??null;state.recordVersion=null;state.view='detail';renderDetail();}
async function openReference(ref){if(!requireProject())return;const data=await request(`${ppath()}/references/resolve`,{method:'POST',body:{ref}});state.record=obj(data.record??data);state.recordContent=data.content??null;state.recordVersion=null;state.view='detail';state.panel='data';await togglePanel(true);renderDetail();}
async function openArtifact(id,version){const data=await request(`${ppath()}/artifacts/${encodeURIComponent(id)}/versions/${encodeURIComponent(version)}`);state.record=obj(data.version??data);if(typeof state.record!=='object')state.record={id: data.id??id,artifact_id:id,version:Number(version),record_type:'artifact_version',content:data.content};state.record.artifact_id??=id;state.record.version??=Number(version);state.recordContent=data.content??state.record.content;state.recordVersion=data.state??null;state.view='detail';state.panel='data';await togglePanel(true);}
function recordHTML(value,key='',depth=0){if(depth>7)return `<pre class="code-box">${e(JSON.stringify(publicView(value),null,2))}</pre>`;if(value===null||value===undefined)return '<span class="muted">未提供</span>';if(typeof value==='object'&&value.record_id)return `<button class="reference" data-action="reference" data-ref="${e(JSON.stringify(value))}">↗ 固定引用 · ${e(short(value.record_id))}${value.version?` · v${e(value.version)}`:''}${value.item_id?` · 条目 ${e(value.item_id)}`:''}${value.json_pointer?` · ${e(value.json_pointer)}`:''}</button>`;if(typeof value==='object'&&value.storage==='blob')return `<button class="button small" data-action="blob" data-id="${e(value.blob_id)}">展开外置内容 · ${e(short(value.blob_id))}</button>`;if(typeof value==='object'&&['inline_text','inline_json'].includes(value.storage))return `<pre class="code-box">${e(textContent(publicView(value)))}</pre>`;if(Array.isArray(value)){if(!value.length)return '<span class="muted">空列表</span>';return value.map((v,i)=>`<details ${value.length<4?'open':''}><summary>第 ${i+1} 项</summary>${recordHTML(v,key.replace(/_ids$/,'_id'),depth+1)}</details>`).join('');}if(typeof value==='object')return `<div class="detail-fields">${Object.entries(publicView(value)).map(([k,v])=>`<dl><dt>${e(labels[k]??k)}<br><span class="small">${e(k)}</span></dt><dd>${recordHTML(v,k,depth+1)}</dd></dl>`).join('')}</div>`;if(typeof value==='string'&&key.endsWith('_id')&&/^[\da-f-]{36}$/i.test(value))return `<button data-action="record" data-id="${e(value)}">${e(value)} ↗</button>`;if(key==='state'||key.endsWith('_status'))return badge(value);if(typeof value==='string'&&value.length>400)return `<details><summary>展开内容 (${value.length.toLocaleString()} 字符)</summary><pre class="code-box">${e(value)}</pre></details>`;return e(typeof value==='boolean'?(value?'是':'否'):value);}
function jsonTree(value,key='',depth=0){const prefix=key?`<span class="json-key">${e(JSON.stringify(key))}</span>: `:'';if(value&&typeof value==='object'){const array=Array.isArray(value);return `<details class="json-node" ${depth<2?'open':''}><summary>${prefix}${array?'[':'{'} · ${Object.keys(value).length} 项</summary>${Object.entries(value).map(([k,v])=>jsonTree(v,array?'':k,depth+1)).join('')}<span>${array?']':'}'}</span></details>`;}return `<div class="json-node json-leaf">${prefix}${e(JSON.stringify(value))}</div>`;}
function showRecordJSON(){const pub=publicView(state.record);$('#detail-body').innerHTML=`<p class="small muted">只读展示副本；内部凭据与隐藏推理载荷已遮蔽。点击对象可折叠。</p><input id="record-json-search" class="json-search" placeholder="搜索字段或内容" aria-label="搜索记录 JSON"><div id="record-json-tree" class="json-tree">${jsonTree(pub)}</div>`;}
function renderDetail(){const r=state.record;if(!r)return;const pub=publicView(r),issue=recordIssue(r);const delivered=deliveryRef();const isDelivered=delivered&&delivered.id===r.artifact_id&&String(delivered.version)===String(r.version);$('#panel-content').innerHTML=`<button class="button small subtle" data-action="back-records">← 全部记录</button><div class="detail-header"><h3>${e(recordTitle(r))}</h3><code>${e(r.id)}</code><div class="small muted">${e(recordTypes[r.record_type]??r.record_type??'固定产物')} ${r.version?`· v${e(r.version)}`:''} · ${date(r.created_at)}</div></div><div class="detail-actions"><button class="button small" data-action="detail-tab" data-tab="fields">结构化详情</button><button class="button small" data-action="detail-tab" data-tab="json">只读 JSON</button><button class="button small" data-action="relations">关联与依赖</button>${r.artifact_id||r.record_type==='artifact'?'<button class="button small" data-action="versions">版本比较</button>':''}${r.record_type==='task'?'<button class="button small" data-action="timeline">任务时间线</button>':''}<button class="button small" data-action="copy-record">复制记录 JSON</button>${state.recordContent!==null?'<button class="button small" data-action="copy-artifact-content">复制该版本内容</button>':''}${isDelivered?`<a class="button small primary" href="${API+ppath()}/artifacts/${encodeURIComponent(r.artifact_id)}/versions/${r.version}/download">下载最终 Project JSON</a>`:''}</div>${issueNotice(issue,r.id)}<div id="detail-body">${state.recordVersion?`<div class="notice">${badge(state.recordVersion.confirmation_status)} ${badge(state.recordVersion.dependency_status)} ${badge(state.recordVersion.quality_status)}<div class="small">查看此版本不改变确认；回到对话的待处理事项确认或修改。</div></div>`:''}${state.recordContent!==null?`<details open class="panel-section"><summary>固定版本内容</summary><pre class="code-box">${e(textContent(publicView(state.recordContent)))}</pre></details>`:''}${recordHTML(pub)}</div>`;}
async function showRelations(){const data=await request(`${ppath()}/records/${encodeURIComponent(state.record.id)}/relations`);const rels=items(data).slice(0,50);const normalized=rels.map(x=>({label:x.relation??x.kind??'关联',from:x.from_id??x.source??x.consumer_ref?.record_id??state.record.id,to:x.to_id??x.target??x.producer_ref?.record_id??x.record?.id??x.id,neighbor:x.record?.id,ref:x.producer_ref??x.ref,record:x.record}));const node=x=>`<button class="relation-node" data-action="${x.ref?'reference':'record'}" ${x.ref?`data-ref="${e(JSON.stringify(x.ref))}"`:`data-id="${e(x.neighbor??x.to)}"`}>${e(x.label)} · ${e(x.record?recordTitle(x.record):short(x.neighbor??x.to))}</button>`;const visible=normalized.slice(0,12),incoming=visible.filter(x=>x.to===state.record.id),outgoing=visible.filter(x=>x.to!==state.record.id);$('#detail-body').innerHTML=`<p class="panel-intro">当前记录的直接关系，最多 50 个对象。选中对象可继续展开。</p><div class="relation-graph">${incoming.map(node).join('')}${incoming.length?'<div class="relation-line">↓ 指向当前记录</div>':''}<div class="relation-node center">${e(recordTitle(state.record))}<br>${e(short(state.record.id))}</div>${outgoing.length?'<div class="relation-line">↓ 当前记录指向</div>':''}${outgoing.map(node).join('')}${!normalized.length?'<span class="small muted">暂无关联</span>':''}</div>${normalized.length>12?'<p class="small muted">图中展示前 12 条关系，完整已加载关系见下表。</p>':''}<table class="relation-table"><thead><tr><th>关系</th><th>来源</th><th>目标</th></tr></thead><tbody>${normalized.map(x=>`<tr><td>${e(x.label)}</td><td><button data-action="record" data-id="${e(x.from)}">${e(short(x.from))} ↗</button></td><td><button data-action="record" data-id="${e(x.to)}">${e(short(x.to))} ↗</button></td></tr>`).join('')}</tbody></table>${data.has_more?'<p class="notice">更多关系请选中关联对象后继续展开。</p>':''}`;}

async function showVersions(){const id=state.record.artifact_id??state.record.id;const data=await request(`${ppath()}/artifacts/${encodeURIComponent(id)}/versions`);const versions=items(data).map(x=>x.version&&typeof x.version==='object'?x.version:x).sort((a,b)=>Number(a.version)-Number(b.version));state.versionList=versions;state.versionArtifact=id;$('#detail-body').innerHTML=`<p class="panel-intro">比较同一产物的两个固定版本。稳定 ID 数组按 ID 对齐，其余数组按原顺序比较。</p><div class="field-grid"><label class="field"><span>旧版本</span><select id="diff-from">${versions.map((v,i)=>`<option value="${e(v.version)}" ${i===Math.max(0,versions.length-2)?'selected':''}>v${e(v.version)} · ${date(v.created_at)}</option>`).join('')}</select></label><label class="field"><span>新版本</span><select id="diff-to">${versions.map((v,i)=>`<option value="${e(v.version)}" ${i===versions.length-1?'selected':''}>v${e(v.version)} · ${date(v.created_at)}</option>`).join('')}</select></label></div><button class="button small primary" data-action="compare-versions">比较版本</button><div id="diff-result"></div><h3 style="margin-top:24px">全部版本</h3>${versions.slice().reverse().map(v=>`<button class="record-row" data-action="artifact" data-id="${e(id)}" data-version="${e(v.version)}"><strong>v${e(v.version)}</strong> <span class="small muted">${date(v.created_at)} · ${e(v.origin??'')}</span></button>`).join('')}`;}
async function compareVersions(){const id=state.versionArtifact,a=$('#diff-from').value,b=$('#diff-to').value;const [from,to]=await Promise.all([request(`${ppath()}/artifacts/${id}/versions/${a}`),request(`${ppath()}/artifacts/${id}/versions/${b}`)]);const normalize=d=>{const c=d.content??d.version?.content??d.content_resolved;return c?.storage==='inline_json'?c.value:c?.storage==='inline_text'?c.text:c;};const old=normalize(from),fresh=normalize(to);const changes=objectDiff(publicView(old),publicView(fresh));const sameSchema=JSON.stringify(from.version?.output_schema)===JSON.stringify(to.version?.output_schema);$('#diff-result').innerHTML=(!sameSchema?'<div class="notice warning">两个版本的 Schema 不同，下方展示保存内容的结构差异，不自动迁移旧版。</div>':'')+(changes.length?changes.map(c=>`<div class="diff-row"><header>${e(c.kind)} · ${e(c.path)}</header>${typeof c.before==='string'&&typeof c.after==='string'?lineDiff(c.before,c.after).map(l=>`<div class="diff-line ${l.kind}">${l.kind==='add'?'＋':l.kind==='delete'?'−':'　'} ${e(l.text)}</div>`).join(''):`<div class="diff-old">− ${e(c.before===undefined?'（不存在）':textContent(c.before))}</div><div class="diff-new">＋ ${e(c.after===undefined?'（不存在）':textContent(c.after))}</div>`}</div>`).join(''):'<p class="notice">这两个版本的内容相同。</p>');}
async function showTimeline(){const data=await request(`${ppath()}/records?record_type=runtime_event&task_id=${encodeURIComponent(state.record.id)}&limit=100`);$('#detail-body').innerHTML=`<div class="timeline">${items(data).map(obj).sort((a,b)=>(a.sequence??0)-(b.sequence??0)).map(r=>`<div class="timeline-item"><time>${date(r.created_at)}</time><button data-action="record" data-id="${e(r.id)}">${e(r.event_name)}</button>${r.payload?`<p class="small muted">${e(textContent(publicView(r.payload)).slice(0,180))}</p>`:''}</div>`).join('')||'<p class="empty">尚无已提交事件</p>'}</div>${data.next_cursor?'<p class="notice">更多历史可在运行事件列表中按任务筛选。</p>':''}`;}
const configSections={global:'全局默认',agent:'Agent 配置',stage:'阶段配置',summary:'上下文摘要 Agent',protocol:'Harness 协议',validation:'最终校验'};
const stageLabels={coordinator:'对话协调','aux.summary':'上下文摘要','aux.history_answer':'历史问答',...Object.fromEntries(Object.entries(stageNames).map(([key,value])=>['step'+key,`Step ${key} · ${value}`]))};
const stageConfigOptions=['coordinator',...[1,2,3,4,5,6,7,8,9,10,11].map(value=>'step'+value),'aux.history_answer'];
function configuredAgents(){const registry=state.config?.registry?.agents??{};if(Object.keys(registry).length)return registry;return Object.fromEntries(Object.entries(state.configValues?.prompts?.agent_names??{}).map(([key,name])=>[key,{name}]));}
function agentName(key){return configuredAgents()[key]?.name??key;}
function configuredAgentKey(stage=state.configStage){return state.configValues?.prompts?.stage_agents?.[stage]??state.config?.selected_agent??'';}
function step1AgentKey(view){return state.configValues?.prompts?.step1_view_agents?.[view]??configuredAgentKey('step1');}
function setConfigSection(section){state.configSection=section;if(section==='global'){state.configScope='project';state.configStage='coordinator';}else if(section==='agent'){state.configScope='agent';state.configStage='coordinator';}else if(section==='stage'){state.configScope='stage';if(!stageConfigOptions.includes(state.configStage))state.configStage='step1';}else if(section==='summary'){state.configScope='auxiliary';state.configStage='aux.summary';}else if(section==='protocol'){state.protocolTarget='global';state.configScope='project';state.configStage='coordinator';}else{state.configScope='project';state.configStage='step11';}}
function configScopeKey(){return state.configScope==='project'?null:state.configScope==='agent'?state.configAgent:state.configStage;}
function outputKey(){return state.configStage;}
function boundSchema(stage=state.configStage){const key=stage==='coordinator'?'conversation_coordinator':stage;return state.configValues?.output?.bindings?.[key]??null;}
function fieldHTML(definition){const [path,label,type,help,displayValue]=definition,v=Object.hasOwn(state.rawConfigFields,path)?state.rawConfigFields[path]:displayValue===undefined?getPath(state.configValues,path):displayValue,origin=state.config.origins?.[path];const source=origin?typeof origin==='string'?origin:origin.scope_kind??origin.config_version_id??JSON.stringify(origin):'当前已解析配置';let control;if(type==='textarea'){control=`<textarea data-config-path="${path}" rows="5">${e(typeof v==='object'?JSON.stringify(v,null,2):v??'')}</textarea>`;}else if(type==='model'||type==='effort'){const choices=type==='model'?Object.keys(state.models):modelEfforts();control=`<select data-config-path="${path}" aria-label="${e(label)}">${choiceOptions(choices,v)}</select>`;}else{control=`<input data-config-path="${path}" data-value-type="${type}" type="${['number','decimal'].includes(type)?'number':'text'}" ${type==='number'?'step="1" min="0"':type==='decimal'?'step="any" min="0"':''} value="${e(v??'')}">`;}return `<label class="field"><span>${e(label)}</span>${control}${help?`<small>${e(help)}</small>`:''}<small>${e(path)} · ${e(source)}</small></label>`;}
async function loadConfig(agentOverride=null){
 const requested=agentOverride??(state.configSection==='agent'||(state.configSection==='protocol'&&state.protocolTarget==='agent')?state.configAgent:null),agentQuery=requested?`&agent_key=${encodeURIComponent(requested)}`:'';
 let data=await request(`/account/config?stage=${encodeURIComponent(state.configStage)}${agentQuery}`);
 const keys=Object.keys(data.registry?.agents??{});
 if((state.configSection==='agent'||(state.configSection==='protocol'&&state.protocolTarget==='agent'))&&keys.length&&!keys.includes(state.configAgent)){state.configAgent=keys[0];return loadConfig(state.configAgent);}
 const scope=state.configScope,key=configScopeKey();
 const working=await request(`/account/config/editor?scope_kind=${encodeURIComponent(scope)}&scope_key=${encodeURIComponent(key??'')}`);
 const draftPrompts=working.payload?.values?.prompts;
 const draftAgent=(scope==='stage'||(scope==='auxiliary'&&state.configStage==='aux.summary'))&&!agentOverride?(state.configStage==='step1'?draftPrompts?.step1_view_agents?.global:draftPrompts?.stage_agents?.[state.configStage]):null;
 if(draftAgent&&draftAgent!==data.selected_agent&&keys.includes(draftAgent))data=await request(`/account/config?stage=${encodeURIComponent(state.configStage)}&agent_key=${encodeURIComponent(draftAgent)}`);
 state.config=data;state.models=data.models??data.registry?.models??{};state.config.registry={...data.registry,models:state.models};
 state.configScopeKey=key??'';state.loadedScope=scope+':'+(key??'');state.editorRevision=working.revision??0;
 state.editorBaseValues=structuredClone(data.values??{});
 state.configValues=working.payload?deepMerge(state.editorBaseValues,configDelta(working.payload.base_values??state.editorBaseValues,working.payload.values??state.editorBaseValues)):structuredClone(state.editorBaseValues);
 state.schemas=structuredClone(working.payload?.schemas??data.schemas??{});
 state.schemaJsonTexts=structuredClone(working.payload?.schema_texts??{});
 state.rawConfigFields=structuredClone(working.payload?.raw_fields??{});
 state.schemaName=boundSchema(state.configStage==='step1'?'step1.global':state.configStage)??Object.keys(state.schemas)[0];state.configDirty=!!working.payload;
 state.schemaValidation=working.payload?null:{valid:true,message:'当前已发布 Schema 已通过服务端校验。'};
 state.schemaValidationRequired=!!working.payload;state.uploadedSchemaFileName='';
 editorChange=0;editorSaved=0;
}
function showNewAgent(){modal('新增 Agent',`<form id="new-agent-form"><label class="field"><span>Agent 名称</span><input name="name" required maxlength="80" placeholder="例如：关系设计 Agent"></label><label class="field"><span>agent_key</span><input name="key" required maxlength="64" pattern="[a-z][a-z0-9_]*" placeholder="例如：relationship_designer"><small>以小写字母开头，只使用小写字母、数字和下划线。</small></label><label class="field"><span>创作职责</span><textarea name="instructions" rows="8" placeholder="描述这个 Agent 的创作专长"></textarea></label><div class="modal-footer"><button class="button subtle" type="button" data-action="close-modal">取消</button><button class="button primary" type="submit">新增 Agent</button></div></form>`);}
async function createAgent(name,key,instructionsText){await flushWorkingDraft();name=name.trim();key=key.trim();if(!name)throw new Error('请输入 Agent 名称');if(!/^[a-z][a-z0-9_]{0,63}$/.test(key))throw new Error('agent_key 格式无效');if(configuredAgents()[key])throw new Error('agent_key 已存在');const published=(state.config.versions??[]).filter(v=>v.state==='published'&&v.scope_kind==='project'&&v.scope_key==null).sort((a,b)=>b.version-a.version)[0],previous=structuredClone(published?.values??{});delete previous.schemas;const prompts=state.configValues.prompts??{},values=deepMerge(previous,{prompts:{layout_version:3,agents:{...(prompts.agents??{}),[key]:instructionsText},harness:{agents:{...(prompts.harness?.agents??{}),[key]:''}},agent_names:{...(prompts.agent_names??{}),[key]:name}}});const saved=await request('/account/config',{method:'POST',body:{values,scope_kind:'project',scope_key:null}}),draft=saved.config??saved.draft??(typeof saved.version==='object'?saved.version:saved);if(!draft?.id)throw new Error('服务端未返回可发布的 Agent 配置');await request(`/account/config/${encodeURIComponent(draft.id)}/publish`,{method:'POST',body:{}});state.configAgent=key;state.configStage='coordinator';state.configScope='agent';$('#modal').close();await loadConfig(key);renderConfig();toast('Agent 已新增，可在阶段配置中选择');}
function modelEfforts(){return state.models[state.configValues?.model?.name]?.reasoning_efforts??[];}
function choiceOptions(choices,current){return (!choices.includes(current)?`<option value="${e(current??'')}" selected disabled>${e(current??'未配置')} · 不在支持范围</option>`:'')+choices.map(value=>`<option value="${e(value)}" ${value===current?'selected':''}>${e(value)}</option>`).join('');}
function updateModelEfforts(){const choices=modelEfforts(),previous=state.configValues.model.reasoning_effort;if(choices.length&&!choices.includes(previous)){const next=choices.includes('medium')?'medium':choices.includes('high')?'high':choices[0];state.configValues.model.reasoning_effort=next;toast(`所选模型不支持 ${previous}，推理强度已调整为 ${next}。`);}const field=$('[data-config-path="model.reasoning_effort"]');if(field)field.innerHTML=choiceOptions(choices,state.configValues.model.reasoning_effort);}
function inputsHTML(){const match=/^step(\d+)$/.exec(state.configStage);if(!match)return '<p class="empty">协调与历史问答 Session 由系统独立管理，不配置阶段产物。</p>';const step=Number(match[1]),selected=new Set(state.configValues?.context?.stage_inputs?.[state.configStage]??[]),sharing=state.configValues?.context?.session_sharing?.[state.configStage]??'',available=stageArtifactCatalog.filter(([, ,producer])=>producer<step||(step===11&&producer===10));const prior=Array.from({length:Math.max(0,step-1)},(_,index)=>`step${index+1}`);return `<div class="session-choice"><label class="field"><span>Session</span><select id="stage-session"><option value="" ${sharing===''?'selected':''}>独立 Session</option>${prior.map(value=>`<option value="${value}" ${sharing===value?'selected':''}>共享 ${e(stageLabels[value]??value)} 的 Session</option>`).join('')}</select><small>共享后会沿用所选阶段的对话历史；只允许选择此前阶段。</small></label></div><div class="stage-input-heading"><strong>传给本阶段 Agent 的产物</strong><small>读取当前有效完整版本；章节产物自动匹配当前章节。</small></div><div class="artifact-choice-grid">${available.map(([kind,label,producer])=>{const locked=state.configStage==='step1'&&kind==='source_text',source=producer===0?'用户导入':`Step ${producer}`;return `<label class="toggle-card"><input type="checkbox" data-stage-input="${e(kind)}" ${selected.has(kind)?'checked':''} ${locked?'disabled':''}><span><strong>${e(label)}</strong><small>${e(source)}</small></span></label>`;}).join('')}</div><p class="small muted">如果勾选的产物尚未生成或没有当前有效版本，任务会明确提示缺少哪项材料并暂停。</p>`;}
function outputHTML(){const key=outputKey();if(key==='step1'){const branches=[['global','作品事件'],['character','主要人物事件']],selected=branches.find(([view])=>boundSchema('step1.'+view)===state.schemaName)?.[0]??'global';if(!branches.some(([view])=>boundSchema('step1.'+view)===state.schemaName))state.schemaName=boundSchema('step1.global');return `<div class="output-mode"><p class="small muted">两个 Agent 各自绑定 output_type 和 Schema；全局分支输出事件与分析，人物分支只输出人物事件；Harness 保存三份独立产物。关闭任一路后只保存原始输出并暂停后续流程。</p>${branches.map(([view,label])=>{const branch='step1.'+view,schema=boundSchema(branch),enabled=state.configValues.output?.structured?.[branch]??!!schema;return `<label class="toggle-card"><input type="checkbox" data-output-toggle="${e(branch)}" ${enabled?'checked':''}><span><strong>${label} · 启用 output_type</strong><small>绑定 ${e(schema??'无 Schema')}</small></span></label>`;}).join('')}<label class="field"><span>编辑哪份 JSON Schema</span><select id="schema-select">${branches.map(([view,label])=>`<option value="${e(boundSchema('step1.'+view)??'')}" ${view===selected?'selected':''}>${label} · ${e(boundSchema('step1.'+view)??'未绑定')}</option>`).join('')}</select></label><div id="schema-editor"></div></div>`;}const schema=boundSchema(),structured=state.configValues.output?.structured?.[key]??!!schema;if(schema)state.schemaName=schema;return `<div class="output-mode"><label class="toggle-card"><input type="checkbox" data-output-toggle="${e(key)}" ${structured?'checked':''}><span><strong>启用 output_type</strong><small>${key==='coordinator'?'关闭后以普通文本答复，工具调度照常运行':'关闭后保存本轮模型原始文本并暂停下游流程'}</small></span></label><div class="contract-card"><div><span>当前绑定 Schema</span><strong>${e(schema??'无（普通文本）')}</strong></div></div>${structured&&schema?'<div id="schema-editor"></div>':'<div class="notice">本阶段返回普通文本，不向模型绑定 JSON Schema。</div>'}</div>`;}
function agentOptions(current){return Object.entries(configuredAgents()).map(([key,value])=>`<option value="${e(key)}" ${key===current?'selected':''}>${e(value.name??key)} · ${e(key)}</option>`).join('');}
const instructionLabels={harness_base:'共通 Harness 规则',creative_base:'共通创作指令',harness_agent:'Agent 执行规则',creative_agent:'Agent 创作职责',harness_stage:'阶段执行规则',creative_stage:'阶段创作任务',harness_runtime:'历史阶段运行规则',legacy:'旧版混合指令（待核对）',manager:'主 Agent 调度规则',manager_format:'主 Agent 输出格式',runtime_state:'运行时状态占位',tool_guidance:'本次提问权限',step1_mode:'本次 Step1 模式规则'};
const creativeInstructionParts=['creative_base','creative_agent','creative_stage'];
function instructionPreviewBranch(suffix='',label=''){
 const id=suffix?'-'+suffix:'';
 return `<div class="instruction-preview-branch">${label?`<h4>${e(label)}</h4>`:''}<div id="instruction-groups${id}" class="instruction-groups"><p class="small muted">正在生成预览…</p></div><details class="panel-section instruction-sources"><summary>展开逐段来源与完整拼接文本</summary><div id="instruction-parts${id}"></div><label class="field"><span>完整 Instructions</span><textarea id="final-instructions${id}" class="code-editor" rows="12" readonly aria-label="${e(label||'当前阶段')} 最终 Instructions">正在生成预览…</textarea></label></details></div>`;
}
function instructionsPreviewHTML(){
 const note=state.configStage==='aux.summary'?'<p class="small muted">这里预览摘要任务实际使用的静态 instructions，包括所选 Agent 的职责、摘要指令和 Harness 规则。待压缩的历史由运行时加入；实际发送文本可在运行数据中查看。</p>':`<p class="small muted">当前预览：${e(stageLabels[state.configStage]??state.configStage)}。创作指令供日常调试；Harness 规则负责工具、流程和格式。这里显示当前配置生效的静态部分，项目状态与原文片段在运行时填入；实际发送文本可在运行数据中查看。</p>`;

 if(state.configStage==='step1')return `<details class="panel-section" open><summary>最终 Instructions 预览 · 两个 Agent</summary>${note}<label class="field preview-mode"><span>Step1 预览模式</span><select id="preview-step1-mode"><option value="full" ${(state.previewStep1Mode??'full')==='full'?'selected':''}>全文输入</option><option value="window" ${state.previewStep1Mode==='window'?'selected':''}>滑动窗口</option></select><small>仅切换预览。实际运行根据原作 token 数与触发阈值决定。</small></label>${instructionPreviewBranch('global','作品事件视图 Agent')}${instructionPreviewBranch('character','主要人物事件视图 Agent')}</details>`;
 return `<details class="panel-section" open><summary>最终 Instructions 预览</summary>${note}${instructionPreviewBranch()}</details>`;
}
let instructionPreviewTimer=null,instructionPreviewSerial=0;
function renderInstructionsPreview(){
 const node=$('#final-instructions')??$('#final-instructions-global');if(!node)return;
 const serial=++instructionPreviewSerial;clearTimeout(instructionPreviewTimer);
 instructionPreviewTimer=setTimeout(async()=>{
  const show=(suffix,data)=>{
   const target=$('#final-instructions'+suffix),parts=$('#instruction-parts'+suffix),groups=$('#instruction-groups'+suffix);
   if(target)target.value=data.final??'';
   const entries=Object.entries(data.parts??{}).filter(([,value])=>value);
   if(groups){
    const creative=entries.filter(([name])=>creativeInstructionParts.includes(name)).map(([,value])=>value).join('\n\n');
    const harness=entries.filter(([name])=>!creativeInstructionParts.includes(name)).map(([,value])=>value).join('\n\n');
    groups.innerHTML=`<section class="instruction-group"><h5>${state.configStage==='aux.summary'?'摘要任务指令':'创作指令'}</h5><pre class="code-box">${e(creative||'本次未设置任务指令')}</pre></section><section class="instruction-group"><h5>当前生效的 Harness 规则</h5><pre class="code-box">${e(harness||'本次没有额外 Harness 规则')}</pre></section>`;
   }
   if(parts)parts.innerHTML=entries.map(([name,value])=>`<details class="panel-section"><summary>${e(instructionLabels[name]??name)}</summary><pre class="code-box">${e(value)}</pre></details>`).join('');
  };
  try{
   const edited=structuredClone(state.configValues),stage=state.configStage;
   if(stage==='step1'){
    const delta=configDelta(state.editorBaseValues,edited);
    const previews=await Promise.all(['global','character'].map(async view=>{
     const key=edited.prompts.step1_view_agents?.[view];
     const resolved=key===state.config?.selected_agent?edited:deepMerge((await request(`/account/config?stage=step1&agent_key=${encodeURIComponent(key)}`)).values,delta);
     resolved.prompts.stage_agents.step1=key;
     return request('/account/config/instructions-preview',{method:'POST',body:{stage:'step1',step1_view:view,step1_mode:state.previewStep1Mode??'full',values:resolved}});
    }));
    if(serial!==instructionPreviewSerial)return;
    show('-global',previews[0]);show('-character',previews[1]);
   }else{
    if(state.configSection==='protocol'&&state.protocolTarget==='agent')edited.prompts.stage_agents[stage]=state.configAgent;
    const result=await request('/account/config/instructions-preview',{method:'POST',body:{stage,values:edited}});
    if(serial===instructionPreviewSerial)show('',result);
   }
  }catch(error){if(serial===instructionPreviewSerial){for(const suffix of state.configStage==='step1'?['-global','-character']:['']){const message=`预览失败：${error.message}`,target=$('#final-instructions'+suffix),groups=$('#instruction-groups'+suffix);if(target)target.value=message;if(groups)groups.innerHTML=`<div class="notice error">${e(message)}</div>`;}}}
 },180);
}
function legacyHarnessHTML(entries){
 const present=entries.filter(([,value])=>typeof value==='string'&&value.trim());
 return present.length?`<details class="panel-section"><summary>旧配置待核对 · ${present.length} 项</summary><p class="small muted">这些旧版混合指令仍会生效。核对后可移入创作指令或 Harness 规则并清空原字段。</p>${present.map(([definition,value])=>fieldHTML([...definition.slice(0,4),value])).join('')}</details>`:'';
}
function agentHarnessHTML(expanded=false){
 const h=state.configValues.prompts?.harness??{},key=state.configAgent;
 const legacy=legacyHarnessHTML([
  [[`prompts.harness.legacy_agents.${key}`,'旧版 Agent 混合指令','textarea'],h.legacy_agents?.[key]],
  [['prompts.harness.legacy_agent','旧版 Agent 范围覆盖','textarea'],h.legacy_agent],
 ]);
 return `<details class="panel-section advanced-protocol" ${expanded?'open':''}><summary>高级设置 · ${e(agentName(key))} 的 Harness 规则</summary><p class="small muted">仅约束这个 Agent 如何使用工具和遵守流程；创作风格请在 Agent 创作职责中调整。这里与“Harness 协议”中编辑的是同一项。</p>${fieldHTML([`prompts.harness.agents.${key}`,'Agent 执行规则','textarea'])}${legacy}</details>`;
}
function stageHarnessValue(stage=state.configStage){
 const h=state.configValues.prompts?.harness??{};
 const rule=h.stages?.[stage]??'',runtime=h.runtime?.[stage]??'';
 return [rule,rule.includes(runtime)?'':runtime].filter(value=>typeof value==='string'&&value.trim()).join('\n\n');
}
function stageHarnessEditor(stage=state.configStage){
 return `<label class="field"><span>阶段执行规则</span><textarea data-harness-stage="${e(stage)}" rows="9">${e(stageHarnessValue(stage))}</textarea><small>本阶段的工具、格式和固定材料要求统一在这里编辑。旧版阶段运行规则会自动并入本编辑框；修改后保存为一条阶段规则。</small></label>`;
}
function stageHarnessHTML(expanded=false){
 const stage=state.configStage,h=state.configValues.prompts?.harness??{};
 const legacy=legacyHarnessHTML([
  [[`prompts.harness.legacy_stages.${stage}`,'旧版阶段混合指令','textarea'],h.legacy_stages?.[stage]],
  [['prompts.harness.legacy_stage','旧版阶段范围覆盖','textarea'],h.legacy_stage],
 ]);
 return `<details class="panel-section advanced-protocol" ${expanded?'open':''}><summary>高级设置 · 本阶段的 Harness 规则</summary><p class="small muted">约束本阶段如何读取材料、调用工具和输出；内容目标请在阶段创作任务中调整。这里与“Harness 协议”中编辑的是同一项。</p>${stageHarnessEditor(stage)}${legacy}</details>`;
}
function commonHarnessHTML(){
 const h=state.configValues.prompts?.harness??{};
 return `<div class="notice">Harness 规则决定流程、提问和输出格式。修改并发布后影响后续新 Run；工具权限、版本绑定和 Schema 校验仍由程序执行。</div>
 <details class="panel-section" open><summary>所有 Agent 共用</summary>${fieldHTML(['prompts.harness.base','通用执行与安全规则','textarea','对每个 Agent 生效。'])}</details>
 <details class="panel-section"><summary>主 Agent 调度与输出</summary><p class="small muted">仅对对话协调主 Agent 生效；输出规则按其 output_type 是否启用二选一。</p>${fieldHTML(['prompts.harness.manager','主 Agent 调度规则','textarea'])}${fieldHTML(['prompts.harness.manager_structured','结构化输出时','textarea'])}${fieldHTML(['prompts.harness.manager_plain','普通文本输出时','textarea'])}</details>
 <details class="panel-section"><summary>条件规则 · 提问与滑动窗口</summary><p class="small muted">按本次 Run 实际能力和输入模式选择，未触发的文字不会加入最终 Instructions。</p>${fieldHTML(['prompts.harness.ask_user','提供 ask_user 时','textarea'])}${fieldHTML(['prompts.harness.no_ask_user','不提供 ask_user 时','textarea'])}${fieldHTML(['prompts.harness.window','Step1 滑动窗口时','textarea'])}</details>
 ${legacyHarnessHTML([[['prompts.harness.legacy_base','旧版共通混合指令','textarea'],h.legacy_base]])}`;
}
function renderConfig(){
 if(!state.config)return;
 const modelFields=[['model.name','模型','model'],['model.reasoning_effort','推理强度','effort'],['model.max_output_tokens','输出上限（含推理）','number'],['run.max_turns','单 Run 模型轮次','number'],['retry.max_retries','临时失败重试次数','number'],['repair.max_rounds','结构化输出修复次数','number','output_type 解析或程序契约校验失败时的最大自动修复轮数。']];
 const sourceFields=[['context.step1_source.trigger_tokens','触发阈值（tokens）','number','原作估算 token 数大于此值时才启用滑动窗口；小于或等于时全文输入。'],['context.step1_source.window_tokens','窗口大小（tokens）','number','每次从尚未确认的原文起点读取最多这么多 tokens；未完成事件留待下一窗口。']];
 const contextFields=[['context.input_token_cap','阶段输入上限','number'],['context.safety_margin_tokens','安全余量','number'],['context.recent_turns','近期完整对话回合','number'],['context.history_token_cap','历史 token 预算','number'],['compaction.trigger_ratio','压缩触发比例','decimal'],['compaction.target_ratio','压缩后比例','decimal']];
 const sectionTabs=`<nav class="config-section-tabs" aria-label="配置分区">${Object.entries(configSections).map(([key,label])=>`<button type="button" data-action="config-section" data-section="${key}" aria-current="${key===state.configSection?'page':'false'}">${label}</button>`).join('')}</nav>`;
 let target='',body='';const prompts=state.configValues.prompts??{};
 if(state.configSection==='global'){
  target='<div class="config-scope-note"><strong>账号全局默认</strong><span>对所有项目和会话生效；Agent 和阶段可覆盖。</span></div>';
  const instructionsField=['prompts.base','共通创作指令','textarea','所有 Agent 的共同创作原则；Harness 工具和流程协议在高级配置中。'];
  body=`<div class="config-columns"><div><details class="panel-section" open><summary>共通创作指令</summary>${fieldHTML(instructionsField)}</details><details class="panel-section" open><summary>默认模型与执行</summary><div class="field-grid">${modelFields.map(fieldHTML).join('')}</div><div id="budget-preview"></div></details></div><div><details class="panel-section" open><summary>原作导入</summary><p class="small muted">原作原样保存为固定版本，切片时按下方参数决定全文输入或逐窗扫描。</p></details><details class="panel-section" open><summary>滑动窗口机制参数</summary><div class="field-grid">${sourceFields.map(fieldHTML).join('')}</div></details><details class="panel-section" open><summary>默认上下文与压缩</summary><div class="field-grid">${contextFields.map(fieldHTML).join('')}</div></details></div></div>`;
 }else if(state.configSection==='agent'){
  const actual=prompts.agent??prompts.agents?.[state.configAgent]??'';
  const legacyValidation=state.configAgent==='validation_agent'&&prompts.validation?`<div class="notice warning">旧版校验指令仍优先于本 Agent 的创作职责；核对并迁移后可清空。</div>${fieldHTML(['prompts.validation','旧版校验创作指令','textarea','清空后使用上方 Agent 创作职责。'])}`:'';
  target=`<section class="config-target"><label class="field"><span>Agent</span><select id="config-agent">${agentOptions(state.configAgent)}</select></label><div class="field"><span>Agent 库</span><button class="button" type="button" data-action="new-agent">＋ 新增 Agent</button><small>新增后可在阶段配置中选择。</small></div></section>`;
  body=`<div class="config-columns"><div><details class="panel-section" open><summary>${e(agentName(state.configAgent))} 创作职责</summary>${fieldHTML(['prompts.agent','Agent 创作指令','textarea','描述这个 Agent 如何分析和创作内容；在任何阶段选中时使用。',actual])}${legacyValidation}</details><details class="panel-section" open><summary>Agent 模型与执行</summary><div class="field-grid">${modelFields.map(fieldHTML).join('')}</div></details></div><div><div class="notice">日常调试主要修改左侧创作职责。阶段任务、输入、Session 和 output_type 在“阶段配置”中设置。</div>${agentHarnessHTML()}</div></div>`;
 }else if(state.configSection==='stage'){
  const selected=configuredAgentKey();
  const validationToggle=state.configStage==='step11'?`<label class="toggle-card"><input type="checkbox" id="validation-agent-enabled" ${prompts.validation_enabled?'checked':''}><span><strong>启用 Step11 校验 Agent</strong><small>默认关闭；独立于创作 instructions 是否为空。</small></span></label>`:'';
  const step1Selectors=`<label class="field"><span>作品事件视图 Agent</span><select id="stage-agent-global">${agentOptions(step1AgentKey('global'))}</select><small>切分完整作品事件并同步分析；视图和分析分别保存。</small></label><label class="field"><span>主要人物事件视图 Agent</span><select id="stage-agent-character">${agentOptions(step1AgentKey('character'))}</select><small>独立阅读原文，不依赖作品事件切分；只保存人物事件，不生成逐事件原文索引。</small></label>`;
  target=`<section class="config-target"><label class="field"><span>阶段</span><select id="config-stage">${stageConfigOptions.map(stage=>`<option value="${stage}" ${stage===state.configStage?'selected':''}>${e(stageLabels[stage]??stage)}</option>`).join('')}</select></label>${state.configStage==='step1'?step1Selectors:`<label class="field"><span>执行 Agent</span><select id="stage-agent">${agentOptions(selected)}</select><small>本阶段运行时实例化所选 Agent。</small></label>`}</section>`;
  const actual=prompts.stage??prompts.stages?.[state.configStage]??'';
  body=`<div class="config-columns"><div>${validationToggle}<details class="panel-section" open><summary>阶段创作任务</summary>${fieldHTML(['prompts.stage',`${stageLabels[state.configStage]} 创作指令`,'textarea','只描述本阶段要完成的内容目标和质量要求。',actual])}</details>${stageHarnessHTML()}<details class="panel-section" open><summary>阶段模型与执行覆盖</summary>${state.configStage==='step1'?'<p class="small muted">两路各自采用 Agent 配置中的模型；本阶段覆盖会同时作用于两路。</p>':''}<div class="field-grid">${modelFields.map(fieldHTML).join('')}</div></details><details class="panel-section" open><summary>Output type</summary>${outputHTML()}</details></div><div><details class="panel-section" open><summary>阶段输入与 Session</summary>${inputsHTML()}</details>${instructionsPreviewHTML()}</div></div>`;
 }else if(state.configSection==='summary'){
  const selected=configuredAgentKey('aux.summary');
  const summaryFields=[...modelFields.slice(0,4),['context.input_token_cap','摘要输入上限','number','摘要 Agent 单次请求可用的输入 token 上限。'],['context.safety_margin_tokens','安全余量','number'],['summary.target_tokens','摘要目标长度','number','目标 token 数用于规划压缩；Harness 检查摘要非空及后续输入预算。']];
  target=`<section class="config-target"><label class="field"><span>执行 Agent</span><select id="summary-agent">${agentOptions(selected)}</select><small>只影响内部上下文压缩任务；Agent 的通用职责可在“Agent 配置”中编辑。</small></label></section>`;
  body=`<div class="config-columns"><div><details class="panel-section" open><summary>摘要任务指令</summary>${fieldHTML(['prompts.summary','摘要指令','textarea','这段文字会加入摘要 Agent 的最终 instructions；只总结已有历史，不改变正式产物和确认。'])}</details>${stageHarnessHTML()}<details class="panel-section" open><summary>摘要模型与预算</summary><div class="field-grid">${summaryFields.map(fieldHTML).join('')}</div></details></div><div><div class="notice">摘要任务由 Harness 自动触发并读取待压缩的 Session 历史。输出固定为纯文本，不绑定 output_type；输入材料和 Session 由运行时确定。</div>${instructionsPreviewHTML()}</div></div>`;
 }else if(state.configSection==='protocol'){
  const area=state.protocolTarget??'global';
  target=`<section class="config-target"><label class="field"><span>编辑哪类 Harness 规则</span><select id="protocol-target"><option value="global" ${area==='global'?'selected':''}>所有 Agent 共用与条件规则</option><option value="agent" ${area==='agent'?'selected':''}>某个 Agent 的执行规则</option><option value="stage" ${area==='stage'?'selected':''}>某个阶段的执行规则</option></select><small>高级设置；发布后影响后续新 Run。</small></label>${area==='agent'?`<label class="field"><span>Agent</span><select id="config-agent">${agentOptions(state.configAgent)}</select></label>`:area==='stage'?`<label class="field"><span>阶段</span><select id="config-stage">${stageConfigOptions.map(stage=>`<option value="${stage}" ${stage===state.configStage?'selected':''}>${e(stageLabels[stage]??stage)}</option>`).join('')}</select></label>`:''}</section>`;
  body=`<div class="config-columns"><div>${area==='global'?commonHarnessHTML():area==='agent'?agentHarnessHTML(true):stageHarnessHTML(true)}</div><div>${area==='agent'?'<div class="notice">同一个 Agent 可以用于不同阶段，最终 Instructions 会随阶段与运行模式变化。请到“阶段配置”选择具体阶段查看准确预览。</div>':instructionsPreviewHTML()}</div></div>`;
 }else{
  target='<div class="config-scope-note"><strong>最终校验</strong><span>固定脚本校验始终执行；Step11 校验 Agent 在阶段配置中显式启用。</span></div>';
  body=`<div class="config-columns"><div><details class="panel-section" open><summary>固定脚本校验</summary><div class="contract-card"><div><span>schema_contract</span><strong>检查最终 Graph 是否符合 Nexo Graph Schema</strong></div><div><span>unreachable_nodes</span><strong>检查未连线或条件永远不可满足的不可达节点</strong></div></div><p class="small muted">脚本报错会直接显示在对话中。</p></details></div><div><div class="notice">Step11 的启用开关、执行 Agent、创作指令、output_type、模型与输入都在“阶段配置”中统一设置；Harness 协议文字可在高级分区编辑。</div><button class="button" type="button" data-action="configure-validation-stage">配置 Step11</button></div></div>`;
 }
 $('#panel-content').innerHTML=`<div class="config-hero"><div><span class="eyebrow">AGENT HARNESS</span><h3>Agent 调试配置</h3><p>Agent 库定义执行者，阶段配置决定每一步使用哪个 Agent。</p></div><div id="config-notice"></div></div>${sectionTabs}${target}${body}<div class="config-footer"><span class="small muted">编辑内容自动保存；完整校验仅在发布时执行。</span><button class="button primary" data-action="publish-config" id="publish-config" ${state.configDirty?'':'disabled'}>发布配置</button></div>`;
 if(state.configDirty)editorNotice('已恢复未发布的编辑草稿；修改会自动保存。');
 renderBudget();renderInstructionsPreview();if(state.schemaName&&$('#schema-editor'))renderSchema();
}
function renderBudget(){if(state.configSection!=='global')return;const name=state.configValues.model?.name;const model=state.models[name]??{};const b=inputBudget(state.configValues,model);const target=$('#budget-preview');if(target)target.innerHTML=`<div class="budget-preview"><small>有效输入预算</small><strong>${b===null?'等待模型能力':number(b)+' tokens'}</strong><small>模型窗口 ${number(model.context_window??model.context_window_tokens)} · 最大输出 ${number(model.max_output_tokens)}</small></div>`;}
function schemaValidationHTML(){if(state.schemaValidationRequired)return '<div class="notice warning">Schema 可继续编辑；发布时会自动校验。</div>';if(state.schemaValidation?.valid)return `<div class="notice">${e(state.schemaValidation.message??'Schema 校验通过。')}</div>`;if(state.schemaValidation?.message)return `<div class="notice error">${e(state.schemaValidation.message)}</div>`;return '<div class="notice warning">尚未校验当前 Schema；发布时会自动校验。</div>';}
function renderSchema(){const schema=state.schemas[state.schemaName],node=$('#schema-editor');if(!node)return;node.innerHTML=schema?`<p class="panel-intro">上传或编辑 ${e(state.schemaName)}。脚本会检查 JSON Schema、Agents SDK strict 模式和下游字段兼容性。</p><div class="schema-upload"><label class="button small" for="schema-file">上传 JSON Schema</label><input class="visually-hidden" id="schema-file" type="file" accept=".json,application/json"><span>${e(state.uploadedSchemaFileName||'尚未选择文件')}</span></div><textarea id="schema-json" class="code-editor" rows="12" aria-label="Schema JSON" spellcheck="false">${e(state.schemaJsonTexts[state.schemaName]??JSON.stringify(schema,null,2))}</textarea><div class="inline-actions"><button class="button small" data-action="apply-schema-json">应用编辑内容</button><button class="button small primary" data-action="validate-output-schema">校验 Schema</button></div><div id="schema-validation">${schemaValidationHTML()}</div>`:'<p class="empty">普通文本阶段没有 output_type。</p>';}
let editorSaveTimer=null,editorSavePromise=Promise.resolve(),editorChange=0,editorSaved=0;
function editorNotice(message,isError=false){const notice=$('#config-notice');if(notice)notice.innerHTML=`<div class="notice ${isError?'error':''}">${e(message)}</div>`;}
function dirty(){state.configDirty=true;editorChange++;$('#publish-config')?.removeAttribute('disabled');editorNotice('正在自动保存编辑草稿…');clearTimeout(editorSaveTimer);editorSaveTimer=setTimeout(()=>{void flushWorkingDraft().catch(error=>editorNotice(`自动保存失败：${error.message}`,true));},650);}
async function flushWorkingDraft(){
 clearTimeout(editorSaveTimer);
 await editorSavePromise.catch(()=>{});
 if(!state.configDirty||editorSaved===editorChange)return;
 if(state.loadedScope!==state.configScope+':'+(configScopeKey()??''))throw new Error('当前配置范围尚未读取完成');
 const revision=state.editorRevision,change=editorChange,scope=state.configScope,key=configScopeKey();
 const payload={values:structuredClone(state.configValues),base_values:structuredClone(state.editorBaseValues),schemas:structuredClone(state.schemas),schema_texts:structuredClone(state.schemaJsonTexts),raw_fields:structuredClone(state.rawConfigFields)};
 editorSavePromise=request('/account/config/editor',{method:'PUT',body:{scope_kind:scope,scope_key:key,payload,expected_revision:revision}})
  .then(result=>{state.editorRevision=result.revision;editorSaved=change;if(editorSaved===editorChange)editorNotice('编辑草稿已自动保存，发布前可继续修改。');});
 return editorSavePromise;
}
function invalidateSchemaValidation(){state.schemaValidationRequired=true;state.schemaValidation=null;dirty();}
function syncConfigEditors(){
 const schemaEditor=$('#schema-json');if(schemaEditor&&state.schemaName)state.schemaJsonTexts[state.schemaName]=schemaEditor.value;
 for(const [name,raw] of Object.entries(state.schemaJsonTexts)){
  let parsed;try{parsed=JSON.parse(raw);}catch(error){throw new Error(`${name} 的 JSON Schema 不是有效 JSON：${error.message}`);}
  if(!parsed||typeof parsed!=='object'||Array.isArray(parsed))throw new Error(`${name} 的 JSON Schema 根节点必须是对象`);
  state.schemas[name]=parsed;
 }
 for(const [path,raw] of Object.entries(state.rawConfigFields)){
  if(raw.trim()==='')throw new Error(`${path} 不能为空`);
  let value;try{value=JSON.parse(raw);}catch{value=Number(raw);}
  if(typeof value==='number'&&!Number.isFinite(value))throw new Error(`${path} 必须是有效数字`);
  setPath(state.configValues,path,value);
 }
}
async function validateOutputSchema(){try{syncConfigEditors();const errors=validateConfig(state.configValues,state.schemas,state.config.registry);if(errors.length)throw new Error(errors.join('；'));const stage=state.configStage==='step1'&&state.schemaName===boundSchema('step1.character')?'step1.character':state.configStage==='step1'?'step1.global':state.configStage;const result=await request('/account/config/validate',{method:'POST',body:{values:state.configValues,schemas:state.schemas,stage}});state.schemaValidation={valid:true,message:`校验通过：${result.schema_id??'普通文本'}${result.schema_sha256?' · '+result.schema_sha256.slice(0,12):''}`};state.schemaValidationRequired=false;const node=$('#schema-validation');if(node)node.innerHTML=schemaValidationHTML();toast('Schema 校验通过');return result;}catch(error){state.schemaValidation={valid:false,message:error.message||String(error)};state.schemaValidationRequired=true;const node=$('#schema-validation');if(node)node.innerHTML=schemaValidationHTML();throw error;}}
async function publishConfig(){
 if(!state.configDirty)throw new Error('当前没有待发布的配置修改');
 await flushWorkingDraft();
 try{
  syncConfigEditors();
  const errors=validateConfig(state.configValues,state.schemas,state.config.registry);
  if(errors.length)throw new Error(errors.join('；'));
  await request('/account/config/validate',{method:'POST',body:{values:state.configValues,schemas:state.schemas,stage:state.configStage}});
  const scope=state.configScope,key=configScopeKey();
  const existing=(state.config.versions??[]).filter(v=>v.state==='published'&&v.scope_kind===scope&&(v.scope_key??null)===(scope==='project'?null:key)).sort((a,b)=>b.version-a.version)[0];
  const previous=structuredClone(existing?.values??{});delete previous.schemas;
  const values=deepMerge(previous,configDelta(state.editorBaseValues,state.configValues));
  values.prompts??={};values.prompts.layout_version=3;
  const saved=await request('/account/config',{method:'POST',body:{values,schemas:state.schemas,scope_kind:scope,scope_key:key}});
  const draft=saved.config??saved.draft??(typeof saved.version==='object'?saved.version:saved);
  if(!draft?.id)throw new Error('服务端未返回可发布的配置 ID');
  await request(`/account/config/${encodeURIComponent(draft.id)}/publish`,{method:'POST',body:{}});
  try{await request('/account/config/editor',{method:'DELETE',body:{scope_kind:scope,scope_key:key,expected_revision:state.editorRevision}});}catch(error){console.warn('Published configuration, but could not clear working draft',error);}
  await loadConfig();renderConfig();toast('配置已发布，后续新 Run 生效');
 }catch(error){editorNotice(`发布未完成：${error.message}。已发布配置保持不变，编辑草稿仍保留。`,true);throw error;}
}

async function queueView(){
  await refreshActions();
  const cards=state.actionCards.filter(card=>card.kind==='queue');
  modal('排队消息',cards.length?`<p class="modal-note">消息已保存，尚未交给 Agent 处理。</p><div class="queue-items">${cards.map(card=>{
    const release=card.actions.find(action=>action.id==='release_one'),cancel=card.actions.find(action=>action.id==='cancel');
    return `<form class="queue-item" data-card-form="${e(card.id)}"><p>${e(card.description)}</p><small>${e(card.details?.find(detail=>detail.label==='原因')?.value??'等待处理')}</small><div class="queue-item-actions"><button type="button" class="button small" data-action="submit-card-action" data-card-action="release_one" ${release?.disabled_reason?'disabled':''}>继续处理</button><button type="button" class="button small danger" data-action="submit-card-action" data-card-action="cancel" ${cancel?.disabled_reason?'disabled':''}>取消排队</button></div></form>`;
  }).join('')}</div>`:'<p class="modal-note">当前没有排队消息。</p>');
}
async function openBlob(id){const response=await fetch(`${API+ppath()}/blobs/${encodeURIComponent(id)}/content`,{credentials:'same-origin'});if(!response.ok){const data=await response.json().catch(()=>({}));throw new Error(data.error?.message??'外置内容读取失败');}const type=response.headers.get('content-type')??'';if(!/text|json/.test(type)){modal('外置内容',`<p class="modal-note">此内容为 ${e(type||'二进制附件')}，可下载查看。</p><a class="button" href="${API+ppath()}/blobs/${encodeURIComponent(id)}/content?download=true">下载附件</a>`);return;}const text=await response.text();let safe=text;try{safe=JSON.stringify(publicView(JSON.parse(text)),null,2);}catch{}modal('固定版本内容',`<pre class="code-box" style="max-height:65vh">${e(safe)}</pre>`);}
async function handleAction(button){const action=button.dataset.action,id=button.dataset.id;switch(action){case 'new-project':return showCreate('project');case 'new-conversation':return showCreate('conversation');case 'refresh-projects':return loadProjects();case 'select-project':return selectProject(id);case 'delete-project':return showDeleteProject(id);case 'select-conversation':return selectConversation(id);case 'close-modal':$('#modal').close();return;case 'source':return sourceModal();case 'focus-input':if(!state.project)return showCreate('project');if(!state.conversation)return showCreate('conversation');$('#message-input').focus();return;case 'toggle-panel':return togglePanel();case 'panel-tab':if(state.panel==='config')await flushWorkingDraft();state.panel=button.dataset.tab;state.view='list';return renderPanel();case 'config-section':await flushWorkingDraft();setConfigSection(button.dataset.section);state.config=null;await loadConfig();return renderConfig();case 'record':return openRecord(id);case 'artifact':return openArtifact(id,button.dataset.version);case 'reference':return openReference(JSON.parse(button.dataset.ref));case 'blob':return openBlob(id);case 'back-records':state.view='list';return loadRuntimeData();case 'refresh-runtime':state.runtimeDetail=null;return loadRuntimeData();case 'refresh-artifacts':return loadArtifactData(true,true);case 'edit-artifact':return editArtifact(button.dataset.kind,'project');case 'edit-artifact-default':return editArtifact(button.dataset.kind,'default');case 'clear-artifact-default':return clearArtifactDefault(button.dataset.kind);case 'toggle-artifact-content':{const kind=button.dataset.kind,expanded=new Set(state.artifactExpandedKinds);expanded.has(kind)?expanded.delete(kind):expanded.add(kind);state.artifactExpandedKinds=[...expanded];renderArtifactMonitor();return;}case 'run-debug':return openRunDebug(id);case 'more-run-debug':return loadRuntimeData(true);case 'refresh-records':return loadRecords();case 'more-records':return loadRecords(new URLSearchParams(state.recordQuery),true);case 'detail-tab':$('#detail-body').innerHTML=button.dataset.tab==='json'?'':recordHTML(publicView(state.record));if(button.dataset.tab==='json')showRecordJSON();return;case 'copy-record':await navigator.clipboard.writeText(JSON.stringify(publicView(state.record),null,2));return toast('已复制公开记录 JSON');case 'copy-artifact-content':await navigator.clipboard.writeText(textContent(publicView(state.recordContent)));return toast('已复制该固定版本内容');case 'relations':return showRelations();case 'versions':return showVersions();case 'compare-versions':return compareVersions();case 'timeline':return showTimeline();case 'new-agent':return showNewAgent();case 'configure-validation-stage':await flushWorkingDraft();state.configSection='stage';state.configScope='stage';state.configStage='step11';state.config=null;await loadConfig();return renderConfig();case 'publish-config':return publishConfig();case 'apply-schema-json':syncConfigEditors();invalidateSchemaValidation();renderSchema();return toast('Schema JSON 已应用，可继续编辑或发布');case 'validate-output-schema':return validateOutputSchema();case 'edit-schema-node':{const path=button.dataset.path,node=pointerGet(state.schemas[state.schemaName],path);modal('编辑 Schema 字段',`<p class="modal-note">${e(path)}<br>可设置 required、enum、items 等结构；应用后需要运行 Schema 校验。</p><form id="schema-node-form" data-path="${e(path)}"><textarea class="code-editor" name="json" rows="15" spellcheck="false">${e(JSON.stringify(node,null,2))}</textarea><div class="modal-footer"><button class="button primary" type="submit">应用字段修改</button></div></form>`);return;}case 'stop-task':await request(`${ppath()}/tasks/${encodeURIComponent(id)}/stop`,{method:'POST',body:{}});await refreshStatus();return toast('停止请求已提交，实际停止状态将由服务端更新');case 'resume-task':case 'focus-actions':return focusActions(id);case 'refresh-actions':return refreshActions(true,id);case 'submit-card-action':return submitCardAction(button);case 'submit-card-answer':return submitCardAnswer(button);case 'submit-custom-answer':return submitCustomAnswer(button);case 'decision-page':{const decisions=state.actionCards.filter(card=>['confirmation','question'].includes(card.kind));const position=decisions.findIndex(card=>card.id===activeDecisionCardId);const next=decisions[position+Number(button.dataset.step)];if(next){activeDecisionCardId=next.id;renderActionCards();}return;}case 'reset-card-draft':actionDrafts.delete(actionKey(id));renderActionCards();return;case 'queue-view':return queueView();case 'resume-queue':return focusActions();case 'older-activity':return loadOlderActivity();case 'older-history':{const data=await request(`${cpath()}/history?limit=100&cursor=${encodeURIComponent(state.historyCursor)}`);state.history=[...items(data).map(obj),...state.history];state.historyCursor=data.previous_cursor??(data.start_cursor>0?String(Math.max(0,data.start_cursor-100)):null);state.historyAll=true;renderThread();return;}}}
document.addEventListener('click',event=>{const button=event.target.closest('[data-action]');if(!button)return;event.preventDefault();busy(button,()=>handleAction(button));});
document.addEventListener('click',event=>{const button=event.target.closest('[data-action="toggle-artifact-content"]');if(!button)return;event.preventDefault();event.stopImmediatePropagation();const key=button.dataset.key,expanded=new Set(state.artifactExpandedKinds);expanded.has(key)?expanded.delete(key):expanded.add(key);state.artifactExpandedKinds=[...expanded];renderArtifactMonitor();},true);
document.addEventListener('submit',event=>{const form=event.target;if(form.id!=='artifact-editor-form')return;event.preventDefault();event.stopImmediatePropagation();void busy($('button[type=submit]',form),()=>saveArtifactEditor(form));},true);
document.addEventListener('submit',event=>{event.preventDefault();const form=event.target;if(form.matches('[data-card-form]')){void submitActionCard(form);return;}const submit=$('button[type=submit]',form);busy(submit,async()=>{const fields=new FormData(form);if(form.id==='login-form'){try{state.account=await request('/auth/login',{method:'POST',body:{username:fields.get('username'),password:fields.get('password')}});$('#modal').close();$('#account-name').textContent=state.account.display_name||state.account.account_id;await loadProjects();if(state.projects.length)await selectProject(state.projects[0].id);else renderStatus();}catch(error){$('#login-error').hidden=false;$('#login-error').textContent=error.message;throw error;}return;}if(form.id==='create-form'){const kind=form.dataset.kind;const data=await request(kind==='project'?'/projects':`${ppath()}/conversations`,{method:'POST',body:{title:fields.get('title').trim()}});$('#modal').close();if(kind==='project'){await loadProjects();await selectProject((data.project??data).id);if(!state.conversations.length){const c=await request(`${ppath()}/conversations`,{method:'POST',body:{title:'创作主会话'}});state.conversations=[c.conversation??c];await selectConversation(state.conversations[0].id);}}else{state.conversations.push(data.conversation??data);await selectConversation((data.conversation??data).id);}return;}if(form.id==='delete-project-form'){const id=form.dataset.id,wasCurrent=state.project?.id===id;await request(`/projects/${encodeURIComponent(id)}`,{method:'DELETE',body:{confirm:true}});$('#modal').close();if(wasCurrent){state.eventSource?.close();clearTimeout(scheduleRefresh.timer);state.projectEpoch=(state.projectEpoch??0)+1;sessionStorage.removeItem('branch-project');state.project=null;state.conversation=null;state.conversations=[];state.history=[];state.activity=[];state.activityTruncatedBefore=false;state.replyStreams.clear();state.chatEventSequences.clear();state.chatEventRows.clear();state.status={tasks:[],runs:[],queue:[],artifacts:[]};clearProjectActions(id);$('#project-title').textContent='故事工作室';renderConversations();renderThread();renderStatus();renderArtifacts();}await loadProjects();if(wasCurrent&&state.projects.length)await selectProject(state.projects[0].id);toast('项目已永久删除');return;}if(form.id==='message-form'){if(!requireProject()||!state.conversation)return;const input=$('#message-input'),text=input.value.trim();if(!text)return;const run=activeRun();const mode=run?$('#message-mode').value:'queue';if(run&&!mode)throw new Error('请选择“调整当前任务”或“排队处理”');await request(`${cpath()}/messages`,{method:'POST',body:{text,mode,target_run_id:run?.id??null,expected_run_row_version:run?.row_version??null}});input.value='';await Promise.all([refreshHistory(true),refreshStatus()]);return;}if(form.id==='source-form'){const file=fields.get('file'),text=String(fields.get('text')??'').trim();if(file?.size&&text)throw new Error('请选择文件或粘贴全文中的一种方式');if(!file?.size&&!text)throw new Error('请选择文件或粘贴原作全文');let body;if(file?.size){body=new FormData();body.set('file',file);body.set('conversation_id',state.conversation.id);if(fields.get('name'))body.set('name',fields.get('name'));}else body={text,conversation_id:state.conversation.id,name:fields.get('name')||'原作全文'};const result=await request(`${ppath()}/source`,{method:'POST',body});if(result.step1_started){$('#modal').close();await Promise.all([refreshHistory(true),refreshActivity(),refreshStatus()]);$('#thread').scrollTop=$('#thread').scrollHeight;toast('原作已保存，Step1 已启动');}else{$('#source-result').innerHTML=sourceReceiptHTML(result);await Promise.all([refreshHistory(),refreshActivity(),refreshStatus()]);$('button[type=submit]',form).hidden=true;$$('[data-action=close-modal]',form).forEach(b=>b.textContent='完成');toast('原作已保存，但 Step1 未启动：请调整模型预算',true);}return;}if(form.id==='new-agent-form'){await createAgent(String(fields.get('name')??''),String(fields.get('key')??''),String(fields.get('instructions')??''));return;}if(form.id==='record-filters'){state.recordType=String(fields.get('record_type'));const exact=String(fields.get('record_id')??'').trim();if(exact)return openRecord(exact);const params=new URLSearchParams();for(const[k,v]of fields.entries()){if(String(v).trim()&&k!=='record_id')params.set(k,k.startsWith('created_')?new Date(v).toISOString():v);}return loadRecords(params);}if(form.id==='schema-node-form'){const value=JSON.parse(fields.get('json'));if(!value||typeof value!=='object'||Array.isArray(value))throw new Error('字段定义必须为 JSON 对象');pointerSet(state.schemas[state.schemaName],form.dataset.path,value);state.schemaJsonTexts[state.schemaName]=JSON.stringify(state.schemas[state.schemaName],null,2);invalidateSchemaValidation();renderSchema();$('#modal').close();return;}});});
document.addEventListener('input',event=>{const t=event.target;if(t.closest('[data-card-form]')){updateActionInput(t);return;}if(t.id==='record-json-search'){const query=t.value.toLowerCase();const pub=publicView(state.record);$('#record-json-tree').innerHTML=query?`<pre class="code-box">${e(JSON.stringify(pub,null,2).split('\n').filter(line=>line.toLowerCase().includes(query)).join('\n')||'无匹配字段或内容')}</pre>`:jsonTree(pub);}if(t.matches('[data-harness-stage]')){const stage=t.dataset.harnessStage,h=state.configValues.prompts.harness;h.stages??={};h.runtime??={};h.stages[stage]=t.value;h.runtime[stage]='';dirty();renderInstructionsPreview();return;}if(t.matches('[data-config-path]')){const path=t.dataset.configPath;let value=t.value;if(path.startsWith('prompts.harness.legacy_stages.')){const stage=path.slice('prompts.harness.legacy_stages.'.length),h=state.configValues.prompts.harness;h.legacy_stages??={};h.legacy_stages[stage]=value;dirty();renderInstructionsPreview();return;}const numeric=t.dataset.valueType==='number'||t.dataset.valueType==='decimal';const previous=getPath(state.configValues,path);if(numeric&&(value===''||t.validity.badInput)){state.rawConfigFields[path]=value;dirty();return;}if(numeric)value=Number(value);if(typeof previous==='object'&&previous!==null){try{value=JSON.parse(value);}catch{state.rawConfigFields[path]=t.value;dirty();return;}}delete state.rawConfigFields[path];setPath(state.configValues,path,value);if(path==='model.name')updateModelEfforts();dirty();renderBudget();if(path.startsWith('prompts.'))renderInstructionsPreview();}if(t.id==='schema-json'){state.schemaJsonTexts[state.schemaName]=t.value;invalidateSchemaValidation();const node=$('#schema-validation');if(node)node.innerHTML=schemaValidationHTML();}if(t.matches('[data-schema-path]')){const node=pointerGet(state.schemas[state.schemaName],t.dataset.schemaPath);if(t.value==='')delete node[t.dataset.schemaKey];else node[t.dataset.schemaKey]=t.value;$('#schema-json').value=JSON.stringify(state.schemas[state.schemaName],null,2);state.schemaJsonTexts[state.schemaName]=$('#schema-json').value;invalidateSchemaValidation();const status=$('#schema-validation');if(status)status.innerHTML=schemaValidationHTML();}});
document.addEventListener('change',event=>{const t=event.target;if(t.closest('[data-card-form]')){updateActionInput(t);return;}if(t.id==='preview-step1-mode'){state.previewStep1Mode=t.value;renderInstructionsPreview();return;}if(t.id==='protocol-target'){void busy(t,async()=>{await flushWorkingDraft();state.protocolTarget=t.value;state.configScope=t.value==='global'?'project':t.value;state.configStage=t.value==='stage'?'step1':'coordinator';state.config=null;await loadConfig();renderConfig();});return;}if(t.id==='schema-file'){void busy(t,async()=>{const file=t.files?.[0];if(!file)return;if(file.size>2*1024*1024)throw new Error('JSON Schema 文件不能超过 2 MB');let parsed;try{parsed=JSON.parse(await file.text());}catch{throw new Error('上传文件不是有效 JSON');}if(!parsed||typeof parsed!=='object'||Array.isArray(parsed))throw new Error('JSON Schema 根节点必须是对象');state.schemas[state.schemaName]=parsed;state.schemaJsonTexts[state.schemaName]=JSON.stringify(parsed,null,2);state.uploadedSchemaFileName=file.name;invalidateSchemaValidation();renderSchema();toast('JSON Schema 已载入，请运行校验');});return;}if(t.id==='runtime-project-select'){void busy(t,async()=>{await selectProject(t.value);state.panel='data';state.view='list';await renderPanel();});return;}if(t.id==='config-agent'){const selected=t.value;void busy(t,async()=>{await flushWorkingDraft();state.configAgent=selected;state.configStage='coordinator';state.config=null;await loadConfig(selected);renderConfig();});return;}if(t.id==='config-stage'){const selected=t.value;void busy(t,async()=>{await flushWorkingDraft();state.configStage=selected;state.config=null;await loadConfig();renderConfig();});return;}if(t.id==='stage-agent'||t.id==='summary-agent'){const selected=t.value;void busy(t,async()=>{await flushWorkingDraft();await loadConfig(selected);state.configValues.prompts.stage_agents[state.configStage]=selected;dirty();renderConfig();});return;}if(t.id==='stage-agent-global'||t.id==='stage-agent-character'){const view=t.id==='stage-agent-global'?'global':'character',selected=t.value;void busy(t,async()=>{await flushWorkingDraft();await loadConfig(selected);state.configValues.prompts.step1_view_agents[view]=selected;dirty();renderConfig();});return;}if(t.id==='config-scope'){const selected=t.value;void busy(t,async()=>{await flushWorkingDraft();state.configScope=selected;state.config=null;await loadConfig();renderConfig();});return;}if(t.id==='validation-agent-enabled'){state.configValues.prompts.validation_enabled=t.checked;dirty();renderInstructionsPreview();return;}if(t.id==='stage-session'){state.configValues.context.session_sharing[state.configStage]=t.value||null;dirty();return;}if(t.matches('[data-stage-input]')){const kind=t.dataset.stageInput,values=new Set(state.configValues.context.stage_inputs[state.configStage]??[]);t.checked?values.add(kind):values.delete(kind);state.configValues.context.stage_inputs[state.configStage]=stageArtifactCatalog.map(([name])=>name).filter(name=>values.has(name));dirty();return;}if(t.matches('[data-output-toggle]')){state.configValues.output.structured[t.dataset.outputToggle]=t.checked;invalidateSchemaValidation();renderConfig();return;}if(t.id==='usage-group'){state.usageGroup=t.value;renderUsageSummary();}if(t.id==='schema-select'){const prior=$('#schema-json');if(prior)state.schemaJsonTexts[state.schemaName]=prior.value;state.schemaName=t.value;renderSchema();}});
$('#message-input').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();$('#message-form').requestSubmit();}});
document.addEventListener('change',event=>{const input=event.target;if(!input.matches('[data-artifact-select]'))return;const visible=new Set(state.artifactVisibleKinds);input.checked?visible.add(input.value):visible.delete(input.value);state.artifactVisibleKinds=[...visible];state.artifactLoading=true;renderArtifactMonitor();void loadArtifactData(false,true);});
document.addEventListener('keydown',event=>{if(event.target.matches('[data-plan-feedback]')&&event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();const button=event.target.closest('[data-card-form]')?.querySelector('.plan-feedback-send');if(button&&!button.disabled)void busy(button,()=>button.dataset.action==='submit-custom-answer'?submitCustomAnswer(button):submitCardAction(button));}});
$('#modal').addEventListener('cancel',event=>{if(!state.account)event.preventDefault();});
window.addEventListener('online',()=>{if(state.project){startEvents();scheduleRefresh();}});
window.addEventListener('pagehide',()=>state.eventSource?.close());
window.addEventListener('focus',()=>{void checkRelease();});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)void checkRelease();});
$('#update-later').addEventListener('click',()=>$('#update-modal').close());
$('#update-now').addEventListener('click',()=>window.location.reload());
void checkRelease();
setInterval(()=>{if(!document.hidden)void checkRelease();},30000);
init();
