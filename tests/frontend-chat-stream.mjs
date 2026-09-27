/** A browser fixture for in-chat activity, streamed replies, reload, and source import. */
import assert from 'node:assert/strict';
import http from 'node:http';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const project={id:'11111111-1111-4111-8111-111111111111',title:'流式对话测试'};
const conversation={id:'22222222-2222-4222-8222-222222222222',title:'创作主会话'};
const runId='33333333-3333-4333-8333-333333333333';
const startedAt=Date.now();
let sequence=0,sourceStarts=true,runs=[];
const events=new Set(),chatRows=[],history=[{
  id:'44444444-4444-4444-8444-444444444444',role:'user',visibility:'conversation',
  sequence:1,created_at:new Date(startedAt).toISOString(),content:{storage:'inline_text',text:'请开始分析'},
}];
const makeRow=(event_name,payload,run_id=runId)=>({
  id:`event-${++sequence}`,sequence,event_name,payload,run_id,task_id:'task-1',
  conversation_id:conversation.id,created_at:new Date(startedAt+sequence*1000).toISOString(),
});
function broadcast(row){
  if(row.event_name.startsWith('chat.'))chatRows.push(row);
  for(const response of events)response.write(`id: ${row.sequence}\ndata: ${JSON.stringify(row)}\n\n`);
}
const server=http.createServer(async(request,response)=>{
  const url=new URL(request.url,'http://127.0.0.1');
  if(!url.pathname.startsWith('/api/')){
    const file=url.pathname==='/'?'index.html':url.pathname.replace('/static/','');
    try{
      const content=await readFile(path.join(root,'branch_agent/static',file));
      response.setHeader('Content-Type',file.endsWith('.html')?'text/html':file.endsWith('.css')?'text/css':'text/javascript');
      response.end(content);
    }catch{response.statusCode=404;response.end();}
    return;
  }
  if(url.pathname.endsWith('/events')){
    response.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-cache'});
    response.write(': connected\n\n');events.add(response);
    request.on('close',()=>events.delete(response));
    return;
  }
  const suffix=url.pathname.replace('/api/branch-agent/v1','');
  let data={};
  if(suffix==='/auth/me')data={account_id:'fixture',display_name:'测试用户',csrf_token:'csrf'};
  else if(suffix==='/projects')data={items:[project]};
  else if(suffix.endsWith('/conversations'))data={items:[conversation]};
  else if(suffix.endsWith('/history'))data={items:history};
  else if(suffix.endsWith('/activity')){
    const limit=Number(url.searchParams.get('limit')||200),before=Number(url.searchParams.get('before')||0);
    const eligible=before?chatRows.filter(row=>row.sequence<before):chatRows;
    const selected=eligible.slice(-limit);
    data={events:selected,last_sequence:sequence,next_sequence:selected.at(-1)?.sequence??0,
      truncated_before:eligible.length>selected.length,has_more:false};
  }
  else if(suffix.endsWith('/status'))data={tasks:[],runs,queue:[],artifacts:[]};
  else if(suffix.endsWith('/actions'))data={cards:[]};
  else if(suffix.endsWith('/source')&&request.method==='POST'){
    const chunks=[];for await(const chunk of request)chunks.push(chunk);
    history.push({id:`source-message-${history.length}`,role:'user',visibility:'conversation',sequence:history.length+1,
      created_at:new Date(startedAt+(sequence+1)*1000).toISOString(),content:{storage:'inline_text',text:'已导入原作：测试原作'}});
    broadcast(makeRow('chat.activity',{text:sourceStarts?'原作已保存，Step1 已排队启动。':'原作已保存，Step1 未启动：当前模型预算不足。',kind:'source'},null));
    data={step1_started:sourceStarts,step1_task_id:sourceStarts?'task-1':null,
      step1_reason:sourceStarts?null:'budget_insufficient',step1_branches:{}};
  }
  response.setHeader('Content-Type','application/json');response.end(JSON.stringify(data));
});

