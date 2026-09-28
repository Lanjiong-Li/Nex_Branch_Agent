import {escapeHTML as e, publicView} from './ui-utils.mjs?v=20260926-2';

export const editableArtifactKinds = ['adaptation_strategy', 'adaptation_plan'];

const labels = {
  source_text: '原作全文', source_global_events: '作品事件视图', source_character_events: '主要人物事件视图',
  source_knowledge_asset: '原作知识资产',
  adaptation_strategy: '玩家与互动策略', adaptation_plan: '互动剧本改编方案',
  game_event_view: '互动剧本事件视图', ending_routes: '结局路线', player_profiles: '目标玩家画像',
  chapter_design: '章节设计', chapter_graph: '章节 Graph', nexo_graph: '完整互动剧本'
};
const fallbackDescriptions = {
  source_text: '保存导入的完整原作。',
  source_global_events: 'Step1 作品事件视图 Agent 切分的完整事件、原文边界及逐事件分析。',
  source_character_events: 'Step1 主要人物事件视图 Agent 直接从原文梳理的人物事件线。',
  source_knowledge_asset: 'Step2 知识资产分析 Agent 综合作品事件及其逐事件分析、人物事件形成的结构化原作知识。',
  adaptation_strategy: '确定玩家身份、改编原则和互动策略。',
  adaptation_plan: '规划互动剧本的目标、范围与改编方案。',
  game_event_view: '将原作事件映射为游戏事件。',
  ending_routes: '设计结局及抵达结局的路线。',
  player_profiles: '描述目标玩家及其体验需求。',
  chapter_design: '规划章节结构和场景。',
  chapter_graph: '保存章节内的互动结构。',
  nexo_graph: '保存完整互动剧本图。'
};

const formatted = value => typeof value === 'string' ? value : JSON.stringify(publicView(value), null, 2);
const title = entry => entry.label ?? labels[entry.kind] ?? entry.kind;
const producer = entry => {
  if (entry.kind === 'source_text') return '人工导入';
  if (entry.version != null && entry.origin === 'program') return 'Harness 写入';
  if (entry.version != null && ['user', 'manual'].includes(entry.origin)) return '人工编辑';
  const name=entry.agent_name ?? entry.agent_key;
  return entry.version == null ? `预期 Agent ${name ?? '未指定'}` : `Agent ${name ?? '未指定'}`;
};
const stage = entry => entry.kind === 'source_text' || entry.stage === 0 ? '项目素材' : entry.version == null && entry.stage != null ? `预期 Step ${entry.stage}` : entry.stage != null ? `Step ${entry.stage}` : entry.expected_stage != null ? `预期 Step ${entry.expected_stage}` : '阶段未记录';
const origin = value => ({user: '人工编辑', manual: '人工编辑', model: 'Agent 生成', program: 'Harness 写入', default: '新项目默认值'})[value] ?? value ?? '已保存';
const entryKey = entry => entry.artifact_id ?? `${entry.kind}:${entry.chapter_id ?? ''}`;

function contentHTML(entry, expanded, loading) {
  if (entry.version == null) return '<p class="artifact-empty">尚无已保存内容。</p>';
  if (entry.content == null) return `<p class="artifact-empty">${loading ? '正在读取当前版本内容…' : '当前版本没有可显示的内容。'}</p>`;
  const value = formatted(entry.content);
  const long = value.length > 12000;
  const visible = long && !expanded ? `${value.slice(0, 12000)}\n…` : value;
  return `<pre class="artifact-content">${e(visible)}</pre>${long ? `<button type="button" class="button small" data-action="toggle-artifact-content" data-key="${e(entryKey(entry))}">${expanded ? '收起内容' : '展开完整内容'}</button>` : ''}`;
}

