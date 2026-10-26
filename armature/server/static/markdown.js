// A small Markdown renderer for run artifacts. It parses to a tree and builds the page from elements and
// text nodes only, so raw HTML in an artifact always shows as text and only vetted links become links.
import { element } from './dom.js';

const MAX_DEPTH = 20;
const MAX_LABEL = 2000;
// Past this many '[' in one block, the rest read as text, which bounds a hostile report's parse time.
const MAX_LINK_TRIES = 500;

// Unbounded indexOf per '[' made unterminated links quadratic on a large report.
function boundedIndex(text, needle, from, until) {
  const found = text.slice(from, Math.min(text.length, until)).indexOf(needle);
  return found === -1 ? -1 : from + found;
}
const PUNCTUATION = /[!-/:-@[-`{-~]/;
const LINK_SCHEMES = new Set(['https:', 'http:', 'mailto:']);

const FENCE = /^( {0,3})(`{3,}|~{3,})(.*)$/;
const ATX = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$/;
const RULE = /^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$/;
const QUOTE = /^ {0,3}> ?(.*)$/;
const ITEM = /^( {0,3})([-*+]|\d{1,9}[.)])(?:([ \t]+)(.*)|$)/;
const SETEXT = /^ {0,3}(=+|-+)[ \t]*$/;
const INDENTED = /^(?: {4}|\t)(.*)$/;
const DELIMITER_CELL = /^:?-+:?$/;
const TASK = /^\[([ xX])\](?:[ \t]+|$)/;

// A link target the page may follow: http, https, or mailto, or a hash on this page. Anything else is null.
export function safeHref(raw) {
  const href = String(raw ?? '').trim();
  if (!href || /[\u0000-\u001f\u007f]/.test(href)) return null;
  if (href.startsWith('#')) return /\s/.test(href) ? null : href;
  let parsed;
  try {
    parsed = new URL(href);
  } catch {
    return null;
  }
  return LINK_SCHEMES.has(parsed.protocol) ? parsed.href : null;
}

const isBlank = (line) => /^[ \t]*$/.test(line);

function fenceOpen(line) {
  const match = FENCE.exec(line);
  if (!match || (match[2][0] === '`' && match[3].includes('`'))) return null;
  return { indent: match[1].length, marker: match[2], info: match[3].trim().split(/\s+/)[0] || '' };
}

function itemStart(line) {
  const match = ITEM.exec(line);
  if (!match) return null;
  const [, indent, marker, gap = '', rest = ''] = match;
  const ordered = /\d/.test(marker[0]);
  const spaces = gap.replace(/\t/g, '    ').length;
  const pad = !rest || spaces > 4 ? 1 : spaces;
  return {
    indent: indent.length, ordered, start: ordered ? Number.parseInt(marker, 10) : null, bullet: ordered ? marker.slice(-1) : marker,
    width: indent.length + marker.length + pad, first: !rest ? '' : spaces > 4 ? `${gap.slice(1)}${rest}` : rest,
  };
}

function splitCells(line) {
  let text = line.trim();
  if (text.startsWith('|')) text = text.slice(1);
  if (text.endsWith('|') && !text.endsWith('\\|')) text = text.slice(0, -1);
  const cells = [];
  let cell = '';
  for (let index = 0; index < text.length; index += 1) {
    if (text[index] === '\\' && text[index + 1] === '|') {
      cell += '|';
      index += 1;
    } else if (text[index] === '|') {
      cells.push(cell.trim());
      cell = '';
    } else {
      cell += text[index];
    }
  }
  cells.push(cell.trim());
  return cells;
}

function tableHead(lines, index) {
  const header = lines[index];
  const rule = lines[index + 1];
  if (rule === undefined || !header.includes('|') || !rule.includes('-')) return null;
  const rules = splitCells(rule);
  if (!rules.every((cell) => DELIMITER_CELL.test(cell))) return null;
  const cells = splitCells(header);
  if (cells.length !== rules.length) return null;
  const align = rules.map((cell) => (cell.startsWith(':') && cell.endsWith(':') ? 'center' : cell.endsWith(':') ? 'right' : cell.startsWith(':') ? 'left' : null));
  return { cells, align };
}

// Whether a line ends a paragraph by starting another block.
function interrupts(lines, index) {
  const line = lines[index];
  if (ATX.test(line) || RULE.test(line) || QUOTE.test(line) || fenceOpen(line) || tableHead(lines, index)) return true;
  const item = itemStart(line);
  return Boolean(item && item.first.trim() && (!item.ordered || item.start === 1));
}

