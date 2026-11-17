// The Insights building blocks: header, filters, tiles, tables, ranked lists, drawer, tooltip, and states.
import { element } from '../../dom.js';
import { cap, delta, figure, initials, isNum, sortRows } from './format.js';

const SVG = 'http://www.w3.org/2000/svg';

export function svgEl(tag, attributes = {}, ...children) {
  const node = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attributes)) if (value !== null && value !== undefined && value !== false) node.setAttribute(name, String(value));
  for (const child of children.flat()) if (child) node.append(child);
  return node;
}

const SHAPES = {
  you: [['path', { d: 'M1.5 9.5l2-6.5h9l2 6.5v4h-13zM1.5 9.5h4l1 2h3l1-2h4' }]],
  bad: [['circle', { cx: 8, cy: 8, r: 6.25 }], ['path', { d: 'M8 4.75v3.75M8 11h.01' }]],
  warn: [['path', { d: 'M8 2l6.5 11.5h-13z' }], ['path', { d: 'M8 6.5v3M8 11.75h.01' }]],
  info: [['circle', { cx: 8, cy: 8, r: 6.25 }], ['path', { d: 'M8 4.5V8l2.25 1.5' }]],
  ok: [['circle', { cx: 8, cy: 8, r: 6.25 }], ['path', { d: 'M5.25 8.25l2 2 3.5-4' }]],
  dash: [['circle', { cx: 8, cy: 8, r: 6.25 }], ['path', { d: 'M5.5 8h5' }]],
};

export function mark(kind, label) {
  return svgEl('svg', { class: `mark is-${kind}`, viewBox: '0 0 16 16', role: label ? 'img' : null, 'aria-label': label || null, 'aria-hidden': label ? null : 'true' },
    (SHAPES[kind] || SHAPES.info).map(([tag, attrs]) => svgEl(tag, attrs)));
}

const glyph = (viewBox, d, attributes = {}) => svgEl('svg', { viewBox, 'aria-hidden': 'true', ...attributes }, svgEl('path', { d }));
export const chevronDown = () => glyph('0 0 12 12', 'M3 4.5l3 3 3-3');
export const chevronRight = () => glyph('0 0 14 14', 'M5.5 3.5L9 7l-3.5 3.5');
const arrowUp = () => glyph('0 0 10 10', 'M5 1.5l3.5 5h-7z');
const arrowDown = () => glyph('0 0 10 10', 'M5 8.5l3.5-5h-7z');
const sortIcon = () => glyph('0 0 8 10', 'M4 1l3.5 5h-7z');
const closeIcon = () => glyph('0 0 16 16', 'M4 4l8 8M12 4l-8 8');

export function avatar(name) {
  return element('span', { class: 'in-avatar', 'aria-hidden': 'true' }, initials(name));
}

let uid = 0;
export const nextId = (prefix) => `${prefix}-${++uid}`;

export function select({ label, options, value, onChange, id = nextId('in-f'), visibleLabel = false }) {
  const control = element('select', { id }, options.map(([optionValue, text]) => element('option', { value: optionValue }, text)));
  control.value = options.some(([optionValue]) => String(optionValue) === String(value ?? '')) ? String(value ?? '') : String(options[0]?.[0] ?? '');
  control.addEventListener('change', () => onChange(control.value));
  const name = element('label', { class: visibleLabel ? 'in-select-label' : 'visually-hidden', for: id }, label);
  if (visibleLabel) return element('span', { class: 'in-select-field' }, name, element('span', { class: 'in-select' }, control, chevronDown()));
  return element('span', { class: 'in-select' }, name, control, chevronDown());
}

export const periodSelect = (days, onChange) => select({
  label: 'Period', value: days, onChange: (value) => onChange(Number(value)),
  options: [[7, 'Last 7 days'], [30, 'Last 30 days'], [90, 'Last 90 days']],
});

export function repoSelect(repos, value, onChange, { all = 'All repos', label = 'Repo' } = {}) {
  return select({ label, value, onChange, options: [...(all ? [['', all]] : []), ...repos.map((repo) => [repo.id, repo.name])] });
}

export function filters(...controls) {
  const form = element('form', { class: 'in-filters', 'aria-label': 'Filters' }, controls);
  form.addEventListener('submit', (submitEvent) => submitEvent.preventDefault());
  return form;
}

