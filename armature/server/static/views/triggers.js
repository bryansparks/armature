import { api, listOf } from '../api.js';
import { attempt, card, clear, confirmDialog, element, empty, errorBox, link, loading, pageHeader, statusChip, table, tabs, when } from '../dom.js';
import { hashFor } from '../router.js';
import { canManageTeam } from '../session.js';
import { prettyMapping, splitList } from './webhooks.js';

export const KINDS = { jira_poll: 'Jira poll', linear_poll: 'Linear poll', schedule: 'Schedule' };
const CONNECTION_KIND = { jira_poll: 'jira', linear_poll: 'linear' };
const LINEAR_STATES = ['triage', 'backlog', 'unstarted', 'started', 'completed', 'canceled'];
export const OPEN_SPRINT_NOTE = 'With sprint in openSprints(), an issue assigned to you but outside an open sprint never matches. One pulled into a sprint later matches on the next poll. An issue that leaves the sprint and comes back is not queued again.';
const OUTCOME_LABEL = { queued: 'Queued', seeded: 'Seeded', shadow: 'Shadow', skipped: 'Skipped' };

export const triggersPath = (teamId) => `/api/teams/${encodeURIComponent(teamId)}/triggers`;
export const triggerPath = (triggerId) => `/api/triggers/${encodeURIComponent(triggerId)}`;
export const triggerHref = (triggerId) => hashFor('trigger', [triggerId]);

export function scheduleText(trigger) {
  const config = trigger.config || {};
  if (trigger.kind === 'schedule') return `${config.cron} (${config.timezone || 'UTC'})`;
  const minutes = config.interval_minutes || 5;
  return minutes === 1 ? 'Every minute' : `Every ${minutes} minutes`;
}

export function lastResultText(state) {
  if (!state?.last_status) return 'Never ran';
  if (state.last_status === 'error') return `Failed: ${state.last_error || 'unknown error'}`;
  return `${state.last_matched ?? 0} matched, ${state.last_new ?? 0} new`;
}

export function seededText(state) {
  if (!state?.seeded_at) return 'Not seeded yet. The first poll marks what it finds and queues nothing.';
  return `Seeded ${state.seeded_count ?? 0} keys on ${when(state.seeded_at)}.`;
}

// Turns the form's values into the config the server checks. Blank optional fields are left out so the server's defaults apply.
export function triggerBody(kind, values) {
  const config = { name: (values.name || '').trim() };
  if (values.inputs?.trim()) config.inputs = JSON.parse(values.inputs);
  if (values.project_id) config.project_id = values.project_id;
  const secrets = splitList(values.secrets);
  config.secrets = secrets;
  if (kind === 'schedule') {
    config.cron = (values.cron || '').trim();
    config.timezone = (values.timezone || '').trim() || 'UTC';
  } else {
    config.connection_id = values.connection_id;
    config.interval_minutes = Number.parseInt(values.interval_minutes, 10) || 5;
    config.seed_first = Boolean(values.seed_first);
    config.shadow = Boolean(values.shadow);
  }
  if (kind === 'jira_poll') config.jql = (values.jql || '').trim();
  if (kind === 'linear_poll') {
    config.team = (values.team || '').trim().toUpperCase();
    config.states = values.states || [];
    config.assignee = values.assignee_me ? 'me' : null;
    config.label = (values.label || '').trim() || null;
  }
  return { kind, workflow_id: values.workflow_id, enabled: Boolean(values.enabled), config };
}

export function outcomeText(row) {
  const label = OUTCOME_LABEL[row.outcome] || row.outcome;
  if (row.outcome === 'queued' && row.run_id) return `${label} as a run`;
  if (row.outcome === 'queued' && row.job_status) return `${label}, job ${row.job_status}`;
  return label;
}

export function verdictText(result) {
  if (result.verdict === 'seen') return `Already seen on ${when(result.seen.first_seen_at)}: ${outcomeText(result.seen).toLowerCase()}.`;
  return result.message || '';
}

