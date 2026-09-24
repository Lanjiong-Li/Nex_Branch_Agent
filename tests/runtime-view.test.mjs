import assert from 'node:assert/strict';
import test from 'node:test';
import {runtimeHTML} from '../branch_agent/static/runtime-view.mjs';

test('Run 调试视图展示实际输入、模型输出与工具结果',()=>{
  const run={id:'run-1',agent_key:'source_parser',state:'succeeded',created_at:'2026-09-24T00:00:00Z'};
  const task={scope:{stage:1},intent:'generate'};
  const data={items:[{run,task,model:'gpt-5.6-luna',calls:1,input_tokens:12,output_tokens:8}],next_cursor:null};
  const detail={run,task,calls:[{id:'call-1',context_snapshot_id:'snapshot-1',state:'succeeded',attempt:1,usage:{input_tokens:12,output_tokens:8}}],
    snapshots:[{id:'snapshot-1',model:'gpt-5.6-luna',instructions:{storage:'inline_text',text:'分析原作'},input_items:{storage:'inline_json',value:[{role:'user',content:'原作正文'}]}}],
    outputs:[{model_call_id:'call-1',history:{content:{storage:'inline_json',value:{output:[{type:'message',text:'分析完成'}]}}}}],
    tools:[{model_call_id:'call-1',tool_name:'get_artifact',state:'succeeded',arguments:{storage:'inline_json',value:{kind:'source_text'}},result:{storage:'inline_text',text:'读取成功'}}],
    artifacts:[{artifact:{artifact_kind:'source_views'},version:{version:1},content:{events:[]}}]};
  const html=runtimeHTML(data,run.id,detail);
  for(const text of ['Step 1 · 原作切分','gpt-5.6-luna','分析原作','原作正文','分析完成','get_artifact','读取成功','source_views'])assert.ok(html.includes(text),text);
  assert.ok(!html.includes('Session、上下文与压缩'));
});
