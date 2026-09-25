import {escapeHTML as e, publicView, stageNames, stateLabels} from './ui-utils.mjs';

const when=value=>value?new Date(value).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'未记录';
const elapsed=run=>{
  if(!run.started_at)return '未开始';
  const seconds=Math.max(0,Math.round((new Date(run.finished_at??Date.now())-new Date(run.started_at))/1000));
  return seconds<60?`${seconds} 秒`:`${Math.floor(seconds/60)} 分 ${seconds%60} 秒`;
};
const content=value=>{
  if(value?.storage==='inline_text')return value.text;
  if(value?.storage==='inline_json')return value.value;
  return value;
};
const formatted=value=>typeof value==='string'?value:JSON.stringify(publicView(value),null,2);
const modelText=history=>{
  const saved=content(history.content);
  const parts=(saved?.output??[]).flatMap(item=>(item.content??[]).filter(part=>part.type==='output_text').map(part=>part.text??''));
  return parts.join('\n').trim();
};
function block(label,value,open=false){
  if(value==null)return '';
  return `<details class="run-debug-block" ${open?'open':''}><summary>${e(label)}</summary><pre>${e(formatted(content(value)))}</pre></details>`;
}
function runName(task,run,agentName){
  if(task?.intent==='query')return '对话协调主Agent';
  if(task?.intent==='summarize')return '触发上下文压缩';
  const stage=task?.scope?.stage;
  if(stage)return `Step ${stage} · ${stageNames[stage-1]??task?.intent??run.agent_key} · ${agentName??run.agent_key}`;
  return task?.intent??run.agent_key;
}
function pill(value){return `<span class="badge ${value==='succeeded'?'good':['failed','interrupted'].includes(value)?'bad':['paused','waiting_user'].includes(value)?'warn':''}">${e(stateLabels[value]??value??'未知')}</span>`;}
const processLabels={task:'SDK 任务',turn:'模型轮次',agent:'Agent 执行',generation:'模型调用',function:'工具调用',handoff:'Agent 交接',guardrail:'护栏校验',custom:'Harness 操作',response:'模型调用',transcription:'语音转写',speech:'语音生成',image_generation:'图像生成'};
const eventLabels={'run.transitioned':'Run 状态变化','task.transitioned':'任务状态变化','checkpoint.saved':'检查点已保存','artifact.presented':'产物已呈现','graph.checked':'Graph 校验','project.delivered':'最终交付','repair.scheduled':'返修已安排','confirmation.requested_by_agent':'Agent 请求确认','session.compacted':'上下文已压缩','source.window_completed':'原作窗口已完成'};
const duration=(start,end)=>{
  if(!start||!end)return '';
  const ms=Math.max(0,new Date(end)-new Date(start));
  return ms<1000?`${ms} ms`:`${(ms/1000).toFixed(1)} 秒`;
};
function processHTML({run,calls,tools,artifacts,trace_spans=[],events=[]}){
  const spans=trace_spans.length?trace_spans:[
    ...calls.map((call,index)=>({kind:'generation',name:`第 ${index+1} 轮`,started_at:call.started_at??call.created_at,ended_at:call.finished_at,error_code:call.error?.code,span_id:call.id,metadata:{usage:call.usage??{}}})),
    ...tools.map(tool=>({kind:'function',name:tool.tool_name,started_at:tool.started_at??tool.created_at,ended_at:tool.finished_at,error_code:tool.error?.code,span_id:tool.id,parent_id:tool.model_call_id}))
  ];
  const byId=new Map(spans.map(span=>[span.span_id,span]));
  function depth(span){let count=0,parent=span.parent_id;const seen=new Set([span.span_id]);while(parent&&byId.has(parent)&&!seen.has(parent)&&count<4){count++;seen.add(parent);parent=byId.get(parent).parent_id;}return count;}
  const entries=[
    ...spans.map(span=>({at:span.started_at,kind:'span',span})),
    ...events.map(event=>({at:event.created_at,kind:'event',event})),
    ...artifacts.map(({artifact,version})=>({at:version.created_at,kind:'artifact',artifact,version}))
  ].sort((a,b)=>String(a.at??'').localeCompare(String(b.at??'')));
  const rows=entries.map(item=>{
    if(item.kind==='span'){
      const span=item.span,usage=span.metadata?.usage??{};
      const tokens=usage.total_tokens!=null?` · ${Number(usage.total_tokens).toLocaleString()} token`:'';
      const label=processLabels[span.kind]??span.kind;
      return `<li class="run-process-item run-process-${e(span.kind)}" style="--depth:${depth(span)}"><span class="run-process-time">${when(item.at)}</span><span class="run-process-main"><strong>${e(label)}${span.name===label?'':` · ${e(span.name)}`}</strong><small>${e(duration(span.started_at,span.ended_at)||'运行中')}${tokens}${span.error_code?` · 错误 ${e(span.error_code)}`:''}</small></span></li>`;
    }
    if(item.kind==='artifact')return `<li class="run-process-item run-process-artifact"><span class="run-process-time">${when(item.at)}</span><span class="run-process-main"><strong>保存产物 · ${e(item.artifact?.artifact_kind??'阶段产物')} v${e(item.version.version)}</strong></span></li>`;
    const event=item.event,change=event.payload?.to_state?` · ${stateLabels[event.payload.to_state]??event.payload.to_state}`:'';
    return `<li class="run-process-item run-process-event"><span class="run-process-time">${when(item.at)}</span><span class="run-process-main"><strong>${e(eventLabels[event.event_name]??event.event_name)}${e(change)}</strong></span></li>`;
  }).join('');
  const origin=trace_spans.length?'SDK Trace（仅保存过程元数据）':'历史 Run：根据审计记录重建';
  return `<section class="run-process"><div class="run-process-head"><h4>过程视图</h4><span>${e(origin)}</span></div><ol class="run-process-list">${rows||'<li class="empty">这次 Run 尚无过程记录。</li>'}</ol></section>`;
}
export function runtimeHTML(data,selectedId,detail){
  const items=data?.items??[];
  const list=items.map(({run,task,agent_name,model,calls,input_tokens,output_tokens})=>`<button class="run-debug-row ${run.id===selectedId?'selected':''}" data-action="run-debug" data-id="${e(run.id)}" aria-current="${run.id===selectedId?'true':'false'}"><span class="run-debug-row-head"><strong>${e(runName(task,run,agent_name))}</strong>${pill(run.state)}</span><span class="run-debug-row-meta">${e(run.agent_key)} · ${e(model??'未调用模型')} · ${when(run.started_at??run.created_at)}</span><span class="run-debug-row-meta">${elapsed(run)} · ${calls} 轮 · ${Number(input_tokens??0).toLocaleString()} / ${Number(output_tokens??0).toLocaleString()} token</span></button>`).join('');
  return `<div class="runtime-hero"><div><h3>Agent Run</h3><p>选择一次运行，查看 Agent、模型和工具的执行过程，展开下方可查看实际输入输出。</p></div><button class="button" data-action="refresh-runtime">刷新</button></div><div class="run-debug-layout"><section class="run-debug-list" aria-label="Agent Run 列表">${list||'<p class="empty">这个项目还没有 Agent Run。</p>'}${data?.next_cursor?'<button class="button run-debug-more" data-action="more-run-debug">加载更多</button>':''}</section><section class="run-debug-detail" aria-label="Agent Run 过程与输入输出">${detail?renderRunDetail(detail):selectedId?'<p class="loading">正在读取运行详情…</p>':'<p class="empty">选择左侧一次 Run 查看详情。</p>'}</section></div>`;
}
function renderRunDetail({run,task,agent_name,calls=[],snapshots=[],outputs=[],tools=[],artifacts=[],trace_spans=[],events=[]}){
  const snapshotsById=new Map(snapshots.map(row=>[row.id,row]));
  const outputsByCall=new Map();
  for(const row of outputs){const list=outputsByCall.get(row.model_call_id)??[];list.push(row.history);outputsByCall.set(row.model_call_id,list);}
  const toolsByCall=new Map();
  for(const row of tools){const list=toolsByCall.get(row.model_call_id)??[];list.push(row);toolsByCall.set(row.model_call_id,list);}
  const turns=calls.map((call,index)=>{
    const snapshot=snapshotsById.get(call.context_snapshot_id);
    const response=outputsByCall.get(call.id)??[];
    const toolList=toolsByCall.get(call.id)??[];
    return `<article class="run-debug-turn"><header><strong>模型调用 ${index+1}${call.attempt>1?` · 尝试 ${call.attempt}`:''}</strong>${pill(call.state)}</header><p class="run-debug-meta">${e(snapshot?.model??'模型未记录')} · 推理 ${e(snapshot?.reasoning_effort??'未记录')} · 输入 ${Number(call.usage?.input_tokens??0).toLocaleString()} / 输出 ${Number(call.usage?.output_tokens??0).toLocaleString()} token</p>${block('实际 instructions',snapshot?.instructions,index===0)}${block('实际输入',snapshot?.input_items,true)}${block('工具定义',snapshot?.tool_definitions)}${block('output_type',snapshot?.output_schema)}${toolList.map(tool=>`<div class="run-debug-tool"><strong>工具调用 · ${e(tool.tool_name)}</strong>${pill(tool.state)}${block('参数',tool.arguments)}${block('执行结果',tool.result, true)}</div>`).join('')}${response.map((history,i)=>`${modelText(history)?block(`模型文本输出${response.length>1?` ${i+1}`:''}`,modelText(history),true):''}${block('模型原始响应',history.content,!modelText(history))}`).join('')}${call.error?block('调用错误',call.error,true):''}${!response.length&&!call.error?'<p class="small muted">尚无模型输出。</p>':''}</article>`;
  }).join('');
  const saved=artifacts.map(({artifact,version,content:artifactContent})=>`<article class="run-debug-artifact"><strong>${e(artifact?.artifact_kind??version.output_schema?.schema_id??'阶段产物')} · v${e(version.version)}</strong>${block('保存的产物',artifactContent)}</article>`).join('');
  return `<header class="run-debug-title"><div><h3>${e(runName(task,run,agent_name))}</h3><p class="run-debug-meta">Agent ${e(run.agent_key)} · ${when(run.started_at??run.created_at)} · ${elapsed(run)} · Run ${e(run.id)}</p></div>${pill(run.state)}</header>${run.error?block('运行错误',run.error,true):''}${processHTML({run,calls,tools,artifacts,trace_spans,events})}<details class="run-debug-evidence"><summary>查看输入、模型输出与保存的产物</summary><h4>输入与模型输出</h4>${turns||'<p class="empty">本次 Run 尚未调用模型。</p>'}<h4>最终保存的产物</h4>${saved||'<p class="small muted">本次 Run 尚未保存阶段产物；可查看上方模型原始输出。</p>'}</details>`;
}
