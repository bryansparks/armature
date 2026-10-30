import { api, listOf } from '../api.js';
import { attempt, card, clear, confirmDialog, element, empty, errorBox, loading, pageHeader, table } from '../dom.js';
import { DISPLAY_DEFAULTS, SERIES_COLORS } from '../display.js';
import { hashFor } from '../router.js';
import { parseJson } from '../schema.js';

const INSTANCE = 'instance';

export const scopeParam = (scope) => (scope === '*' || scope === INSTANCE || !scope ? INSTANCE : String(scope));

export function settingText(value) {
  return JSON.stringify(value, null, 2) ?? 'null';
}

function settingForm(scopes, setting, onSaved, onCancel) {
  const scopeSelect = element('select', { id: 'setting-scope', disabled: Boolean(setting) }, scopes.map(([id, label]) => element('option', { value: id }, label)));
  scopeSelect.value = setting ? scopeParam(setting.scope) : INSTANCE;
  const keyInput = element('input', { type: 'text', id: 'setting-key', required: true, maxlength: 191, spellcheck: 'false', autocapitalize: 'none', readonly: Boolean(setting) });
  keyInput.value = setting?.key || '';
  const valueInput = element('textarea', { id: 'setting-value', class: 'mono', rows: 8, spellcheck: 'false', 'aria-describedby': 'setting-value-check' });
  valueInput.value = setting ? settingText(setting.value) : '';
  const check = element('span', { class: 'note', id: 'setting-value-check', 'aria-live': 'polite' });
  const saveButton = element('button', { type: 'submit', class: 'primary' }, setting ? 'Save setting' : 'Add setting');
  const status = element('div', { role: 'status' });
  const validate = () => {
    const parsed = parseJson(valueInput.value);
    check.textContent = parsed.error ? `Not valid JSON${parsed.line ? ` at line ${parsed.line}, column ${parsed.col}` : ''}: ${parsed.error}` : 'Valid JSON.';
    check.classList.toggle('bad', Boolean(parsed.error));
    valueInput.setAttribute('aria-invalid', parsed.error ? 'true' : 'false');
    saveButton.disabled = Boolean(parsed.error);
    return parsed;
  };
  valueInput.addEventListener('input', validate);
  if (setting) validate();
  const form = element('form', { class: 'stacked-form' },
    element('h2', { class: 'inline' }, setting ? `Edit ${setting.key}` : 'Add a setting'),
    element('label', { class: 'stacked', for: scopeSelect.id }, 'Scope', scopeSelect),
    element('label', { class: 'stacked', for: keyInput.id }, 'Key', keyInput),
    element('label', { class: 'stacked', for: valueInput.id }, 'Value, as JSON', valueInput, check),
    status, element('div', { class: 'wf-actions' }, saveButton, element('button', { type: 'button', onclick: onCancel }, 'Cancel')));
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const parsed = validate();
    if (parsed.error) return;
    const key = keyInput.value.trim();
    const saved = await attempt(status, saveButton, () => api('/api/settings', { method: 'POST', body: { scope: scopeSelect.value, key, value: parsed.value } }));
    if (saved) onSaved(`Saved ${key}.`);
  });
  requestAnimationFrame(() => (setting ? valueInput : keyInput).focus());
  return card(form);
}

const COLOR_NAMES = { s1: 'Blue', s2: 'Orange', s3: 'Aqua', s4: 'Yellow', s5: 'Pink', s6: 'Green', s7: 'Violet', s8: 'Red' };

function zoneNames() {
  try {
    return Intl.supportedValuesOf('timeZone');
  } catch {
    return [];
  }
}

