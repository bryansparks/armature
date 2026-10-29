import { api, listOf } from '../api.js';
import { attempt, card, clear, confirmDialog, element, empty, errorBox, link, loading, pageHeader, statusChip, table, tabs, when } from '../dom.js';
import { hashFor } from '../router.js';
import { canManageTeam, isAdmin } from '../session.js';
import { teamConnectionsPath } from './connections.js';

export const SOURCES = { github: 'GitHub', slack: 'Slack', jira: 'Jira', linear: 'Linear', generic: 'Generic' };
const SETUP = {
  jira: 'In Jira, open Settings, System, WebHooks, and create a webhook. Paste the URL, paste the secret into Secret, and pick the events.',
  linear: 'In Linear, open Settings, API, Webhooks, and add a webhook with the URL. Linear makes the signing secret: paste it below under Secret.',
};

export const webhooksPath = (teamId) => `/api/teams/${encodeURIComponent(teamId)}/webhooks`;
export const webhookPath = (webhookId) => `/api/webhooks/${encodeURIComponent(webhookId)}`;
export const rulePath = (ruleId) => `/api/rules/${encodeURIComponent(ruleId)}`;
export const webhookHref = (webhookId) => hashFor('webhook', [webhookId]);

const SOURCE_KIND = { github: 'github_app', slack: 'slack', jira: 'jira', linear: 'linear' };

export function connectionOptions(connections, source) {
  return [['', 'None: a team secret'], ...connections.filter((connection) => connection.kind === SOURCE_KIND[source])
    .map((connection) => [connection.id, `Connection ${connection.name}`])];
}

const CONCURRENCY = { none: 'Run them all', queue: 'One at a time, in order', supersede: 'One at a time, newest wins' };
const DELIVERY_TONE = { accepted: 'ok', ignored: 'unknown', duplicate: 'unknown', rejected: 'bad', error: 'bad', held: 'warn' };

export function splitList(text) {
  return String(text || '').split(',').map((item) => item.trim()).filter(Boolean);
}

