/** Browser regression: Agent selection must survive editor delta and publication. */
import assert from 'node:assert/strict';
import http from 'node:http';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const agentKeys=['context_summarizer','format_repairer','alternate','source_parser',
  'source_global_parser','source_character_parser','interaction_architect'];
const agents=Object.fromEntries(agentKeys.map(key=>[key,{name:key}]));
const model={context_window:100000,max_output_tokens:32000,reasoning_efforts:['high','low']};
const defaults={
  prompts:{base:'',agents:Object.fromEntries(agentKeys.map(key=>[key,'职责'])),
    agent_names:Object.fromEntries(agentKeys.map(key=>[key,key])),
    stage_agents:{coordinator:'context_summarizer',step1:'source_parser',step5:'interaction_architect',
      'aux.summary':'context_summarizer','aux.format_repair':'format_repairer'},
    step1_view_agents:{global:'source_global_parser',character:'source_character_parser'},
    stages:{'aux.summary':'摘要','aux.format_repair':'格式修复'},summary:'摘要',
    harness:{stages:{},runtime:{}}},
  model:{name:'test-model',reasoning_effort:'high',max_output_tokens:8000},
  run:{max_turns:10},retry:{max_retries:2},
  repair:{max_rounds:2,content_max_rounds:5,format_max_rounds:1},
  summary:{target_tokens:1500},compaction:{trigger_ratio:0.75,target_ratio:0.5},
  context:{input_token_cap:32000,safety_margin_tokens:1000,recent_turns:6,
    history_token_cap:500000,step1_source:{trigger_tokens:500000,window_tokens:1000},
    stage_inputs:{step1:['source_text'],step5:[]},session_sharing:{step1:null,step5:null}},
  output:{bindings:{},structured:{'aux.summary':false,'aux.format_repair':false,
    'step1.global':true,'step1.character':true,step5:true}},
};
const editor=new Map(),published=[],drafts=new Map(),posted=[];
let nextDraft=0;
const clone=value=>structuredClone(value);
const scopeKey=(kind,key)=>`${kind}:${key??''}`;
const selectedFor=stage=>published.filter(row=>row.scope_key===stage).at(-1)?.values?.prompts?.stage_agents?.[stage]
  ??defaults.prompts.stage_agents[stage]??'context_summarizer';
function configView(stage,agentOverride){
  const selected=agentOverride??selectedFor(stage),values=clone(defaults);
  if(stage.startsWith('aux.'))values.prompts.stage_agents[stage]=selected;
  if(agentOverride==='alternate')values.model.reasoning_effort='low';
  return {values,schemas:{},selected_agent:selected,registry:{agents,models:{'test-model':model}},
    models:{'test-model':model},versions:published,origins:{}};
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
  const suffix=url.pathname.replace('/api/branch-agent/v1','');
  let body={};
  if(request.method!=='GET'){
    const chunks=[];for await(const chunk of request)chunks.push(chunk);
    body=chunks.length?JSON.parse(Buffer.concat(chunks).toString()):{};
  }
  let data={};
  if(suffix==='/auth/me')data={account_id:'fixture',display_name:'配置测试',csrf_token:'csrf'};
  else if(suffix==='/projects')data={items:[]};
  else if(suffix==='/release')data={id:'fixture-release'};
  else if(suffix==='/account/config'&&request.method==='GET')
    data=configView(url.searchParams.get('stage')??'coordinator',url.searchParams.get('agent_key'));
  else if(suffix==='/account/config/editor'){
    const key=scopeKey(request.method==='GET'?url.searchParams.get('scope_kind'):body.scope_kind,
      request.method==='GET'?url.searchParams.get('scope_key'):body.scope_key);
    if(request.method==='GET')data=editor.get(key)??{revision:0,payload:null};
    else if(request.method==='PUT'){
      const current=editor.get(key)??{revision:0,payload:null};
      data={revision:current.revision+1};editor.set(key,{...data,payload:body.payload});
    }else{editor.delete(key);data={deleted:true};}
  }
  else if(suffix==='/account/config/instructions-preview')data={parts:{creative_agent:'职责'},final:'职责'};
  else if(suffix==='/account/config/validate')data={valid:true,enabled:false};
  else if(suffix==='/account/config'&&request.method==='POST'){
    const id=`draft-${++nextDraft}`;drafts.set(id,body);posted.push(body);data={id};
  }
  else if(/^\/account\/config\/draft-\d+\/publish$/.test(suffix)){
    const id=suffix.split('/')[3],saved=drafts.get(id);
    published.push({id,scope_kind:saved.scope_kind,scope_key:saved.scope_key,
      state:'published',version:published.length+1,values:saved.values});
    data={id,state:'published'};
  }
  response.setHeader('Content-Type','application/json');response.end(JSON.stringify(data));
});

await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true,channel:process.env.BROWSER_CHANNEL||'chrome'});
try{
  const page=await browser.newPage({viewport:{width:1440,height:900}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.locator('#panel-toggle').click();
  const selectAndPublish=async(section,selector,expectedScope,expectedStage)=>{
    await page.locator(`[data-action="config-section"][data-section="${section}"]`).click();
    await page.locator(selector).selectOption('alternate');
    const publication=page.waitForResponse(response=>/\/account\/config\/draft-\d+\/publish$/.test(new URL(response.url()).pathname));
    await page.locator('#publish-config:not([disabled])').click();
    await publication;
    await page.waitForFunction(({selector})=>document.querySelector(selector)?.value==='alternate'
      &&document.querySelector('#publish-config')?.disabled,{selector});
    const saved=posted.at(-1);
    assert.equal(saved.scope_kind,expectedScope);
    assert.equal(saved.scope_key,expectedStage);
    return saved.values;
  };
  for(const [section,selector,stage] of [
    ['summary','#summary-agent','aux.summary'],
    ['repair','#format-repair-agent','aux.format_repair'],
  ]){
    const saved=await selectAndPublish(section,selector,'auxiliary',stage);
    assert.equal(saved.prompts.stage_agents[stage],'alternate');
    assert.equal(saved.model?.reasoning_effort,undefined,
      'selecting an Agent must not publish its inherited model settings as an auxiliary override');
  }
  await page.locator('[data-action="config-section"][data-section="stage"]').click();
  await page.locator('#stage-agent-global').selectOption('alternate');
  const publication=page.waitForResponse(response=>/\/account\/config\/draft-\d+\/publish$/.test(new URL(response.url()).pathname));
  await page.locator('#publish-config:not([disabled])').click();
  await publication;
  await page.waitForFunction(()=>document.querySelector('#publish-config')?.disabled);
  assert.equal(posted.at(-1).values.prompts.step1_view_agents.global,'alternate');
  assert.deepEqual(errors,[]);
  console.log('PASS: summary and format repair Agent switches publish their bindings; Step1 selection remains intact.');
}finally{
  await browser.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));
}