export function header({ question, answer, scope, filterBar, who }) {
  const answerNode = element('p', { class: 'in-answer' }, answer ?? element('span', { class: 'in-skel is-line' }));
  const scopeNode = element('p', { class: 'in-scope' }, scope ?? '');
  const title = element('h1', { class: 'in-question' }, question);
  const node = element('header', { class: 'in-head' }, element('div', {}, who ? element('div', { class: 'in-who' }, who, title) : title, answerNode), filterBar || null);
  return { node, answerNode, scopeNode, title };
}

// The number before the chart: label, value with unit, delta with its period, and an optional sparkline.
// none names an empty value ("None yet") and drops the delta, for a measure where zero means nothing has happened.
export function tile({ measure, value, previous, days, period = null, series = null, selected = null, href = null, ref = null, onSelect = null, id = null, none = null }) {
  const describe = nextId('in-tile-desc');
  const loadingValue = value === undefined;
  const isNone = Boolean(none) && !loadingValue && !(Number(value) > 0);
  const shown = figure(value, measure.unit, measure.digits);
  const change = loadingValue ? null : delta(value, previous, measure, days, period || undefined);
  const isTab = selected !== null;
  const tag = isTab ? 'button' : href ? 'a' : 'div';
  const node = element(tag, {
    class: 'in-tile', id, type: isTab ? 'button' : null, role: isTab ? 'tab' : null, href, 'aria-selected': isTab ? String(selected) : null,
    tabindex: isTab ? (selected ? 0 : -1) : (tag === 'div' ? 0 : null), 'data-def': measure.def, 'aria-describedby': describe,
  });
  node.append(element('span', { class: 'in-tile-label' }, measure.label));
  node.append(isNone ? element('span', { class: 'in-tile-value is-none' }, none)
    : element('span', { class: 'in-tile-value' }, loadingValue ? element('span', { class: 'in-skel' }) : shown.value,
      !loadingValue && shown.unit ? element('span', { class: 'in-unit' }, shown.unit) : null));
  let spoken = '';
  if (isNone) spoken = none;
  else if (loadingValue) node.append(element('span', { class: 'in-delta', 'aria-hidden': 'true' }, element('span', { class: 'in-skel' })));
  else if (change) {
    const tone = change.dir === 'flat' ? 'is-flat' : change.good ? 'is-good' : 'is-bad';
    node.append(element('span', { class: 'in-delta', 'aria-hidden': 'true' },
      element('span', { class: `in-change ${tone}` }, change.dir === 'up' ? arrowUp() : change.dir === 'down' ? arrowDown() : null, change.text),
      element('span', { class: 'in-delta-period' }, `vs ${period || `previous ${days} days`}`)));
    spoken = change.sr;
  } else if (isNum(value)) {
    node.append(element('span', { class: 'in-delta' }, 'Not enough history to compare yet.'));
  } else {
    node.append(element('span', { class: 'in-delta' }, 'No data in this period.'));
  }
  if (ref) node.append(element('span', { class: 'in-ref' }, ref));
  node.append(element('span', { hidden: true, id: describe }, `${spoken} ${measure.def}`.trim()));
  if (series) {
    const spark = svgEl('svg', { class: 'in-spark', 'aria-hidden': 'true' });
    node.append(spark);
    whenSized(spark, () => drawSpark(spark, series));
  }
  if (onSelect) node.addEventListener('click', onSelect);
  return node;
}