// Pretty-prints JSON for the reader. YAML, or anything that does not parse, goes to the server as typed.
export function prettyMapping(text) {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

export function mappingText(config) {
  return JSON.stringify({ conditions: config?.conditions || [], inputs: config?.inputs || {}, ...(config?.thread ? { thread: config.thread } : {}) }, null, 2);
}

export function ruleBody(values) {
  const config = {
    order: Number.parseInt(values.order, 10) || 0,
    stop: Boolean(values.stop),
    shadow: Boolean(values.shadow),
    dry: Boolean(values.dry),
    events: splitList(values.events),
    secrets: splitList(values.secrets),
  };
  if (values.project_from?.trim()) config.project_from = values.project_from.trim();
  if (values.event_from?.trim()) config.event_from = values.event_from.trim();
  // An imported rule keeps its import hash, so a later import can tell the rule was edited.
  if (values.import_hash) config.import_hash = values.import_hash;
  // A rule that only cancels keeps that flag through a save. The form has no control for it.
  if (values.cancel_only) config.cancel_only = true;
  const key = splitList(values.key);
  if (key.length) config.key = key;
  if (values.concurrency && values.concurrency !== 'none') config.concurrency = values.concurrency;
  if (String(values.debounce_seconds ?? '').trim()) config.debounce_seconds = Number.parseInt(values.debounce_seconds, 10) || 0;
  if (config.concurrency === 'supersede') {
    config.cancel_running = Boolean(values.cancel_running);
    const sticky = splitList(values.sticky);
    if (sticky.length) config.sticky = sticky;
  }
  return { workflow_id: values.workflow_id, enabled: Boolean(values.enabled), config, mapping: values.mapping ?? '' };
}

const SENDER_KINDS = [
  [/^github:repo:(.+)$/, 'repository'], [/^github:installation:(.+)$/, 'installation'], [/^jira:cloud:(.+)$/, 'Jira site'],
  [/^jira:(.+)$/, 'Jira site'], [/^linear:org:(.+)$/, 'Linear workspace'], [/^slack:team:(.+)$/, 'Slack workspace'],
];

export function senderLabel(sender) {
  if (!sender) return 'an unknown sender';
  for (const [pattern, kind] of SENDER_KINDS) {
    const match = pattern.exec(sender);
    if (match) return `${kind} ${match[1]}`;
  }
  return sender;
}

// Pending: approve or reject. Approved: replay what pinning admits, approve to add any other sender, or dismiss.
// Only a generic webhook can approve a delivery with no sender, since there is nothing to pin.
export function heldActions(webhook, delivery) {
  if (delivery.status !== 'held') return [];
  const pending = webhook.approval !== 'approved';
  if (!delivery.sender && webhook.source !== 'generic') return [pending ? 'reject' : 'dismiss'];
  if (pending) return ['approve', 'reject'];
  const admitted = !webhook.pinned_senders || webhook.pinned_senders.includes(delivery.sender);
  return [admitted ? 'replay' : 'approve', 'dismiss'];
}

export function conditionsSummary(conditions) {
  if (!conditions?.length) return 'Always';
  return conditions.map((condition) => `${condition.path} ${condition.op}${'value' in condition ? ` ${JSON.stringify(condition.value)}` : ''}${condition.ignore_case ? ' (ignoring case)' : ''}`).join('; ');
}

function badge(text, tone) {
  return element('span', { class: `chip${tone ? ` ${tone}` : ''}` }, text);
}

function deliveryChip(status) {
  return element('span', { class: `state state-${DELIVERY_TONE[status] || 'unknown'}` }, status);
}

function copyField(id, label, value) {
  const input = element('input', { type: 'text', id, readonly: true, class: 'mono', spellcheck: 'false', autocomplete: 'off' });
  input.value = value;
  const copyStatus = element('span', { role: 'status', class: 'muted' });
  const copyButton = element('button', { type: 'button' }, 'Copy');
  copyButton.addEventListener('click', async () => {
    clear(copyStatus);
    try {
      await navigator.clipboard.writeText(input.value);
      copyStatus.append('Copied.');
    } catch {
      input.select();
      copyStatus.append('Select the text and copy it.');
    }
  });
  return { input, root: element('div', { class: 'stacked-form' }, element('label', { class: 'stacked', for: id }, label, element('div', { class: 'wf-actions' }, input, copyButton, copyStatus))) };
}

// The secret lives only in this element. Nothing writes it to storage or the address.
function revealBox(name, secret) {
  const { input, root } = copyField(`webhook-secret-${Date.now()}`, `Secret for ${name}`, secret);
  const dismissButton = element('button', { type: 'button', class: 'link-button' }, 'Done');
  const box = element('div', { class: 'stacked-form' }, element('p', { class: 'note', role: 'alert' }, `Copy the secret for ${name} now and paste it into the sender. It is not shown again.`), root, dismissButton);
  dismissButton.addEventListener('click', () => {
    input.value = '';
    box.remove();
  });
  return box;
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

function genericFields(prefix, webhook, sourceSelect) {
  const authSelect = element('select', { id: `${prefix}-auth` }, element('option', { value: 'hmac' }, 'HMAC signature'), element('option', { value: 'bearer' }, 'Bearer token'));
  authSelect.value = webhook?.generic_auth || 'hmac';
  const signatureInput = textInput(`${prefix}-signature-header`, webhook?.generic_signature_header, { placeholder: 'X-Signature-256' });
  const deliveryInput = textInput(`${prefix}-delivery-header`, webhook?.generic_delivery_header, { placeholder: 'X-Delivery-Id' });
  const signatureLabel = element('label', { class: 'stacked', for: signatureInput.id }, 'Signature header', signatureInput);
  const root = element('div', { class: 'stacked-form' },
    element('label', { class: 'stacked', for: authSelect.id }, 'Authentication', authSelect), signatureLabel,
    element('label', { class: 'stacked', for: deliveryInput.id }, 'Delivery id header (optional, for replay protection)', deliveryInput));
  const sync = () => {
    root.hidden = sourceSelect.value !== 'generic';
    signatureLabel.hidden = authSelect.value !== 'hmac';
  };
  sourceSelect.addEventListener('change', sync);
  authSelect.addEventListener('change', sync);
  sync();
  return {
    root,
    values: () => (sourceSelect.value === 'generic'
      ? { generic_auth: authSelect.value, generic_signature_header: signatureInput.value.trim() || null, generic_delivery_header: deliveryInput.value.trim() || null }
      : {}),
  };
}

function createForm(team, onCreated) {
  const prefix = `webhook-new-${team.id}`;
  const nameInput = textInput(`${prefix}-name`, '', { required: true, maxlength: 100, placeholder: 'github-org' });
  const sourceSelect = element('select', { id: `${prefix}-source` }, Object.entries(SOURCES).map(([value, label]) => element('option', { value }, label)));
  const generic = genericFields(prefix, null, sourceSelect);
  const setup = element('p', { class: 'note' });
  const connectionSelect = element('select', { id: `${prefix}-connection` });
  let connections = [];
  const syncSetup = () => {
    clear(setup).append(SETUP[sourceSelect.value] || '');
    clear(connectionSelect).append(...connectionOptions(connections, sourceSelect.value).map(([value, label]) => element('option', { value }, label)));
  };
  sourceSelect.addEventListener('change', syncSetup);
  syncSetup();
  api(teamConnectionsPath(team.id)).then((payload) => {
    connections = listOf(payload, 'connections');
    syncSetup();
  }).catch(() => {});
  const status = element('div', { role: 'status' });
  const createButton = element('button', { type: 'submit', class: 'primary' }, 'Create webhook');
  const form = element('form', { class: 'stacked-form' },
    element('h2', { class: 'inline' }, 'Add a webhook'),
    element('label', { class: 'stacked', for: nameInput.id }, 'Name', nameInput),
    element('label', { class: 'stacked', for: sourceSelect.id }, 'Source', sourceSelect), setup,
    element('label', { class: 'stacked', for: connectionSelect.id }, 'Verify deliveries with', connectionSelect),
    element('p', { class: 'note' }, 'A new webhook holds its deliveries until you approve one.'),
    generic.root, status, element('div', { class: 'wf-actions' }, createButton));
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const body = { name: nameInput.value.trim(), source: sourceSelect.value, ...generic.values(), ...(connectionSelect.value ? { connection_id: connectionSelect.value } : {}) };
    const created = await attempt(status, createButton, async () => {
      const made = await api(webhooksPath(team.id), { method: 'POST', body });
      if (made.webhook.connection_id) return { webhook: made.webhook, secret: null };
      const secret = await api(`${webhookPath(made.webhook.id)}/secret`, { method: 'POST', body: {} });
      return { webhook: made.webhook, secret: secret.secret };
    });
    if (!created?.webhook) return;
    nameInput.value = '';
    onCreated(created);
  });
  return card(form);
}