export function connectionChoices(connections, kind) {
  return connections.filter((connection) => connection.kind === CONNECTION_KIND[kind])
    .map((connection) => [connection.id, `${connection.name}${connection.scope_kind === 'project' ? ' (project)' : ''}`]);
}

function textInput(id, value, attributes = {}) {
  const input = element('input', { type: 'text', id, spellcheck: 'false', autocapitalize: 'none', autocomplete: 'off', ...attributes });
  input.value = value ?? '';
  return input;
}

function checkbox(id, label, checked) {
  const input = element('input', { type: 'checkbox', id });
  input.checked = Boolean(checked);
  return { input, root: element('label', { for: id }, input, ` ${label}`) };
}

function labeled(input, text, note) {
  return element('label', { class: 'stacked', for: input.id }, text, input, note ? element('span', { class: 'note' }, note) : null);
}

function triggerForm(context, trigger, onDone, onCancel) {
  const prefix = `trigger-${trigger?.id || 'new'}`;
  const config = trigger?.config || {};
  const kindSelect = element('select', { id: `${prefix}-kind`, disabled: Boolean(trigger) }, Object.entries(KINDS).map(([value, label]) => element('option', { value }, label)));
  kindSelect.value = trigger?.kind || 'jira_poll';
  const nameInput = textInput(`${prefix}-name`, config.name, { maxlength: 100, placeholder: 'My sprint' });
  const workflowSelect = element('select', { id: `${prefix}-workflow`, required: true }, context.workflows.map((workflow) => element('option', { value: workflow.id }, `${workflow.name} (${workflow.scope_kind})`)));
  if (trigger) workflowSelect.value = trigger.workflow_id;
  const connectionSelect = element('select', { id: `${prefix}-connection` });
  const jqlInput = element('textarea', { id: `${prefix}-jql`, class: 'mono', rows: 3, spellcheck: 'false' });
  jqlInput.value = config.jql || context.defaultJql;
  const teamInput = textInput(`${prefix}-team`, config.team, { placeholder: 'ENG', maxlength: 10 });
  const states = LINEAR_STATES.map((state) => checkbox(`${prefix}-state-${state}`, state, (config.states || ['unstarted', 'started']).includes(state)));
  const assignee = checkbox(`${prefix}-assignee`, 'Only issues assigned to the connection’s user', config.assignee === 'me');
  const labelInput = textInput(`${prefix}-label`, config.label, { maxlength: 80 });
  const cronInput = textInput(`${prefix}-cron`, config.cron, { placeholder: '30 6 * * 1-5' });
  const zoneInput = textInput(`${prefix}-zone`, config.timezone || 'UTC', { placeholder: 'America/Denver' });
  const intervalInput = element('input', { type: 'number', id: `${prefix}-interval`, min: 1, max: 1440, step: 1 });
  intervalInput.value = String(config.interval_minutes ?? 5);
  const inputsInput = element('textarea', { id: `${prefix}-inputs`, class: 'mono', rows: 3, spellcheck: 'false' });
  inputsInput.value = JSON.stringify(config.inputs || (trigger?.kind === 'schedule' ? {} : { issue: '$.key' }), null, 2);
  inputsInput.addEventListener('blur', () => { inputsInput.value = prettyMapping(inputsInput.value); });
  const secretsInput = textInput(`${prefix}-secrets`, (config.secrets || []).join(', '));
  const enabled = checkbox(`${prefix}-enabled`, 'Enabled', trigger ? trigger.enabled : true);
  const seedFirst = checkbox(`${prefix}-seed`, 'Seed on the first poll: mark what it finds, queue nothing', config.seed_first ?? true);
  const shadow = checkbox(`${prefix}-shadow`, 'Shadow: mark keys, queue nothing', config.shadow);
  const jiraFields = element('div', { class: 'stacked-form' }, labeled(jqlInput, 'JQL', OPEN_SPRINT_NOTE));
  const linearFields = element('div', { class: 'stacked-form' }, labeled(teamInput, 'Linear team key'),
    element('fieldset', {}, element('legend', {}, 'States'), ...states.map((state) => state.root)), assignee.root, labeled(labelInput, 'Label (optional)'));
  const pollFields = element('div', { class: 'stacked-form' }, labeled(connectionSelect, 'Connection', 'The query runs as this connection’s account.'),
    labeled(intervalInput, 'Every how many minutes'), seedFirst.root, shadow.root);
  const scheduleFields = element('div', { class: 'stacked-form' }, labeled(cronInput, 'Cron (minute hour day month weekday)'), labeled(zoneInput, 'Time zone', 'Times are read on this zone’s clock, across daylight saving changes.'));
  const sync = () => {
    const kind = kindSelect.value;
    jiraFields.hidden = kind !== 'jira_poll';
    linearFields.hidden = kind !== 'linear_poll';
    pollFields.hidden = kind === 'schedule';
    scheduleFields.hidden = kind !== 'schedule';
    const current = connectionSelect.value || config.connection_id;
    clear(connectionSelect).append(...connectionChoices(context.connections, kind).map(([value, label]) => element('option', { value }, label)));
    if (current) connectionSelect.value = current;
  };
  kindSelect.addEventListener('change', sync);
  sync();
  const status = element('div', { role: 'status' });
  const saveButton = element('button', { type: 'submit', class: 'primary' }, trigger ? 'Save trigger' : 'Add trigger');
  const form = element('form', { class: 'stacked-form' }, element('h2', { class: 'inline' }, trigger ? 'Edit trigger' : 'Add a trigger'),
    labeled(kindSelect, 'Kind'), labeled(nameInput, 'Name'), labeled(workflowSelect, 'Workflow'),
    jiraFields, linearFields, pollFields, scheduleFields,
    labeled(inputsInput, 'Inputs (JSON)', 'Maps each workflow input to "$.key", "$.summary", "$.status", or a literal.'),
    labeled(secretsInput, 'Team secrets the run gets, comma-separated'), enabled.root, status,
    element('div', { class: 'wf-actions' }, saveButton, onCancel ? element('button', { type: 'button', onclick: onCancel }, 'Cancel') : null));
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const saved = await attempt(status, saveButton, () => {
      const body = triggerBody(kindSelect.value, {
        name: nameInput.value, workflow_id: workflowSelect.value, connection_id: connectionSelect.value, jql: jqlInput.value,
        team: teamInput.value, states: states.filter((state) => state.input.checked).map((state) => state.input.id.split('-state-')[1]),
        assignee_me: assignee.input.checked, label: labelInput.value, cron: cronInput.value, timezone: zoneInput.value,
        interval_minutes: intervalInput.value, inputs: inputsInput.value, secrets: secretsInput.value, enabled: enabled.input.checked,
        seed_first: seedFirst.input.checked, shadow: shadow.input.checked,
      });
      if (trigger) delete body.kind;
      return api(trigger ? triggerPath(trigger.id) : context.createPath, { method: 'POST', body });
    });
    if (saved?.trigger) onDone(saved.trigger);
  });
  return card(form);
}

