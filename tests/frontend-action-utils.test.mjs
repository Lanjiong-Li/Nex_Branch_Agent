import test from 'node:test';
import assert from 'node:assert/strict';
import {actionDraft,actionSubmission,artifactStatusLabel,pendingQueueCount} from '../branch_agent/static/ui-utils.mjs';
const card={id:'pending:a',revision:'r1',actions:[{id:'answer',label:'回答',fields:[{name:'answer',label:'选项',type:'select',required:true,default:'yes',options:[{value:'yes',label:'是'},{value:'__custom__',label:'自填'}]},{name:'text',label:'补充',type:'textarea'}]}]};
test('required choice and acknowledgement never silently use defaults',()=>{
  const d=actionDraft(card);assert.equal(d.values.answer.answer,'');assert.match(actionSubmission(card,d).error,/选项/);
  const c={...card,actions:[{id:'restart',fields:[{name:'ok',label:'费用确认',type:'checkbox',required:true,default:true}]}]};
  const x=actionDraft(c);assert.equal(x.values.restart.ok,false);assert.match(actionSubmission(c,x).error,/费用确认/);
});
test('custom choice requires written answer, preserves explicit values',()=>{
  const d=actionDraft(card);d.values.answer.answer='__custom__';assert.match(actionSubmission(card,d).error,/自定义/);
  d.values.answer.text='保留这个想法';assert.deepEqual(actionSubmission(card,d).body,{card_id:'pending:a',action_id:'answer',expected_revision:'r1',values:{answer:'__custom__',text:'保留这个想法'}});
  d.values.answer.answer='yes';assert.deepEqual(actionSubmission(card,d).body.values,{answer:'yes'});assert.equal(d.values.answer.text,'保留这个想法');
});
test('new revision preserves draft but requires explicit review',()=>{
  const d=actionDraft(card);d.values.answer.answer='yes';d.values.answer.text='草稿';
  const next={...card,revision:'r2'};const stale=actionDraft(next,d);assert.equal(stale.values.answer.text,'草稿');assert.equal(stale.stale,true);assert.match(actionSubmission(next,stale).error,/更新/);
  const reviewed=actionDraft(next,stale,{acceptRevision:true});assert.equal(reviewed.stale,false);assert.equal(actionSubmission(next,reviewed).body.expected_revision,'r2');
});
test('disabled actions, duplicate submissions and removed choices never submit',()=>{
  const d=actionDraft(card);d.values.answer.answer='yes';assert.match(actionSubmission({...card,actions:[{...card.actions[0],disabled_reason:'结果尚未确认'}]},d).error,/结果尚未确认/);
  assert.match(actionSubmission(card,{...d,inFlight:true}).error,/提交/);
  d.values.answer.answer='removed';assert.match(actionSubmission(card,d).error,/有效/);
});
test('budgets must be explicitly present and finite, unknown fields omitted',()=>{
  const c={id:'task:t',revision:'r',actions:[{id:'restart',fields:[{name:'max_cost_usd',label:'费用',type:'number',required:true},{name:'max_active_seconds',label:'时间',type:'number',required:true}]}]};
  const d=actionDraft(c);d.values.restart={max_cost_usd:'',max_active_seconds:'3600',extra:'secret'};assert.match(actionSubmission(c,d).error,/费用/);
  d.values.restart.max_cost_usd='20';assert.deepEqual(actionSubmission(c,d).body.values,{max_cost_usd:20,max_active_seconds:3600});
  d.values.restart.max_cost_usd='Infinity';assert.match(actionSubmission(c,d).error,/有效数字/);
});
test('revised cost acknowledgement must be actively checked again',()=>{
  const c={id:'task:t',revision:'r1',actions:[{id:'restart',fields:[{name:'accept',label:'费用确认',type:'checkbox',required:true},{name:'notes',type:'text'}]}]};
  const d=actionDraft(c);d.values.restart.accept=true;d.values.restart.notes='保留我的备注';
  const updated=actionDraft({...c,revision:'r2'},d,{acceptRevision:true});assert.equal(updated.values.restart.accept,false);assert.equal(updated.values.restart.notes,'保留我的备注');
});
test('artifact dependency review/invalid overrides the retained effective pointer',()=>{
  const artifact={id:'views',latest_version:1,current_effective_version:1};
  const state={artifact_id:'views',version:1,confirmation_status:'not_required',dependency_status:'review_required',quality_status:'passed'};
  assert.equal(artifactStatusLabel(artifact,[state]),'依赖待复核');
  assert.equal(artifactStatusLabel(artifact,[{...state,dependency_status:'invalid'}]),'依赖已失效');
  assert.equal(artifactStatusLabel(artifact,[{...state,dependency_status:'valid'}]),'当前有效版本');
});
test('missing or different-version state never claims a currently valid artifact',()=>{
  const artifact={id:'views',latest_version:2,current_effective_version:2};
  assert.equal(artifactStatusLabel(artifact,[]),'已保存 · 状态未提供');
  assert.equal(artifactStatusLabel(artifact,[{artifact_id:'views',version:1,dependency_status:'valid'}]),'已保存 · 状态未提供');
  assert.equal(artifactStatusLabel(artifact,[{artifact_id:'views',version:2,dependency_status:'valid',confirmation_status:'unconfirmed'}]),'待确认');
});
test('queue badge counts only pending and blocked entries in this conversation',()=>{
  const rows=[{conversation_id:'a',state:'blocked'},{conversation_id:'a',state:'dispatched'},{conversation_id:'a',state:'cancelled'},{conversation_id:'b',state:'pending'}];
  assert.equal(pendingQueueCount(rows,'a'),1);assert.equal(pendingQueueCount(rows,null),0);
});