function teamPanel(team, panel) {
  const status = element('div', { role: 'status' });
  const reveal = element('div');
  const listSlot = element('div', {}, loading());
  async function reload() {
    let hooks;
    try {
      hooks = listOf(await api(webhooksPath(team.id)), 'webhooks');
    } catch (err) {
      clear(listSlot).append(errorBox(err));
      return;
    }
    clear(listSlot).append(card(hooks.length ? table(['Name', 'Source', 'Enabled', 'Last delivery'], hooks.map((webhook) => [
      link(webhookHref(webhook.id), webhook.name), SOURCES[webhook.source] || webhook.source,
      webhook.approval === 'pending' ? element('span', { class: 'state state-warn' }, 'Pending approval') : webhook.enabled ? 'Yes' : 'No',
      webhook.last_delivery ? element('span', {}, when(webhook.last_delivery.received_at), ' ', deliveryChip(webhook.last_delivery.status)) : 'Never',
    ])) : empty('This team has no webhooks yet.')));
  }
  panel.append(status, reveal, listSlot, createForm(team, ({ webhook, secret }) => {
    clear(reveal).append(card(element('p', {}, 'Created ', link(webhookHref(webhook.id), webhook.name), '. Point the sender at this URL:'),
      copyField(`webhook-url-${webhook.id}`, 'Webhook URL', webhook.url).root, secret ? revealBox(webhook.name, secret) : null));
    reload();
  }));
  reload();
}

async function renderList(root, args) {
  root.append(pageHeader('Webhooks'));
  root.append(element('p', { class: 'note' }, 'Each webhook has its own URL. Rules on it decide which workflows a delivery queues.'));
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
  const { tablist, panel } = tabs('#/webhooks', scopes, teamId, { label: 'Team' });
  root.append(tablist, panel);
  teamPanel(teams.find((team) => String(team.id) === teamId), panel);
}

function settingsForm(webhook, onSaved) {
  const prefix = `webhook-${webhook.id}`;
  const enabled = checkbox(`${prefix}-enabled`, 'Enabled', webhook.enabled);
  const forwardInput = textInput(`${prefix}-forward`, webhook.forward_url, { placeholder: 'An http or https URL', inputmode: 'url' });
  const sourceSelect = element('select', { hidden: true }, element('option', { value: webhook.source }));
  const generic = genericFields(prefix, webhook, sourceSelect);
  const status = element('div', { role: 'status' });
  const saveButton = element('button', { type: 'submit', class: 'primary' }, 'Save settings');
  // Only an admin may set where deliveries are forwarded. A team lead sees it read-only.
  forwardInput.readOnly = !isAdmin();
  const form = element('form', { class: 'stacked-form' }, element('h2', { class: 'inline' }, 'Settings'), enabled.root,
    element('label', { class: 'stacked', for: forwardInput.id }, 'Forward URL (optional)', forwardInput,
      element('span', { class: 'note' }, isAdmin() ? 'Each verified delivery is also posted here, unchanged, after it is answered.' : 'Each verified delivery is also posted here. Only an admin can change it.')),
    generic.root, status, element('div', { class: 'wf-actions' }, saveButton));
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const body = { enabled: enabled.input.checked, ...(isAdmin() ? { forward_url: forwardInput.value.trim() || null } : {}), ...generic.values() };
    const saved = await attempt(status, saveButton, () => api(webhookPath(webhook.id), { method: 'POST', body }));
    if (saved) onSaved('Saved.');
  });
  return card(form);
}

