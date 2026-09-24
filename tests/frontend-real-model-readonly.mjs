/** Browser inspection of existing live-model records. The temporary API must disable workers and database writes. */
import assert from 'node:assert/strict';
import {readFile,writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const base=process.env.FRONTEND_BASE_URL||'http://127.0.0.1:8768';
const project=process.env.FRONTEND_PROJECT_ID||'dcfc7a66-1c6c-498c-bb4b-17ac0047900e';
const conversation=process.env.FRONTEND_CONVERSATION_ID||'5beadfdb-262e-446e-8e79-cb91d7314e23';
assert.ok(process.env.FRONTEND_READONLY_AUTH_DIR,'Use the private directory of the isolated acceptance adapter.');
const credentials=await readFile(path.join(process.env.FRONTEND_READONLY_AUTH_DIR,'local-login.txt'),'utf8');
const health=await fetch(base+'/health').then(response=>response.json());
assert.equal(health.worker,false);
const browser=await chromium.launch({headless:true,channel:process.env.BROWSER_CHANNEL||'chrome'});
const screenshots=[];
try{
  const page=await browser.newPage({viewport:{width:1440,height:1000},acceptDownloads:true});
  const errors=[],httpErrors=[],blockedWrites=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('response',response=>{if(response.status()>=400&&!response.url().endsWith('/auth/me'))httpErrors.push({status:response.status(),path:new URL(response.url()).pathname});});
  await page.route('**/api/**',route=>{const request=route.request();if(!['GET','HEAD'].includes(request.method())&&!request.url().endsWith('/auth/login')&&!request.url().endsWith('/references/resolve')){blockedWrites.push(new URL(request.url()).pathname);return route.abort();}return route.continue();});
  const get=async suffix=>{const response=await page.request.get(base+'/api/branch-agent/v1'+suffix);assert.equal(response.status(),200);return response.json();};
  const capture=async name=>{const file=path.join(root,'tests',name);await page.screenshot({path:file,fullPage:true});screenshots.push(file);};
  await page.goto(base);
  await page.locator('#login-form [name=username]').fill(credentials.match(/用户名：(.*)/)[1]);
  await page.locator('#login-form [name=password]').fill(credentials.match(/密码：(.*)/)[1]);
  await page.getByRole('button',{name:'登录工作区'}).click();
  await page.locator('#modal').waitFor({state:'hidden'});
  await page.locator(`.project-link[data-id="${project}"]`).click();
  await page.locator(`.conversation-link[data-id="${conversation}"]`).click();
  const history=await get(`/projects/${project}/conversations/${conversation}/history?tail=true&limit=100`);
  const messages=history.items.filter(row=>row.visibility!=='internal');
  assert.ok(messages.length,'The existing smoke conversation must contain real saved messages.');
  const last=messages.at(-1);const text=last.content?.storage==='inline_text'?last.content.text:JSON.stringify(last.content?.value??last.content);
  if(text)await page.getByText(text.slice(0,70),{exact:false}).first().waitFor();
  await capture('frontend-real-model-conversation.png');
  const status=await get(`/projects/${project}/status`);
  let resumeTarget=null;
  let controlTarget=null;
  const control=page.locator('#run-status [data-action="resume-task"], #run-status [data-action="stop-task"]');
  if(await control.count()){
    const taskId=await control.getAttribute('data-id');
    const controlled=status.tasks.find(row=>row.id===taskId);
    assert.ok(controlled,'The primary control must address an actual saved Task.');
    assert.equal(controlled.conversation_id,conversation);
    assert.ok(!(controlled.parent_task_id&&controlled.intent==='summarize'),'Internal summary attempts cannot become the main control target.');
    assert.ok(!(controlled.state==='failed'&&status.tasks.find(row=>row.id===controlled.parent_task_id)?.state==='succeeded'),'Historical child failure cannot override its completed parent.');
    if(process.env.FRONTEND_EXPECT_TASK_ID)assert.equal(taskId,process.env.FRONTEND_EXPECT_TASK_ID);
    controlTarget={task_id:taskId,state:controlled.state,action:await control.getAttribute('data-action'),submitted:false};
    if(controlled.pause_reason)assert.ok((await page.locator('#run-status').innerText()).includes(controlled.pause_reason));
    if(controlTarget.action==='resume-task')resumeTarget={task_id:taskId,pause_reason:controlled.pause_reason,submitted:false};
    await capture('frontend-real-model-resume-target.png');
  }
  const artifact=status.artifacts.filter(row=>row.latest_version>0&&row.artifact_kind!=='work_summary').at(-1);
  assert.ok(artifact,'A saved source or stage artifact must exist.');
  await page.locator(`.artifact-chip[data-id="${artifact.id}"]`).first().click();
  await page.locator('#detail-body .code-box').first().waitFor();
  const inspected=await get(`/projects/${project}/artifacts/${artifact.id}/versions/${artifact.latest_version}`);
  assert.match(await page.locator('#panel-content').innerText(),new RegExp(inspected.version.id));
  await capture('frontend-real-model-artifact.png');
  await page.getByRole('button',{name:'← 全部记录'}).click();
  const paused=status.tasks.filter(row=>['paused','failed','stopped'].includes(row.state)).at(-1);
  if(paused){
    await page.getByLabel('精确记录 ID').fill(paused.id);
    await page.getByRole('button',{name:'筛选记录',exact:true}).click();
    await page.getByRole('button',{name:'只读 JSON'}).click();
    assert.match(await page.locator('#detail-body').innerText(),new RegExp(paused.pause_reason||paused.state));
    await page.getByLabel('搜索记录 JSON').fill('pause_reason');
    await page.locator('#record-json-tree').scrollIntoViewIfNeeded();
    await capture('frontend-real-model-paused-task.png');
    await page.getByRole('button',{name:'← 全部记录'}).click();
  }
  await page.getByLabel('记录类型',{exact:true}).selectOption('model_call');
  const calls=await get(`/projects/${project}/records?record_type=model_call&limit=100`);
  assert.ok(calls.items.length,'The existing smoke project must contain genuine model call records.');
  const call=calls.items.at(-1);
  await page.getByLabel('精确记录 ID').fill(call.id);
  await page.getByRole('button',{name:'筛选记录',exact:true}).click();
  await page.getByRole('button',{name:'只读 JSON'}).click();
  assert.match(await page.locator('#detail-body').innerText(),new RegExp(call.id));
  if(call.error){await page.getByLabel('搜索记录 JSON').fill('error');await page.locator('#record-json-tree').scrollIntoViewIfNeeded();}
  await capture('frontend-real-model-call.png');
  let delivery=null;
  const latest=(await get(`/projects/${project}/status`)).latest_delivery;
  if(latest){
    const reference=latest.artifact_ref;
    const fixed=await get(`/projects/${project}/artifacts/${reference.record_id}/versions/${reference.version}`);
    await page.getByRole('button',{name:'✓ 查看最终交付',exact:false}).click();
    await page.getByRole('link',{name:'下载最终 Project JSON',exact:true}).waitFor();
    assert.match(await page.locator('#panel-content').innerText(),new RegExp(fixed.version.id));
    await capture('frontend-real-model-delivery.png');
    const downloadPromise=page.waitForEvent('download');
    await page.getByRole('link',{name:'下载最终 Project JSON',exact:true}).click();
    const download=await downloadPromise;
    const file=path.join(root,'tests/frontend-real-model-delivery.json');await download.saveAs(file);
    const bytes=await readFile(file);
    assert.deepEqual(JSON.parse(bytes.toString('utf8')),fixed.content);
    const sha256=createHash('sha256').update(bytes).digest('hex');
    assert.equal(sha256,fixed.version.content_sha256,'Downloaded bytes must match the persisted ArtifactVersion content hash.');
    delivery={artifact_ref:reference,artifact_version_id:fixed.version.id,sha256,stored_content_sha256:fixed.version.content_sha256,byte_count:bytes.length,download_path:file,download_filename:download.suggestedFilename()};
  }
  assert.deepEqual(errors,[]);assert.deepEqual(httpErrors,[]);assert.deepEqual(blockedWrites,[]);
  const result={observed_at:new Date().toISOString(),origin:'API/Engine-driven real model execution; browser read-only acceptance',project_id:project,conversation_id:conversation,worker:false,visible_history_count:messages.length,artifact_count:status.artifacts.length,artifact_kinds:status.artifacts.map(row=>row.artifact_kind),inspected_artifact:{artifact_id:artifact.id,version:artifact.latest_version,record_id:inspected.version.id},model_call_count_loaded:calls.items.length,model_calls_next_cursor:calls.next_cursor,model_call_states_loaded:Object.fromEntries([...new Set(calls.items.map(row=>row.state))].map(state=>[state,calls.items.filter(row=>row.state===state).length])),inspected_model_call:{id:call.id,state:call.state,provider_response_id:call.provider_response_id,usage:call.usage,error:call.error},tasks:status.tasks.map(row=>({id:row.id,parent_task_id:row.parent_task_id,intent:row.intent,stage:row.scope?.stage,state:row.state,pause_reason:row.pause_reason})),control_target:controlTarget,resume_target:resumeTarget,delivery,screenshots,console_errors:errors,http_errors:httpErrors,blocked_write_attempts:blockedWrites};
  await writeFile(path.join(root,'tests/frontend-real-model-results.json'),JSON.stringify(result,null,2)+'\n');
  console.log(JSON.stringify({status:'passed',project_id:project,visible_history_count:messages.length,artifact_kinds:result.artifact_kinds,model_calls_loaded:calls.items.length,delivery,console_errors:errors,http_errors:httpErrors,blocked_write_attempts:blockedWrites}));
}finally{await browser.close();}
