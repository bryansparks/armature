import { element, empty } from './dom.js';

// Rows of a unified diff, numbered on each side. Header lines count only before the first hunk.
export function diffRows(diff) {
  const text = Array.isArray(diff) ? diff.join('\n') : String(diff ?? '');
  const lines = text.split('\n');
  if (lines.at(-1) === '') lines.pop();
  const rows = [];
  let oldLine = 0;
  let newLine = 0;
  let inHunk = false;
  for (const raw of lines) {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(raw);
    if (hunk) {
      [oldLine, newLine] = [Number(hunk[1]), Number(hunk[2])];
      inHunk = true;
      rows.push({ kind: 'hunk', sign: '', text: raw, old: null, new: null });
    } else if (!inHunk || raw.startsWith('\\')) {
      rows.push({ kind: 'meta', sign: '', text: raw, old: null, new: null });
    } else if (raw.startsWith('+')) {
      rows.push({ kind: 'add', sign: '+', text: raw.slice(1), old: null, new: newLine++ });
    } else if (raw.startsWith('-')) {
      rows.push({ kind: 'del', sign: '-', text: raw.slice(1), old: oldLine++, new: null });
    } else {
      rows.push({ kind: 'ctx', sign: ' ', text: raw.startsWith(' ') ? raw.slice(1) : raw, old: oldLine++, new: newLine++ });
    }
  }
  return rows;
}

export function diffView(diff, label = 'Differences') {
  const rows = diffRows(diff);
  if (!rows.some((row) => row.kind === 'add' || row.kind === 'del')) return empty('No differences.');
  return element('div', { class: 'diff', role: 'group', 'aria-label': label, tabindex: 0 }, rows.map((row) => element('div', { class: `diff-row diff-${row.kind}` },
    element('span', { class: 'diff-num' }, row.old ?? ''), element('span', { class: 'diff-num' }, row.new ?? ''),
    element('span', { class: 'diff-sign' }, row.sign), element('span', { class: 'diff-text' }, row.text))));
}
