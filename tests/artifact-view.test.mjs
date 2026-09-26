import test from 'node:test';
import assert from 'node:assert/strict';
import {artifactMonitorHTML} from '../branch_agent/static/artifact-view.mjs';

const defaults = {adaptation_strategy: {result_kind: 'ready'}, adaptation_plan: null};
const project = {id: 'example-project'};

test('one selectable type can show multiple chapters with the configured Agent and a Chinese description', () => {
  const entries = ['ch01', 'ch02'].map((chapter_id, index) => ({
    kind: 'chapter_design', label: '章节设计', stage: 9, agent_key: 'custom_chapter_agent',
    description: '保存每一章的设计内容。', chapter_id, artifact_id: `artifact-${index}`,
    version: index + 1, content: {chapter_id}
  }));
  const html = artifactMonitorHTML({entries, selectedKinds: ['chapter_design'], defaults, project});
  assert.equal((html.match(/data-artifact-select/g) ?? []).length, 1);
  assert.equal((html.match(/class="artifact-current-card"/g) ?? []).length, 2);
  assert.match(html, /Step 9 · Agent custom_chapter_agent/);
  assert.match(html, /保存每一章的设计内容。/);
  assert.match(html, /章节 ch01/);
  assert.match(html, /章节 ch02/);
});

test('imported source is labelled as an import, not an Agent run', () => {
  const html = artifactMonitorHTML({
    entries: [{kind: 'source_text', label: '原作全文', stage: 0, agent_key: null,
      description: '导入的完整原作。', version: 1, content: '故事正文'}],
    selectedKinds: ['source_text'], defaults, project
  });
  assert.match(html, /项目素材 · 人工导入/);
  assert.doesNotMatch(html, /Agent 未指定/);
  assert.match(html, /导入的完整原作。/);
});

test('the latest project content and new-project defaults have distinct empty and saved states', () => {
  const entries = [
    {kind: 'adaptation_strategy', stage: 3, agent_key: 'planner', version: null, content: null},
    {kind: 'adaptation_plan', stage: 4, agent_key: 'planner', version: 4,
      content: {title: '<unsafe>', result_kind: 'ready'}, origin: 'user', effective: true}
  ];
  const html = artifactMonitorHTML({entries, selectedKinds: ['adaptation_strategy', 'adaptation_plan'], defaults, project});
  assert.match(html, /尚无已保存内容。/);
  assert.match(html, /最新 v4/);
  assert.match(html, /人工编辑/);
  assert.match(html, /&lt;unsafe&gt;/);
  assert.doesNotMatch(html, /<unsafe>/);
  assert.match(html, /已设置；新项目将复制为该产物的初始版本/);
  assert.match(html, /未设置；新项目初始为空/);
});

test('program output and empty slots do not impersonate an Agent', () => {
  const entries = [
    {kind: 'adaptation_plan', stage: null, agent_name: 'Harness 写入', agent_key: null,
      version: 2, origin: 'program', content: {result_kind: 'ready'}},
    {kind: 'ending_routes', stage: null, expected_stage: 7, agent_key: 'route_designer',
      version: null, content: null}
  ];
  const html = artifactMonitorHTML({entries, selectedKinds: ['adaptation_plan', 'ending_routes'], defaults, project});
  assert.match(html, /阶段未记录 · Harness 写入/);
  assert.doesNotMatch(html, /Agent Harness/);
  assert.match(html, /预期 Step 7 · 预期 Agent route_designer/);
});
