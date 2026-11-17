import { api } from '../api.js';
import { attempt, clear, confirmDialog, element, empty, table } from '../dom.js';
import { isAdmin } from '../session.js';

// Fields marked admin show only to an admin, since only an admin may set them.
export const PROVIDER_KINDS = {
  bedrock: { label: 'Bedrock', fields: [{ name: 'region', label: 'Region', placeholder: 'us-east-1' }] },
  anthropic: { label: 'Anthropic API', fields: [
    { name: 'api_base', label: 'Gateway base URL', placeholder: 'Leave empty for the Anthropic API', admin: true },
    { name: 'auth', label: 'Gateway auth', admin: true, options: [['', 'x-api-key'], ['bearer', 'Authorization: Bearer']] },
    { name: 'prices', label: 'Prices per alias, USD per million tokens (JSON)', placeholder: '{"review": {"input": 1, "output": 4}}', admin: true, json: true },
  ] },
  'claude-subscription': { label: 'Claude subscription', fields: [] },
  'openai-compatible': { label: 'OpenAI-compatible', fields: [{ name: 'api_base', label: 'API base URL' }] },
  ollama: { label: 'Ollama', fields: [{ name: 'api_base', label: 'API base URL' }] },
};

export const kindLabel = (kind) => PROVIDER_KINDS[kind]?.label || kind;

export function aliasRows(aliases) {
  return Object.entries(aliases || {}).map(([alias, model]) => ({ alias, model: String(model ?? '') }));
}

export function aliasesFrom(rows) {
  const aliases = {};
  for (const row of rows) {
    const alias = row.alias.trim();
    const model = row.model.trim();
    if (!alias && !model) continue;
    if (!alias) return { error: `The model ${model} needs an alias.` };
    if (!model) return { error: `The alias ${alias} needs a model.` };
    if (alias in aliases) return { error: `The alias ${alias} is listed twice.` };
    aliases[alias] = model;
  }
  return { aliases };
}

export const kindFields = (kind, admin) => (PROVIDER_KINDS[kind]?.fields || []).filter((field) => admin || !field.admin);

// Keeps config keys this form does not show for the kind, and takes the shown ones from the form.
export function providerBody(kind, values, rows, credential, previousConfig = {}, admin = false) {
  if (!PROVIDER_KINDS[kind]) return { error: 'Pick a provider kind.' };
  const { aliases, error } = aliasesFrom(rows);
  if (error) return { error };
  const fields = kindFields(kind, admin);
  const shown = new Set(fields.map((field) => field.name));
  const config = Object.fromEntries(Object.entries(previousConfig || {}).filter(([key]) => !shown.has(key) && key !== 'credential_secret'));
  for (const field of fields) {
    const raw = values[field.name];
    if (field.json && raw && typeof raw === 'object') {
      config[field.name] = raw;
      continue;
    }
    const value = String(raw ?? '').trim();
    if (!value) continue;
    if (!field.json) {
      config[field.name] = value;
      continue;
    }
    try {
      config[field.name] = JSON.parse(value);
    } catch {
      return { error: `${field.label} is not valid JSON.` };
    }
  }
  return { body: { kind, config, aliases, credential_secret: credential || null } };
}

function fieldInput(field, id, value, onInput) {
  if (field.options) {
    const input = element('select', { id }, field.options.map(([option, label]) => element('option', { value: option }, label)));
    input.value = value ?? '';
    input.addEventListener('change', () => onInput(input.value));
    return input;
  }
  const input = element(field.json ? 'textarea' : 'input', { ...(field.json ? { rows: 4 } : { type: 'text' }), id, placeholder: field.placeholder || null, spellcheck: 'false', autocapitalize: 'none' });
  input.value = value && typeof value === 'object' ? JSON.stringify(value, null, 2) : value ?? '';
  input.addEventListener('input', () => onInput(input.value));
  return input;
}

