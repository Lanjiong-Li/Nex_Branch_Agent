import test from 'node:test';
import assert from 'node:assert/strict';
import {publicView,textContent,escapeHTML,objectDiff,validateConfig,inputBudget,schemaFields,pointerGet,pointerSet,recordTypes} from '../branch_agent/static/ui-utils.mjs';
test('untrusted story content remains text, and public copies redact secrets without hiding usage',()=>{assert.equal(escapeHTML('<img src=x onerror="alert(1)">'),'&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');const actual=publicView({nested:{api_key:'secret',encrypted_content:'chain'},reasoning_tokens:1024,output_tokens:2048});assert.match(actual.nested.api_key,/遮蔽/);assert.match(actual.nested.encrypted_content,/遮蔽/);assert.equal(actual.reasoning_tokens,1024);assert.equal(textContent({storage:'inline_text',text:'<script>x</script>'}),'<script>x</script>');});
test('unknown budgets are not shown as zero and reasoning output shares context headroom',()=>{assert.equal(inputBudget({model:{max_output_tokens:32000},context:{input_token_cap:256000,safety_margin_tokens:2000}},{context_window:1050000}),256000);assert.equal(inputBudget({model:{max_output_tokens:8000},context:{input_token_cap:32000,safety_margin_tokens:2000}},{context_window:20000}),10000);assert.equal(inputBudget({model:{},context:{}},{}),null);});
test('config checks reject unsupported effort, oversized output, and missing window parameters',()=>{const values={model:{name:'test',max_output_tokens:128001,reasoning_effort:'max'},context:{input_token_cap:10000,safety_margin_tokens:2000,step1_source:{}}};const errors=validateConfig(values,{}, {models:{test:{context_window:100000,max_output_tokens:128000,reasoning_efforts:['medium']}}});assert.ok(errors.length>=5);});
test('sliding window parameters must be positive and leave room in the model input budget',()=>{const values={model:{name:'test',max_output_tokens:1000,reasoning_effort:'medium'},context:{input_token_cap:10000,safety_margin_tokens:1000,step1_source:{trigger_tokens:500000,window_tokens:8000}}},models={models:{test:{context_window:10000,max_output_tokens:2000,reasoning_efforts:['medium']}}};assert.ok(validateConfig(values,{},models).some(error=>error.includes('滑动窗口大小须小于有效输入预算')));values.context.step1_source.window_tokens=7999;assert.equal(validateConfig(values,{},models).length,0);values.context.step1_source.window_tokens=0;assert.ok(validateConfig(values,{},models).some(error=>error.includes('须为正整数')));});
test('version diffs align stable array IDs without confusing reordering with content changes',()=>{const a={nodes:[{id:'a',text:'old'},{id:'b',text:'unchanged'}]},b={nodes:[{id:'b',text:'unchanged'},{id:'a',text:'new'}]};assert.deepEqual(objectDiff(a,b),[{path:'/nodes/[id=a]/text',kind:'修改',before:'old',after:'new'}]);assert.equal(objectDiff({x:1},{})[0].kind,'删除');});
test('schema tree editing uses escaped JSON pointers and covers internal definitions',()=>{const s={type:'object',properties:{'a/b':{type:'string'}},required:['a/b'],$defs:{Nested:{type:'object',properties:{count:{type:'integer'}}}}};const fields=schemaFields(s);assert.equal(fields[0].path,'/properties/a~1b');assert.equal(fields[0].required,true);pointerSet(s,fields[0].path,{type:'number'});assert.equal(pointerGet(s,fields[0].path).type,'number');assert.ok(fields.some(f=>f.name==='count'));assert.equal(Object.keys(recordTypes).length,22);});
test('line comparison preserves unchanged text and locates inserted and replaced paragraphs',async()=>{const {lineDiff}=await import('../branch_agent/static/ui-utils.mjs');assert.deepEqual(lineDiff('第一行\n旧对白\n结尾','第一行\n新对白\n结尾'),[{kind:'same',text:'第一行'},{kind:'delete',text:'旧对白'},{kind:'add',text:'新对白'},{kind:'same',text:'结尾'}]);});
test('publishing an unrelated setting does not turn inherited stage defaults into project overrides',async()=>{const {configDelta,deepMerge}=await import('../branch_agent/static/ui-utils.mjs');const baseline={model:{max_output_tokens:8000},context:{input_token_cap:32000}};const delta=configDelta(baseline,{...baseline,model:{max_output_tokens:9000}});assert.deepEqual(delta,{model:{max_output_tokens:9000}});assert.deepEqual(deepMerge({retry:{max_retries:1}},delta),{retry:{max_retries:1},model:{max_output_tokens:9000}});});
test('display aggregates decimal costs without binary floating point or unknown-as-zero',async()=>{const {decimalSum}=await import('../branch_agent/static/ui-utils.mjs');assert.equal(decimalSum(['0.1','0.2']),'0.3');assert.equal(decimalSum(['1.001','2.09']),'3.091');assert.equal(decimalSum([null,undefined]),null);});