function teamPanel(team, panel) {
  const listSlot = element('div', {}, loading());
  const formSlot = element('div');
  async function reload() {
    let page;
    try {
      page = await api(triggersPath(team.id));
    } catch (err) {
      clear(listSlot).append(errorBox(err));
      return;
    }
    const triggers = listOf(page, 'triggers');
    clear(listSlot).append(card(triggers.length ? table(['Name', 'Kind', 'Workflow', 'Runs', 'Enabled', 'Last run', 'Last result', 'Picked up (24 h)', 'Next run'], triggers.map((trigger) => [
      link(triggerHref(trigger.id), trigger.config?.name || KINDS[trigger.kind]), KINDS[trigger.kind] || trigger.kind, trigger.workflow_name || trigger.workflow_id,
      scheduleText(trigger), trigger.enabled ? 'Yes' : 'No', when(trigger.state?.last_run_at),
      trigger.state?.last_status ? element('span', {}, statusChip(trigger.state.last_status === 'ok' ? 'succeeded' : 'failed'), ' ', lastResultText(trigger.state)) : 'Never ran',
      String(trigger.picked_up_24h), when(trigger.state?.next_run_at),
    ])) : empty('This team has no triggers yet.')));
    const context = { workflows: page.workflows || [], connections: page.connections || [], defaultJql: page.default_jql, createPath: triggersPath(team.id) };
    clear(formSlot).append(context.workflows.length ? triggerForm(context, null, (created) => { location.hash = triggerHref(created.id); }, null) : card(empty('This team has no workflows to start.')));
  }
  panel.append(listSlot, formSlot);
  reload();
}