function workflowRow(workflow, entry = {}) {
  const labelInput = element('input', { type: 'text', maxlength: 32, placeholder: workflow, 'aria-label': `Label for ${workflow}` });
  labelInput.value = entry.label || '';
  const colorSelect = element('select', { 'aria-label': `Color for ${workflow}` }, element('option', { value: '' }, 'Default color'),
    SERIES_COLORS.map((color) => element('option', { value: color }, COLOR_NAMES[color])));
  colorSelect.value = entry.color || '';
  const shownBox = element('input', { type: 'checkbox', checked: !entry.hidden, 'aria-label': `Show ${workflow} in runs by default` });
  return {
    cells: [element('span', { class: 'mono wrap-anywhere' }, workflow), labelInput, colorSelect, shownBox],
    read: () => [workflow, { label: labelInput.value.trim(), color: colorSelect.value, hidden: !shownBox.checked }],
  };
}

// Each changed value is one instance setting, written through the same route as any other setting.
async function displayCard(onSaved) {
  const shown = await api('/api/display');
  const current = { ...DISPLAY_DEFAULTS, ...shown.display };
  const showCost = element('input', { type: 'checkbox', id: 'display-show-cost', checked: current.show_cost });
  const showHours = element('input', { type: 'checkbox', id: 'display-show-hours', checked: current.show_man_hours });
  const multiplier = element('input', { type: 'number', id: 'display-multiplier', min: '0.1', max: '100', step: 'any', required: true, value: String(current.human_multiplier) });
  const zones = element('datalist', { id: 'display-zones' }, zoneNames().map((zone) => element('option', { value: zone })));
  const timezone = element('input', { type: 'text', id: 'display-timezone', list: zones.id, required: true, spellcheck: 'false', autocapitalize: 'none', value: current.timezone });
  const names = [...new Set([...listOf(shown, 'workflows'), ...Object.keys(current.workflow_labels)])].sort();
  const workflowRows = names.map((workflow) => workflowRow(workflow, current.workflow_labels[workflow]));
  const saveButton = element('button', { type: 'submit', class: 'primary' }, 'Save display settings');
  const status = element('div', { role: 'status' });
  const form = element('form', { class: 'stacked-form display-form' },
    element('h2', { class: 'inline' }, 'Display'),
    element('p', { class: 'note' }, 'How the Overview and Trends pages show numbers to everyone on this server.'),
    element('fieldset', {}, element('legend', {}, 'Show'),
      element('label', { for: showCost.id }, showCost, ' Cost'), ' ', element('label', { for: showHours.id }, showHours, ' Man hours')),
    element('label', { class: 'stacked', for: multiplier.id }, 'Man hours per hour of agent time', multiplier),
    element('label', { class: 'stacked', for: timezone.id }, 'Time zone for day boundaries', timezone, zones),
    element('fieldset', {}, element('legend', {}, 'Workflows'),
      element('p', { class: 'note' }, 'A label replaces the workflow name on the Overview and Trends pages. Leave it empty for the name itself. A workflow not shown by default stays out of the run lists unless someone filters for it.'),
      workflowRows.length ? table(['Workflow', 'Label', 'Color', 'Show in runs by default'], workflowRows.map((row) => row.cells)) : element('p', { class: 'note' }, 'No workflows yet.')),
    status, element('div', { class: 'wf-actions' }, saveButton));
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const labels = Object.fromEntries(workflowRows.map((row) => row.read()).filter(([, entry]) => entry.label || entry.color || entry.hidden));
    const wanted = { show_cost: showCost.checked, show_man_hours: showHours.checked, human_multiplier: Number(multiplier.value), timezone: timezone.value.trim(), workflow_labels: labels };
    const changed = Object.entries(wanted).filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(current[key] ?? DISPLAY_DEFAULTS[key]));
    if (!changed.length) {
      clear(status).append(element('p', { class: 'note' }, 'Nothing changed.'));
      return;
    }
    const saved = await attempt(status, saveButton, async () => {
      for (const [key, value] of changed) {
        await api('/api/settings', { method: 'POST', body: { scope: INSTANCE, key: `display.${key}`, value } });
        current[key] = value;
      }
    });
    if (saved) onSaved('Saved the display settings.');
  });
  return card(form);
}

