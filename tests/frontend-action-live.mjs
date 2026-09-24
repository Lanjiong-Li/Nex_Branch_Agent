/** Run only after explicit deployment approval. Existing main service, GET-only except local login. */
import assert from 'node:assert/strict';
import {readFile,writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const base=process.env.FRONTEND_BASE_URL||'http://127.0.0.1:8767';
const project=process.env.FRONTEND_PROJECT_ID||'926aed68-d15f-5576-8990-1a1d9488fc5d';
const credentials=await readFile(path.join(root,'.data/local-login.txt'),'utf8');
const browser=await chromium.launch({headless:true,channel:process.env.BROWSER_CHANNEL||'chrome'});
try{
  const page=await browser.newPage({viewport:{width:1440,height:1080}}),errors=[],httpErrors=[],blockedWrites=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('response',response=>{if(response.status()>=400&&!response.url().endsWith('/auth/me'))httpErrors.push({status:response.status(),path:new URL(response.url()).pathname});});
  await page.route('**/api/**',route=>{const req=route.request();if(!['GET','HEAD'].includes(req.method())&&!(req.method()==='POST'&&req.url().endsWith('/auth/login'))){blockedWrites.push(new URL(req.url()).pathname);return route.abort();}return route.continue();});
  const get=async suffix=>{const response=await page.request.get(base+'/api/branch-agent/v1'+suffix);assert.equal(response.status(),200);return response.json();};
  const modelCalls=async()=>{const rows=[];let cursor=null;do{const data=await get(`/projects/${project}/records?record_type=model_call&limit=200${cursor?`&cursor=${encodeURIComponent(cursor)}`:''}`);rows.push(...data.items);cursor=data.next_cursor;}while(cursor);return rows;};
  await page.goto(base);await page.locator('#login-form [name=username]').fill(credentials.match(/用户名：(.*)/)[1]);await page.locator('#login-form [name=password]').fill(credentials.match(/密码：(.*)/)[1]);await page.getByRole('button',{name:'登录工作区'}).click();await page.locator('#modal').waitFor({state:'hidden'});
  await page.locator(`.project-link[data-id="${project}"]`).click();
  const before=await get(`/projects/${project}/status`),callsBefore=await modelCalls();
  const tasks=before.tasks.filter(t=>['paused','failed','stopped'].includes(t.state));assert.ok(tasks.length,'Expected an existing paused task.');
  let selected=null,selectedCards=null;
  for(const task of tasks){const data=await get(`/projects/${project}/conversations/${task.conversation_id}/actions`);if(data.cards.some(card=>card.kind==='recovery')){selected=task;selectedCards=data.cards;break;}}
  assert.ok(selected,'Expected structured recovery actions for an existing paused task.');
  await page.locator(`.conversation-link[data-id="${selected.conversation_id}"]`).click();
  const region=page.locator('#action-cards');await region.getByText('待你处理',{exact:true}).waitFor();
  const recovery=selectedCards.find(card=>card.kind==='recovery');await page.locator(`[data-card-form="${recovery.id}"]`).waitFor();
  if(before.runtime_policy?.cost_gates_enabled===false){
    for(const card of selectedCards)for(const action of card.actions){
      assert.notEqual(action.id,'report_usage');
      assert.ok(!action.fields.some(field=>['max_cost_usd','additional_cost','accept_unknown_cost'].includes(field.name)));
    }
    assert.match(recovery.description,/已暂停费用门禁/);
  }
  assert.ok(recovery.actions.some(action=>action.id==='restart'));assert.ok(recovery.actions.some(action=>action.id==='rerun_published'));
  const uiCard=page.locator(`[data-card-form="${recovery.id}"]`);assert.equal(await uiCard.locator('input[type=checkbox]:checked').count(),0);assert.equal(await uiCard.locator('[data-card-choice]:checked').count(),0);
  const details=Object.fromEntries(recovery.details.map(item=>[item.label,item.value]));
  assert.equal(details['本次旧配置输出上限'],'8000');assert.equal(details['已发布配置输出上限'],'100000');assert.match(details['当前原作'],/^v2 /);assert.match(details['现有 Step1 分段'],/v1.*依赖原作 v1.*上游已变化，需重新分段/);
  assert.equal(recovery.actions.find(action=>action.id==='restart').disabled_reason,null);assert.match(recovery.actions.find(action=>action.id==='rerun_published').disabled_reason,/上游产物缺失或已失效/);
  const viewsChip=page.locator('#artifact-shelf .artifact-chip').filter({has:page.getByText('原作双视图',{exact:true})});await viewsChip.getByText('v1 · 依赖待复核',{exact:false}).waitFor();assert.ok(!(await viewsChip.innerText()).includes('当前有效版本'));
  const queueCards=selectedCards.filter(card=>card.kind==='queue');assert.equal(queueCards.length,1);await page.locator('#run-status').getByRole('button',{name:'队列 1',exact:true}).waitFor();
  assert.match(await uiCard.innerText(),/已发布配置/);assert.equal(await page.locator('[data-action="resume-queue"]').count(),0);assert.equal(await page.locator('#resume-form').count(),0);
  await region.evaluate(node=>{node.scrollTop=0;});const screenshot=path.join(root,'tests/frontend-action-live.png');await page.screenshot({path:screenshot,fullPage:true});
  await uiCard.locator('.action-choices').evaluate(node=>node.scrollIntoView({block:'center'}));
  const choicesScreenshot=path.join(root,'tests/frontend-action-live-options.png');await page.screenshot({path:choicesScreenshot,fullPage:true});
  assert.equal(await uiCard.locator('[data-card-choice="restart"]').isDisabled(),false);assert.equal(await uiCard.locator('[data-card-choice]:checked').count(),0);
  const after=await get(`/projects/${project}/status`),callsAfter=await modelCalls();
  assert.deepEqual(callsAfter.map(c=>[c.id,c.row_version,c.state]).sort(),callsBefore.map(c=>[c.id,c.row_version,c.state]).sort());
  assert.deepEqual(after.tasks.map(t=>[t.id,t.row_version,t.state]).sort(),before.tasks.map(t=>[t.id,t.row_version,t.state]).sort());
  assert.deepEqual(errors,[]);assert.deepEqual(httpErrors,[]);assert.deepEqual(blockedWrites,[]);
  const result={observed_at:new Date().toISOString(),origin:'Existing main service; browser GET-only verification except local login; no action selected or submitted',project_id:project,conversation_id:selected.conversation_id,recovery_details:details,cards:selectedCards.map(card=>({id:card.id,kind:card.kind,revision:card.revision,actions:card.actions.map(action=>({id:action.id,disabled_reason:action.disabled_reason??null}))})),model_calls_before:callsBefore.length,model_calls_after:callsAfter.length,task_rows_unchanged:true,model_call_rows_unchanged:true,console_errors:errors,http_errors:httpErrors,blocked_business_write_attempts:blockedWrites,screenshot,choices_screenshot:choicesScreenshot};
  result.source_views_label=await viewsChip.innerText();result.pending_queue_count=queueCards.length;result.runtime_policy=before.runtime_policy;
  await writeFile(path.join(root,'tests/frontend-action-live-results.json'),JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify({status:'passed',card_count:result.cards.length,model_calls_before:callsBefore.length,model_calls_after:callsAfter.length,source_views_label:result.source_views_label,pending_queue_count:queueCards.length,console_errors:errors,http_errors:httpErrors,blocked_business_write_attempts:blockedWrites,screenshot}));
}finally{await browser.close();}
