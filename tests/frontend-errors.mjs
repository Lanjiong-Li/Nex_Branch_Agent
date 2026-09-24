/** Focused error-display browser regression with local API fixtures; no provider or business writes. */
import assert from 'node:assert/strict';
import http from 'node:http';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const project={id:'11111111-1111-4111-8111-111111111111',title:'错误展示 · 明确测试数据'};
const conversation={id:'22222222-2222-4222-8222-222222222222',title:'测试会话'};
const parent={id:'33333333-3333-4333-8333-333333333333',record_type:'task',conversation_id:conversation.id,state:'paused',pause_reason:'child_blocked',updated_at:'2026-09-21T03:00:00Z'};
const child={id:'44444444-4444-4444-8444-444444444444',record_type:'task',conversation_id:conversation.id,parent_task_id:parent.id,intent:'generate',scope:{stage:9},state:'paused',pause_reason:'output_limit_exceeded',current_run_id:null,updated_at:'2026-09-21T02:59:00Z'};
const run={id:'55555555-5555-4555-8555-555555555555',record_type:'run',task_id:child.id,state:'paused',created_at:'2026-09-21T02:00:00Z',error:{code:'output_limit_exceeded',message:'模型输出达到本次上限。',retryable:false,details:{provider_status:'incomplete',incomplete_reason:'max_output_tokens',max_output_tokens:8000,raw_response:'RAW_PROVIDER_SECRET',request_headers:{Authorization:'Bearer PRIVATE_CREDENTIAL'}}}};
const oldBranch={id:'66666666-6666-4666-8666-666666666666',record_type:'task',conversation_id:conversation.id,parent_task_id:parent.id,state:'succeeded'};
const oldFailure={id:'77777777-7777-4777-8777-777777777777',record_type:'task',conversation_id:conversation.id,parent_task_id:oldBranch.id,state:'failed',pause_reason:'operation_uncertain',updated_at:'2026-09-21T04:00:00Z'};
let tasks=[parent,child,oldBranch,oldFailure],runs=[run];
const writes=[];
const server=http.createServer(async(req,res)=>{
  const url=new URL(req.url,'http://127.0.0.1');
  if(!url.pathname.startsWith('/api/')){
    const file=url.pathname==='/'?'index.html':url.pathname.replace('/static/','');
    try{const body=await readFile(path.join(root,'branch_agent/static',file));res.setHeader('Content-Type',file.endsWith('.html')?'text/html':file.endsWith('.css')?'text/css':'text/javascript');res.end(body);}catch{res.statusCode=404;res.end();}return;
  }
  if(req.method!=='GET'){writes.push(url.pathname);res.statusCode=403;res.end(JSON.stringify({error:{code:'read_only_fixture',message:'禁止写入'}}));return;}
  if(url.pathname.endsWith('/events')){res.writeHead(200,{'Content-Type':'text/event-stream'});res.write(': fixture\n\n');return;}
  const suffix=url.pathname.replace('/api/branch-agent/v1','');let data={};
  if(suffix==='/auth/me')data={account_id:'fixture',display_name:'测试创作者',csrf_token:'fixture'};
  else if(suffix==='/projects')data={items:[project]};
  else if(suffix.endsWith('/conversations'))data={items:[conversation]};
  else if(suffix.endsWith('/history'))data={items:[]};
  else if(suffix.endsWith('/actions'))data={cards:tasks.filter(t=>t.id===parent.id&&t.state==='paused').map(t=>({id:'task:'+t.id,revision:'fixture-r1',kind:'recovery',title:'任务需要处理',description:'保留旧记录，选择明确的处理方式。',details:[],targets:[],actions:[{id:'restart',label:'按已发布配置重新开始',fields:[],disabled_reason:'只读错误展示测试'}]}))};
  else if(suffix.endsWith('/status'))data={tasks,runs,queue:[],artifacts:[]};
  else if(suffix.endsWith('/records'))data={items:[...tasks,...runs],next_cursor:null};
  else if(suffix.includes('/records/'))data=[...tasks,...runs].find(row=>row.id===suffix.split('/').at(-1));
  res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data??{}));
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true,channel:process.env.BROWSER_CHANNEL||'chrome'});
try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  const base=`http://127.0.0.1:${server.address().port}`;
  await page.goto(base);
  const strip=page.locator('#run-status');
  await strip.getByText('模型输出达到本次上限，结果未完整生成。',{exact:false}).waitFor();
  assert.match(await strip.innerText(),/output_limit_exceeded/);
  assert.equal(await strip.getByRole('button',{name:'错误详情'}).getAttribute('data-id'),run.id);
  assert.equal(await strip.getByRole('button',{name:'处理暂停',exact:true}).getAttribute('data-id'),'task:'+parent.id);
  await strip.getByRole('button',{name:'处理暂停',exact:true}).click();
  assert.equal(await page.locator('#resume-form').count(),0);
  assert.match(await page.locator(`[data-card-form="task:${parent.id}"]`).innerText(),/模型输出达到本次上限/);
  await strip.getByRole('button',{name:'错误详情'}).click();
  await page.locator('#panel-content [data-error-code="output_limit_exceeded"]').waitFor();
  assert.ok((await page.locator('#panel-content').innerText()).includes(run.id));
  assert.ok((await page.locator('#panel-content').innerText()).includes('max_output_tokens: 8000'));
  const text=await page.locator('#panel-content').innerText();
  assert.ok(!text.includes('RAW_PROVIDER_SECRET'));assert.ok(!text.includes('PRIVATE_CREDENTIAL'));
  await page.screenshot({path:path.join(root,'tests/frontend-errors.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth),false);
  await page.setViewportSize({width:1440,height:1000});
  tasks=[{...child,pause_reason:'operation_uncertain'}];
  runs=[run,{...run,id:'88888888-8888-4888-8888-888888888888',created_at:'2026-09-21T03:00:00Z',error:{code:'APITimeoutError',message:'请求状态无法确认。',details:{known_outcome:false,error_code:'transport_timeout'}}}];
  await page.reload();await strip.getByText('模型请求的完成状态无法确认，需要先核对调用记录。',{exact:false}).waitFor();
  assert.ok(!(await strip.innerText()).includes('output_limit_exceeded'));
  assert.equal(await strip.getByRole('button',{name:'错误详情'}).getAttribute('data-id'),runs[1].id);
  tasks=[{...child,pause_reason:'response_incomplete'}];
  runs=[{...run,error:{code:'response_incomplete',message:'受控错误说明',details:{terminal_status:'incomplete',terminal_event:'response.incomplete',incomplete_reason:'content_filter'}}}];
  await page.reload();await strip.getByText('供应商因内容策略限制返回未完成的响应，该响应未作为有效产物。',{exact:false}).waitFor();
  assert.ok(!(await strip.innerText()).includes('output_limit_exceeded'));
  tasks=[{...child,pause_reason:'response_failed'}];
  runs=[{...run,error:{code:'response_failed',message:'受控错误说明',details:{terminal_status:'failed',provider_error_code:'server_error'}}}];
  await page.reload();await strip.getByText('供应商已明确返回失败结果，请查看对应错误分类。',{exact:false}).waitFor();
  await strip.getByRole('button',{name:'错误详情'}).click();
  await page.locator('#panel-content [data-error-code="response_failed"]').waitFor();
  assert.match(await page.locator('#panel-content [data-error-code="response_failed"]').innerText(),/provider_error_code: server_error/);
  tasks=[oldBranch,oldFailure,{...parent,state:'waiting_user',pause_reason:null}];runs=[];
  await page.reload();await strip.getByText('等待你回复',{exact:true}).waitFor();
  assert.equal(await strip.locator('[data-error-code]').count(),0);
  assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
  console.log('PASS: current child output limit, exact Run detail and unchanged recovery-card target, safe diagnostics, real uncertain outcome, content filter and provider failure, historical failure exclusion, no business writes.');
}finally{await browser.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