export async function render(root, { query }) {
  root.append(pageHeader('Settings'));
  root.append(element('p', { class: 'note' }, 'Settings for all teams, each team, and each project, stored as JSON. A project’s env. settings win over its team’s. Secrets do not belong here. Keep them on the Secrets page.'));
  const teams = await api('/api/teams').then((payload) => listOf(payload, 'teams')).catch(() => []);
  const projectLists = await Promise.all(teams.map((team) => api(`/api/teams/${encodeURIComponent(team.id)}/projects`)
    .then((payload) => listOf(payload, 'projects')).catch(() => [])));
  const scopes = [[INSTANCE, 'Default (all teams)'], ...teams.flatMap((team, index) => [[String(team.id), team.name],
    ...projectLists[index].map((project) => [String(project.id), `${team.name} / ${project.name}`])])];
  const scopeName = (scope) => scopes.find(([id]) => id === scopeParam(scope))?.[1] || scope;
  const filter = query.get('scope') || '';
  const filterSelect = element('select', { id: 'settings-scope' }, element('option', { value: '' }, 'All scopes'), scopes.map(([id, label]) => element('option', { value: id }, label)));
  filterSelect.value = filter;
  filterSelect.addEventListener('change', () => {
    location.hash = hashFor('settings', [], { scope: filterSelect.value });
  });
  const addButton = element('button', { type: 'button', class: 'primary' }, 'Add setting');
  const status = element('div', { role: 'status' });
  const formSlot = element('div');
  const listSlot = element('div', {}, loading());
  const displaySlot = element('div', {}, loading());
  root.append(displaySlot, element('h2', {}, 'All settings'), element('div', { class: 'filters' }, element('label', { for: filterSelect.id }, 'Scope', filterSelect), addButton), status, formSlot, listSlot);

  const closeForm = () => clear(formSlot);
  const openForm = (setting) => {
    clear(status);
    clear(formSlot).append(settingForm(scopes, setting, (message) => {
      closeForm();
      reload(message);
    }, closeForm));
  };
  addButton.addEventListener('click', () => openForm(null));

  async function reload(message) {
    let settings;
    try {
      const params = new URLSearchParams(filter ? { scope: filter } : {});
      settings = listOf(await api(`/api/settings?${params}`), 'settings');
    } catch (err) {
      clear(listSlot).append(errorBox(err));
      return;
    }
    clear(status);
    if (message) status.append(element('p', { class: 'note' }, message));
    clear(listSlot).append(card(settings.length ? table(['Scope', 'Key', 'Value', ''], settings.map((setting) => {
      const editButton = element('button', { type: 'button', class: 'link-button', 'aria-label': `Edit ${setting.key}` }, 'Edit');
      editButton.addEventListener('click', () => openForm(setting));
      const deleteButton = element('button', { type: 'button', class: 'link-button', 'aria-label': `Delete ${setting.key}` }, 'Delete');
      deleteButton.addEventListener('click', async () => {
        if (!(await confirmDialog(`Delete ${setting.key}?`, `The ${scopeName(setting.scope)} setting goes back to its default.`, 'Delete'))) return;
        const deleted = await attempt(status, deleteButton, () => api('/api/settings/delete', { method: 'POST', body: { scope: scopeParam(setting.scope), key: setting.key } }));
        if (deleted) reload(`Deleted ${setting.key}.`);
      });
      return [scopeName(setting.scope), element('span', { class: 'mono' }, setting.key), element('pre', { class: 'setting-value' }, settingText(setting.value)),
        element('span', { class: 'row-actions' }, editButton, ' ', deleteButton)];
    })) : empty(filter ? 'No settings in this scope.' : 'No settings yet.')));
  }

  displayCard((message) => reload(message)).then((displayForm) => clear(displaySlot).append(displayForm), (err) => clear(displaySlot).append(errorBox(err)));
  await reload();
}