function aliasEditor(initialRows, idPrefix) {
  let rows = initialRows.length ? initialRows.map((row) => ({ ...row })) : [{ alias: '', model: '' }];
  const body = element('div', { class: 'alias-rows' });
  function draw() {
    clear(body);
    rows.forEach((row, index) => {
      const aliasInput = element('input', { type: 'text', id: `${idPrefix}-alias-${index}`, 'aria-label': `Alias ${index + 1}`, placeholder: 'review', spellcheck: 'false', autocapitalize: 'none' });
      const modelInput = element('input', { type: 'text', id: `${idPrefix}-model-${index}`, 'aria-label': `Model for alias ${index + 1}`, placeholder: 'Model id', spellcheck: 'false', autocapitalize: 'none' });
      aliasInput.value = row.alias;
      modelInput.value = row.model;
      aliasInput.addEventListener('input', () => { row.alias = aliasInput.value; });
      modelInput.addEventListener('input', () => { row.model = modelInput.value; });
      const removeButton = element('button', { type: 'button', class: 'link-button', 'aria-label': `Remove alias ${index + 1}` }, 'Remove');
      removeButton.addEventListener('click', () => {
        rows = rows.filter((_, position) => position !== index);
        draw();
      });
      body.append(element('div', { class: 'alias-row' }, aliasInput, modelInput, removeButton));
    });
  }
  const addButton = element('button', { type: 'button', class: 'link-button' }, 'Add alias');
  addButton.addEventListener('click', () => {
    rows.push({ alias: '', model: '' });
    draw();
    body.lastElementChild?.querySelector('input')?.focus();
  });
  draw();
  return {
    root: element('fieldset', { class: 'aliases' }, element('legend', {}, 'Model aliases'),
      element('p', { class: 'note' }, 'Workflows and roles name an alias, such as review or lead. Each maps to a model this provider serves.'),
      element('div', { class: 'alias-row alias-head', 'aria-hidden': 'true' }, element('span', {}, 'Alias'), element('span', {}, 'Model'), element('span')), body, addButton),
    rows: () => rows,
  };
}

function providerForm(team, provider, secretNames, onDone, onCancel, createPath) {
  const idPrefix = `provider-${team.id}-${provider?.id || 'new'}`;
  const kindSelect = element('select', { id: `${idPrefix}-kind`, disabled: Boolean(provider) },
    Object.entries(PROVIDER_KINDS).map(([kind, entry]) => element('option', { value: kind }, entry.label)));
  kindSelect.value = provider?.kind || 'bedrock';
  const fieldSlot = element('div', { class: 'stacked-form' });
  const values = { ...(provider?.config || {}) };
  function drawFields() {
    clear(fieldSlot);
    for (const field of kindFields(kindSelect.value, isAdmin())) {
      const input = fieldInput(field, `${idPrefix}-${field.name}`, values[field.name], (value) => { values[field.name] = value; });
      fieldSlot.append(element('label', { class: 'stacked', for: input.id }, field.label, input));
    }
  }
  kindSelect.addEventListener('change', drawFields);
  drawFields();
  const current = provider?.config?.credential_secret || '';
  const names = current && !secretNames.includes(current) ? [...secretNames, current] : secretNames;
  const secretSelect = element('select', { id: `${idPrefix}-secret` }, element('option', { value: '' }, 'None'),
    names.map((name) => element('option', { value: name }, name)));
  secretSelect.value = current;
  const aliases = aliasEditor(aliasRows(provider?.aliases), idPrefix);
  const status = element('div', { role: 'status' });
  const saveButton = element('button', { type: 'submit', class: 'primary' }, provider ? 'Save provider' : 'Add provider');
  const cancelButton = element('button', { type: 'button', onclick: onCancel }, 'Cancel');
  const form = element('form', { class: 'provider-form' },
    element('h3', {}, provider ? `Edit ${kindLabel(provider.kind)}` : 'Add a provider'),
    element('div', { class: 'stacked-form' },
      element('label', { class: 'stacked', for: kindSelect.id }, 'Kind', kindSelect), fieldSlot,
      element('label', { class: 'stacked', for: secretSelect.id }, 'Credential secret', secretSelect,
        element('span', { class: 'note' }, names.length ? 'The secret holds the key. Its value never shows here. It is one of the team’s secrets.' : 'This team has no secrets yet. Add one on the Secrets page.'))),
    aliases.root, status, element('div', { class: 'wf-actions' }, saveButton, cancelButton));
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    clear(status);
    const { body, error } = providerBody(kindSelect.value, values, aliases.rows(), secretSelect.value, provider?.config, isAdmin());
    if (error) {
      status.append(element('div', { class: 'error-box', role: 'alert' }, error));
      return;
    }
    const path = provider ? `/api/providers/${encodeURIComponent(provider.id)}` : createPath || `/api/teams/${encodeURIComponent(team.id)}/providers`;
    if (await attempt(status, saveButton, () => api(path, { method: 'POST', body }))) onDone();
  });
  requestAnimationFrame(() => (provider ? secretSelect : kindSelect).focus());
  return form;
}

