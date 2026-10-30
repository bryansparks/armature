import { api, listOf } from '../api.js';
import { attempt, card, clear, confirmDialog, element, empty, errorBox, loading, pageHeader, table, when } from '../dom.js';

export function keysPath(teamId) {
  return teamId ? `/api/teams/${encodeURIComponent(teamId)}/runner-keys` : '/api/runner-keys';
}

export function deleteKeyPath(teamId, keyId) {
  return `${keysPath(teamId)}/${encodeURIComponent(keyId)}/delete`;
}

export function keyCells(key) {
  return [key.name, when(key.created_at), key.created_by || '—', key.last_used_at ? when(key.last_used_at) : 'Never'];
}

// The key lives only in this element. Nothing writes it to storage or the address.
function revealBox(name, key) {
  const input = element('input', { type: 'text', readonly: true, class: 'mono', spellcheck: 'false', autocomplete: 'off', 'aria-label': `Runner key ${name}` });
  input.value = key;
  const copyStatus = element('span', { role: 'status', class: 'muted' });
  const copyButton = element('button', { type: 'button' }, 'Copy');
  copyButton.addEventListener('click', async () => {
    clear(copyStatus);
    try {
      await navigator.clipboard.writeText(key);
      copyStatus.append('Copied.');
    } catch {
      input.select();
      copyStatus.append('Select the key and copy it.');
    }
  });
  const dismissButton = element('button', { type: 'button', class: 'link-button' }, 'Done');
  const box = element('div', { class: 'stacked-form' },
    element('p', { class: 'note', role: 'alert' }, `Copy the key for ${name} now. It is not shown again.`),
    element('div', { class: 'wf-actions' }, input, copyButton, dismissButton, copyStatus));
  dismissButton.addEventListener('click', () => {
    input.value = '';
    box.remove();
  });
  return box;
}

export function runnerKeysSection(teamId, { heading = 'Runner keys' } = {}) {
  const status = element('div', { role: 'status' });
  const reveal = element('div');
  const listSlot = element('div', {}, loading());
  const nameInput = element('input', { type: 'text', id: `runner-key-name-${teamId || 'instance'}`, required: true, maxlength: 100, spellcheck: 'false', autocomplete: 'off', placeholder: 'build host' });
  const createButton = element('button', { type: 'submit' }, 'Create key');
  const form = element('form', { class: 'wf-actions' }, element('label', { for: nameInput.id }, 'Name'), nameInput, createButton);

  async function reload() {
    let keys;
    try {
      keys = listOf(await api(keysPath(teamId)), 'runner_keys');
    } catch (err) {
      clear(listSlot).append(errorBox(err));
      return;
    }
    clear(listSlot).append(keys.length
      ? table(['Name', 'Created', 'Created by', 'Last used', ''], keys.map((key) => [...keyCells(key), deleteButton(key)]))
      : empty('No runner keys yet.'));
  }

  function deleteButton(key) {
    const button = element('button', { type: 'button', class: 'link-button', 'aria-label': `Delete ${key.name}` }, 'Delete');
    button.addEventListener('click', async () => {
      if (!(await confirmDialog(`Delete ${key.name}?`, 'Hosts using this key can no longer register runs, and its running runs stop streaming.', 'Delete'))) return;
      if (await attempt(status, button, () => api(deleteKeyPath(teamId, key.id), { method: 'POST', body: {} }))) reload();
    });
    return button;
  }

  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const name = nameInput.value.trim();
    const created = await attempt(status, createButton, () => api(keysPath(teamId), { method: 'POST', body: { name } }));
    if (!created?.key) return;
    nameInput.value = '';
    clear(reveal).append(revealBox(name, created.key));
    reload();
  });

  reload();
  return element('section', {}, heading ? element('h3', {}, heading) : null, listSlot, reveal, form, status);
}

export async function render(root) {
  root.append(pageHeader('Runner keys'));
  root.append(element('p', { class: 'note' }, 'A key for all teams registers runs for every team. Each run lands in the team whose project matches its repository, else in Operations.'));
  root.append(card(runnerKeysSection(null, { heading: 'Keys for all teams' })));
}