async function renderList(root, args) {
  root.append(pageHeader('Triggers'));
  root.append(element('p', { class: 'note' }, 'A trigger polls Jira or Linear, or follows a schedule, and queues a workflow. Each issue is queued once.'));
  let teams;
  try {
    teams = listOf(await api('/api/teams'), 'teams').filter((team) => canManageTeam(team.id));
  } catch (err) {
    root.append(errorBox(err));
    return;
  }
  if (!teams.length) {
    root.append(card(empty('You do not lead any team.')));
    return;
  }
  const scopes = teams.map((team) => ({ id: String(team.id), label: team.name }));
  const teamId = scopes.some((scope) => scope.id === args[0]) ? args[0] : scopes[0].id;
  const { tablist, panel } = tabs('#/triggers', scopes, teamId, { label: 'Team' });
  root.append(tablist, panel);
  teamPanel(teams.find((team) => String(team.id) === teamId), panel);
}

function whySection(trigger, onChanged) {
  const prefix = `why-${trigger.id}`;
  const keyInput = textInput(`${prefix}-key`, '', { placeholder: 'ENG-123', maxlength: 40 });
  const checkButton = element('button', { type: 'submit' }, 'Check');
  const status = element('div', { role: 'status' });
  const result = element('div');
  const form = element('form', { class: 'inline-form' }, element('label', { for: keyInput.id }, 'Why isn’t this key picked up?'), keyInput, checkButton);
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    clear(result);
    const found = await attempt(status, checkButton, () => api(`${triggerPath(trigger.id)}/why`, { method: 'POST', body: { key: keyInput.value } }));
    if (!found) return;
    result.append(element('p', {}, element('strong', {}, found.key), ': ', verdictText(found)));
    if (found.verdict === 'seen' && !trigger.config?.shadow) {
      const queueButton = element('button', { type: 'button' }, 'Queue it anyway');
      queueButton.addEventListener('click', async () => {
        if (!(await confirmDialog(`Queue ${found.key} again?`, 'The workflow runs for this issue again, even though it ran or was seeded before.', 'Queue'))) return;
        const queued = await attempt(status, queueButton, () => api(`${triggerPath(trigger.id)}/requeue`, { method: 'POST', body: { key: found.key } }));
        if (queued) onChanged(`Queued ${queued.key} again.`);
      });
      result.append(element('div', { class: 'wf-actions' }, queueButton));
    }
  });
  return card(element('h2', { class: 'inline' }, 'Check a key'), form, status, result);
}

function previewTable(items) {
  if (!items.length) return empty('The query returns nothing right now.');
  return table(['Key', 'Title', 'Seen'], items.map((item) => [item.key, item.title || '', item.seen ? `${OUTCOME_LABEL[item.seen.outcome]} ${when(item.seen.first_seen_at)}` : 'New: the next poll picks it up']));
}

