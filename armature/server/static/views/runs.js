import { api, listOf } from '../api.js';
import { append, card, clear, duration, element, empty, errorBox, link, loading, money, pageHeader, secondsBetween, statusText, statusTone, table, tableRow, when } from '../dom.js';
import { loadDisplay, workflowColor, workflowLabel } from '../display.js';
import { elapsedSeconds } from '../feed.js';
import { every, subscribe } from '../live.js';
import { hashFor } from '../router.js';
import { openTail } from '../tail.js';
import { runningCard } from './overview.js';

const STATUSES = ['queued', 'running', 'succeeded', 'failed', 'canceled', 'lost'];
const PAGE_SIZE = 50;
const MAX_ROWS = 200;
const POLL_MS = 5000;
const PULL_SUBJECT = /^([^#\s]+\/[^#\s]+)#([1-9][0-9]{0,9})$/;
const APPROVED = new Set(['approve', 'approved', 'approval recommended']);
const CHANGES = new Set(['comment', 'changes', 'request_changes', 'changes_requested', 'changes recommended']);
const ACTIVE = new Set(['queued', 'running']);
const ENDED = new Set(['failed', 'canceled', 'lost']);

export const runHeaders = (display) => [['Started', 'col-tight'], ['Run', 'col-title'], ['Workflow', 'col-tight'], 'Repository',
  ['Status', 'col-tight'], 'Trigger', ['Time', 'num col-tight'], ...(display?.show_cost ? [['Cost', 'num col-tight']] : [])];

export function shortWhen(timestamp, now = new Date()) {
  const date = new Date(timestamp || '');
  if (!timestamp || Number.isNaN(date.getTime())) return '—';
  const time = `${date.getHours()}:${String(date.getMinutes()).padStart(2, '0')}`;
  const day = `${date.getMonth() + 1}/${date.getDate()}`;
  return date.getFullYear() === now.getFullYear() ? `${day} ${time}` : `${day}/${String(date.getFullYear()).slice(2)} ${time}`;
}

export const prTitle = (run, pr, display) => pr?.title || run.title || workflowLabel(display, run.workflow);

export function repoOf(subject) {
  const match = PULL_SUBJECT.exec(subject || '');
  return match ? { repo: match[1], number: match[2] } : null;
}

export function verdictText(verdict) {
  const text = String(verdict || '').trim();
  if (!text) return '';
  if (APPROVED.has(text.toLowerCase())) return 'Approved';
  if (CHANGES.has(text.toLowerCase())) return 'Changes Recommended';
  return text.charAt(0).toUpperCase() + text.slice(1).replace(/_/g, ' ');
}

const outcomeTone = (label) => {
  if (label === 'Changes Recommended') return 'warn';
  return /fail|reject|block/i.test(label) ? 'bad' : 'ok';
};

export function runStatus(run, pr) {
  if (ACTIVE.has(run.status)) return { text: 'In Progress', tone: 'running' };
  if (ENDED.has(run.status)) return { text: statusText(run.status), tone: statusTone(run.status) };
  if (pr) {
    const verdict = verdictText(pr.verdict);
    const text = [verdict, pr.merged ? 'Merged' : ''].filter(Boolean).join(' - ');
    if (text) return { text, tone: outcomeTone(verdict) };
  }
  const own = verdictText(run.verdict) || verdictText(run.outcome) || 'Done';
  return { text: own, tone: outcomeTone(own) };
}

export const isHidden = (display, workflow) => display?.workflow_labels?.[workflow]?.hidden === true;

export function chosenWorkflows(query) {
  return [...new Set(String(query.get('workflows') || '').split(',').map((name) => name.trim()).filter(Boolean))];
}

export function listParams(filter, before = '') {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
  for (const key of ['team', 'project', 'status', 'subject']) if (filter[key]) params.set(key, filter[key]);
  if (filter.workflows?.length) params.set('workflows', filter.workflows.join(','));
  if (filter.hidden) params.set('include_hidden', '1');
  if (before) params.set('before', before);
  return params;
}

export function hashParams(filter) {
  return { team: filter.team, project: filter.project, status: filter.status, subject: filter.subject,
    workflows: filter.workflows?.join(',') || '', hidden: filter.hidden ? '1' : '' };
}

export function matches(run, filter, display) {
  if (filter.status && run.status !== filter.status) return false;
  if (filter.subject && run.subject !== filter.subject) return false;
  if (filter.hidden) return !filter.workflows?.length || filter.workflows.includes(run.workflow);
  if (display?.hidden_actors?.includes(run.triggered_by)) return false;
  if (filter.workflows?.length) return filter.workflows.includes(run.workflow);
  return !isHidden(display, run.workflow);
}

export function workflowOptions(display, names) {
  const all = new Set([...Object.keys(display?.workflow_labels || {}), ...names.filter(Boolean)]);
  return [...all].map((name) => ({ id: name, name: workflowLabel(display, name), hidden: isHidden(display, name) }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

export function isLive(run, filter, display) {
  return ACTIVE.has(run?.status) && matches(run, { ...filter, status: '' }, display);
}

export function liveRuns(runs, filter, display) {
  const byId = new Map();
  for (const run of runs) if (isLive(run, filter, display)) byId.set(String(run.id), run);
  return [...byId.values()].sort((left, right) => String(right.started_at || '').localeCompare(String(left.started_at || '')));
}

// "All" first, then each visible workflow. One chosen workflow presses its capsule, several press none.
export function capsules(display, names, chosen) {
  const shown = workflowOptions(display, names).filter((item) => !item.hidden);
  const order = shown.map((item) => item.id);
  return [{ id: '', name: 'All', color: null, pressed: !chosen.length },
    ...shown.map((item) => ({ id: item.id, name: item.name, color: workflowColor(display, item.id, order), pressed: chosen.length === 1 && chosen[0] === item.id }))];
}

export function triggerOf(run) {
  const trigger = run.trigger;
  return trigger?.label ? { label: trigger.label, detail: trigger.detail || '' } : { label: run.triggered_by || '—', detail: '' };
}

export const liveElapsed = (run, now = Date.now()) => (run.started_at ? duration(elapsedSeconds(run.started_at, null, now)) : 'Waiting');

export const subjectHref =(subject) => hashFor('subject', [subject]);

export function verdictTone(verdict) {
  const text = String(verdict || '').toLowerCase();
  if (/not approv|block|change|fail|reject/.test(text)) return 'bad';
  if (/approv|pass|merged|done|pushed|opened/.test(text)) return 'ok';
  return '';
}

export function verdictChip(verdict) {
  if (!verdict) return null;
  return element('span', { class: `chip ${verdictTone(verdict)}`.trim() }, String(verdict).replace(/_/g, ' '));
}

export function subjectLink(subject) {
  return subject ? link(subjectHref(subject), subject) : '—';
}

function triggerCell(run) {
  const { label, detail } = triggerOf(run);
  return [element('span', { class: 'trigger-label' }, label), detail ? element('span', { class: 'muted trigger-detail' }, detail) : null];
}

function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

function repoCell(subject) {
  const pull = repoOf(subject);
  return pull ? [pull.repo, ' ', link(subjectHref(subject), `#${pull.number}`, { class: 'muted' })] : '—';
}

function projectLoader() {
  const cache = new Map();
  return (teamId) => {
    if (!cache.has(teamId)) {
      cache.set(teamId, api(`/api/teams/${encodeURIComponent(teamId)}/projects`).then((payload) => listOf(payload, 'projects')).catch(() => []));
    }
    return cache.get(teamId);
  };
}

function labelled(text, control) {
  return element('label', {}, text, control);
}

function fillOptions(select, anyLabel, items, selected) {
  append(clear(select), [element('option', { value: '' }, anyLabel), items.map((item) => element('option', { value: item.id }, item.name))]);
  select.value = items.some((item) => String(item.id) === selected) ? selected : '';
}

function elapsed(run) {
  if (!run.finished_at) return run.status === 'running' ? 'Running' : '—';
  return duration(secondsBetween(run.started_at, run.finished_at));
}

export async function render(root, { query, signal }) {
  root.append(pageHeader('Runs'));
  const projectsOf = projectLoader();
  const teamSelect = element('select', { name: 'team' });
  const projectSelect = element('select', { name: 'project' });
  const statusSelect = element('select', { name: 'status' }, element('option', { value: '' }, 'Any status'),
    STATUSES.map((status) => element('option', { value: status }, statusText(status))));
  statusSelect.value = STATUSES.includes(query.get('status')) ? query.get('status') : '';
  const subject = query.get('subject') || '';
  const workflowSelect = element('select', { name: 'workflows', multiple: true, size: 1 });
  const pickerToggle = element('button', { type: 'button', class: 'picker-toggle', 'aria-expanded': 'false', 'aria-controls': 'runs-workflow-picker' }, 'Workflows');
  const picker = element('label', { id: 'runs-workflow-picker', hidden: true }, element('span', { class: 'visually-hidden' }, 'Workflows'), workflowSelect);
  pickerToggle.addEventListener('click', () => {
    const open = pickerToggle.getAttribute('aria-expanded') !== 'true';
    pickerToggle.setAttribute('aria-expanded', String(open));
    picker.hidden = !open;
    if (open) workflowSelect.focus();
  });
  const hiddenBox = element('input', { type: 'checkbox', name: 'hidden' });
  hiddenBox.checked = query.get('hidden') === '1';
  const filters = element('form', { class: 'filters', role: 'search', 'aria-label': 'Filter runs' },
    labelled('Team', teamSelect), labelled('Project', projectSelect), labelled('Status', statusSelect),
    pickerToggle, picker, labelled('Show hidden', hiddenBox), element('button', { type: 'submit' }, 'Apply'),
    subject ? element('span', { class: 'muted' }, `Subject: ${subject} `, link(hashFor('runs'), 'Clear')) : null);
  const capsuleRow = element('div', { class: 'capsules', role: 'group', 'aria-label': 'Show one workflow' });
  const liveList = element('div', { class: 'running-list', tabindex: -1 });
  const liveSection = element('section', { class: 'live-runs', 'aria-labelledby': 'runs-live-heading' },
    element('h2', { id: 'runs-live-heading' }, 'Live'), liveList);
  const body = element('div', {}, loading());
  root.append(capsuleRow, filters, liveSection, body);

  const [teams, display, definitions] = await Promise.all([
    api('/api/teams').then((payload) => listOf(payload, 'teams')).catch(() => []),
    loadDisplay(),
    api('/api/workflows').then((payload) => listOf(payload, 'workflows')).catch(() => []),
  ]);
  const filter = { team: '', project: '', status: statusSelect.value, subject, workflows: chosenWorkflows(query), hidden: hiddenBox.checked };
  let before = query.get('before') || '';
  const names = new Set(definitions.map((definition) => definition.name));
  const fillWorkflows = (more) => {
    for (const name of more) if (name) names.add(name);
    const selected = new Set([...workflowSelect.selectedOptions].map((option) => option.value).concat(filter.workflows));
    append(clear(workflowSelect), workflowOptions(display, [...names, ...filter.workflows]).map((item) =>
      element('option', { value: item.id, selected: selected.has(item.id) }, item.hidden ? `${item.name} (hidden)` : item.name)));
    workflowSelect.size = Math.min(Math.max(workflowSelect.options.length, 1), 6);
  };
  const syncPicker = () => {
    for (const option of workflowSelect.options) option.selected = filter.workflows.includes(option.value);
    setText(pickerToggle, filter.workflows.length > 1 ? `Workflows (${filter.workflows.length})` : 'Workflows');
  };
  fillWorkflows([]);
  syncPicker();
  fillOptions(teamSelect, 'All teams', teams, query.get('team') || '');
  fillOptions(projectSelect, 'All projects', teamSelect.value ? await projectsOf(teamSelect.value) : [], query.get('project') || '');
  filter.team = teamSelect.value;
  filter.project = projectSelect.value;
  teamSelect.addEventListener('change', async () => {
    fillOptions(projectSelect, 'All projects', teamSelect.value ? await projectsOf(teamSelect.value) : [], '');
  });
  filters.addEventListener('submit', (submitEvent) => {
    submitEvent.preventDefault();
    location.hash = hashFor('runs', [], hashParams({ team: teamSelect.value, project: projectSelect.value, status: statusSelect.value, subject,
      workflows: [...workflowSelect.selectedOptions].map((option) => option.value), hidden: hiddenBox.checked }));
  });

  const prs = new Map();
  const prFor = (run) => {
    if (run.pr && run.subject) prs.set(run.subject, run.pr);
    return prs.get(run.subject) || null;
  };
  const cells = (run) => {
    const pr = prFor(run);
    const status = runStatus(run, pr);
    return [
      element('span', { title: when(run.started_at) }, shortWhen(run.started_at)),
      link(hashFor('run', [run.id]), prTitle(run, pr, display)), workflowLabel(display, run.workflow),
      repoCell(run.subject), element('span', { class: `state state-${status.tone}` }, status.text),
      triggerCell(run), elapsed(run), ...(display.show_cost ? [money(run.cost_usd)] : []),
    ];
  };
  const headers = runHeaders(display);
  const rowFor = (run) => {
    const row = tableRow(headers, cells(run));
    row.dataset.runId = String(run.id);
    return row;
  };

  let runTable = table(headers, []);
  let tbody = runTable.querySelector('tbody');
  let tableCard = card(runTable);
  let emptyCard = card(empty('No runs yet.'));
  let listToken = 0;
  const loadList = async () => {
    const token = ++listToken;
    let page;
    try {
      page = await api(`/api/runs?${listParams(filter, before)}`);
    } catch (err) {
      if (token === listToken) clear(body).append(errorBox(err));
      return;
    }
    if (token !== listToken || signal?.aborted) return;
    const runs = listOf(page, 'runs');
    fillWorkflows(runs.map((run) => run.workflow));
    const isFiltered = [filter.team, filter.project, filter.status, filter.subject, filter.workflows.length, before].some(Boolean);
    emptyCard = card(empty(isFiltered ? 'No runs match these filters. Clear a filter to see more.' : 'No runs yet.'));
    runTable = table(headers, []);
    tbody = runTable.querySelector('tbody');
    for (const run of runs) tbody.append(rowFor(run));
    tableCard = card(runTable);
    clear(body).append(runs.length ? tableCard : emptyCard);
    const pager = element('nav', { class: 'pager', 'aria-label': 'Pages' });
    if (before) pager.append(link(hashFor('runs', [], hashParams(filter)), 'Newest'));
    if (page.next_before) pager.append(link(hashFor('runs', [], { ...hashParams(filter), before: page.next_before }), 'Older'));
    if (pager.firstChild) body.append(pager);
  };

  // Live: running and queued runs that pass the filter. A click opens their tail beside the list.
  const live = new Map();
  const order = [];
  const colorOf = (workflow) => workflowColor(display, workflow, order);
  let tail = null;
  const liveDetails = (run) => {
    const { label, detail } = triggerOf(run);
    return [
      element('span', { class: 'running-title' }, prTitle(run, prFor(run), display)),
      run.subject ? element('span', { class: 'muted' }, run.subject) : null,
      element('span', { class: 'muted' }, label, detail ? ` · ${detail}` : ''),
      element('span', { class: 'running-elapsed mono', 'data-run-id': String(run.id) }, liveElapsed(run)),
    ];
  };
  const liveButton = (runId) => [...liveList.querySelectorAll('button.running-run')].find((button) => button.dataset.runId === runId);
  const drawLive = () => {
    const focusedId = liveList.contains(document.activeElement) ? document.activeElement.dataset?.runId : null;
    const shown = liveRuns([...live.values()], filter, display);
    clear(liveList).append(...(shown.length ? shown.map((run) => {
      const item = runningCard(run, display, { open: () => openFor(run), color: colorOf(run.workflow), details: liveDetails(run) });
      item.setAttribute('aria-controls', 'runs-tail');
      item.setAttribute('aria-expanded', String(tail?.runId === String(run.id)));
      return item;
    }) : [element('p', { class: 'live-empty' }, 'Nothing running')]));
    if (focusedId) liveButton(focusedId)?.focus();
  };
  const openFor = (run) => {
    if (tail?.runId === String(run.id)) {
      tail.focus();
      return;
    }
    tail?.close();
    const opened = openTail({ runId: run.id, run, display, signal, label: workflowLabel(display, run.workflow), color: colorOf(run.workflow),
      title: prTitle(run, prFor(run), display), subject: run.subject ? subjectLink(run.subject) : null,
      onClose: () => {
        if (tail === opened) tail = null;
        drawLive();
        (liveButton(opened.runId) || liveList).focus();
      } });
    tail = opened;
    root.append(opened.node);
    drawLive();
    opened.focus();
  };
  let liveToken = 0;
  const seedLive = async () => {
    const token = ++liveToken;
    try {
      const pages = await Promise.all([...ACTIVE].map((status) => api(`/api/runs?${listParams({ ...filter, status })}`)));
      if (token !== liveToken || signal?.aborted) return;
      live.clear();
      for (const run of liveRuns(pages.flatMap((page) => listOf(page, 'runs')), filter, display)) live.set(String(run.id), run);
      drawLive();
    } catch {}
  };
  const ticker = setInterval(() => {
    const now = Date.now();
    for (const span of liveList.querySelectorAll('.running-elapsed')) {
      const run = live.get(span.dataset.runId);
      if (run) setText(span, liveElapsed(run, now));
    }
  }, 1000);
  signal?.addEventListener('abort', () => clearInterval(ticker));

  // Capsules filter in place, so the Live block and an open tail stay put.
  const capsuleButtons = new Map();
  const choose = (workflows) => {
    filter.workflows = workflows;
    before = '';
    history.replaceState(null, '', hashFor('runs', [], hashParams(filter)));
    syncPicker();
    for (const item of capsules(display, [...names], filter.workflows)) capsuleButtons.get(item.id)?.setAttribute('aria-pressed', String(item.pressed));
    loadList();
    seedLive();
  };

  await loadList();
  const shownCapsules = capsules(display, [...names], filter.workflows);
  order.push(...shownCapsules.map((item) => item.id).filter(Boolean));
  for (const item of shownCapsules) {
    const button = element('button', { type: 'button', class: 'capsule', 'aria-pressed': String(item.pressed) },
      item.color ? element('span', { class: `swatch sw-${item.color}`, 'aria-hidden': 'true' }) : null, item.name);
    button.addEventListener('click', () => choose(item.id ? [item.id] : []));
    capsuleButtons.set(item.id, button);
    capsuleRow.append(button);
  }
  await seedLive();
  drawLive();

  const pending = new Map();
  let frame = 0;
  const flush = () => {
    frame = 0;
    const batch = [...pending.values()];
    pending.clear();
    if (signal?.aborted) return;
    let liveChanged = false;
    for (const run of batch) {
      const id = String(run.id);
      if (isLive(run, filter, display)) live.set(id, { ...live.get(id), ...run });
      else if (!live.delete(id)) continue;
      liveChanged = true;
    }
    if (liveChanged) drawLive();
    for (const run of batch) {
      if (tail?.runId === String(run.id)) tail.update(run);
      if (!before) upsert(run);
    }
  };
  const upsert = (run) => {
    const existing = tbody.querySelector(`tr[data-run-id="${CSS.escape(String(run.id))}"]`);
    if (existing) {
      const freshCells = [...rowFor(run).cells];
      [...existing.cells].forEach((cell, index) => {
        if (!cell.contains(document.activeElement)) cell.replaceWith(freshCells[index]);
      });
      return;
    }
    if (!matches(run, filter, display)) return;
    const row = rowFor(run);
    const scrolledPast = window.scrollY > 0 && runTable.getBoundingClientRect().top < 0;
    tbody.prepend(row);
    if (!tableCard.isConnected) emptyCard.replaceWith(tableCard);
    if (scrolledPast) window.scrollBy(0, row.getBoundingClientRect().height);
    while (tbody.rows.length > MAX_ROWS) tbody.lastElementChild.remove();
  };
  const receive = (run) => {
    if (!run || run.id === undefined || run.id === null) return;
    pending.set(String(run.id), run);
    if (!frame) frame = requestAnimationFrame(flush);
  };
  const streamParams = new URLSearchParams();
  if (filter.team) streamParams.set('team', filter.team);
  if (filter.project) streamParams.set('project', filter.project);
  const liveNote = element('span', { class: 'live-state muted', role: 'status' });
  root.querySelector('header.page').append(liveNote);
  const states = { live: 'Live', reconnecting: 'Reconnecting…', polling: 'Refreshing every 5 s' };
  subscribe(`/api/stream/runs?${streamParams}`, {
    events: { run: receive },
    onState: (state) => { liveNote.textContent = states[state] || ''; },
    onFallback: () => every(POLL_MS, async () => {
      await seedLive();
      if (before) return;
      for (const run of listOf(await api(`/api/runs?${listParams(filter)}`), 'runs').reverse()) receive(run);
    }, signal),
  }, { signal });
}
