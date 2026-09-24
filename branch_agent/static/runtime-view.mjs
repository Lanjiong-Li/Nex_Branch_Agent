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
function runName(task,run){
  const stage=task?.scope?.stage;
  return stage?`Step ${stage} · ${stageNames[stage-1]??task?.intent??run.agent_key}`:task?.intent??run.agent_key;
}
function pill(value){return `<span class="badge ${value==='succeeded'?'good':['failed','interrupted'].includes(value)?'bad':['paused','waiting_user'].includes(value)?'warn':''}">${e(stateLabels[value]??value??'未知')}</span>`;}
export function runtimeHTML(data,selectedId,detail){
  const items=data?.items??[];
  const list=items.map(({run,task,model,calls,input_tokens,output_tokens})=>`<button class="run-debug-row ${run.id===selectedId?'selected':''}" data-action="run-debug" data-id="${e(run.id)}" aria-current="${run.id===selectedId?'true':'false'}"><span class="run-debug-row-head"><strong>${e(runName(task,run))}</strong>${pill(run.state)}</span><span class="run-debug-row-meta">${e(run.agent_key)} · ${e(model??'未调用模型')} · ${when(run.started_at??run.created_at)}</span><span class="run-debug-row-meta">${elapsed(run)} · ${calls} 轮 · ${Number(input_tokens??0).toLocaleString()} / ${Number(output_tokens??0).toLocaleString()} token</span></button>`).join('');
  return `<div class="runtime-hero"><div><h3>Agent Run</h3><p>选择一次运行，查看模型实际收到的输入和产生的输出。</p></div><button class="button" data-action="refresh-runtime">刷新</button></div><div class="run-debug-layout"><section class="run-debug-list" aria-label="Agent Run 列表">${list||'<p class="empty">这个项目还没有 Agent Run。</p>'}${data?.next_cursor?'<button class="button run-debug-more" data-action="more-run-debug">加载更多</button>':''}</section><section class="run-debug-detail" aria-label="Agent Run 输入输出">${detail?renderRunDetail(detail):selectedId?'<p class="loading">正在读取运行详情…</p>':'<p class="empty">选择左侧一次 Run 查看详情。</p>'}</section></div>`;
}
function renderRunDetail({run,task,calls=[],snapshots=[],outputs=[],tools=[],artifacts=[]}){
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
  return `<header class="run-debug-title"><div><h3>${e(runName(task,run))}</h3><p class="run-debug-meta">Agent ${e(run.agent_key)} · ${when(run.started_at??run.created_at)} · ${elapsed(run)} · Run ${e(run.id)}</p></div>${pill(run.state)}</header>${run.error?block('运行错误',run.error,true):''}<h4>输入与模型输出</h4>${turns||'<p class="empty">本次 Run 尚未调用模型。</p>'}<h4>最终保存的产物</h4>${saved||'<p class="small muted">本次 Run 尚未保存阶段产物；可查看上方模型原始输出。</p>'}`;
}