function paragraphText(lines) {
  return lines.map((line) => line.replace(/^[ \t]+/, '')).join('\n').replace(/[ \t]+$/, '');
}

export function parseBlocks(text, depth = 0) {
  return blocks(String(text ?? '').replace(/\r\n?/g, '\n').split('\n'), depth);
}

function blocks(lines, depth) {
  const out = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (isBlank(line)) {
      index += 1;
      continue;
    }
    const fence = fenceOpen(line);
    if (fence) {
      const body = [];
      index += 1;
      const close = new RegExp(`^ {0,3}${fence.marker[0] === '`' ? '`' : '~'}{${fence.marker.length},}[ \\t]*$`);
      while (index < lines.length && !close.test(lines[index])) {
        body.push(lines[index].replace(new RegExp(`^ {0,${fence.indent}}`), ''));
        index += 1;
      }
      index += 1;
      out.push({ type: 'code', lang: fence.info, text: body.join('\n') });
      continue;
    }
    const heading = ATX.exec(line);
    if (heading) {
      out.push({ type: 'heading', level: heading[1].length, children: inline((heading[2] || '').replace(/(?:^|[ \t]+)#+[ \t]*$/, ''), depth) });
      index += 1;
      continue;
    }
    if (RULE.test(line)) {
      out.push({ type: 'hr' });
      index += 1;
      continue;
    }
    if (QUOTE.test(line) && depth < MAX_DEPTH) {
      const quoted = [];
      while (index < lines.length && !isBlank(lines[index])) {
        const match = QUOTE.exec(lines[index]);
        if (match) quoted.push(match[1]);
        else if (quoted.length && !isBlank(quoted.at(-1)) && !interrupts(lines, index)) quoted.push(lines[index]);
        else break;
        index += 1;
      }
      out.push({ type: 'blockquote', children: blocks(quoted, depth + 1) });
      continue;
    }
    const table = tableHead(lines, index);
    if (table) {
      const rows = [];
      index += 2;
      while (index < lines.length && !isBlank(lines[index]) && lines[index].includes('|') && !interrupts(lines, index)) {
        const cells = splitCells(lines[index]);
        rows.push(table.cells.map((_, column) => inline(cells[column] ?? '', depth)));
        index += 1;
      }
      out.push({ type: 'table', align: table.align, header: table.cells.map((cell) => inline(cell, depth)), rows });
      continue;
    }
    const first = itemStart(line);
    if (first && depth < MAX_DEPTH) {
      index = list(lines, index, first, depth, out);
      continue;
    }
    const code = INDENTED.exec(line);
    if (code) {
      const body = [];
      while (index < lines.length && (INDENTED.test(lines[index]) || isBlank(lines[index]))) {
        body.push(INDENTED.exec(lines[index])?.[1] ?? '');
        index += 1;
      }
      while (body.length && isBlank(body.at(-1))) body.pop();
      out.push({ type: 'code', lang: '', text: body.join('\n') });
      continue;
    }
    const text = [line];
    index += 1;
    while (index < lines.length && !isBlank(lines[index])) {
      const setext = SETEXT.exec(lines[index]);
      if (setext) {
        out.push({ type: 'heading', level: setext[1][0] === '=' ? 1 : 2, children: inline(paragraphText(text), depth) });
        text.length = 0;
        index += 1;
        break;
      }
      if (interrupts(lines, index)) break;
      text.push(lines[index]);
      index += 1;
    }
    if (text.length) out.push({ type: 'paragraph', children: inline(paragraphText(text), depth) });
  }
  return out;
}

function list(lines, index, first, depth, out) {
  const node = { type: 'list', ordered: first.ordered, start: first.start, tight: true, items: [] };
  let item = first;
  while (item) {
    const body = [item.first];
    index += 1;
    while (index < lines.length) {
      const line = lines[index];
      if (isBlank(line)) {
        body.push('');
        index += 1;
        continue;
      }
      const indent = line.match(/^ */)[0].length;
      // Two spaces past the marker's own indent nest an item, as most writers expect, even short of its content.
      if (indent >= item.width || (indent >= item.indent + 2 && itemStart(line))) body.push(line.slice(Math.min(indent, item.width)));
      else if (!isBlank(body.at(-1)) && !itemStart(line) && !interrupts(lines, index)) body.push(line);
      else break;
      index += 1;
    }
    let trailing = 0;
    while (body.length > 1 && isBlank(body.at(-1))) {
      body.pop();
      trailing += 1;
    }
    if (body.some((line, at) => at > 0 && isBlank(line))) node.tight = false;
    const task = TASK.exec(body[0]);
    if (task) body[0] = body[0].slice(task[0].length);
    node.items.push({ task: task ? task[1] !== ' ' : null, children: blocks(body, depth + 1) });
    const next = index < lines.length ? itemStart(lines[index]) : null;
    if (!next || next.ordered !== first.ordered || next.bullet !== first.bullet) break;
    if (trailing) node.tight = false;
    item = next;
  }
  out.push(node);
  return index;
}

// Positions past which no closer of a kind exists, so unmatched markers cost one scan each, not one per opener.
function memo() {
  return new Map();
}

function skipCode(text, index) {
  let run = 0;
  while (text[index + run] === '`') run += 1;
  const close = text.indexOf('`'.repeat(run), index + run);
  let at = close;
  while (at !== -1 && (text[at + run] === '`' || text[at - 1] === '`')) {
    at = text.indexOf('`'.repeat(run), at + 1);
  }
  return at === -1 ? index + run : at + run;
}

function closerAt(text, index, marker) {
  const char = marker[0];
  let run = 0;
  while (text[index + run] === char) run += 1;
  if (marker.length === 1 ? run !== 1 : run < marker.length) return false;
  if (/\s/.test(text[index - 1] ?? ' ')) return false;
  return char !== '_' || !/[\p{L}\p{N}]/u.test(text[index + marker.length] ?? '');
}

function findCloser(text, from, marker, misses) {
  const key = marker;
  if ((misses.get(key) ?? Infinity) <= from) return -1;
  let index = from;
  while (index < text.length) {
    const char = text[index];
    if (char === '\\') {
      index += 2;
      continue;
    }
    if (char === '`') {
      index = skipCode(text, index);
      continue;
    }
    if (char === marker[0]) {
      if (closerAt(text, index, marker)) return index;
      let run = 0;
      while (text[index + run] === char) run += 1;
      index += run;
      continue;
    }
    index += 1;
  }
  misses.set(key, Math.min(misses.get(key) ?? Infinity, from));
  return -1;
}

function parseLink(text, open) {
  let depth = 0;
  let index = open;
  let labelEnd = -1;
  const limit = Math.min(text.length, open + MAX_LABEL);
  while (index < limit) {
    const char = text[index];
    if (char === '\\') {
      index += 2;
      continue;
    }
    if (char === '`') {
      index = skipCode(text, index);
      continue;
    }
    if (char === '[') depth += 1;
    if (char === ']') {
      depth -= 1;
      if (depth === 0) {
        labelEnd = index;
        break;
      }
    }
    index += 1;
  }
  if (labelEnd === -1 || text[labelEnd + 1] !== '(') return null;
  index = labelEnd + 2;
  while (text[index] === ' ' || text[index] === '\n') index += 1;
  let dest = '';
  if (text[index] === '<') {
    const close = boundedIndex(text, '>', index, index + MAX_LABEL);
    if (close === -1 || text.slice(index, close).includes('\n')) return null;
    dest = text.slice(index + 1, close);
    index = close + 1;
  } else {
    let parens = 0;
    const start = index;
    while (index < text.length && index - start < MAX_LABEL) {
      const char = text[index];
      if (char === '\\' && PUNCTUATION.test(text[index + 1] ?? '')) {
        index += 2;
        continue;
      }
      if (/\s/.test(char)) break;
      if (char === '(') parens += 1;
      if (char === ')') {
        if (parens === 0) break;
        parens -= 1;
      }
      index += 1;
    }
    dest = text.slice(start, index).replace(/\\([!-/:-@[-`{-~])/g, '$1');
  }
  while (text[index] === ' ' || text[index] === '\n') index += 1;
  let title = null;
  const quote = { '"': '"', "'": "'", '(': ')' }[text[index]];
  if (quote) {
    const close = boundedIndex(text, quote, index + 1, index + 1 + MAX_LABEL);
    if (close === -1) return null;
    title = text.slice(index + 1, close);
    index = close + 1;
    while (text[index] === ' ' || text[index] === '\n') index += 1;
  }
  if (text[index] !== ')') return null;
  return { label: text.slice(open + 1, labelEnd), dest, title, end: index + 1 };
}

function plainText(nodes) {
  return nodes.map((node) => (node.type === 'text' || node.type === 'code' ? node.text : node.type === 'break' ? '\n' : plainText(node.children || []))).join('');
}

const BARE_URL = /^https?:\/\/[^\s<]+/;
const AUTOLINK = /^<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*)>/;
const EMAIL = /^<([^\s<>@]+@[^\s<>@]+\.[^\s<>@]+)>/;

function trimUrl(url) {
  let end = url.length;
  for (;;) {
    const last = url[end - 1];
    if (/[?!.,:;*_~'"]/.test(last)) end -= 1;
    else if (last === ')' && (url.slice(0, end).match(/\)/g) || []).length > (url.slice(0, end).match(/\(/g) || []).length) end -= 1;
    else break;
  }
  return url.slice(0, end);
}

export function inline(text, depth = 0, misses = memo()) {
  const out = [];
  let linkTries = 0;
  let buffer = '';
  const flush = () => {
    if (buffer) out.push({ type: 'text', text: buffer });
    buffer = '';
  };
  const push = (node) => {
    flush();
    out.push(node);
  };
  const nested = (inner) => (depth < MAX_DEPTH ? inline(inner, depth + 1) : [{ type: 'text', text: inner }]);
  let index = 0;
  while (index < text.length) {
    const char = text[index];
    if (char === '\\' && index + 1 < text.length) {
      if (text[index + 1] === '\n') {
        push({ type: 'break' });
        index += 2;
        continue;
      }
      if (PUNCTUATION.test(text[index + 1])) {
        buffer += text[index + 1];
        index += 2;
        continue;
      }
    }
    if (char === '\n') {
      const hard = / {2,}$/.test(buffer);
      buffer = buffer.replace(/ +$/, '');
      if (hard) push({ type: 'break' });
      else buffer += '\n';
      index += 1;
      while (text[index] === ' ') index += 1;
      continue;
    }
    if (char === '`') {
      let run = 0;
      while (text[index + run] === '`') run += 1;
      const end = skipCode(text, index);
      if (end > index + run) {
        let code = text.slice(index + run, end - run).replace(/\n/g, ' ');
        if (code.length > 2 && code.startsWith(' ') && code.endsWith(' ') && code.trim()) code = code.slice(1, -1);
        push({ type: 'code', text: code });
      } else {
        buffer += '`'.repeat(run);
      }
      index = end;
      continue;
    }
    if (char === '!' && text[index + 1] === '[' && linkTries++ < MAX_LINK_TRIES) {
      const link = parseLink(text, index + 1);
      if (link) {
        push({ type: 'image', alt: plainText(nested(link.label)), url: link.dest });
        index = link.end;
        continue;
      }
    }
    if (char === '[' && linkTries++ < MAX_LINK_TRIES) {
      const link = parseLink(text, index);
      if (link) {
        const children = nested(link.label);
        const href = safeHref(link.dest);
        if (href) push({ type: 'link', href, title: link.title, children });
        else {
          flush();
          out.push(...children);
        }
        index = link.end;
        continue;
      }
    }
    if (char === '<') {
      const rest = text.slice(index, index + MAX_LABEL);
      const auto = AUTOLINK.exec(rest);
      const email = !auto && EMAIL.exec(rest);
      const href = auto ? safeHref(auto[1]) : email ? safeHref(`mailto:${email[1]}`) : null;
      if (href) {
        push({ type: 'link', href, title: null, children: [{ type: 'text', text: (auto || email)[1] }] });
        index += (auto || email)[0].length;
        continue;
      }
    }
    if (char === 'h' && (index === 0 || /[\s(*_~]/.test(text[index - 1]))) {
      const bare = BARE_URL.exec(text.slice(index, index + MAX_LABEL));
      if (bare) {
        const url = trimUrl(bare[0]);
        const href = safeHref(url);
        if (href) {
          push({ type: 'link', href, title: null, children: [{ type: 'text', text: url }] });
          index += url.length;
          continue;
        }
      }
    }
    if (char === '~' && text[index + 1] === '~' && text[index + 2] !== '~' && !/\s/.test(text[index + 2] ?? ' ')) {
      const close = findCloser(text, index + 2, '~~', misses);
      if (close > index + 2) {
        push({ type: 'del', children: nested(text.slice(index + 2, close)) });
        index = close + 2;
        continue;
      }
    }
    if ((char === '*' || char === '_') && !/\s/.test(text[index + 1] ?? ' ') && (char === '*' || !/[\p{L}\p{N}]/u.test(text[index - 1] ?? ' '))) {
      let run = 0;
      while (text[index + run] === char) run += 1;
      const opened = (marker, type) => {
        const close = findCloser(text, index + marker.length, marker, misses);
        if (close <= index + marker.length) return false;
        push({ type, children: nested(text.slice(index + marker.length, close)) });
        index = close + marker.length;
        return true;
      };
      if (run >= 3) {
        const close = findCloser(text, index + 3, char.repeat(3), misses);
        if (close > index + 3) {
          push({ type: 'strong', children: [{ type: 'em', children: nested(text.slice(index + 3, close)) }] });
          index = close + 3;
          continue;
        }
      }
      if (run >= 2 && opened(char.repeat(2), 'strong')) continue;
      if (run === 1 && opened(char, 'em')) continue;
      buffer += char.repeat(run);
      index += run;
      continue;
    }
    buffer += char;
    index += 1;
  }
  flush();
  return out;
}

export function parseMarkdown(text) {
  return parseBlocks(text);
}

const ALIGN_CLASS = { left: 'align-left', center: 'align-center', right: 'align-right' };

function renderInline(nodes) {
  return nodes.map((node) => {
    switch (node.type) {
      case 'text': return node.text;
      case 'code': return element('code', {}, node.text);
      case 'strong': return element('strong', {}, renderInline(node.children));
      case 'em': return element('em', {}, renderInline(node.children));
      case 'del': return element('del', {}, renderInline(node.children));
      case 'break': return element('br');
      case 'image': return node.alt ? `${node.alt} (${node.url})` : node.url;
      case 'link': {
        const anchor = element('a', { rel: 'noopener noreferrer', title: node.title || null, target: node.href.startsWith('#') ? null : '_blank' }, renderInline(node.children));
        // dom.js allows only https and hash links, and safeHref has already vetted this one.
        anchor.setAttribute('href', node.href);
        return anchor;
      }
      default: return null;
    }
  });
}

function renderBlock(block, tight = false) {
  switch (block.type) {
    case 'heading': return element(`h${Math.min(block.level + 1, 6)}`, {}, renderInline(block.children));
    case 'paragraph': return tight ? renderInline(block.children) : element('p', {}, renderInline(block.children));
    case 'code': return element('pre', { class: 'md-code' }, element('code', { 'data-lang': block.lang || null }, block.text));
    case 'hr': return element('hr');
    case 'blockquote': return element('blockquote', {}, block.children.map((child) => renderBlock(child)));
    case 'table': {
      // Phone layouts stack each row and label every cell with its column, as the app's own tables do.
      const labels = block.header.map((nodes) => plainText(nodes).trim() || null);
      const cell = (tag, nodes, column) => element(tag, {
        class: ALIGN_CLASS[block.align[column]] || null, scope: tag === 'th' ? 'col' : null, 'data-label': tag === 'td' ? labels[column] : null,
      }, renderInline(nodes));
      return element('div', { class: 'md-table' }, element('table', {},
        element('thead', {}, element('tr', {}, block.header.map((nodes, column) => cell('th', nodes, column)))),
        element('tbody', {}, block.rows.map((row) => element('tr', {}, row.map((nodes, column) => cell('td', nodes, column)))))));
    }
    case 'list': {
      const hasTasks = block.items.some((item) => item.task !== null);
      return element(block.ordered ? 'ol' : 'ul', { start: block.ordered && block.start !== 1 ? block.start : null, class: hasTasks ? 'md-tasks' : null },
        block.items.map((item) => element('li', { class: item.task === null ? null : 'md-task' },
          item.task === null ? null : element('input', { type: 'checkbox', disabled: true, checked: item.task, 'aria-label': item.task ? 'Done' : 'Not done' }),
          item.children.map((child) => renderBlock(child, block.tight)))));
    }
    default: return null;
  }
}

// The rendered artifact, in a container the page styles as Markdown.
export function renderMarkdown(text) {
  return element('div', { class: 'markdown' }, parseMarkdown(text).map((block) => renderBlock(block)));
}
