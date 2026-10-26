import { clear, element } from './dom.js';

const INDENT = '  ';

export const lineCount = (text) => String(text ?? '').split('\n').length;

export function lineOfOffset(text, offset) {
  return String(text ?? '').slice(0, Math.max(0, offset)).split('\n').length;
}

export function offsetOfLine(text, line, column = 1) {
  const lines = String(text ?? '').split('\n');
  const target = Math.min(Math.max(1, Math.trunc(line) || 1), lines.length);
  let offset = 0;
  for (let index = 0; index < target - 1; index += 1) offset += lines[index].length + 1;
  return offset + Math.min(Math.max(0, (Math.trunc(column) || 1) - 1), lines[target - 1].length);
}

export function scrollTopForLine(line, lineHeight, viewHeight) {
  return Math.max(0, (line - 1) * lineHeight - viewHeight / 3);
}

export function issueList(errors = [], warnings = [], total = Infinity) {
  const place = (issue, severity) => {
    const line = Number.isInteger(issue?.line) && issue.line > 0 ? Math.min(issue.line, total) : null;
    return { ...issue, line, severity };
  };
  const issues = [...(errors || []).map((issue) => place(issue, 'error')), ...(warnings || []).map((issue) => place(issue, 'warning'))];
  return issues.sort((first, second) => (first.line ?? 0) - (second.line ?? 0) || (first.col ?? 0) - (second.col ?? 0));
}

export function issuesByLine(issues) {
  const byLine = new Map();
  for (const issue of issues) {
    if (issue.line === null) continue;
    const severity = byLine.get(issue.line);
    byLine.set(issue.line, severity === 'error' || issue.severity === 'error' ? 'error' : 'warning');
  }
  return byLine;
}

export function issueText(issue) {
  const where = issue.line === null ? '' : `Line ${issue.line}${issue.col ? `, column ${issue.col}` : ''}: `;
  if (issue.code === 'FROZEN_FIELD' && issue.path) return `${where}Only an admin can change ${issue.path}.`;
  const detail = [issue.code, issue.path ? `at ${issue.path}` : ''].filter(Boolean).join(', ');
  return `${where}${issue.message || 'Problem'}${detail ? ` (${detail})` : ''}`;
}

// The edit Tab or Shift+Tab makes: replace text[from, to) with insert, then select [selStart, selEnd).
export function indentEdit(text, start, end, outdent = false) {
  if (!outdent && start === end) return { from: start, to: end, insert: INDENT, selStart: start + INDENT.length, selEnd: start + INDENT.length };
  const from = text.lastIndexOf('\n', start - 1) + 1;
  const lastEnd = end > start && text[end - 1] === '\n' ? end - 1 : end;
  let to = text.indexOf('\n', lastEnd);
  if (to < 0) to = text.length;
  const original = text.slice(from, to);
  const lines = original.split('\n');
  const deltas = lines.map((line) => {
    if (!outdent) return line === '' && lines.length > 1 ? 0 : INDENT.length;
    return -(line.startsWith(INDENT) ? INDENT.length : line.startsWith(' ') ? 1 : 0);
  });
  const insert = lines.map((line, index) => (deltas[index] >= 0 ? INDENT.slice(0, deltas[index]) + line : line.slice(-deltas[index]))).join('\n');
  if (insert === original) return null;
  const total = deltas.reduce((sum, delta) => sum + delta, 0);
  const selStart = Math.max(from, start + deltas[0]);
  return { from, to, insert, selStart, selEnd: start === end ? selStart : Math.max(selStart, end + total) };
}

export function debounce(callback, wait) {
  let timer = null;
  const debounced = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => callback(...args), wait);
  };
  debounced.cancel = () => clearTimeout(timer);
  return debounced;
}

let editorCount = 0;