function actionsSection(trigger, onChanged) {
  const status = element('div', { role: 'status' });
  const results = element('div');
  const post = (path, button) => attempt(status, button, () => api(`${triggerPath(trigger.id)}/${path}`, { method: 'POST', body: {} }));
  const runButton = element('button', { type: 'button', class: 'primary', disabled: !trigger.enabled }, trigger.kind === 'schedule' ? 'Run now' : 'Poll now');
  runButton.addEventListener('click', async () => {
    const done = await post('run', runButton);
    if (done?.poll) onChanged(done.poll.status === 'ok' ? `Ran: ${done.poll.matched ?? 0} matched, ${(done.poll.new_keys || []).length} new.` : `Failed: ${done.poll.error}`);
  });
  const buttons = [runButton];
  if (trigger.kind !== 'schedule') {
    const previewButton = element('button', { type: 'button' }, 'Preview');
    previewButton.addEventListener('click', async () => {
      const found = await post('preview', previewButton);
      if (found) clear(results).append(previewTable(found.items));
    });
    const seedButton = element('button', { type: 'button' }, 'Seed now');
    seedButton.addEventListener('click', async () => {
      if (!(await confirmDialog('Seed this trigger?', 'Every key the query returns now is marked seen, and none of them is queued.', 'Seed'))) return;
      const seeded = await post('seed', seedButton);
      if (seeded) onChanged(`Seeded ${seeded.seeded} of ${seeded.matched} keys.`);
    });
    const resetButton = element('button', { type: 'button' }, 'Forget seen keys');
    resetButton.addEventListener('click', async () => {
      if (!(await confirmDialog('Forget every seen key?', 'The next poll seeds again if seeding is on. If it is off, every key the query returns is queued.', 'Forget'))) return;
      const reset = await post('reset', resetButton);
      if (reset) onChanged(`Forgot ${reset.forgot} keys.`);
    });
    buttons.push(previewButton, seedButton, resetButton);
  }
  return element('div', {}, element('div', { class: 'wf-actions' }, ...buttons), status, results);
}

function pollsSection(polls) {
  return card(element('h2', { class: 'inline' }, 'Poll history'), polls.length ? table(['Started', 'Result', 'Matched', 'New keys', 'Took', 'Error'], polls.map((poll) => [
    when(poll.started_at), statusChip(poll.status === 'ok' ? 'succeeded' : poll.status === 'error' ? 'failed' : 'skipped'),
    poll.matched ?? '—', (poll.new_keys || []).join(', ') || '—', poll.duration_ms !== null && poll.duration_ms !== undefined ? `${poll.duration_ms} ms` : '—', poll.error || '',
  ])) : empty('No polls yet.'));
}

function seenSection(trigger) {
  const prefix = `seen-${trigger.id}`;
  const searchInput = textInput(`${prefix}-q`, '', { placeholder: 'ENG-12', maxlength: 40 });
  const outcomeSelect = element('select', { id: `${prefix}-outcome` }, element('option', { value: '' }, 'Any'), Object.entries(OUTCOME_LABEL).map(([value, label]) => element('option', { value }, label)));
  const listSlot = element('div', {}, loading());
  async function load() {
    const query = new URLSearchParams();
    if (searchInput.value.trim()) query.set('q', searchInput.value.trim());
    if (outcomeSelect.value) query.set('outcome', outcomeSelect.value);
    let rows;
    try {
      rows = listOf(await api(`${triggerPath(trigger.id)}/seen?${query}`), 'seen');
    } catch (err) {
      clear(listSlot).append(errorBox(err));
      return;
    }
    clear(listSlot).append(rows.length ? table(['Key', 'Outcome', 'First seen', 'Last seen'], rows.map((row) => [
      row.key, row.run_id ? link(hashFor('run', [row.run_id]), outcomeText(row)) : outcomeText(row), when(row.first_seen_at), when(row.last_seen_at),
    ])) : empty('No seen keys match.'));
  }
  const form = element('form', { class: 'inline-form' }, element('label', { for: searchInput.id }, 'Key'), searchInput,
    element('label', { for: outcomeSelect.id }, 'Outcome'), outcomeSelect, element('button', { type: 'submit' }, 'Search'));
  form.addEventListener('submit', (submitEvent) => {
    submitEvent.preventDefault();
    load();
  });
  load();
  return card(element('h2', { class: 'inline' }, 'Seen keys'), form, listSlot);
}

