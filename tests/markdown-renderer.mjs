import assert from 'node:assert/strict';
import {renderMarkdown} from '../branch_agent/static/markdown.mjs';

const document = renderMarkdown(
  '# 阶段结果\n\n## 事件\n\n1. **相遇**\n2. 第二件事\n\n' +
  '> 原作引文\n\n````text\n原文 <script>alert(1)</script>\n````\n');
assert.match(document, /<h1>阶段结果<\/h1>/);
assert.match(document, /<h2>事件<\/h2>/);
assert.match(document, /<ol><li><strong>相遇<\/strong><\/li><li>第二件事<\/li><\/ol>/);
assert.match(document, /<blockquote>原作引文<\/blockquote>/);
assert.match(document, /<pre><code>原文 &lt;script&gt;alert\(1\)&lt;\/script&gt;<\/code><\/pre>/);
assert.doesNotMatch(document, /<script>|onerror=/);

const hostile = renderMarkdown('# <img src=x onerror=alert(1)>\n\n[click](javascript:alert(1))');
assert.match(hostile, /&lt;img src=x onerror=alert\(1\)&gt;/);
assert.doesNotMatch(hostile, /<img|<a |href=/);

const fixedURL = '/api/branch-agent/v1/projects/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/' +
  'artifacts/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb/versions/2/download.md';
const download = renderMarkdown(`[下载 Markdown](${fixedURL})`);
assert.match(download, /<a href="\/api\/branch-agent\/v1\/projects\/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\/artifacts\/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb\/versions\/2\/download\.md" download>下载 Markdown<\/a>/);

const external = renderMarkdown('[**参考**](https://example.com/source?q=1&lang=zh)');
assert.match(external, /<a href="https:\/\/example\.com\/source\?q=1&amp;lang=zh" target="_blank" rel="noopener noreferrer"><strong>参考<\/strong><\/a>/);
for (const unsafe of [
  '[危险](javascript:alert(1))', '[危险](data:text/html,abc)',
  '[危险](https://user:pass@example.com/)',
  '[危险](https://example.com/\"onclick=\"alert(1))',
]) assert.doesNotMatch(renderMarkdown(unsafe), /<a /);

const nested = renderMarkdown('- 事件\n  - 起因\n    1. 初遇\n    2. 冲突\n  - 结果\n- 下一件事');
assert.equal(nested, '<ul><li>事件<ul><li>起因<ol><li>初遇</li><li>冲突</li></ol></li><li>结果</li></ul></li><li>下一件事</li></ul>');

const table = renderMarkdown(
  '| 事件 | 顺序 | 备注 |\n| :--- | ---: | :---: |\n' +
  '| <img src=x onerror=alert(1)> | 1 | 甲 \\| 乙 |\n' +
  '| [来源](https://example.com/source) | 2 | `a|b` |');
assert.match(table, /<table><thead><tr><th style="text-align:left">事件<\/th><th style="text-align:right">顺序<\/th><th style="text-align:center">备注<\/th><\/tr><\/thead><tbody>/);
assert.match(table, /<td style="text-align:left">&lt;img src=x onerror=alert\(1\)&gt;<\/td>/);
assert.match(table, /<td style="text-align:center">甲 \| 乙<\/td>/);
assert.match(table, /<td style="text-align:center"><code>a\|b<\/code><\/td>/);
assert.match(table, /<a href="https:\/\/example\.com\/source" target="_blank" rel="noopener noreferrer">来源<\/a>/);
assert.doesNotMatch(table, /<img|<script/);