// A textarea with a line gutter. Undo survives Tab edits because they go through execCommand.
export function codeEditor({ value = '', label = 'Source', onInput = () => {}, onSave = null } = {}) {
  editorCount += 1;
  const hintId = `code-hint-${editorCount}`;
  const textarea = element('textarea', {
    class: 'code-input', spellcheck: 'false', autocapitalize: 'off', autocomplete: 'off', autocorrect: 'off', wrap: 'off',
    'aria-label': label, 'aria-describedby': hintId,
  });
  textarea.value = value;
  const gutter = element('div', { class: 'code-gutter', 'aria-hidden': 'true' });
  const issuesList = element('ul', { class: 'code-issues', role: 'list', 'aria-label': 'Problems' });
  const hint = element('p', { class: 'note code-hint', id: hintId }, `Tab indents. Press Escape, then Tab, to leave the editor.${onSave ? ' Ctrl+S or Cmd+S saves.' : ''}`);
  const root = element('div', { class: 'code-editor' }, element('div', { class: 'code-frame' }, gutter, textarea), issuesList, hint);
  let savedValue = value;
  let shownLines = 0;
  let issues = [];
  let tabReleased = false;

  function drawGutter() {
    const total = lineCount(textarea.value);
    const marks = issuesByLine(issues);
    if (total !== shownLines) {
      clear(gutter);
      for (let line = 1; line <= total; line += 1) gutter.append(element('div', { class: 'gutter-line' }, String(line)));
      gutter.append(element('div', { class: 'gutter-tail' }));
      shownLines = total;
    }
    gutter.childNodes.forEach((node, index) => {
      const mark = marks.get(index + 1);
      node.classList.toggle('mark-error', mark === 'error');
      node.classList.toggle('mark-warning', mark === 'warning');
    });
    gutter.scrollTop = textarea.scrollTop;
  }

  function focusLine(line, column) {
    const offset = offsetOfLine(textarea.value, line, column);
    textarea.focus();
    textarea.setSelectionRange(offset, offset);
    const lineHeight = parseFloat(getComputedStyle(textarea).lineHeight) || 20;
    textarea.scrollTop = scrollTopForLine(line, lineHeight, textarea.clientHeight);
    gutter.scrollTop = textarea.scrollTop;
  }

  function drawIssues() {
    clear(issuesList);
    for (const issue of issues) {
      const text = issueText(issue);
      const item = issue.line === null
        ? element('span', {}, text)
        : element('button', { type: 'button', class: 'link-button', onclick: () => focusLine(issue.line, issue.col) }, text);
      issuesList.append(element('li', { class: `issue-${issue.severity}` }, item));
    }
    drawGutter();
  }

  function applyEdit(edit) {
    textarea.setSelectionRange(edit.from, edit.to);
    const done = edit.insert ? document.execCommand('insertText', false, edit.insert) : document.execCommand('delete');
    if (!done) {
      textarea.setRangeText(edit.insert, edit.from, edit.to);
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
    }
    textarea.setSelectionRange(edit.selStart, edit.selEnd);
  }

  textarea.addEventListener('keydown', (keyEvent) => {
    if ((keyEvent.metaKey || keyEvent.ctrlKey) && keyEvent.key.toLowerCase() === 's' && onSave) {
      keyEvent.preventDefault();
      onSave();
      return;
    }
    if (keyEvent.key === 'Escape') {
      tabReleased = true;
      return;
    }
    if (keyEvent.key === 'Tab' && !tabReleased && !keyEvent.altKey && !keyEvent.ctrlKey && !keyEvent.metaKey) {
      keyEvent.preventDefault();
      const edit = indentEdit(textarea.value, textarea.selectionStart, textarea.selectionEnd, keyEvent.shiftKey);
      if (edit) applyEdit(edit);
      return;
    }
    tabReleased = false;
  });
  textarea.addEventListener('input', () => {
    drawGutter();
    onInput(textarea.value);
  });
  textarea.addEventListener('scroll', () => {
    gutter.scrollTop = textarea.scrollTop;
  });
  drawGutter();

  return {
    root,
    textarea,
    value: () => textarea.value,
    setValue(text) {
      textarea.value = text;
      drawGutter();
      onInput(text);
    },
    setIssues(errors, warnings) {
      issues = issueList(errors, warnings, lineCount(textarea.value));
      drawIssues();
    },
    hasErrors: () => issues.some((issue) => issue.severity === 'error'),
    focusLine,
    isDirty: () => textarea.value !== savedValue,
    markClean() {
      savedValue = textarea.value;
    },
  };
}
