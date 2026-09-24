/** Real Chrome interaction with local action fixtures. Never connects to a model or real project. */
import assert from 'node:assert/strict';
import http from 'node:http';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const project={id:'action-project',title:'待处理操作 · 本地测试数据'},conversation={id:'action-conversation',title:'创作主会话'};
const make=(id,kind,title,actions)=>({id,revision:'r1',kind,title,description:'基于已保存的真实状态与固定版本选择处理方式。',details:[{label:'固定版本',value:'产物 v3'},{label:'已发布配置',value:'step9 · revision 4'}],targets:[],actions});
const confirmation=make('pending:confirmation','confirmation','确认改编方案',[{id:'confirm',label:'确认此版本',fields:[]},{id:'request_changes',label:'提出修改',fields:[{name:'text',label:'修改要求',type:'textarea',required:true}]}]);
const question=make('pending:question','question','故事采用哪个视角？',[{id:'answer',label:'提交回答',fields:[{name:'answer',label:'选择视角',type:'select',required:true,default:'first',options:[{value:'first',label:'第一人称'},{value:'third',label:'第三人称'},{value:'__custom__',label:'自行填写'}]},{name:'text',label:'补充说明',type:'textarea'}]}]);
const recovery=make('task:paused','recovery','输出达到上限，选择恢复方式',[{id:'restart',label:'按已发布配置重开',fields:[{name:'accept_unknown_cost',label:'我已知晓旧调用费用尚未核实',type:'checkbox',required:true,default:true},{name:'max_cost_usd',label:'新任务费用上限（USD）',type:'number',required:true,default:20},{name:'max_active_seconds',label:'新任务时间上限（秒）',type:'number',required:true,default:3600}]}]);
const queue=make('queue:one','queue','排队请求：调整第一章',[{id:'release_one',label:'放行这一条',fields:[]},{id:'cancel',label:'取消这一条',fields:[]}]);
const cancel=make('queue:two','queue','排队请求：不再需要的修改',queue.actions);
const unknown=make('task:unknown','recovery','请求结果尚未确认',[{id:'restart',label:'重开任务',fields:[],disabled_reason:'请先核实请求结果，不能再次执行。'}]);
let cards=structuredClone([confirmation,question,recovery,queue,cancel,unknown]),seq=0,nextError=null,delay=0,actionReads=0;
const writes=[],events=new Set();
function broadcast(){for(const res of events)res.write(`id: ${++seq}\ndata: ${JSON.stringify({sequence:seq})}\n\n`);}
const server=http.createServer(async(req,res)=>{
  const url=new URL(req.url,'http://127.0.0.1');
  if(!url.pathname.startsWith('/api/')){const file=url.pathname==='/'?'index.html':url.pathname.replace('/static/','');try{res.setHeader('Content-Type',file.endsWith('.html')?'text/html':file.endsWith('.css')?'text/css':'text/javascript');res.end(await readFile(path.join(root,'branch_agent/static',file)));}catch{res.statusCode=404;res.end();}return;}
  res.setHeader('Content-Type','application/json');
  if(req.method==='POST'){
    let raw='';for await(const chunk of req)raw+=chunk;const body=JSON.parse(raw);writes.push({path:url.pathname,body,key:req.headers['idempotency-key'],csrf:req.headers['x-csrf-token']});
    if(!url.pathname.endsWith('/actions')){res.statusCode=403;res.end(JSON.stringify({error:{code:'fixture_forbidden',message:'禁止调用其他业务写接口'}}));return;}
    if(delay)await new Promise(resolve=>setTimeout(resolve,delay));
    if(nextError){const error=nextError;nextError=null;res.statusCode=error.status;res.end(JSON.stringify({error:{code:error.code,message:error.message}}));return;}
    const card=cards.find(c=>c.id===body.card_id);if(!card||body.expected_revision!==card.revision){res.statusCode=409;res.end(JSON.stringify({error:{code:'action_stale',message:'卡片版本已更新，请重新核对。'}}));return;}
    cards=cards.filter(c=>c.id!==body.card_id);res.end(JSON.stringify({status:'applied',message_id:'fixture-message',event_id:'fixture-event'}));return;
  }
  if(url.pathname.endsWith('/events')){res.writeHead(200,{'Content-Type':'text/event-stream'});res.write(': connected\n\n');events.add(res);req.on('close',()=>events.delete(res));return;}
  const suffix=url.pathname.replace('/api/branch-agent/v1','');let data={};
  if(suffix==='/auth/me')data={account_id:'fixture',display_name:'测试创作者',csrf_token:'fixture-csrf'};
  else if(suffix==='/projects')data={items:[project]};
  else if(suffix.endsWith('/conversations'))data={items:[conversation]};
  else if(suffix.endsWith('/history'))data={items:[{id:'message',role:'assistant',sequence:1,content:'请在下方选择要处理的事项。所有按钮都通过明确的操作接口执行。'}]};
  else if(suffix.endsWith('/status'))data={tasks:[{id:'paused',record_type:'task',conversation_id:conversation.id,state:'paused',pause_reason:'output_limit_exceeded'}],runs:[],queue:[{id:'one'},{id:'two'}],artifacts:[]};
  else if(suffix.endsWith('/actions')){data={cards};actionReads++;}
  else if(suffix.endsWith('/records'))data={items:[]};
  res.end(JSON.stringify(data));
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true,channel:process.env.BROWSER_CHANNEL||'chrome'});
try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  const card=id=>page.locator(`[data-card-form="${id}"]`),choose=(id,action)=>card(id).locator(`[data-card-choice="${action}"]`),submit=id=>card(id).locator('button[type=submit]'),confirmAction=id=>card(id).getByRole('button',{name:'确认',exact:true}),changeAction=id=>card(id).getByRole('button',{name:'提出修改',exact:true});
  await card(question.id).waitFor();
  assert.equal(await card(confirmation.id).getByText('阶段确认',{exact:true}).count(),0);
  assert.equal(await card(confirmation.id).getByText('确认改编方案',{exact:true}).count(),0);
  assert.equal(await card(confirmation.id).getByText('固定版本',{exact:true}).count(),0);
  assert.equal(await card(confirmation.id).getByText('提交后按所选操作直接处理',{exact:true}).count(),0);
  assert.equal(await page.locator('#composer-wrap').isHidden(),true);
  assert.equal(await card(question.id).locator('[data-card-field="answer"]:checked').count(),0);
  assert.equal(await card(recovery.id).locator('[data-card-field="accept_unknown_cost"]').isChecked(),false);
  assert.equal(await submit(unknown.id).isDisabled(),true);
  await page.locator('#run-status').getByRole('button',{name:'处理暂停'}).click();assert.equal(await page.locator('#resume-form').count(),0);assert.equal(writes.length,0);
  // Drafts survive unrelated real-time status updates, including active typing focus.
  await card(confirmation.id).getByLabel('修改要求').fill('保留结局，调整中段');
  await card(question.id).getByLabel('自行填写',{exact:true}).check();await card(question.id).getByLabel('补充说明').fill('双视角交替');
  await card(question.id).getByLabel('第一人称',{exact:true}).check();assert.equal(await card(question.id).getByLabel('补充说明').count(),0);await card(question.id).getByLabel('自行填写',{exact:true}).check();assert.equal(await card(question.id).getByLabel('补充说明').inputValue(),'双视角交替');
  const readBefore=actionReads;broadcast();await page.waitForFunction(()=>document.querySelector('#connection').textContent==='实时同步');
  await page.waitForTimeout(450);assert.ok(actionReads>readBefore);
  assert.equal(await card(confirmation.id).getByLabel('修改要求').inputValue(),'保留结局，调整中段');assert.equal(await card(question.id).getByLabel('补充说明').inputValue(),'双视角交替');
  assert.equal(await card(question.id).getByLabel('自行填写',{exact:true}).isChecked(),true);
  // A changed revision cannot submit until explicitly reviewed; drafts remain.
  cards=cards.map(c=>c.id===confirmation.id?{...c,revision:'r2',details:[{label:'固定版本',value:'产物 v4'}]}:c);broadcast();
  await card(confirmation.id).getByText('产物已更新，请刷新后再确认。',{exact:true}).waitFor();assert.equal(await confirmAction(confirmation.id).isDisabled(),true);assert.equal(await changeAction(confirmation.id).isDisabled(),true);
  cards=cards.map(c=>c.id===confirmation.id?{...c,revision:'r3',details:[{label:'固定版本',value:'产物 v5'}]}:c);
  await card(confirmation.id).getByRole('button',{name:'刷新',exact:true}).click();assert.equal(await confirmAction(confirmation.id).isDisabled(),true);
  await card(confirmation.id).getByRole('button',{name:'刷新',exact:true}).click();assert.equal(await card(confirmation.id).getByLabel('修改要求').inputValue(),'保留结局，调整中段');
  // 503 retains both typed content and the idempotency key for the exact retry.
  nextError={status:503,code:'temporary_failure',message:'暂时无法保存，请重试相同操作。'};
  await changeAction(confirmation.id).click();await card(confirmation.id).getByText('暂时无法保存，请重试相同操作。',{exact:true}).waitFor();assert.equal(await card(confirmation.id).getByLabel('修改要求').inputValue(),'保留结局，调整中段');
  const failed=writes.at(-1);await changeAction(confirmation.id).click();await card(confirmation.id).waitFor({state:'detached'});assert.equal(writes.at(-1).key,failed.key);assert.equal(writes.at(-1).body.expected_revision,'r3');assert.equal(writes.at(-1).body.action_id,'request_changes');assert.deepEqual(writes.at(-1).body.values,{text:'保留结局，调整中段'});
  // Custom reply is structured and duplicate submit events produce one request.
  delay=300;const before=writes.length;await submit(question.id).click();await card(question.id).evaluate(form=>{form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));});
  await card(question.id).waitFor({state:'detached'});assert.equal(writes.length,before+1);assert.deepEqual(writes.at(-1).body.values,{answer:'__custom__',text:'双视角交替'});delay=0;
  // Required cost acknowledgement blocks before POST; explicit new budgets are sent.
  const costBefore=writes.length;await submit(recovery.id).click();await card(recovery.id).getByText('请主动勾选',{exact:false}).waitFor();assert.equal(writes.length,costBefore);
  await card(recovery.id).getByLabel('我已知晓旧调用费用尚未核实').check();await card(recovery.id).getByLabel('新任务费用上限（USD）').fill('7.5');
  await submit(recovery.id).click();await card(recovery.id).waitFor({state:'detached'});assert.deepEqual(writes.at(-1).body.values,{accept_unknown_cost:true,max_cost_usd:7.5,max_active_seconds:3600});
  // One queue item can be released, another cancelled; there is no whole-queue call.
  await choose(queue.id,'release_one').check();await submit(queue.id).click();await card(queue.id).waitFor({state:'detached'});assert.equal(writes.at(-1).body.action_id,'release_one');
  await choose(cancel.id,'cancel').check();await submit(cancel.id).click();await card(cancel.id).waitFor({state:'detached'});assert.equal(writes.at(-1).body.action_id,'cancel');
  // Usage corrections require a selected call and a written billing source.
  cards.push(make('task:usage','recovery','填写实际费用',[{id:'report_usage',label:'提交实际费用',fields:[{name:'model_call_id',label:'对应调用',type:'select',required:true,options:[{value:'call-a',label:'调用 A'}]},{name:'reported_cost_usd',label:'实际费用',type:'number',required:true},{name:'source',label:'账单依据',type:'textarea',required:true}]}]));broadcast();
  await card('task:usage').waitFor();assert.equal(await card('task:usage').locator('input[type=radio]:checked').count(),0);await card('task:usage').getByLabel('调用 A',{exact:true}).check();await card('task:usage').locator('[data-card-field="reported_cost_usd"]').fill('0');await card('task:usage').getByLabel('账单依据').fill('账单记录 fixture 001');await submit('task:usage').click();await card('task:usage').waitFor({state:'detached'});assert.deepEqual(writes.at(-1).body.values,{model_call_id:'call-a',reported_cost_usd:0,source:'账单记录 fixture 001'});
  // A direct confirm has no synthetic natural-language reply or field payload.
  cards.push({...confirmation,id:'pending:direct',revision:'direct-r'});broadcast();await card('pending:direct').waitFor();await confirmAction('pending:direct').click();await card('pending:direct').waitFor({state:'detached'});assert.equal(writes.at(-1).body.action_id,'confirm');assert.deepEqual(writes.at(-1).body.values,{});
  // Server-side stale response also blocks old values before another submission.
  cards.push({...question,id:'pending:stale'});broadcast();await card('pending:stale').waitFor();await card('pending:stale').getByLabel('自行填写',{exact:true}).check();await card('pending:stale').getByLabel('补充说明').fill('不应随预设答案发送');await card('pending:stale').getByLabel('第一人称',{exact:true}).check();assert.equal(await card('pending:stale').getByLabel('补充说明').count(),0);
  nextError={status:409,code:'action_stale',message:'卡片版本已更新，请重新核对。'};await submit('pending:stale').click();await card('pending:stale').getByRole('button',{name:'已核对，刷新此卡片'}).waitFor();assert.equal(await submit('pending:stale').isDisabled(),true);assert.equal(await card('pending:stale').getByLabel('第一人称',{exact:true}).isChecked(),true);
  assert.deepEqual(writes.at(-1).body.values,{answer:'first'});
  await page.screenshot({path:path.join(root,'tests/frontend-action-cards.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);await page.screenshot({path:path.join(root,'tests/frontend-action-mobile.png'),fullPage:true});
  assert.deepEqual(errors,[]);assert.ok(writes.every(w=>w.path.endsWith('/actions')&&w.csrf==='fixture-csrf'&&w.key));
  console.log('PASS: confirmation, change request, structured/custom question, recovery budgets and active acknowledgement, per-item queue release/cancel, disabled uncertain result, SSE draft retention, stale refresh, failed retry idempotency, duplicate-submit guard, mobile layout. Only fixture /actions POSTs.');
}finally{await browser.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