function currentCard(entry, expanded, loading) {
  const editable = editableArtifactKinds.includes(entry.kind);
  const chapter = entry.chapter_id ? ` · 章节 ${e(entry.chapter_id)}` : '';
  const source=entry.kind==='source_text'?'人工导入':editable&&entry.origin==='program'&&entry.version===1?'新项目初始值 · Harness 写入':origin(entry.origin);
  return `<article class="artifact-current-card" data-kind="${e(entry.kind)}"><header><div><h4>${e(title(entry))}${chapter}</h4><code>${e(entry.kind)}</code></div><span class="badge ${entry.version == null ? '' : 'good'}">${entry.version == null ? '尚无产物' : `最新 v${e(entry.version)}`}</span></header><p class="artifact-card-meta">${e(stage(entry))} · ${e(producer(entry))} · ${e(entry.description ?? fallbackDescriptions[entry.kind] ?? '保存该阶段的中间产物。')}</p>${entry.version != null ? `<p class="small muted">${e(source)}${entry.effective === false ? ' · 尚未生效' : ''}</p>` : ''}${contentHTML(entry, expanded, loading)}${editable ? `<div class="artifact-card-actions"><button type="button" class="button small" data-action="edit-artifact" data-kind="${e(entry.kind)}">${entry.version == null ? '填写当前产物' : '编辑当前产物'}</button></div>` : ''}</article>`;
}

function defaultCard(kind, defaults) {
  const content = defaults?.[kind];
  const name = labels[kind];
  const preview=content == null ? '' : `<details class="artifact-default-preview"><summary>查看默认内容</summary><pre class="artifact-content">${e(formatted(content))}</pre></details>`;
  return `<article class="artifact-default-card"><div><strong>${e(name)}</strong><code>${e(kind)}</code><span class="small muted">${content == null ? '未设置；新项目初始为空' : '已设置；新项目将复制为该产物的初始版本'}</span>${preview}</div><div class="artifact-card-actions"><button type="button" class="button small" data-action="edit-artifact-default" data-kind="${e(kind)}">${content == null ? '设置默认值' : '编辑默认值'}</button>${content == null ? '' : `<button type="button" class="button small subtle" data-action="clear-artifact-default" data-kind="${e(kind)}">清空</button>`}</div></article>`;
}

export function artifactMonitorHTML({entries = [], selectedKinds = [], defaults = null, expandedKinds = [], error = '', defaultsError = '', project = null, loading = false}) {
  const selected = new Set(selectedKinds);
  const expanded = new Set(expandedKinds);
  const pickerEntries = [...new Map(entries.map(entry => [entry.kind, entry])).values()];
  const picker = pickerEntries.map(entry => `<label class="artifact-pick"><input type="checkbox" data-artifact-select value="${e(entry.kind)}" ${selected.has(entry.kind) ? 'checked' : ''}><span><strong>${e(title(entry))}</strong><small>${e(stage(entry))} · ${e(producer(entry))}</small><small>${e(entry.description ?? fallbackDescriptions[entry.kind] ?? '保存该阶段的中间产物。')}</small></span></label>`).join('');
  const cards = entries.filter(entry => selected.has(entry.kind)).map(entry => currentCard(entry, expanded.has(entryKey(entry)), loading)).join('');
  return `<section id="artifact-monitor" class="artifact-monitor" aria-label="中间产物"><div class="runtime-hero"><div><h3>中间产物</h3><p>选择产物类型，查看当前项目最新保存的内容；目录标注来源阶段、执行者和内容用途。</p></div><button type="button" class="button" data-action="refresh-artifacts" ${!project ? 'disabled' : ''}>刷新产物</button></div>${project ? `${error ? `<p class="action-error" role="alert">${e(error)}</p>` : ''}${loading && !entries.length ? '<p class="loading">正在读取中间产物…</p>' : `<div class="artifact-browser"><fieldset class="artifact-picker"><legend>选择要展示的产物</legend>${picker || '<p class="small muted">尚无可展示的产物类型。</p>'}</fieldset><div class="artifact-current">${selected.size ? cards || '<p class="artifact-empty">当前选择的产物类型尚未返回数据。</p>' : '<p class="artifact-empty">从左侧选择产物，查看最新保存版本。</p>'}</div></div>`}` : '<p class="artifact-empty">创建或选择项目后可查看其中的产物；下方仍可设置所有新项目的初始值。</p>'}<div class="artifact-defaults"><div><h4>新项目默认值</h4><p>保存后只应用于之后创建的项目。新项目会把这里的内容复制到同一类中间产物中；已有项目保持自己的版本。</p></div>${defaultsError ? `<p class="action-error" role="alert">${e(defaultsError)}</p>` : ''}<div class="artifact-default-grid">${editableArtifactKinds.map(kind => defaultCard(kind, defaults)).join('')}</div></div></section>`;
}
