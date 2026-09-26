import assert from 'node:assert/strict';
import test from 'node:test';
import {runtimeHTML} from '../branch_agent/static/runtime-view.mjs';

test('Run 调试视图展示实际输入、模型输出与工具结果',()=>{
  const run={id:'run-1',agent_key:'source_parser',state:'succeeded',created_at:'2026-09-24T00:00:00Z'};
  const task={scope:{stage:1},intent:'generate'};
  const data={items:[{run,task,agent_name:'原作切片 Agent',model:'gpt-5.6-luna',calls:1,input_tokens:12,output_tokens:8}],next_cursor:null};
  const detail={run,task,agent_name:'原作切片 Agent',calls:[{id:'call-1',context_snapshot_id:'snapshot-1',state:'succeeded',attempt:1,usage:{input_tokens:12,output_tokens:8}}],
    snapshots:[{id:'snapshot-1',model:'gpt-5.6-luna',instructions:{storage:'inline_text',text:'分析原作'},input_items:{storage:'inline_json',value:[{role:'user',content:'原作正文'}]}}],
    outputs:[{model_call_id:'call-1',history:{content:{storage:'inline_json',value:{output:[{type:'message',text:'分析完成'}]}}}}],
    tools:[{model_call_id:'call-1',tool_name:'get_artifact',state:'succeeded',arguments:{storage:'inline_json',value:{kind:'source_text'}},result:{storage:'inline_text',text:'读取成功'}}],
    artifacts:[{artifact:{artifact_kind:'source_global_events'},version:{version:1},content:{events:[]}}]};
  const html=runtimeHTML(data,run.id,detail);
  for(const text of ['Step 1 · 原作事件整理 · 原作切片 Agent','gpt-5.6-luna','分析原作','原作正文','分析完成','get_artifact','读取成功','source_global_events'])assert.ok(html.includes(text),text);
  assert.ok(!html.includes('Session、上下文与压缩'));
  assert.ok(html.includes('过程视图'));
  assert.ok(html.includes('历史 Run：根据审计记录重建'));
});

test('主 Agent 与压缩 Run 显示业务名称',()=>{
  const items=[
    {run:{id:'query-run',agent_key:'conversation_coordinator',state:'succeeded'},task:{intent:'query'}},
    {run:{id:'summary-run',agent_key:'context_summarizer',state:'succeeded'},task:{intent:'summarize',scope:{stage:3}}}
  ];
  const html=runtimeHTML({items},null,null);
  assert.ok(html.includes('对话协调主Agent'));
  assert.ok(html.includes('触发上下文压缩'));
  assert.ok(!html.includes('<strong>query</strong>'));
  assert.ok(!html.includes('<strong>summarize</strong>'));
  assert.ok(!html.includes('Step 3 ·'));
});

test('新 Run 的 SDK Trace 显示层级过程且不显示敏感载荷',()=>{
  const run={id:'run-2',agent_key:'writer',state:'succeeded',created_at:'2026-09-24T00:00:00Z'};
  const detail={run,task:{intent:'generate'},calls:[],tools:[],artifacts:[],
    trace_spans:[{span_id:'span-a',kind:'agent',name:'writer',started_at:'2026-09-24T00:00:00Z',ended_at:'2026-09-24T00:00:01Z'},
      {span_id:'span-b',parent_id:'span-a',kind:'function',name:'get_artifact',started_at:'2026-09-24T00:00:00Z',ended_at:'2026-09-24T00:00:01Z'}],events:[]};
  const html=runtimeHTML({items:[]},run.id,detail);
  assert.ok(html.includes('SDK Trace（仅保存过程元数据）'));
  assert.ok(html.includes('Agent 执行 · writer'));
  assert.ok(html.includes('工具调用 · get_artifact'));
  assert.ok(html.includes('--depth:1'));
});
