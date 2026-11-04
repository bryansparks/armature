// Residents on a workflow: the instance list is a tab on the workflow page (residentsTab,
// reached from definition.js), the instance its own page (render, routed by app.js). Everything
// here reads and writes the /api/residents endpoints — the server is the real gate, so the
// client only hides what a role cannot use, mirroring rbac's ranks (session.js holds the facts).
import { api, listOf } from '../api.js';
import { ago, card, clear, confirmDialog, element, empty, errorBox, link, loading, meta, money, pageHeader, table, when } from '../dom.js';
import { hashFor } from '../router.js';
import { isAdmin, memberships } from '../session.js';

const PAGE_SIZE = 25;       // hundreds of instances per workflow: simple client-side pages
const EVENTS_LIMIT = 200;    // the endpoint's cap — the newest events, one call
const JOBS_LIMIT = 500;      // best effort: a turn's run is linked only if its job is this recent
const ASK_WAIT_S = 30;       // how long an ask holds the request open for the turn's reply
const ASK_MAX = 8192;        // the conversation message cap: an ask is one of those
const CLOSE_REASON_MAX = 32; // the column's width: the server refuses anything longer
const REMEMBER_KEY = 'armature-resident-state'; // localStorage: last-seen state, for the diff

export const residentsPath = (workflowId) => `/api/residents/${encodeURIComponent(workflowId)}`;

// A singleton instance lives at the same paths as a keyed one, without the key segment.
export const instancePath = (workflowId, instanceKey = '', tail = '') => {
  const base = `/api/residents/${encodeURIComponent(workflowId)}`;
  return instanceKey ? `${base}/${encodeURIComponent(instanceKey)}${tail}` : `${base}${tail}`;
};

export const instanceHref = (workflowId, instanceKey = '') =>
  hashFor('resident', instanceKey ? [workflowId, instanceKey] : [workflowId]);

const STATE_TONES = { idle: 'ok', paused: 'warn', closed: 'unknown' };
export const residentStateChip = (state) =>
  element('span', { class: `state state-${STATE_TONES[state] || 'unknown'}` }, state || 'unknown');

const RANK = { viewer: 1, contributor: 2, project_lead: 3, team_lead: 4, admin: 5 };
const higher = (a, b) => (a == null ? b : b == null ? a : (RANK[a] || 0) >= (RANK[b] || 0) ? a : b);

// Mirrors rbac.role_for: a team membership covers every workflow in the team, a project one adds.
function roleOn(workflow) {
  let role = null;
  for (const membership of memberships() || []) {
    if (membership.scope_kind === 'team' && String(membership.scope_id) === String(workflow.team_id)) role = higher(role, membership.role);
    else if (membership.scope_kind === 'project' && String(membership.scope_id) === String(workflow.project_id)) role = higher(role, membership.role);
  }
  return role;
}

const atLeast = (workflow, min) => isAdmin() || (RANK[roleOn(workflow)] || 0) >= RANK[min];

const shortText = (text, max = 160) => (text.length > max ? `${text.slice(0, max - 1)}…` : text);

// A JSON value as a table cell: monospaced, cut short, full text on the tooltip.
function jsonCell(value) {
  if (value === null || value === undefined) return element('span', { class: 'muted' }, '—');
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  return element('span', { class: 'mono', title: text.length > 160 ? text : null }, shortText(text));
}

function publishedCell(data) {
  const text = data && Object.keys(data).length ? JSON.stringify(data) : '';
  return text ? element('span', { class: 'mono', title: text }, shortText(text)) : element('span', { class: 'muted' }, '—');
}