function secretSection(webhook, onSaved) {
  if (webhook.connection_id) {
    return card(element('h2', { class: 'inline' }, 'Secret'),
      element('p', {}, webhook.secret_set ? 'Set' : element('span', { class: 'state state-warn' }, 'Not set'), ' on connection ', element('span', { class: 'mono' }, webhook.connection_name),
        '. Change it on the team’s Connections tab.'),
      webhook.source === 'slack' ? element('p', { class: 'note' }, 'Deliveries are refused until the connection is verified, so the server knows which workspace to accept.') : null);
  }
  const status = element('div', { role: 'status' });
  const reveal = element('div');
  const generateButton = element('button', { type: 'button' }, webhook.secret_set ? 'Rotate secret' : 'Generate secret');
  generateButton.addEventListener('click', async () => {
    if (webhook.secret_set && !(await confirmDialog('Rotate the secret?', 'Deliveries signed with the old secret are rejected until the sender has the new one.', 'Rotate'))) return;
    const result = await attempt(status, generateButton, () => api(`${webhookPath(webhook.id)}/secret`, { method: 'POST', body: {} }));
    if (result?.secret) clear(reveal).append(revealBox(webhook.name, result.secret));
  });
  const pasteInput = element('input', { type: 'password', id: `webhook-${webhook.id}-paste`, autocomplete: 'new-password', spellcheck: 'false', required: true });
  const pasteButton = element('button', { type: 'submit' }, 'Save pasted secret');
  const pasteForm = element('form', { class: 'inline-form' }, element('label', { for: pasteInput.id }, 'Or paste one'), pasteInput, pasteButton);
  pasteForm.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const saved = await attempt(status, pasteButton, () => api(`${webhookPath(webhook.id)}/secret`, { method: 'POST', body: { value: pasteInput.value } }));
    pasteInput.value = '';
    if (saved) onSaved('Secret saved.');
  });
  return card(element('h2', { class: 'inline' }, 'Secret'),
    element('p', {}, webhook.secret_set ? 'Set' : element('span', { class: 'state state-warn' }, 'Not set'), ' in team secret ', element('span', { class: 'mono' }, webhook.secret_name), '. Its value never shows here.'),
    element('div', { class: 'wf-actions' }, generateButton), pasteForm, reveal, status);
}