function credentialCell(provider) {
  const name = provider.config?.credential_secret;
  return element('span', {}, name ? element('span', { class: 'mono' }, name) : 'None', ' ',
    provider.configured ? element('span', { class: 'state state-ok' }, 'Configured') : element('span', { class: 'state state-warn' }, 'Not configured'));
}

function aliasesCell(aliases) {
  const entries = Object.entries(aliases || {});
  if (!entries.length) return '—';
  return element('div', {}, entries.map(([alias, model]) => element('div', {}, element('span', { class: 'mono' }, alias), ` ${model}`)));
}

// Config holds only names, such as the secret a provider reads. The secret's value never reaches the page.
export function providersSection(team, providers, { canManage, secretNames = [], reload, heading = 'Model providers', createPath = null }) {
  if (!providers.length && !canManage) return null;
  const formSlot = element('div');
  const status = element('div', { role: 'status' });
  const openForm = (provider) => clear(formSlot).append(providerForm(team, provider, secretNames, reload, () => clear(formSlot), createPath));
  const headers = ['Provider', 'Region or URL', 'Credential', 'Aliases', ...(canManage ? [''] : [])];
  const rows = providers.map((provider) => {
    const row = [kindLabel(provider.kind), provider.config?.region || provider.config?.api_base || '—', credentialCell(provider), aliasesCell(provider.aliases)];
    if (canManage) {
      const editButton = element('button', { type: 'button', class: 'link-button', 'aria-label': `Edit ${kindLabel(provider.kind)}` }, 'Edit');
      editButton.addEventListener('click', () => openForm(provider));
      const deleteButton = element('button', { type: 'button', class: 'link-button', 'aria-label': `Delete ${kindLabel(provider.kind)}` }, 'Delete');
      deleteButton.addEventListener('click', async () => {
        if (!(await confirmDialog(`Delete ${kindLabel(provider.kind)}?`, 'Runs that use its aliases fail until another provider maps them.', 'Delete'))) return;
        if (await attempt(status, deleteButton, () => api(`/api/providers/${encodeURIComponent(provider.id)}/delete`, { method: 'POST', body: {} }))) reload();
      });
      row.push(element('span', { class: 'row-actions' }, editButton, ' ', deleteButton));
    }
    return row;
  });
  const addButton = canManage ? element('button', { type: 'button', onclick: () => openForm(null) }, 'Add provider') : null;
  return element('div', { class: 'providers' }, heading ? element('h3', {}, heading) : null, status,
    providers.length ? table(headers, rows) : empty('No providers yet. Runs need one to reach a model.'),
    addButton ? element('div', { class: 'wf-actions' }, addButton) : null, formSlot);
}