async function renderDetail(root, triggerId) {
  const crumb = element('p', { class: 'crumb' }, link(hashFor('triggers'), 'Triggers'));
  let detail;
  try {
    detail = await api(triggerPath(triggerId));
  } catch (err) {
    root.append(crumb, pageHeader('Trigger'), errorBox(err));
    return;
  }
  const { trigger, polls } = detail;
  const message = element('div', { role: 'status', 'data-message': '' });
  const rerender = async (note) => {
    const fresh = element('div');
    await renderDetail(fresh, triggerId);
    clear(root).append(...fresh.childNodes);
    if (note) root.querySelector('[data-message]')?.append(element('p', { class: 'note' }, note));
  };
  const deleteButton = element('button', { type: 'button' }, 'Delete trigger');
  deleteButton.addEventListener('click', async () => {
    if (!(await confirmDialog('Delete this trigger?', 'It stops polling, and its seen keys and history are deleted. Jobs it queued stay.', 'Delete'))) return;
    if (await attempt(message, deleteButton, () => api(`${triggerPath(trigger.id)}/delete`, { method: 'POST', body: {} }))) location.hash = hashFor('triggers', [trigger.team_id]);
  });
  const state = trigger.state || {};
  const context = { workflows: detail.workflows || [], connections: detail.connections || [], defaultJql: trigger.config?.jql };
  const formSlot = element('div');
  const editButton = element('button', { type: 'button' }, 'Edit');
  editButton.addEventListener('click', () => clear(formSlot).append(triggerForm(context, trigger, () => rerender('Saved. Seen keys stay, so an edited query never queues an old key again.'), () => clear(formSlot))));
  root.append(crumb, pageHeader(trigger.config?.name || KINDS[trigger.kind]), message,
    card(element('p', {}, `${KINDS[trigger.kind]} starting ${trigger.workflow_name || trigger.workflow_id}. ${scheduleText(trigger)}. `, trigger.enabled ? 'Enabled.' : 'Disabled.'),
      trigger.kind === 'jira_poll' ? element('pre', { class: 'setting-value' }, trigger.config.jql) : null,
      trigger.kind === 'jira_poll' ? element('p', { class: 'note' }, OPEN_SPRINT_NOTE) : null,
      trigger.connection_name ? element('p', {}, 'Connection ', element('span', { class: 'mono' }, trigger.connection_name), ' ', statusChip(trigger.connection_state)) : null,
      trigger.config?.shadow ? element('p', { class: 'note', role: 'alert' }, 'Shadow: keys are marked and nothing is queued.') : null,
      element('p', {}, `Last run ${when(state.last_run_at)}: ${lastResultText(state)}. Next run ${when(state.next_run_at)}. Picked up ${trigger.picked_up_24h} in the last 24 hours.`),
      trigger.kind === 'schedule' ? null : element('p', {}, seededText(state)),
      actionsSection(trigger, rerender), element('div', { class: 'wf-actions' }, editButton)),
    formSlot, trigger.kind === 'schedule' ? null : whySection(trigger, rerender), pollsSection(polls || []),
    trigger.kind === 'schedule' ? null : seenSection(trigger), element('div', { class: 'wf-actions' }, deleteButton));
}

export async function render(root, { name, args }) {
  if (name === 'trigger') await renderDetail(root, args[0]);
  else await renderList(root, args);
}