await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true,channel:process.env.BROWSER_CHANNEL||'chrome'});
try{
  const page=await browser.newPage({viewport:{width:1440,height:900}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.locator('#connection.live').waitFor();
  broadcast(makeRow('chat.activity',{text:'全局事件 Agent 已开始读取原作。',kind:'agent'}));
  broadcast(makeRow('chat.reply.started',{stream_id:'stream-1'}));
  broadcast(makeRow('chat.reply.delta',{stream_id:'stream-1',delta:'正在整理**事件**。<img src=x onerror="window.XSS=true">'}));
  await page.locator('.chat-activity.agent').getByText('全局事件 Agent 已开始读取原作。').waitFor();
  await page.locator('.message.streaming').getByText('事件',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>window.XSS),undefined);
  broadcast(makeRow('chat.reply.completed',{stream_id:'stream-1'}));
  history.push({id:'official-answer',role:'assistant',visibility:'conversation',run_id:runId,
    sequence:2,created_at:new Date(startedAt+(sequence+1)*1000).toISOString(),
    content:{storage:'inline_text',text:'这是最终回复。'}});
  broadcast(makeRow('message.created',{message_id:'official-answer'}));
  await page.getByText('这是最终回复。').waitFor();
  await page.waitForFunction(()=>!document.querySelector('.message.streaming'));
  await page.reload();
  await page.getByText('全局事件 Agent 已开始读取原作。').waitFor();
  assert.equal(await page.locator('.message.streaming').count(),0);
  assert.equal(await page.getByText('这是最终回复。').count(),1);

  await page.locator('.composer [data-action="source"]').click();
  await page.locator('#source-form [name=text]').fill('原作第一章：林夏回到雾港。');
  await page.getByRole('button',{name:'导入并开始 Step1'}).click();
  await page.locator('#modal').waitFor({state:'hidden'});
  await page.locator('.chat-activity.source').getByText('原作已保存，Step1 已排队启动。').waitFor();
  sourceStarts=false;
  await page.locator('.composer [data-action="source"]').click();
  await page.locator('#source-form [name=text]').fill('另一版原作全文。');
  await page.getByRole('button',{name:'导入并开始 Step1'}).click();
  await page.getByText('原作已保存，但当前模型预算不足，Step1 未启动。请调整配置后重试。').waitFor();
  assert.equal(await page.locator('#modal').isVisible(),true);
  await page.locator('#modal [data-action="close-modal"]').first().click();
  chatRows.push(makeRow('chat.reply.started',{stream_id:'long-stream'}));
  for(let index=0;index<205;index++)chatRows.push(makeRow('chat.reply.delta',{stream_id:'long-stream',delta:`段${index}|`}));
  await page.reload();
  await page.locator('[data-stream-id="long-stream"]').getByText(/段0\|/).waitFor();
  assert.match(await page.locator('[data-stream-id="long-stream"]').innerText(),/段204\|/);
  assert.equal(await page.getByText('较早的回复片段尚未加载；可加载更早的运行记录。').count(),0);
  for(let index=0;index<205;index++)chatRows.push(makeRow('chat.activity',{text:`批量进度 ${index}`,kind:'agent'}));
  await page.reload();
  await page.getByRole('button',{name:'加载更早的运行记录'}).waitFor();
  assert.equal(await page.getByText('全局事件 Agent 已开始读取原作。').count(),0);
  for(let pageIndex=0;pageIndex<3&&await page.getByText('全局事件 Agent 已开始读取原作。').count()===0;pageIndex++){
    const current=await page.locator('.chat-activity').count();
    await page.getByRole('button',{name:'加载更早的运行记录'}).click();
    await page.waitForFunction(previous=>document.querySelectorAll('.chat-activity').length>previous||!document.querySelector('[data-action="older-activity"]'),current);
  }
  await page.getByText('全局事件 Agent 已开始读取原作。').waitFor();
  const ghostRun='55555555-5555-4555-8555-555555555555';
  broadcast(makeRow('chat.reply.started',{stream_id:'ghost-stream'},ghostRun));
  broadcast(makeRow('chat.reply.delta',{stream_id:'ghost-stream',delta:'这段未通过后续检查。'},ghostRun));
  broadcast(makeRow('chat.reply.completed',{stream_id:'ghost-stream'},ghostRun));
  await page.locator('[data-stream-id="ghost-stream"]').getByText('这段未通过后续检查。').waitFor();
  runs=[{id:ghostRun,state:'failed'}];
  broadcast(makeRow('run.transitioned',{object_id:ghostRun,to_state:'failed'},ghostRun));
  await page.locator('[data-stream-id="ghost-stream"]').getByText('本次回复未完成，后续状态会继续显示在对话中。').waitFor();
  assert.equal(await page.getByText('这段未通过后续检查。').count(),0);
  await page.reload();
  await page.locator('[data-stream-id="ghost-stream"]').getByText('本次回复未完成，后续状态会继续显示在对话中。').waitFor();
  assert.deepEqual(errors,[]);
  console.log('PASS: in-chat activity, safe reply streaming, final reply reconciliation, reload, older activity, and source auto-start receipt.');
}finally{
  await browser.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));
}