function drawSpark(svg, values) {
  while (svg.firstChild) svg.firstChild.remove();
  const width = svg.clientWidth || 160;
  const height = 28;
  const pad = 3;
  const present = values.map((value, index) => [index, value]).filter(([, value]) => isNum(value));
  if (present.length < 2) return;
  const numbers = present.map(([, value]) => Number(value));
  const min = Math.min(...numbers);
  const max = Math.max(...numbers);
  const points = present.map(([index, value]) => [pad + (index * (width - pad * 2)) / Math.max(1, values.length - 1), pad + (height - pad * 2) * (1 - (Number(value) - min) / (max - min || 1))]);
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.append(svgEl('polyline', { class: 'line', points: points.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ') }));
  const last = points[points.length - 1];
  svg.append(svgEl('circle', { class: 'now', cx: last[0], cy: last[1], r: 2.75 }));
}

// Runs draw once the node has a width, and again when the width changes.
export function whenSized(node, draw) {
  let lastWidth = -1;
  let pending = false;
  const run = () => {
    pending = false;
    if (!node.isConnected) return;
    const width = node.clientWidth;
    if (!width || width === lastWidth) return;
    lastWidth = width;
    draw(width);
  };
  if (typeof ResizeObserver === 'function') {
    const observer = new ResizeObserver(() => {
      if (!node.isConnected) {
        if (lastWidth >= 0) observer.disconnect();
        return;
      }
      if (pending) return;
      pending = true;
      requestAnimationFrame(run);
    });
    observer.observe(node);
  } else requestAnimationFrame(run);
}

// Tiles that drive a chart: a tablist with arrow keys, Home, and End.
export function tileTabs({ ids, selected, build, onSelect, label = 'Key numbers' }) {
  const strip = element('div', { class: 'in-tiles', role: 'tablist', 'aria-label': label });
  const tiles = new Map();
  for (const id of ids) {
    const node = build(id, id === selected);
    node.addEventListener('click', () => choose(id));
    tiles.set(id, node);
    strip.append(node);
  }
  let current = selected;
  function choose(id, focus = false) {
    current = id;
    for (const [tileId, node] of tiles) {
      node.setAttribute('aria-selected', String(tileId === id));
      node.tabIndex = tileId === id ? 0 : -1;
    }
    if (focus) tiles.get(id).focus();
    onSelect(id);
  }
  strip.addEventListener('keydown', (keyEvent) => {
    const index = ids.indexOf(current);
    const moves = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: ids.length - 1 };
    if (!(keyEvent.key in moves)) return;
    keyEvent.preventDefault();
    choose(ids[(moves[keyEvent.key] + ids.length) % ids.length], true);
  });
  return { strip, tiles, choose };
}

export function section({ title, count = null, link = null, takeaway = null, id = nextId('in-sec'), extra = null }, ...children) {
  const heading = element('h2', { class: 'in-h2', id }, title, isNum(count) ? element('span', { class: 'in-h2-count' }, String(count)) : null);
  const head = element('div', { class: 'in-section-head' }, element('div', {}, heading, takeaway ? element('p', { class: 'in-takeaway' }, takeaway) : null), link, extra);
  return element('section', { class: 'in-section', 'aria-labelledby': id }, head, ...children);
}

export function link(href, text, className = 'in-link') {
  return element('a', { class: className, href }, text);
}

// A sortable table. Each column: key, label, num, opt, def, value(row) for sorting, cell(row) for display.
export function sortableTable({ columns, rows, sort, caption, foot = null, rowClass = 'in-rowlink', onSort = null }) {
  const state = { ...sort };
  const tableNode = element('table', { class: `in-table ${rowClass}` });
  const valueOf = (row, key) => {
    const column = columns.find((col) => col.key === key);
    return column?.value ? column.value(row) : row[key];
  };
  function draw(focusKey = null) {
    while (tableNode.firstChild) tableNode.firstChild.remove();
    if (caption) tableNode.append(element('caption', { class: 'visually-hidden' }, `${caption} Column headers sort the table.`));
    tableNode.append(element('thead', {}, element('tr', {}, columns.map((column) => {
      const cls = [column.num ? 'num' : '', column.opt ? 'in-opt' : '', column.wide ? 'in-wide' : ''].filter(Boolean).join(' ') || null;
      const button = element('button', { type: 'button', class: 'in-sort', 'data-sort': column.key, 'data-def': column.def || null }, column.label, sortIcon());
      button.addEventListener('click', () => {
        if (state.key === column.key) state.dir = state.dir === 'ascending' ? 'descending' : 'ascending';
        else { state.key = column.key; state.dir = column.num ? 'descending' : 'ascending'; }
        onSort?.(state);
        draw(column.key);
      });
      return element('th', { scope: 'col', class: cls, 'aria-sort': state.key === column.key ? state.dir : null }, column.sortable === false ? column.label : button);
    }))));
    const body = element('tbody');
    for (const row of sortRows(rows, state.key, state.dir, valueOf)) {
      body.append(element('tr', {}, columns.map((column) => element('td', {
        class: [column.num ? 'num' : '', column.opt ? 'in-opt' : '', column.cellClass || ''].filter(Boolean).join(' ') || null,
      }, column.cell ? column.cell(row) : row[column.key]))));
    }
    tableNode.append(body);
    if (foot) tableNode.append(element('tfoot', {}, element('tr', {}, columns.map((column) => element('td', {
      class: [column.num ? 'num' : '', column.opt ? 'in-opt' : ''].filter(Boolean).join(' ') || null,
    }, foot(column))))));
    if (focusKey) tableNode.querySelector(`[data-sort="${focusKey}"]`)?.focus();
  }
  draw();
  return tableNode;
}

export function personCell(name, href, sub = '') {
  return element('span', { class: 'in-person' }, avatar(name), element('span', {}, href ? element('a', { href, class: 'in-cover' }, name) : element('span', {}, name),
    sub ? element('span', { class: 'in-person-repo' }, sub) : null));
}

// Ranked rows: name, value, and a bar scaled to the largest value.
export function ranked(rows, { className = '', label = null } = {}) {
  const top = Math.max(0, ...rows.map((row) => Number(row.amount) || 0));
  const hasCurrent = rows.some((row) => row.current);
  return element('ul', { class: `in-ranked ${className}${hasCurrent ? ' has-current' : ''}`.trim(), 'aria-label': label }, rows.map((row) => {
    const name = row.href ? element('a', { href: row.href, 'data-def': row.def || null }, row.name)
      : row.onClick ? element('button', { type: 'button', onclick: row.onClick, 'data-def': row.def || null, 'aria-pressed': row.current === undefined ? null : String(Boolean(row.current)) }, row.name)
        : element('span', { 'data-def': row.def || null, tabindex: row.def ? 0 : null }, row.name);
    const bar = element('span', { class: 'bar', 'aria-hidden': 'true' });
    bar.style.width = `${(top > 0 ? ((Number(row.amount) || 0) / top) * 100 : 0).toFixed(1)}%`;
    return element('li', { 'aria-current': row.current ? 'true' : null },
      element('span', { class: 'name' }, name),
      element('span', { class: 'value' }, row.value, row.after || null), bar);
  }));
}

export function changeMark(change) {
  if (!change || change.dir === 'flat') return null;
  const tone = change.good ? 'is-good' : 'is-bad';
  return element('span', { class: `in-change ${tone}`, 'aria-label': change.sr }, change.dir === 'up' ? arrowUp() : arrowDown(), change.text);
}

export function events(rows, className = '') {
  return element('ul', { class: `in-events ${className}`.trim() }, rows.map(([when, text, href]) => element('li', {},
    element('span', { class: 'when' }, when), href ? element('span', {}, element('a', { href }, text)) : element('span', {}, text))));
}

// States

export function skeletonRows(count = 5) {
  return element('ul', { class: 'in-skel-rows', 'aria-hidden': 'true' }, Array.from({ length: count }, (_, index) => element('li', {},
    element('span', { class: `in-skel is-line${index % 2 ? ' is-short' : ''}` }))));
}

export function busy() {
  return element('div', { role: 'status', class: 'visually-hidden' }, 'Loading');
}

export function emptyState(text, action = null) {
  return element('div', { class: 'in-empty-block' }, element('p', { class: 'in-empty' }, text), action);
}

export function emptyLine(text, action = null) {
  return element('p', { class: 'in-empty' }, text, action ? ' ' : null, action);
}

export function errorState(what, err, retry) {
  const reason = !err?.status ? 'The server didn’t respond.' : err.status === 403 ? 'You don’t have access to this.' : err?.message || 'The server returned an error.';
  const button = retry ? element('button', { type: 'button', class: 'in-button is-quiet', onclick: retry }, 'Try again') : null;
  return element('div', { class: 'in-error', role: 'alert' }, mark('bad', 'Error'), element('span', {}, `Couldn’t load ${what}. ${reason}`), button);
}

export function noPermission(text) {
  return element('p', { class: 'in-empty' }, text);
}

// Definition tooltip and drawer share one overlay root, so they pick up the Insights tokens.

let overlay = null;

function overlayRoot() {
  if (overlay?.isConnected) return overlay;
  overlay = element('div', { class: 'insights in-overlays' });
  const tip = element('div', { class: 'in-def', role: 'tooltip', id: 'in-def', hidden: true });
  overlay.append(tip);
  document.body.append(overlay);
  return overlay;
}

let tooltipsReady = false;

export function installTooltips() {
  if (tooltipsReady) return;
  tooltipsReady = true;
  const tip = () => overlayRoot().querySelector('.in-def');
  const show = (target) => {
    const text = target.getAttribute('data-def');
    if (!text) return;
    const node = tip();
    node.textContent = text;
    node.hidden = false;
    const anchor = target.querySelector('.in-tile-label') || target;
    const rect = anchor.getBoundingClientRect();
    const tipRect = node.getBoundingClientRect();
    const left = Math.min(Math.max(8, rect.left), window.innerWidth - tipRect.width - 8);
    let top = rect.top - tipRect.height - 8;
    if (top < 8) top = rect.bottom + 8;
    node.style.left = `${left}px`;
    node.style.top = `${top}px`;
  };
  const hide = () => {
    const node = overlay?.querySelector('.in-def');
    if (node) node.hidden = true;
  };
  const within = (target) => target?.closest?.('.insights [data-def]');
  document.addEventListener('mouseover', (overEvent) => {
    const target = within(overEvent.target);
    if (target) show(target);
  });
  document.addEventListener('mouseout', (outEvent) => {
    const target = within(outEvent.target);
    if (target && !target.contains(outEvent.relatedTarget)) hide();
  });
  document.addEventListener('focusin', (focusEvent) => {
    const target = within(focusEvent.target);
    if (target) show(target);
    else hide();
  });
  document.addEventListener('focusout', hide);
  document.addEventListener('keydown', (keyEvent) => {
    if (keyEvent.key === 'Escape') hide();
  });
  window.addEventListener('scroll', hide, { passive: true });
  window.addEventListener('hashchange', hide);
}

let drawerState = null;

export function closeDrawer({ restore = true } = {}) {
  if (!drawerState) return;
  const { scrim, panel, opener, onKey } = drawerState;
  drawerState = null;
  scrim.remove();
  panel.remove();
  document.removeEventListener('keydown', onKey, true);
  document.body.style.overflow = '';
  if (restore && opener?.isConnected) opener.focus();
}

// A preview panel from the right. Focus moves to its title, Tab stays inside, Escape or the scrim closes it.
export function openDrawer({ title, why, evidenceTitle, evidence, action }, opener) {
  closeDrawer({ restore: false });
  const root = overlayRoot();
  const scrim = element('div', { class: 'in-scrim' });
  const heading = element('h2', { id: 'in-drawer-title', tabindex: -1 }, title);
  const close = element('button', { type: 'button', class: 'in-close', 'aria-label': 'Close' }, closeIcon());
  const panel = element('aside', { class: 'in-drawer', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'in-drawer-title' },
    element('div', { class: 'in-drawer-top' }, close), heading,
    why instanceof Node ? why : why ? element('p', { class: 'in-drawer-why' }, why) : null,
    evidenceTitle ? element('h3', {}, evidenceTitle) : null, evidence || null, action || null);
  const onKey = (keyEvent) => {
    if (keyEvent.key === 'Escape') {
      keyEvent.preventDefault();
      keyEvent.stopPropagation();
      closeDrawer();
      return;
    }
    if (keyEvent.key !== 'Tab') return;
    const focusable = [...panel.querySelectorAll('button, a[href], select, [tabindex="0"]')];
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (keyEvent.shiftKey && (document.activeElement === first || document.activeElement === heading)) {
      keyEvent.preventDefault();
      last.focus();
    } else if (!keyEvent.shiftKey && document.activeElement === last) {
      keyEvent.preventDefault();
      first.focus();
    } else if (!panel.contains(document.activeElement)) {
      keyEvent.preventDefault();
      first.focus();
    }
  };
  close.addEventListener('click', () => closeDrawer());
  scrim.addEventListener('click', () => closeDrawer());
  panel.addEventListener('click', (clickEvent) => {
    if (clickEvent.target.closest('a[href^="#"]')) closeDrawer({ restore: false });
  });
  root.append(scrim, panel);
  drawerState = { scrim, panel, opener, onKey };
  document.addEventListener('keydown', onKey, true);
  document.body.style.overflow = 'hidden';
  heading.focus();
}

// A drawer whose evidence loads after it opens: skeleton rows first, then fill(data), or an error with Try again.
export function openLoadedDrawer({ title, why = null, evidenceTitle = null, what, load, fill }, opener) {
  const whyNode = element('p', { class: 'in-drawer-why' }, why || '');
  const holder = element('div', { 'aria-busy': 'true' }, skeletonRows(4));
  const action = element('div');
  const run = () => {
    holder.replaceChildren(skeletonRows(4));
    holder.setAttribute('aria-busy', 'true');
    load().then((data) => {
      if (!holder.isConnected) return;
      const filled = fill(data);
      if (filled.why) whyNode.textContent = filled.why;
      holder.replaceChildren(...[filled.evidence].flat().filter(Boolean));
      action.replaceChildren(...[filled.action].flat().filter(Boolean));
      holder.removeAttribute('aria-busy');
    }).catch((err) => {
      if (!holder.isConnected) return;
      holder.replaceChildren(errorState(what, err, run));
      holder.removeAttribute('aria-busy');
    });
  };
  openDrawer({ title, why: whyNode, evidenceTitle, evidence: holder, action }, opener);
  run();
}

export function actionLink([text, href] = []) {
  return text && href ? element('a', { class: 'in-button', href }, text) : null;
}

export const sentence = (text) => cap(String(text || '').trim());