function ruleForm(webhook, rule, workflows, onDone, onCancel) {
  const prefix = `rule-${rule?.id || 'new'}`;
  const config = rule?.config || {};
  const workflowSelect = element('select', { id: `${prefix}-workflow`, required: true }, workflows.map((workflow) => element('option', { value: workflow.id }, `${workflow.name} (${workflow.scope_kind})`)));
  if (rule) workflowSelect.value = rule.workflow_id;
  const orderInput = element('input', { type: 'number', id: `${prefix}-order`, step: 1 });
  orderInput.value = String(config.order ?? 0);
  const eventsInput = textInput(`${prefix}-events`, (config.events || []).join(', '), { placeholder: webhook.source === 'slack' ? 'app_mention' : 'pull_request, push' });
  const eventFromInput = textInput(`${prefix}-event-from`, config.event_from, { placeholder: 'kind' });
  const projectInput = textInput(`${prefix}-project`, config.project_from, { placeholder: 'repository.full_name' });
  const secretsInput = textInput(`${prefix}-secrets`, (config.secrets || []).join(', '));
  const keyInput = textInput(`${prefix}-key`, (config.key || []).join(', '), { placeholder: 'repository.full_name, number' });
  const concurrencySelect = element('select', { id: `${prefix}-concurrency` }, Object.entries(CONCURRENCY).map(([value, label]) => element('option', { value }, label)));
  concurrencySelect.value = config.concurrency || 'none';
  const debounceInput = element('input', { type: 'number', id: `${prefix}-debounce`, min: 0, max: 3600, step: 1, placeholder: '600 for pull_request with a key, else 0' });
  debounceInput.value = config.debounce_seconds === undefined ? '' : String(config.debounce_seconds);
  const cancelRunning = checkbox(`${prefix}-cancel-running`, 'Also stop the key’s running job', config.cancel_running);
  const stickyInput = textInput(`${prefix}-sticky`, (config.sticky || []).join(', '), { placeholder: 'full' });
  const enabled = checkbox(`${prefix}-enabled`, 'Enabled', rule ? rule.enabled : true);
  const stop = checkbox(`${prefix}-stop`, 'Stop after this rule when it matches', config.stop);
  const shadow = checkbox(`${prefix}-shadow`, 'Shadow: record the decision, queue nothing', config.shadow);
  const dry = checkbox(`${prefix}-dry`, 'Dry: run, but record each tool call instead of sending it', config.dry);
  const mappingInput = element('textarea', { id: `${prefix}-mapping`, class: 'mono', rows: 12, spellcheck: 'false', 'aria-describedby': `${prefix}-mapping-help` });
  mappingInput.value = mappingText(config);
  mappingInput.addEventListener('blur', () => { mappingInput.value = prettyMapping(mappingInput.value); });
  const status = element('div', { role: 'status' });
  const saveButton = element('button', { type: 'submit', class: 'primary' }, rule ? 'Save rule' : 'Add rule');
  const form = element('form', { class: 'stacked-form' },
    element('h3', {}, rule ? 'Edit rule' : 'Add a rule'),
    element('label', { class: 'stacked', for: workflowSelect.id }, 'Workflow', workflowSelect),
    element('label', { class: 'stacked', for: orderInput.id }, 'Order (lower runs first)', orderInput),
    element('label', { class: 'stacked', for: eventsInput.id }, 'Events, comma-separated (empty means any)', eventsInput),
    webhook.source === 'generic' ? element('label', { class: 'stacked', for: eventFromInput.id }, 'Event path in the payload (optional)', eventFromInput) : null,
    element('label', { class: 'stacked', for: projectInput.id }, 'Project from payload path (optional, matched against project repositories)', projectInput),
    element('label', { class: 'stacked', for: secretsInput.id }, 'Team secrets the run gets, comma-separated', secretsInput),
    element('label', { class: 'stacked', for: keyInput.id }, 'Key: payload paths, comma-separated (optional)', keyInput,
      element('span', { class: 'note' }, 'Jobs with the same key run one at a time, such as one pull request.')),
    element('label', { class: 'stacked', for: concurrencySelect.id }, 'Jobs of one key', concurrencySelect),
    element('label', { class: 'stacked', for: debounceInput.id }, 'Quiet period in seconds before a job starts', debounceInput),
    cancelRunning.root, element('label', { class: 'stacked', for: stickyInput.id }, 'Inputs a newer job keeps from the one it replaces, comma-separated', stickyInput),
    enabled.root, stop.root, shadow.root, dry.root,
    element('label', { class: 'stacked', for: mappingInput.id }, 'Conditions and inputs (YAML or JSON)', mappingInput),
    element('p', { class: 'note', id: `${prefix}-mapping-help` }, 'conditions is a list of {path, op, value}. op is eq, ne, in, not_in, starts_with, contains, exists, or missing. starts_with and contains take text and an optional ignore_case: true. inputs maps each workflow input to "$.payload.path", "$" for the whole payload, or a literal. An optional thread names the thread its runs join, such as "slack:{{ event.channel }}:{{ event.thread_ts or event.ts }}".'),
    status, element('div', { class: 'wf-actions' }, saveButton, element('button', { type: 'button', onclick: onCancel }, 'Cancel')));
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const body = ruleBody({
      workflow_id: workflowSelect.value, order: orderInput.value, events: eventsInput.value, event_from: webhook.source === 'generic' ? eventFromInput.value : '',
      project_from: projectInput.value, secrets: secretsInput.value, enabled: enabled.input.checked, stop: stop.input.checked, shadow: shadow.input.checked, dry: dry.input.checked, import_hash: config.import_hash, cancel_only: config.cancel_only, mapping: mappingInput.value,
      key: keyInput.value, concurrency: concurrencySelect.value, debounce_seconds: debounceInput.value, cancel_running: cancelRunning.input.checked, sticky: stickyInput.value,
    });
    const path = rule ? rulePath(rule.id) : `${webhookPath(webhook.id)}/rules`;
    if (await attempt(status, saveButton, () => api(path, { method: 'POST', body }))) onDone(rule ? 'Rule saved.' : 'Rule added.');
  });
  requestAnimationFrame(() => workflowSelect.focus());
  return card(form);
}

