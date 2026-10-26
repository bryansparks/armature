import { api, listOf } from '../api.js';
import { attempt, card, clear, confirmDialog, element, empty, errorBox, loading, pageHeader, table, tabs, when } from '../dom.js';
import { canManageTeam, isAdmin } from '../session.js';

const INSTANCE = 'instance';

function secretsPath(scope) {
  return scope === INSTANCE ? '/api/secrets' : `/api/teams/${encodeURIComponent(scope)}/secrets`;
}

function deletePath(scope, name) {
  return scope === INSTANCE
    ? `/api/secrets/${encodeURIComponent(name)}/delete`
    : `/api/teams/${encodeURIComponent(scope)}/secrets/${encodeURIComponent(name)}/delete`;
}

function secretState(secret) {
  if (!secret.set) return element('span', { class: 'muted' }, 'Not set');
  return `Set, updated ${when(secret.updated_at)}${secret.updated_by ? ` by ${secret.updated_by}` : ''}`;
}

// The value field is write-only. Saved values never come back from the server.
function valueInput(id, ariaLabel) {
  return element('input', { type: 'password', id, autocomplete: 'new-password', spellcheck: 'false', required: true, 'aria-label': ariaLabel });
}

function replaceForm(scope, secret, status, reload) {
  const input = valueInput(`value-${secret.name}`, `New value for ${secret.name}`);
  const saveButton = element('button', { type: 'submit' }, secret.set ? 'Replace' : 'Set');
  const form = element('form', { class: 'inline-form' }, input, saveButton);
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const saved = await attempt(status, saveButton, () => api(secretsPath(scope), { method: 'POST', body: { name: secret.name, value: input.value } }));
    input.value = '';
    if (saved) reload(`Saved ${secret.name}.`);
  });
  return form;
}

function deleteButton(scope, secret, status, reload) {
  const button = element('button', { type: 'button', class: 'link-button', 'aria-label': `Delete ${secret.name}` }, 'Delete');
  button.addEventListener('click', async () => {
    if (!(await confirmDialog(`Delete ${secret.name}?`, 'Runs that need it fail until someone sets it again.', 'Delete'))) return;
    if (await attempt(status, button, () => api(deletePath(scope, secret.name), { method: 'POST', body: {} }))) reload(`Deleted ${secret.name}.`);
  });
  return button;
}

function addForm(scope, status, reload) {
  const nameInput = element('input', { type: 'text', id: 'new-secret-name', required: true, maxlength: 100, autocapitalize: 'characters', spellcheck: 'false', autocomplete: 'off' });
  const input = valueInput('new-secret-value', null);
  const addButton = element('button', { type: 'submit', class: 'primary' }, 'Add secret');
  const form = element('form', { class: 'stacked-form' },
    element('h2', { class: 'inline' }, 'Add a secret'),
    element('label', { class: 'stacked', for: nameInput.id }, 'Name', nameInput),
    element('label', { class: 'stacked', for: input.id }, 'Value', input),
    element('div', { class: 'wf-actions' }, addButton));
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const name = nameInput.value.trim();
    const saved = await attempt(status, addButton, () => api(secretsPath(scope), { method: 'POST', body: { name, value: input.value } }));
    input.value = '';
    if (saved) reload(`Saved ${name}.`);
  });
  return card(form);
}

export async function render(root, { args }) {
  root.append(pageHeader('Secrets'));
  root.append(element('p', { class: 'note' }, 'A saved value is never shown again. Replace it to change it.'));
  let teams;
  try {
    teams = listOf(await api('/api/teams'), 'teams').filter((team) => canManageTeam(team.id));
  } catch (err) {
    root.append(errorBox(err));
    return;
  }
  const scopes = [...(isAdmin() ? [{ id: INSTANCE, label: 'Instance' }] : []), ...teams.map((team) => ({ id: String(team.id), label: team.name }))];
  if (!scopes.length) {
    root.append(card(empty('You do not manage any team’s secrets.')));
    return;
  }
  const scope = scopes.some((candidate) => candidate.id === args[0]) ? args[0] : scopes[0].id;
  const { tablist, panel } = tabs('#/secrets', scopes, scope, { label: 'Scope' });
  root.append(tablist, panel);
  const status = element('div', { role: 'status' });
  const listSlot = element('div', {}, loading());
  panel.append(status, listSlot, addForm(scope, status, (message) => reload(message)));

  async function reload(message) {
    let secrets;
    try {
      secrets = listOf(await api(secretsPath(scope)), 'secrets');
    } catch (err) {
      clear(listSlot).append(errorBox(err));
      return;
    }
    if (message) clear(status).append(element('p', { class: 'note' }, message));
    clear(listSlot).append(card(secrets.length ? table(['Name', 'Status', 'New value', ''], secrets.map((secret) => [
      element('span', { class: 'mono' }, secret.name), secretState(secret), replaceForm(scope, secret, status, reload), deleteButton(scope, secret, status, reload),
    ])) : empty(scope === INSTANCE ? 'No instance secrets yet.' : 'This team has no secrets yet.')));
  }

  await reload();
}