export function residentsTab(workflow) {
  const status = element('div', { role: 'status' });
  const body = element('div', {}, loading());
  const count = element('span', { class: 'muted' });
  const previous = element('button', { type: 'button' }, 'Previous');
  const next = element('button', { type: 'button' }, 'Next');
  const pager = element('div', { class: 'wf-actions' }, previous, next, count);
  let residents = [];
  let page = 0;

  const row = (resident) => [
    link(instanceHref(workflow.id, resident.instance_key), resident.instance_key || 'singleton', { class: 'mono' }),
    residentStateChip(resident.state),
    String(resident.turns ?? 0),
    String(resident.inbox ?? 0),
    ago(resident.last_turn_at),
    money(resident.spent_usd_today),
    publishedCell(resident.data),
  ];

  function draw() {
    const start = page * PAGE_SIZE;
    const shown = residents.slice(start, start + PAGE_SIZE);
    clear(body).append(residents.length
      ? table(['Instance', 'State', 'Turns', 'Inbox', 'Last turn', 'Today', 'Published metrics'], shown.map(row))
      : empty('No resident instances yet. They appear once a request, ask, schedule or signal arrives.'));
    previous.disabled = page === 0;
    next.disabled = start + PAGE_SIZE >= residents.length;
    count.textContent = residents.length ? `${start + 1}–${Math.min(start + PAGE_SIZE, residents.length)} of ${residents.length}` : '';
  }

  previous.addEventListener('click', () => { page -= 1; draw(); });
  next.addEventListener('click', () => { page += 1; draw(); });

  (async () => {
    try {
      residents = listOf(await api(residentsPath(workflow.id)), 'residents');
    } catch (err) {
      status.append(errorBox(err));
    } finally {
      draw();
    }
  })();
  return element('div', {}, status, body, pager);
}

// The last-seen published state, kept per instance so a reload can show what changed.
function recallState(workflowId, instanceKey) {
  try {
    return JSON.parse(localStorage.getItem(REMEMBER_KEY) || '{}')[`${workflowId}/${instanceKey}`] || null;
  } catch {
    return null;
  }
}

function rememberState(workflowId, instanceKey, version, data) {
  try {
    const all = JSON.parse(localStorage.getItem(REMEMBER_KEY) || '{}');
    all[`${workflowId}/${instanceKey}`] = { version, data };
    localStorage.setItem(REMEMBER_KEY, JSON.stringify(all));
  } catch {}
}

// What moved between two versions of the published state: key-level, added/removed/changed.
export function dataDiff(before, after) {
  const rows = [];
  for (const key of new Set([...Object.keys(before || {}), ...Object.keys(after || {})])) {
    const oldText = JSON.stringify((before || {})[key]);
    const newText = JSON.stringify((after || {})[key]);
    if (!(key in (before || {}))) rows.push({ key, change: 'added', text: newText });
    else if (!(key in (after || {}))) rows.push({ key, change: 'removed', text: oldText });
    else if (oldText !== newText) rows.push({ key, change: 'changed', text: `${oldText} → ${newText}` });
  }
  return rows;
}

const DIFF_TONES = { added: 'ok', removed: 'bad', changed: 'warn' };
const DIFF_MARKS = { added: '+', removed: '-', changed: '~' };

function eventCell(event) {
  const chip = event.handled
    ? element('span', { class: 'state state-ok' }, 'Handled')
    : element('span', { class: 'state state-warn' }, 'Waiting');
  return element('span', {}, chip, event.attempts > 1 ? ` · ${event.attempts} tries` : null,
    event.dry_run ? ' · dry run' : null);
}