function rulesSection(webhook, rules, workflows, onChanged) {
  const formSlot = element('div');
  const status = element('div', { role: 'status' });
  const openForm = (rule) => clear(formSlot).append(ruleForm(webhook, rule, workflows, onChanged, () => clear(formSlot)));
  const rows = rules.map((rule) => {
    const config = rule.config || {};
    const editButton = element('button', { type: 'button', class: 'link-button', 'aria-label': `Edit rule for ${rule.workflow_name}` }, 'Edit');
    editButton.addEventListener('click', () => openForm(rule));
    const deleteButton = element('button', { type: 'button', class: 'link-button', 'aria-label': `Delete rule for ${rule.workflow_name}` }, 'Delete');
    deleteButton.addEventListener('click', async () => {
      if (!(await confirmDialog('Delete this rule?', 'Deliveries stop queuing this workflow. Jobs it already queued stay.', 'Delete'))) return;
      if (await attempt(status, deleteButton, () => api(`${rulePath(rule.id)}/delete`, { method: 'POST', body: {} }))) onChanged('Rule deleted.');
    });
    return [String(config.order ?? 0), rule.workflow_name || rule.workflow_id, (config.events || []).join(', ') || 'Any',
      conditionsSummary(config.conditions),
      element('span', { class: 'chips' }, rule.enabled ? null : badge('Disabled'), config.shadow ? badge('Shadow', 'warn') : null, config.dry ? badge('Dry', 'warn') : null, config.cancel_only ? badge('Cancels only') : null, config.stop ? badge('Stop') : null,
        config.concurrency ? badge(CONCURRENCY[config.concurrency]) : null, config.debounce_seconds ? badge(`Quiet ${config.debounce_seconds}s`) : null),
      element('span', { class: 'row-actions' }, editButton, deleteButton)];
  });
  const addButton = element('button', { type: 'button', onclick: () => openForm(null) }, 'Add rule');
  return card(element('h2', { class: 'inline' }, 'Rules'), status,
    rules.length ? table(['Order', 'Workflow', 'Events', 'Conditions', 'Flags', ''], rows) : empty('No rules yet. Deliveries are recorded and queue nothing.'),
    workflows.length ? element('div', { class: 'wf-actions' }, addButton) : element('p', { class: 'note' }, 'This team has no workflows to queue.'), formSlot);
}

function matchedCell(matched) {
  if (!matched?.length) return '—';
  return element('div', {}, matched.map((entry) => element('div', {}, entry.workflow_name || entry.workflow_id, ' ',
    entry.shadow ? badge('Shadow', 'warn') : entry.run_id ? link(hashFor('run', [entry.run_id]), 'run') : entry.job_status ? statusChip(entry.job_status) : null)));
}

function decisionsTable(decisions, rules) {
  const names = Object.fromEntries(rules.map((rule) => [rule.id, rule.workflow_name]));
  if (!decisions.length) return empty('No rules to test.');
  return table(['Rule', 'Matched', 'Reason', 'Inputs'], decisions.map((decision) => [
    names[decision.trigger_id] || decision.trigger_id, decision.matched ? element('span', { class: 'state state-ok' }, 'Yes') : 'No', decision.reason,
    element('pre', { class: 'setting-value' }, JSON.stringify(decision.inputs, null, 2)),
  ]));
}