test('unregistered models cannot bypass the local publication check',()=>{const errors=validateConfig({model:{name:'unknown',max_output_tokens:8000,reasoning_effort:'medium'}},{},{models:{registered:{context_window:400000,max_output_tokens:128000,reasoning_efforts:['medium']}}});assert.ok(errors.some(error=>error.includes('未注册')));});
test('the current conversation exposes a paused child before its waiting parent, retaining the exact resume target',async()=>{
  const {selectActiveTask}=await import('../branch_agent/static/ui-utils.mjs');
  const tasks=[{id:'parent',conversation_id:'current',state:'waiting_user'},
    {id:'paused-coordinator',conversation_id:'current',state:'paused',pause_reason:'input_budget_exceeded'},
    {id:'foreign-running',conversation_id:'other',state:'running'}];
  assert.equal(selectActiveTask(tasks,'current').id,'paused-coordinator');
  assert.equal(selectActiveTask([...tasks,{id:'active-run',conversation_id:'current',state:'running',current_run_id:'run-id'}],'current').id,'active-run');
  assert.equal(selectActiveTask([{id:'failed-child',conversation_id:'current',state:'failed'},tasks[0]],'current').id,'failed-child');
  assert.equal(selectActiveTask([tasks[0]],'current').id,'parent');
  assert.equal(selectActiveTask([{id:'queued',conversation_id:'current',state:'queued'}],'current').id,'queued');
  assert.equal(selectActiveTask(tasks,'missing'),undefined);
  assert.deepEqual(tasks.map(task=>task.id),['parent','paused-coordinator','foreign-running']);
});
test('internal summary attempts never become the primary task control while their parent reports the error',async()=>{
  const {selectActiveTask}=await import('../branch_agent/static/ui-utils.mjs');
  const parent={id:'coordinator',conversation_id:'current',parent_task_id:null,intent:'query',state:'paused',pause_reason:'input_budget_exceeded'};
  for(const state of ['queued','running','stopping','paused','failed','stopped','waiting_user']){
    const summary={id:'summary-attempt',conversation_id:'current',parent_task_id:parent.id,intent:'summarize',state,updated_at:'2026-09-21T00:00:00Z'};
    assert.equal(selectActiveTask([parent,summary],'current').id,parent.id);
    assert.equal(selectActiveTask([summary],'current'),undefined);
    assert.equal(summary.state,state);
  }
  assert.equal(selectActiveTask([{...parent,intent:'summarize'}],'current').id,parent.id);
});
test('a failed child of a completed parent does not hide current confirmation or a real business pause',async()=>{
  const {selectActiveTask}=await import('../branch_agent/static/ui-utils.mjs');
  const parent={id:'completed-parent',conversation_id:'current',state:'succeeded'};
  const failure={id:'old-failure',parent_task_id:parent.id,intent:'generate',conversation_id:'current',state:'failed'};
  const waiting={id:'current-stage',conversation_id:'current',state:'waiting_user'};
  assert.equal(selectActiveTask([parent,failure,waiting],'current').id,waiting.id);
  const paused={id:'business-pause',parent_task_id:waiting.id,intent:'generate',conversation_id:'current',state:'paused'};
  assert.equal(selectActiveTask([parent,failure,waiting,paused],'current').id,paused.id);
  assert.equal(selectActiveTask([failure,waiting],'current').id,failure.id);
});
test('paused tasks show the latest closed Run output-limit cause after current_run_id is cleared',async()=>{
  const {taskIssue}=await import('../branch_agent/static/ui-utils.mjs');
  const task={id:'task',state:'paused',pause_reason:'task_failed',current_run_id:null};
  const issue=taskIssue(task,{tasks:[task],runs:[{id:'latest-run',task_id:'task',state:'paused',created_at:'2026-09-21T02:00:00Z',error:{code:'output_limit_exceeded',message:'raw response must not be copied',details:{max_output_tokens:8000}}},{id:'old-run',task_id:'task',state:'failed',created_at:'2026-09-21T01:00:00Z',error:{code:'operation_uncertain'}}]});
  assert.equal(issue.code,'output_limit_exceeded');assert.match(issue.message,/输出.*上限/);
  assert.equal(issue.task_id,'task');assert.equal(issue.run_id,'latest-run');assert.equal(issue.details.max_output_tokens,8000);
  assert.ok(!JSON.stringify(issue).includes('raw response'));
});
test('a genuine uncertain operation is not overwritten by an older output-limit failure',async()=>{
  const {taskIssue}=await import('../branch_agent/static/ui-utils.mjs');
  const task={id:'task',state:'paused',pause_reason:'operation_uncertain',updated_at:'2026-09-21T03:00:00Z'};
  const issue=taskIssue(task,{tasks:[task],runs:[{id:'old-run',task_id:'task',state:'failed',created_at:'2026-09-21T01:00:00Z',error:{code:'output_limit_exceeded'}}]});
  assert.equal(issue.code,'operation_uncertain');assert.match(issue.message,/无法确认/);assert.equal(issue.run_id,null);
  for(const code of ['APIConnectionError','APITimeoutError']){
    const transport={id:'latest-transport',task_id:task.id,state:'paused',error:{code,details:{known_outcome:false}}};
    const current=taskIssue(task,{tasks:[task],runs:[transport]});
    assert.equal(current.code,'operation_uncertain');assert.equal(current.run_id,transport.id);assert.match(current.message,/无法确认/);
    const known=taskIssue(task,{tasks:[task],runs:[{...transport,error:{code,details:{known_outcome:true}}}]});
    assert.equal(known.code,'operation_uncertain');assert.equal(known.run_id,null);
  }
});
test('child_blocked explains the current child while preserving the parent control target',async()=>{
  const {taskIssue}=await import('../branch_agent/static/ui-utils.mjs');
  const parent={id:'parent',state:'paused',pause_reason:'child_blocked'};
  const child={id:'child',parent_task_id:'parent',intent:'generate',state:'paused',pause_reason:'output_limit_exceeded',updated_at:'2026-09-21T03:00:00Z'};
  const finished={id:'finished',parent_task_id:'parent',state:'succeeded'};
  const old={id:'historical',parent_task_id:'finished',state:'failed',pause_reason:'operation_uncertain',updated_at:'2026-09-21T04:00:00Z'};
  const issue=taskIssue(parent,{tasks:[parent,child,finished,old],runs:[{id:'child-run',task_id:child.id,state:'paused',error:{code:'output_limit_exceeded'}}]});
  assert.equal(issue.task_id,'child');assert.equal(issue.control_task_id,'parent');assert.equal(issue.run_id,'child-run');assert.equal(issue.code,'output_limit_exceeded');
  assert.equal(parent.id,'parent');assert.equal(parent.pause_reason,'child_blocked');
});
test('a new parent run and a successful retry exclude stale child and Run errors',async()=>{
  const {taskIssue}=await import('../branch_agent/static/ui-utils.mjs');
  const parent={id:'parent',state:'paused',pause_reason:'child_blocked'};
  const child={id:'old-child',parent_task_id:'parent',state:'failed',pause_reason:'output_limit_exceeded',updated_at:'2026-09-21T01:00:00Z'};
  const issue=taskIssue(parent,{tasks:[parent,child],runs:[{id:'parent-new',task_id:'parent',state:'paused',created_at:'2026-09-21T02:00:00Z',error:{code:'child_blocked'}}]});
  assert.equal(issue.code,'child_blocked');assert.equal(issue.task_id,'parent');
  const stillPaused=taskIssue(parent,{tasks:[parent,{...child,state:'paused'}],runs:[{id:'parent-new',task_id:'parent',state:'paused',created_at:'2026-09-21T02:00:00Z',error:{code:'child_blocked'}}]});
  assert.equal(stillPaused.task_id,child.id);assert.equal(stillPaused.code,'output_limit_exceeded');
  assert.equal(taskIssue({...child,state:'running',pause_reason:null},{tasks:[child],runs:[{id:'old',task_id:child.id,state:'failed',error:{code:'output_limit_exceeded'}}]}),null);
  const paused={id:'retry',state:'paused',pause_reason:'cost_limit'};
  const current=taskIssue(paused,{tasks:[paused],runs:[{id:'old',task_id:'retry',state:'failed',created_at:'2026-09-21T01:00:00Z',error:{code:'output_limit_exceeded'}},{id:'new',task_id:'retry',state:'succeeded',created_at:'2026-09-21T02:00:00Z',error:null}]});
  assert.equal(current.code,'cost_limit');assert.equal(current.run_id,null);
});
test('error summaries expose controlled codes and allowlisted diagnostics without provider payload or credentials',async()=>{
  const {describeFailure}=await import('../branch_agent/static/ui-utils.mjs');
  const issue=describeFailure('output_limit_exceeded',{message:'Authorization: Bearer private-token',details:{provider_status:'incomplete',incomplete_reason:'max_output_tokens',max_output_tokens:8000,raw_response:{api_key:'secret'},request_headers:{Authorization:'secret'},error_code:'some_provider_code'}});
  assert.deepEqual(issue.details,{provider_status:'incomplete',incomplete_reason:'max_output_tokens',max_output_tokens:8000,error_code:'some_provider_code'});
  assert.ok(!JSON.stringify(issue).includes('secret'));assert.ok(!JSON.stringify(issue).includes('private-token'));
  assert.equal(describeFailure('sk-proj-private-key',{}).code,'unclassified_error');
});
test('provider terminal diagnostics distinguish content filtering, explicit failure, and token exhaustion',async()=>{
  const {describeFailure}=await import('../branch_agent/static/ui-utils.mjs');
  const filtered=describeFailure('response_incomplete',{details:{known_outcome:true,terminal_status:'incomplete',terminal_event:'response.incomplete',incomplete_reason:'content_filter',provider_response_id:'resp_saved',model_call_id:'12345678-1234-4234-8234-123456789abc'}});
  assert.match(filtered.message,/内容策略/);assert.ok(!filtered.message.includes('输出上限'));
  assert.equal(filtered.details.terminal_status,'incomplete');assert.equal(filtered.details.terminal_event,'response.incomplete');assert.equal(filtered.details.known_outcome,true);
  assert.equal(filtered.details.model_call_id,'12345678-1234-4234-8234-123456789abc');
  const failed=describeFailure('response_failed',{details:{terminal_status:'failed',provider_error_code:'server_error'}});
  assert.match(failed.message,/明确.*失败/);assert.equal(failed.details.provider_error_code,'server_error');
  assert.ok(!failed.message.includes('输出上限'));assert.match(describeFailure('AuthenticationError').message,/身份验证/);
  for(const [code,meaning] of [['authentication_failed',/身份验证/],['permission_denied',/权限/],['quota_exhausted',/额度|余额/],['rate_limit_exceeded',/频率/],['model_not_found',/模型/],['context_length_exceeded',/上下文长度/],['invalid_output_schema',/Schema/]]){
    assert.match(describeFailure(code).message,meaning);
  }
});
