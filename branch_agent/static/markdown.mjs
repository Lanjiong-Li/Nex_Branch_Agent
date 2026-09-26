import {escapeHTML as escape} from './ui-utils.mjs?v=20260926-2';

// Only this small, explicit Markdown subset creates HTML. Source text and link
// labels are escaped before insertion; arbitrary HTML is never passed through.
const DOWNLOAD = /^\/api\/branch-agent\/v1\/projects\/[0-9a-f-]{36}\/artifacts\/[0-9a-f-]{36}\/versions\/[1-9]\d*\/download\.md$/i;
const MAX_INLINE_DEPTH = 8;
const MAX_LIST_DEPTH = 8;
function closing(text, marker, start) {
  for (let index = start; index < text.length; index++) {
    if (text[index] === '\\') { index++; continue; }
    if (text.startsWith(marker, index)) return index;
  }
  return -1;
}

function externalHref(candidate) {
  if (!/^https?:\/\//i.test(candidate)) return null;
  try {
    const url = new URL(candidate);
    if (!['http:', 'https:'].includes(url.protocol) || !url.hostname ||
        url.username || url.password) return null;
    return url.href;
  } catch {
    return null;
  }
}

function inline(text, depth = 0, allowLinks = true) {
  if (depth >= MAX_INLINE_DEPTH) return escape(text);
  let html = '', plain = '';
  const flush = () => { if (plain) { html += escape(plain); plain = ''; } };
  for (let index = 0; index < text.length;) {
    if (text[index] === '\\' && index + 1 < text.length &&
        /[\\`*_{}\[\]()#+\-.!|<>]/.test(text[index + 1])) {
      plain += text[index + 1];
      index += 2;
      continue;
    }
    if (allowLinks && text[index] === '[') {
      // Conservative URL syntax keeps unsupported forms literal rather than
      // guessing at an attribute value supplied by model-authored text.
      const match = /^\[([^\]\n]{1,120})\]\(([^)\s<>"'\\]{1,2048})\)/.exec(text.slice(index));
      if (match) {
        const href = match[2];
        const internal = DOWNLOAD.test(href);
        const external = internal ? null : externalHref(href);
        if (internal || external) {
          flush();
          const attributes = internal ? ' download' : ' target="_blank" rel="noopener noreferrer"';
          html += '<a href="' + escape(internal ? href : external) + '"' + attributes + '>' +
            inline(match[1], depth + 1, false) + '</a>';
          index += match[0].length;
          continue;
        }
      }
    }
    const marker = text.startsWith('**', index) ? '**'
      : text[index] === '`' ? '`'
      : text[index] === '*' ? '*' : null;
    if (marker) {
      const end = closing(text, marker, index + marker.length);
      if (end > index + marker.length) {
        flush();
        const body = text.slice(index + marker.length, end);
        html += marker === '`' ? '<code>' + escape(body) + '</code>'
          : marker === '**' ? '<strong>' + inline(body, depth + 1, allowLinks) + '</strong>'
          : '<em>' + inline(body, depth + 1, allowLinks) + '</em>';
        index = end + marker.length;
        continue;
      }
    }
    plain += text[index++];
  }
  flush();
  return html;
}

function tableCells(line) {
  let text = line.trim();
  if (!text.includes('|')) return null;
  if (text.startsWith('|')) text = text.slice(1);
  if (text.endsWith('|') && !text.endsWith('\\|')) text = text.slice(0, -1);
  const cells = [];
  let current = '', codeTicks = 0;
  for (let index = 0; index < text.length;) {
    if (text[index] === '\\' && index + 1 < text.length) {
      current += text.slice(index, index + 2);
      index += 2;
      continue;
    }
    if (text[index] === '`') {
      let end = index + 1;
      while (text[end] === '`') end++;
      const count = end - index;
      codeTicks = codeTicks === count ? 0 : codeTicks || count;
      current += text.slice(index, end);
      index = end;
      continue;
    }
    if (text[index] === '|' && !codeTicks) {
      cells.push(current.trim());
      current = '';
      index++;
      continue;
    }
    current += text[index++];
  }
  cells.push(current.trim());
  return cells;
}

function tableAlignment(line) {
  const cells = tableCells(line);
  if (!cells || cells.length < 2 || !cells.every(cell => /^:?-{3,}:?$/.test(cell))) return null;
  return cells.map(cell => cell.startsWith(':') && cell.endsWith(':') ? 'center'
    : cell.endsWith(':') ? 'right' : cell.startsWith(':') ? 'left' : null);
}

function tableCell(tag, text, align) {
  return '<' + tag + (align ? ' style="text-align:' + align + '"' : '') + '>' +
    inline(text) + '</' + tag + '>';
}

export function renderMarkdown(source) {
  const lines = String(source ?? '').replace(/\r\n?/g, '\n').split('\n');
  const output = [];
  let paragraph = [], quote = [], listStack = [];
  const flushParagraph = () => {
    if (paragraph.length) output.push('<p>' + paragraph.map(line => inline(line)).join('<br>') + '</p>');
    paragraph = [];
  };
  const flushQuote = () => {
    if (quote.length) output.push('<blockquote>' + quote.map(line => inline(line)).join('<br>') + '</blockquote>');
    quote = [];
  };
  const closeList = () => {
    const frame = listStack.pop();
    if (frame.hasItem) output.push('</li>');
    output.push('</' + frame.tag + '>');
  };
  const flushList = () => { while (listStack.length) closeList(); };
  const flush = () => { flushParagraph(); flushQuote(); flushList(); };
  const openList = (tag, indent) => {
    output.push('<' + tag + '>');
    listStack.push({tag, indent, hasItem: false});
  };
  const listItem = (tag, indent, body) => {
    if (!listStack.length) openList(tag, indent);
    else {
      while (listStack.length && indent < listStack.at(-1).indent) closeList();
      if (!listStack.length) openList(tag, indent);
      else if (indent > listStack.at(-1).indent) {
        if (listStack.length < MAX_LIST_DEPTH) openList(tag, indent);
        else indent = listStack.at(-1).indent;
      }
      if (listStack.at(-1).tag !== tag) {
        const levelIndent = listStack.at(-1).indent;
        closeList();
        openList(tag, levelIndent);
      } else if (listStack.at(-1).hasItem) output.push('</li>');
    }
    output.push('<li>' + inline(body));
    listStack.at(-1).hasItem = true;
  };
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    const fence = /^\s*(`{3,})/.exec(line);
    if (fence) {
      flush();
      const code = [];
      while (++index < lines.length) {
        const end = /^\s*(`{3,})\s*$/.exec(lines[index]);
        if (end && end[1].length >= fence[1].length) break;
        code.push(lines[index]);
      }
      output.push('<pre><code>' + escape(code.join('\n')) + '</code></pre>');
      continue;
    }
    if (!line.trim()) { flush(); continue; }
    const header = tableCells(line);
    const alignments = index + 1 < lines.length ? tableAlignment(lines[index + 1]) : null;
    if (header && alignments && header.length === alignments.length) {
      flush();
      output.push('<table><thead><tr>' + header.map((cell, i) => tableCell('th', cell, alignments[i])).join('') +
        '</tr></thead><tbody>');
      index++;
      while (index + 1 < lines.length && lines[index + 1].trim()) {
        const cells = tableCells(lines[index + 1]);
        if (!cells) break;
        output.push('<tr>' + alignments.map((align, i) => tableCell('td', cells[i] ?? '', align)).join('') + '</tr>');
        index++;
      }
      output.push('</tbody></table>');
      continue;
    }
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      flush();
      const level = heading[1].length;
      output.push('<h' + level + '>' + inline(heading[2]) + '</h' + level + '>');
      continue;
    }
    const quoted = /^>\s?(.*)$/.exec(line);
    if (quoted) {
      flushParagraph(); flushList();
      quote.push(quoted[1]);
      continue;
    }
    const item = /^([ \t]{0,32})(?:(\d+)\.|([-*+]))[ \t]+(.+)$/.exec(line);
    if (item) {
      flushParagraph(); flushQuote();
      listItem(item[2] ? 'ol' : 'ul', item[1].replace(/\t/g, '    ').length, item[4]);
      continue;
    }
    flushQuote(); flushList();
    paragraph.push(line);
  }
  flush();
  return output.join('');
}