function pinningSection(webhook, onChanged) {
  if (webhook.source === 'generic' || webhook.approval !== 'approved') return null;
  const status = element('div', { role: 'status' });
  const post = (path, body, button) => attempt(status, button, () => api(`${webhookPath(webhook.id)}/pinning${path}`, { method: 'POST', body }));
  if (!webhook.pinned_senders) {
    const onButton = element('button', { type: 'button' }, 'Turn on sender pinning');
    onButton.addEventListener('click', async () => {
      const preview = await attempt(status, onButton, () => api(`${webhookPath(webhook.id)}/pinning`));
      if (!preview) return;
      if (!preview.senders.length) {
        clear(status).append(element('p', { class: 'note' }, 'No delivery has a sender to pin yet.'));
        return;
      }
      const listed = preview.senders.map(senderLabel).join(', ');
      if (!(await confirmDialog('Turn on sender pinning?', `Only these senders are let through: ${listed}. Deliveries from anyone else are held.`, 'Turn on'))) return;
      if (await post('', { senders: preview.senders }, onButton)) onChanged('Sender pinning is on.');
    });
    return element('div', {}, element('p', {}, 'Sender pinning: off. Any delivery with a valid signature is let through.'),
      element('div', { class: 'wf-actions' }, onButton), status);
  }
  const offButton = element('button', { type: 'button' }, 'Turn off sender pinning');
  offButton.addEventListener('click', async () => {
    if (!(await confirmDialog('Turn off sender pinning?', 'Any delivery with a valid signature is let through again.', 'Turn off'))) return;
    if (await post('/off', {}, offButton)) onChanged('Sender pinning is off.');
  });
  const rows = webhook.pinned_senders.map((sender) => {
    const removeButton = element('button', { type: 'button', class: 'link-button', disabled: webhook.pinned_senders.length === 1, 'aria-label': `Remove ${senderLabel(sender)}` }, 'Remove');
    removeButton.addEventListener('click', async () => {
      if (!(await confirmDialog(`Remove ${senderLabel(sender)}?`, 'Its deliveries are held until you approve one again.', 'Remove'))) return;
      if (await post('/remove', { sender }, removeButton)) onChanged('Sender removed.');
    });
    return [senderLabel(sender), element('span', { class: 'mono' }, sender), removeButton];
  });
  return element('div', {}, element('p', {}, 'Sender pinning: on. Only these senders are let through.'),
    table(['Sender', 'Id', ''], rows), element('div', { class: 'wf-actions' }, offButton), status);
}

function heldSection(webhook, delivery, onChanged) {
  const actions = heldActions(webhook, delivery);
  if (!actions.length) return null;
  const status = element('div', { role: 'status' });
  const sender = senderLabel(delivery.sender);
  const release = (action, button) => attempt(status, button, () => api(`${webhookPath(webhook.id)}/deliveries/${encodeURIComponent(delivery.id)}/${action}`, { method: 'POST', body: {} }));
  const done = (released) => {
    if (!released) return;
    onChanged(released.replayed ? `Replayed: ${released.status}, ${released.jobs} job(s) queued.` : 'Approved. No payload was stored, so nothing was replayed.');
  };
  const buttons = actions.map((action) => {
    const button = element('button', { type: 'button', class: action === 'approve' || action === 'replay' ? 'primary' : null }, { approve: 'Approve', replay: 'Replay', reject: 'Reject', dismiss: 'Dismiss' }[action]);
    button.addEventListener('click', async () => {
      if (action === 'approve') {
        if (!(await confirmDialog(`Approve ${sender}?`, 'Deliveries from this sender are let through from now on, and this delivery runs through the rules now.', 'Approve'))) return;
        done(await release('approve', button));
      } else if (action === 'replay') {
        done(await release('replay', button));
      } else if (action === 'dismiss') {
        if (await release('dismiss', button)) onChanged('Dismissed. The delivery queued nothing.');
      } else {
        if (!(await confirmDialog(`Reject and delete ${webhook.name}?`, 'Its URL stops working, and its rules, deliveries, and team secret are deleted.', 'Reject'))) return;
        if (await attempt(status, button, () => api(`${webhookPath(webhook.id)}/reject`, { method: 'POST', body: {} }))) location.hash = hashFor('webhooks', [webhook.team_id]);
      }
    });
    return button;
  });
  return element('div', { class: 'stacked-form' }, element('h4', {}, 'Held'),
    element('p', {}, 'Sent by ', sender, delivery.sender ? element('span', { class: 'mono' }, ` (${delivery.sender})`) : null, '. Approve only a sender you recognize.'),
    element('div', { class: 'wf-actions' }, ...buttons), status);
}

async function deliveryDetail(webhook, deliveryId, rules, slot, onChanged) {
  clear(slot).append(loading());
  let delivery;
  try {
    delivery = (await api(`${webhookPath(webhook.id)}/deliveries/${encodeURIComponent(deliveryId)}`)).delivery;
  } catch (err) {
    clear(slot).append(errorBox(err));
    return;
  }
  const results = element('div', { role: 'status' });
  const testButton = element('button', { type: 'button', disabled: delivery.payload === null }, 'Test rules');
  testButton.addEventListener('click', async () => {
    const tested = await attempt(results, testButton, () => api(`${webhookPath(webhook.id)}/deliveries/${encodeURIComponent(deliveryId)}/test`, { method: 'POST', body: {} }));
    if (tested?.decisions) clear(results).append(decisionsTable(tested.decisions, rules));
  });
  clear(slot).append(card(element('h3', {}, `Delivery ${when(delivery.received_at)}`),
    element('p', {}, deliveryChip(delivery.status), ' ', delivery.reason || ''),
    heldSection(webhook, delivery, onChanged),
    element('h4', {}, 'Headers'), element('pre', { class: 'setting-value' }, JSON.stringify(delivery.headers, null, 2)),
    element('h4', {}, 'Payload'), delivery.payload === null ? empty('No payload stored. Rejected deliveries and bodies over 1 MB keep none.') : element('pre', { class: 'payload' }, prettyMapping(delivery.payload)),
    element('div', { class: 'wf-actions' }, testButton), results));
}