export async function render(root, { args }) {
  const workflowId = args[0];
  const instanceKey = args[1] || '';
  if (!workflowId) {
    root.append(pageHeader('Resident'), card(empty('Pick a workflow to see its resident.')));
    return;
  }
  let workflow;
  try {
    workflow = (await api(`/api/workflows/${encodeURIComponent(workflowId)}`)).workflow;
  } catch (err) {
    root.append(pageHeader('Resident'), errorBox(err));
    return;
  }
  root.append(element('p', { class: 'crumb' },
    link(hashFor('workflow', [workflowId, 'residents']), `${workflow.name} · residents`)),
    pageHeader(instanceKey || 'Resident'));

  let resident = null;
  let events = [];
  const jobs = new Map();
  try {
    resident = (await api(instancePath(workflowId, instanceKey, '/read'))).resident;
    events = listOf(await api(instancePath(workflowId, instanceKey, `/events?limit=${EVENTS_LIMIT}`)), 'events');
  } catch (err) {
    root.append(errorBox(err));
    return;
  }
  // A turn's run: the event knows its job, the job list knows the run — the newest jobs only.
  try {
    for (const job of listOf(await api(`/api/jobs?limit=${JOBS_LIMIT}`), 'jobs')) jobs.set(job.id, job);
  } catch {}

  const metaSlot = element('div');
  const actStatus = element('div', { role: 'status' });
  const stateSlot = element('div');
  const eventsSlot = element('div');
  const askSlot = element('div', { role: 'status' });
  const askForm = element('form', { class: 'stacked-form', id: 'resident-ask-form' });

  const pauseButton = element('button', { type: 'button' }, 'Pause');
  const resumeButton = element('button', { type: 'button' }, 'Resume');
  const upgradeButton = element('button', { type: 'button' }, 'Upgrade');
  const showClose = element('button', { type: 'button' }, 'Close…');
  const closeReason = element('input', { type: 'text', id: 'resident-close-reason', maxlength: CLOSE_REASON_MAX,
                                          placeholder: 'Why it is closing' });
  const closeButton = element('button', { type: 'submit' }, 'Close instance');
  const cancelClose = element('button', { type: 'button', class: 'link-button' }, 'Cancel');
  const closeForm = element('form', { class: 'stacked-form', hidden: true },
    element('label', { class: 'stacked', for: closeReason.id }, 'Reason', closeReason),
    element('div', { class: 'wf-actions' }, closeButton, cancelClose));

  function drawMeta() {
    clear(metaSlot).append(meta(
      residentStateChip(resident.state),
      resident.state === 'closed' && resident.close_reason ? `closed: ${resident.close_reason}` : null,
      resident.state === 'paused' && resident.close_reason && resident.close_reason !== 'admin'
        ? `paused: ${resident.close_reason}` : null,
      link(hashFor('workflow', [workflowId]), workflow.name),
      `Pinned version ${resident.workflow_version} of ${workflow.current_version}`,
      `Turns ${resident.turns}`,
      `Inbox ${resident.inbox}`,
      `Today ${money(resident.spent_usd_today)}`,
      `Last turn ${ago(resident.last_turn_at)}`,
    ));
  }

  function drawState() {
    const remembered = recallState(workflowId, instanceKey);
    const diff = remembered && remembered.version !== resident.data_version
      ? dataDiff(remembered.data, resident.data) : [];
    rememberState(workflowId, instanceKey, resident.data_version, resident.data);
    const changes = diff.length ? element('ul', { class: 'copy-list' }, diff.map((row) => element('li', {},
      element('span', { class: `chip ${DIFF_TONES[row.change]}` }, `${DIFF_MARKS[row.change]} ${row.key}`), ' ',
      element('span', { class: 'mono' }, shortText(row.text))))) : null;
    clear(stateSlot).append(card(element('div', { class: 'card-head' },
      element('h2', { class: 'inline' }, 'Published state'),
      element('span', { class: 'muted' }, `data version ${resident.data_version}`)),
      changes ? [element('p', { class: 'note' }, 'What changed since you last looked:'), changes] : null,
      element('pre', { class: 'source', tabindex: 0 }, JSON.stringify(resident.data ?? {}, null, 2))));
  }

  function drawEvents() {
    const rows = events.map((event) => {
      const job = event.handled_by_job ? jobs.get(event.handled_by_job) : null;
      const live = resident.active_job_id && resident.active_job_id === event.handled_by_job ? ' (turn in progress)' : '';
      const kindText = event.name ? `${event.kind}: ${event.name}` : event.kind;
      return [
        when(event.created_at),
        element('span', { class: 'mono' }, `${kindText} · ${event.seq}`),
        jsonCell(event.payload),
        event.reply ? element('span', { class: 'pre-line' }, shortText(event.reply.text ?? JSON.stringify(event.reply))) : jsonCell(null),
        eventCell(event),
        job?.run_id ? link(hashFor('run', [job.run_id]), 'Open run')
          : event.handled_by_job ? element('span', { class: 'muted', title: event.handled_by_job }, `job ${String(event.handled_by_job).slice(0, 8)}${live}`)
            : element('span', { class: 'muted' }, '—'),
      ];
    });
    clear(eventsSlot).append(element('h2', { class: 'inline' }, 'Events'),
      card(events.length ? table(['When', 'Kind', 'Payload', 'Reply', 'Status', 'Turn'], rows) : empty('No events yet.')),
      events.length >= EVENTS_LIMIT ? element('p', { class: 'note' }, `The newest ${EVENTS_LIMIT} events. Older ones are not shown.`) : null);
  }

  function syncActions() {
    pauseButton.hidden = resident.state !== 'idle';
    resumeButton.hidden = resident.state !== 'paused';
    upgradeButton.hidden = resident.state === 'closed';
    showClose.hidden = resident.state === 'closed' || !closeForm.hidden;
    askForm.hidden = resident.state === 'closed';
  }

  async function act(tail, body, button) {
    clear(actStatus);
    button.disabled = true;
    try {
      resident = (await api(instancePath(workflowId, instanceKey, tail), { method: 'POST', body })).resident;
      closeForm.hidden = true;
      drawMeta();
      drawState();
      syncActions();
      actStatus.append(element('p', { class: 'note' }, 'Done.'));
    } catch (err) {
      actStatus.append(errorBox(err));
    } finally {
      button.disabled = false;
    }
  }

  pauseButton.addEventListener('click', () => act('/pause', {}, pauseButton));
  resumeButton.addEventListener('click', () => act('/resume', {}, resumeButton));
  upgradeButton.addEventListener('click', async () => {
    const target = workflow.current_version;
    if (!(await confirmDialog(`Re-pin to version ${target}?`,
      `This moves the instance from version ${resident.workflow_version} to the workflow's current version ${target}. If its state does not satisfy the new version's schema, it pauses for a migrate instead of closing.`,
      'Upgrade'))) return;
    act('/upgrade', {}, upgradeButton);
  });
  showClose.addEventListener('click', () => {
    closeForm.hidden = false;
    syncActions();
    closeReason.focus();
  });
  cancelClose.addEventListener('click', () => {
    closeForm.hidden = true;
    syncActions();
  });
  closeForm.addEventListener('submit', (submitEvent) => {
    submitEvent.preventDefault();
    const reason = closeReason.value.trim();
    if (!reason) {
      clear(actStatus).append(errorBox({ message: 'Give a reason; it is kept on the instance.' }));
      closeReason.focus();
      return;
    }
    act('/close', { reason }, closeButton);
  });

  const input = element('textarea', { id: 'resident-ask', rows: 3, maxlength: ASK_MAX });
  const askButton = element('button', { type: 'submit', class: 'primary' }, 'Ask');
  askForm.append(element('label', { class: 'stacked', for: input.id }, 'Your question', input),
    element('div', { class: 'wf-actions' }, askButton));
  askForm.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const question = input.value.trim();
    if (!question) return;
    askButton.disabled = true;
    clear(askSlot).append(element('p', { class: 'note', role: 'status' },
      `Asked. The instance takes a turn to answer; up to ${ASK_WAIT_S}s…`));
    try {
      const result = await api(instancePath(workflowId, instanceKey, '/ask'),
        { method: 'POST', body: { question, wait: ASK_WAIT_S } });
      input.value = '';
      clear(askSlot).append(result.reply?.text
        ? element('p', { class: 'pre-line' }, result.reply.text)
        : element('p', { class: 'note' }, 'No answer in time. It lands on the asking event below once the turn finishes.'));
    } catch (err) {
      askSlot.append(errorBox(err));
    } finally {
      askButton.disabled = false;
    }
  });

  drawMeta();
  drawState();
  drawEvents();
  root.append(metaSlot);
  if (atLeast(workflow, 'admin')) {
    root.append(card(element('h2', { class: 'inline' }, 'Actions'),
      element('div', { class: 'wf-actions' }, pauseButton, resumeButton, upgradeButton, showClose),
      closeForm, actStatus));
  }
  if (atLeast(workflow, 'contributor')) root.append(card(element('h2', { class: 'inline' }, 'Ask'), askForm, askSlot));
  syncActions();
  root.append(stateSlot, eventsSlot);
}