function deliveriesSection(webhook, rules, onChanged) {
  const listSlot = element('div', {}, loading());
  const detailSlot = element('div');
  const moreSlot = element('div');
  let shown = [];
  const draw = () => {
    clear(listSlot).append(shown.length ? table(['Received', 'Event', 'Status', 'Reason', 'Matched', 'Forward', ''], shown.map((delivery) => {
      const viewButton = element('button', { type: 'button', class: 'link-button', 'aria-label': `View delivery from ${when(delivery.received_at)}` }, delivery.status === 'held' ? 'Review' : 'View');
      viewButton.addEventListener('click', () => deliveryDetail(webhook, delivery.id, rules, detailSlot, onChanged));
      return [when(delivery.received_at), delivery.event || '—', deliveryChip(delivery.status), delivery.reason || '', matchedCell(delivery.matched), delivery.forward_status || '—', viewButton];
    })) : empty('No deliveries in the last two weeks.'));
  };
  async function load(before) {
    let page;
    try {
      page = await api(`${webhookPath(webhook.id)}/deliveries${before ? `?before=${encodeURIComponent(before)}` : ''}`);
    } catch (err) {
      clear(listSlot).append(errorBox(err));
      return;
    }
    shown = [...shown, ...listOf(page, 'deliveries')];
    draw();
    clear(moreSlot);
    if (page.next_before) moreSlot.append(element('button', { type: 'button', onclick: () => load(page.next_before) }, 'Older deliveries'));
  }
  load(null);
  return card(element('h2', { class: 'inline' }, 'Deliveries'), listSlot, moreSlot, detailSlot);
}

async function renderDetail(root, webhookId) {
  const crumb = element('p', { class: 'crumb' }, link(hashFor('webhooks'), 'Webhooks'));
  let detail;
  try {
    detail = await api(webhookPath(webhookId));
  } catch (err) {
    root.append(crumb, pageHeader('Webhook'), errorBox(err));
    return;
  }
  const { webhook, rules, workflows } = detail;
  const message = element('div', { role: 'status' });
  const rerender = async (note) => {
    const fresh = element('div');
    await renderDetail(fresh, webhookId);
    clear(root).append(...fresh.childNodes);
    if (note) root.querySelector('[data-message]')?.append(element('p', { class: 'note' }, note));
  };
  message.setAttribute('data-message', '');
  const deleteButton = element('button', { type: 'button' }, 'Delete webhook');
  deleteButton.addEventListener('click', async () => {
    if (!(await confirmDialog(`Delete ${webhook.name}?`, 'Its URL stops working, and its rules and deliveries are deleted. Jobs it queued stay.', 'Delete'))) return;
    if (await attempt(message, deleteButton, () => api(`${webhookPath(webhook.id)}/delete`, { method: 'POST', body: {} }))) location.hash = hashFor('webhooks', [webhook.team_id]);
  });
  const pending = webhook.approval === 'pending'
    ? element('p', { class: 'note', role: 'alert' }, 'Pending approval. Verified deliveries are held and queue nothing. Review a held delivery below and approve it to start queuing jobs from its sender.')
    : null;
  root.append(crumb, pageHeader(webhook.name), message, pending,
    card(element('p', {}, `${SOURCES[webhook.source] || webhook.source} webhook. `, webhook.enabled ? 'Enabled.' : 'Disabled: deliveries get 404.'),
      pinningSection(webhook, rerender), SETUP[webhook.source] ? element('p', { class: 'note' }, SETUP[webhook.source]) : null,
      copyField(`webhook-url-${webhook.id}`, 'URL for the sender', webhook.url).root),
    secretSection(webhook, rerender), settingsForm(webhook, rerender), rulesSection(webhook, rules, workflows, rerender),
    deliveriesSection(webhook, rules, rerender), element('div', { class: 'wf-actions' }, deleteButton));
}

export async function render(root, { name, args }) {
  if (name === 'webhook') await renderDetail(root, args[0]);
  else await renderList(root, args);
}
